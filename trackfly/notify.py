"""Signal notifications.

Signal has no official public HTTP API for third-party bots. The two practical
options are supported here:

  * ``SIGNAL_MODE=signal-cli``  -> call the ``signal-cli`` binary (a linked
    device account) directly. No server needed.
  * ``SIGNAL_MODE=http``        -> POST to signal-cli-rest-api
    (``/v2/send``), e.g. ``http://127.0.0.1:8080``.
  * ``SIGNAL_MODE=none``        -> only log the message.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
import urllib.error
import urllib.request

from . import i18n
from .config import Config, Pair
from .flights import Offer

log = logging.getLogger(__name__)

TIMEOUT_SEC = 60


def describe_offer(lang: str, offer: Offer) -> str:
    bits: list[str] = []
    if offer.airlines:
        bits.append(", ".join(offer.airlines[:2]))
    if offer.stops is not None:
        bits.append(i18n.stops(lang, offer.stops))
    shown = i18n.duration(lang, offer.duration_min)
    if shown:
        bits.append(shown)
    return " · ".join(bits)


def build_alert_text(
    cfg: Config,
    pair: Pair,
    offer: Offer,
    prev_price: float | None,
    window_low: bool,
    lang: str | None = None,
) -> str:
    lang = i18n.normalize(lang or cfg.language)
    lines = [i18n.get(lang, "new_low").format(origin=cfg.origin, dest=cfg.destination)]
    lines.append(f"{i18n.fmt_long(lang, pair.depart)} → {i18n.fmt_long(lang, pair.ret)}")

    price_line = i18n.get(lang, "round_trip").format(cur=cfg.currency, price=f"{offer.price:.0f}")
    if prev_price is not None and prev_price > 0:
        pct = (prev_price - offer.price) / prev_price * 100
        price_line += i18n.get(lang, "was").format(
            cur=cfg.currency, prev=f"{prev_price:.0f}", pct=f"{pct:.1f}"
        )
    lines.append(price_line)

    if window_low:
        lines.append(i18n.get(lang, "window_low"))

    details = describe_offer(lang, offer)
    if details:
        lines.append(details)

    lines.append(offer.booking_url)
    return "\n".join(lines)


def build_first_seen_text(
    cfg: Config, pair: Pair, offer: Offer, lang: str | None = None
) -> str:
    lang = i18n.normalize(lang or cfg.language)
    lines = [i18n.get(lang, "watching").format(origin=cfg.origin, dest=cfg.destination)]
    lines.append(f"{i18n.fmt_long(lang, pair.depart)} → {i18n.fmt_long(lang, pair.ret)}")
    lines.append(
        i18n.get(lang, "round_trip").format(cur=cfg.currency, price=f"{offer.price:.0f}")
        + i18n.get(lang, "first_obs")
    )
    details = describe_offer(lang, offer)
    if details:
        lines.append(details)
    lines.append(offer.booking_url)
    return "\n".join(lines)


def localized(cfg: Config, builder, *args) -> dict[str, str]:
    """Build `builder` once per language used by the configured recipients."""
    languages = {cfg.language}
    languages.update(cfg.language_for(number) for number in cfg.signal_recipients)
    return {lang: builder(cfg, *args, lang=lang) for lang in languages}


def build_stats_text(
    cfg: Config,
    data: dict,
    public_url: str,
    max_stops: int | None = None,
    sort: str = "price",
    lang: str | None = None,
) -> str:
    """Short on-demand summary for the `stats` Signal command."""
    lang = i18n.normalize(lang or cfg.language)
    pairs = data.get("pairs") or []
    current = data.get("current_lowest")
    best = data.get("all_time_low")
    counters = data.get("counters") or {}
    last = data.get("last_run") or {}

    lines = [i18n.get(lang, "stats_title").format(origin=cfg.origin, dest=cfg.destination)]

    if current:
        lines.append(
            i18n.get(lang, "lowest_now").format(
                cur=cfg.currency,
                price=f"{current['latest']:.0f}",
                dep=i18n.fmt_day(lang, current["depart"]),
                ret=i18n.fmt_day(lang, current["return"]),
            )
        )
    else:
        lines.append(i18n.get(lang, "no_prices"))

    if best:
        lines.append(
            i18n.get(lang, "all_time").format(
                cur=cfg.currency,
                price=f"{best['price']:.0f}",
                dep=i18n.fmt_day(lang, best["depart"]),
                ret=i18n.fmt_day(lang, best["return"]),
            )
        )

    lines.append(
        i18n.get(lang, "pairs_line").format(
            pairs=i18n.pairs_phrase(lang, len(pairs)),
            runs=i18n.polls_phrase(lang, counters.get("runs", 0)),
            when=i18n.fmt_when(lang, last.get("finished_at") or last.get("started_at")),
        )
    )

    if max_stops is not None:
        pairs = [
            pair
            for pair in pairs
            if pair.get("stops") is not None and pair["stops"] <= max_stops
        ]

    if sort == "date":
        ordered = sorted(pairs, key=lambda item: (item["depart"], item["return"]))
    else:
        ordered = sorted(pairs, key=lambda item: (item["latest"], item["depart"], item["return"]))

    top = ordered[:3]
    if not top:
        lines.append(i18n.get(lang, "no_match" if max_stops is not None else "no_pairs"))
    else:
        if max_stops is None:
            lines.append(i18n.get(lang, "by_date" if sort == "date" else "cheapest"))
        elif max_stops == 0:
            lines.append(i18n.get(lang, "only_nonstop"))
        else:
            lines.append(i18n.max_stops_phrase(lang, max_stops))

        for index, item in enumerate(top, 1):
            flight = []
            if item.get("airlines"):
                flight.append(item["airlines"])
            if item.get("stops") is not None:
                flight.append(i18n.stops(lang, item["stops"]))
            details = f" · {', '.join(flight)}" if flight else ""
            lines.append(
                f"{index}. {cfg.currency} {item['latest']:.0f} · "
                f"{i18n.fmt_day(lang, item['depart'])} → {i18n.fmt_day(lang, item['return'])}{details}"
            )

    lines.append(i18n.get(lang, "checks_every").format(minutes=cfg.poll_interval_min))
    lines.append(public_url)
    return "\n".join(lines)


def build_help_text(cfg: Config, public_url: str, lang: str | None = None) -> str:
    lang = i18n.normalize(lang or cfg.language)
    lines = [
        i18n.get(lang, "stats_title").format(origin=cfg.origin, dest=cfg.destination),
        i18n.get(lang, "help_intro"),
        i18n.get(lang, "help_stats"),
        i18n.get(lang, "help_nonstop"),
        i18n.get(lang, "help_max"),
        i18n.get(lang, "help_date"),
        i18n.get(lang, "help_help"),
    ]
    if lang == "ru":
        lines.append(i18n.get(lang, "help_alias"))
    lines.append(i18n.get(lang, "help_footer").format(minutes=cfg.poll_interval_min))
    lines.append(public_url)
    return "\n".join(lines)


class Notifier:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.enabled = cfg.signal_mode != "none"
        if cfg.signal_mode == "signal-cli" and not shutil.which(cfg.signal_cli):
            log.error(
                "SIGNAL_MODE=signal-cli but %r was not found on PATH - alerts will only be logged",
                cfg.signal_cli,
            )
            self.enabled = False

    # -- transport ---------------------------------------------------------

    def send_to(self, recipient: str, text: str) -> tuple[bool, str]:
        """Send one message to one recipient."""
        if not self.enabled:
            log.warning("signal disabled, message to %s not sent", mask_number(recipient))
            return False, "signal disabled"
        ok, detail = self._send_one(recipient, text)
        if ok:
            log.info("message sent to %s", mask_number(recipient))
        else:
            log.error("message to %s failed: %s", mask_number(recipient), detail)
        return ok, detail

    def _send_one(self, recipient: str, text: str) -> tuple[bool, str]:
        last_detail = ""
        for attempt in (1, 2):
            try:
                if self.cfg.signal_mode == "http":
                    return self._send_http(recipient, text)
                return self._send_cli(recipient, text)
            except Exception as exc:  # noqa: BLE001 - report, then retry
                last_detail = str(exc)
                log.warning("send attempt %d to %s failed: %s", attempt, mask_number(recipient), exc)
                if attempt == 1:
                    time.sleep(3)
        return False, last_detail

    def _send_http(self, recipient: str, text: str) -> tuple[bool, str]:
        url = _api_url(self.cfg.signal_api_url)
        payload = {
            "message": text,
            "number": self.cfg.signal_account,
            "recipients": [recipient],
        }
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        if self.cfg.signal_token:
            request.add_header("Authorization", f"Bearer {self.cfg.signal_token}")
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SEC) as response:
                body = response.read(500).decode("utf-8", "replace")
                return True, f"{response.status} {body}".strip()
        except urllib.error.HTTPError as exc:
            body = exc.read(500).decode("utf-8", "replace")
            return False, f"HTTP {exc.code}: {body}"

    def _send_cli(self, recipient: str, text: str) -> tuple[bool, str]:
        command = [self.cfg.signal_cli]
        if self.cfg.signal_account:
            command += ["-a", self.cfg.signal_account]
        command += ["send", "-m", text, recipient]

        process = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        if process.returncode == 0:
            return True, "ok"
        detail = (process.stderr or process.stdout or "").strip().splitlines()
        return False, f"exit {process.returncode}: {' '.join(detail[-2:])}"


def _api_url(base: str) -> str:
    if base.endswith("/send"):
        return base
    if base.endswith("/v2"):
        return base + "/send"
    return base + "/v2/send"


def mask_number(number: str) -> str:
    return f"***{number[-3:]}" if len(number) > 3 else "***"
