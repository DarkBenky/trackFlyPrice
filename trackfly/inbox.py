"""Read incoming Signal messages and answer simple commands.

The bot is a linked device of your own account, so:

  * a message from another Signal user (e.g. your girlfriend) arrives as
    ``envelope.dataMessage``;
  * a message you send from your phone (for example to Note to Self) arrives as
    ``envelope.syncMessage.sentMessage``.

Only recognised commands are answered, which keeps the bot from replying to
normal conversation (and from answering its own alerts).
"""

from __future__ import annotations

import json
import logging
import subprocess
import threading
import time

from . import notify, storage
from .config import Config
from .notify import Notifier

log = logging.getLogger(__name__)

STATS_COMMANDS = {"stats", "status", "price", "prices", "now", "/stats", "/status"}
HELP_COMMANDS = {"help", "/help", "?", "start", "/start"}

RU_STATS = {"цены", "цена", "статы", "статистика", "минимум"}
RU_HELP = {"помощь", "справка", "команды", "старт"}
STATS_COMMANDS |= RU_STATS
HELP_COMMANDS |= RU_HELP

RECEIVE_TIMEOUT_SEC = 8
IDLE_SEC = 10
STALE_SEC = 15 * 60


class CommandInbox(threading.Thread):
    """Background thread: signal-cli receive -> reply with stats."""

    def __init__(self, cfg: Config, notifier: Notifier, interval: int = IDLE_SEC):
        super().__init__(name="signal-inbox", daemon=True)
        self.cfg = cfg
        self.notifier = notifier
        self.interval = interval
        self.allowed = {number for number in cfg.signal_recipients}
        if cfg.signal_account:
            self.allowed.add(cfg.signal_account)
        self._last_error = ""

    # -- loop --------------------------------------------------------------

    def run(self) -> None:
        log.info("listening for Signal commands (send: stats / help)")
        while True:
            try:
                self._poll_once()
            except Exception:  # noqa: BLE001 - the listener must survive
                log.exception("signal inbox poll failed")
            time.sleep(self.interval)

    def _poll_once(self) -> None:
        # --output is a GLOBAL option in signal-cli, it must come before `receive`.
        command = [
            self.cfg.signal_cli,
            "--output=json",
            "-a",
            self.cfg.signal_account,
            "receive",
            "--timeout",
            str(RECEIVE_TIMEOUT_SEC),
            "--ignore-attachments",
        ]
        process = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=RECEIVE_TIMEOUT_SEC + 60,
            check=False,
        )

        if process.returncode != 0:
            detail = " ".join((process.stderr or process.stdout or "").strip().splitlines()[-2:])
            if detail != self._last_error:  # do not repeat the same error every 18s
                self._last_error = detail
                log.error("signal-cli receive failed (exit %s): %s", process.returncode, detail)
            return
        self._last_error = ""

        for line in (process.stdout or "").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                envelope = json.loads(line)
            except json.JSONDecodeError:
                log.debug("unparsable receive line: %s", line[:200])
                continue
            try:
                self._handle(envelope)
            except Exception:  # noqa: BLE001
                log.exception("could not handle envelope")

    # -- parsing -----------------------------------------------------------

    def _handle(self, envelope: dict) -> None:
        message = envelope.get("envelope") or {}
        text, reply_to, kind = self._extract(message)
        if not text:
            return

        sent_ms = message.get("timestamp")
        if isinstance(sent_ms, (int, float)) and time.time() * 1000 - sent_ms > STALE_SEC * 1000:
            log.info("ignoring stale command %r (queued while the bot was down)", text[:40])
            return

        command = text.strip().lower().split()[0]
        if command not in STATS_COMMANDS and command not in HELP_COMMANDS:
            return

        if kind == "sync":
            # A message you sent from another device: only act on Note to Self,
            # never on a message you sent to somebody else.
            destination = reply_to
            if destination and destination != self.cfg.signal_account:
                return
            reply_to = destination or self.cfg.signal_account
        else:
            # Direct message: only answer known phone numbers.
            if reply_to not in self.allowed:
                log.info("ignoring command from unknown sender %s", notify.mask_number(reply_to or "?"))
                return

        if not reply_to:
            return

        log.info("command %r received, replying to %s", command, notify.mask_number(reply_to))
        reply_lang = "ru" if command in (RU_STATS | RU_HELP) else self.cfg.language_for(reply_to)
        text_out = self._build_reply(text, reply_lang)
        self.notifier.send_to(reply_to, text_out)

    def _build_reply(self, text: str, lang: str) -> str:
        command, max_stops, sort = self._parse_command(text)
        if command in HELP_COMMANDS:
            return notify.build_help_text(self.cfg, self.cfg.public_url, lang=lang)

        conn = storage.connect(self.cfg.db_path)
        try:
            data = storage.summary(
                conn, self.cfg.origin, self.cfg.destination, self.cfg.currency
            )
        finally:
            conn.close()
        return notify.build_stats_text(
            self.cfg, data, self.cfg.public_url, max_stops=max_stops, sort=sort, lang=lang
        )

    @staticmethod
    def _parse_command(text: str) -> tuple[str, int | None, str]:
        """`stats nonstop`, `stats 1`, `stats date` -> (command, max_stops, sort)."""
        tokens = text.strip().lower().split()
        if not tokens:
            return "", None, "price"

        command = tokens[0]
        max_stops: int | None = None
        sort = "price"
        for token in tokens[1:]:
            if token in {"nonstop", "direct", "0", "без", "прямые", "прямой"}:
                max_stops = 0
            elif token in {"1", "2", "3"}:
                max_stops = int(token)
            elif token in {"date", "dates", "soon", "earliest", "дата", "даты", "вылет"}:
                sort = "date"
            elif token in {"price", "cheap", "cheapest", "цена", "цены", "дешевле"}:
                sort = "price"
        return command, max_stops, sort

    @staticmethod
    def _extract(message: dict) -> tuple[str, str | None, str | None]:
        direct = message.get("dataMessage") or {}
        text = (direct.get("message") or "").strip()
        if text:
            sender = message.get("sourceNumber") or message.get("source")
            return text, sender, "direct"

        sync = message.get("syncMessage") or {}
        sent = sync.get("sentMessage") or {}
        text = (sent.get("message") or "").strip()
        if text:
            destination = sent.get("destinationNumber") or message.get("destinationNumber")
            return text, destination, "sync"

        return "", None, None
