"""Entry point: polling loop + dashboard thread.

Usage
-----
    python -m trackfly.main                 # watcher + dashboard (run in tmux)
    python -m trackfly.main --once          # one polling cycle, then exit
    python -m trackfly.main --web-only      # dashboard only
    python -m trackfly.main --test-notify   # send a test Signal message
    python -m trackfly.main --list-pairs    # print the date pairs + URLs
"""

from __future__ import annotations

import argparse
import logging
import random
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import __version__, flights, inbox, notify, storage, web
from .config import Config, ConfigError, Pair, load_config

log = logging.getLogger("trackfly")


# --------------------------------------------------------------------- logging


def setup_logging(cfg: Config, verbose: bool = False) -> None:
    cfg.log_path.parent.mkdir(parents=True, exist_ok=True)
    level = logging.DEBUG if verbose else logging.INFO

    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)

    file_handler = RotatingFileHandler(
        cfg.log_path, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)

    logging.basicConfig(level=level, handlers=[console, file_handler], force=True)
    # primp (the Rust HTTP client) is chatty about every request; keep it quiet.
    for noisy in ("primp", "h2", "hickory_net", "hickory_resolver", "cookie_store", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# ----------------------------------------------------------------------- cycle


def select_pairs(cfg: Config, conn) -> list[Pair]:
    """All date pairs, or the least-recently-checked subset when capped."""
    pairs = list(cfg.pairs)
    if cfg.pairs_per_cycle and cfg.pairs_per_cycle < len(pairs):
        checked = storage.last_checked(conn, cfg.origin, cfg.destination)
        pairs.sort(key=lambda pair: checked.get((pair.dep_iso, pair.ret_iso), ""))
        pairs = pairs[: cfg.pairs_per_cycle]
    return pairs


def send_alert(
    cfg: Config,
    conn,
    notifier: notify.Notifier,
    pair: Pair,
    offer: flights.Offer,
    prev_price: float | None,
    texts: dict[str, str],
) -> bool:
    fallback = texts.get(cfg.language) or next(iter(texts.values()))

    if cfg.signal_mode == "none" or not notifier.enabled:
        storage.add_alert(
            conn,
            origin=cfg.origin,
            dest=cfg.destination,
            depart=pair.dep_iso,
            ret=pair.ret_iso,
            price=offer.price,
            prev_price=prev_price,
            currency=offer.currency,
            text=fallback,
            channels="log",
        )
        return True

    results: list[tuple[str, bool, str]] = []
    for recipient in cfg.signal_recipients:
        text = texts.get(cfg.language_for(recipient)) or fallback
        ok, detail = notifier.send_to(recipient, text)
        results.append((recipient, ok, detail))

    delivered = [item for item in results if item[1]]
    channels = ", ".join(
        f"{notify.mask_number(number)}={'ok' if ok else 'failed'}" for number, ok, _ in results
    )

    if not delivered:
        log.error("alert could not be delivered to any recipient:\n%s", fallback)
        return False

    storage.add_alert(
        conn,
        origin=cfg.origin,
        dest=cfg.destination,
        depart=pair.dep_iso,
        ret=pair.ret_iso,
        price=offer.price,
        prev_price=prev_price,
        currency=offer.currency,
        text=fallback,
        channels=channels,
    )
    return True


def handle_offer(
    cfg: Config,
    conn,
    notifier: notify.Notifier,
    run_id: int,
    pair: Pair,
    offer: flights.Offer,
) -> bool:
    """Store the observation and notify on a new all-time low. Returns True if notified."""
    previous = storage.pair_best(conn, cfg.origin, cfg.destination, pair.dep_iso, pair.ret_iso)
    prev_price = float(previous["price"]) if previous else None

    window_previous = storage.window_best(conn)
    window_prev_price = float(window_previous["price"]) if window_previous else None

    storage.insert_quote(
        conn,
        run_id=run_id,
        origin=cfg.origin,
        dest=cfg.destination,
        depart=pair.dep_iso,
        ret=pair.ret_iso,
        price=offer.price,
        currency=offer.currency,
        airlines=offer.airlines,
        stops=offer.stops,
        duration_min=offer.duration_min,
        legs=offer.legs,
        booking_url=offer.booking_url,
    )

    if prev_price is None:
        storage.upsert_best(
            conn,
            origin=cfg.origin,
            dest=cfg.destination,
            depart=pair.dep_iso,
            ret=pair.ret_iso,
            price=offer.price,
            currency=offer.currency,
        )
        if cfg.alert_on_first_run:
            texts = notify.localized(cfg, notify.build_first_seen_text, pair, offer)
            return send_alert(cfg, conn, notifier, pair, offer, None, texts)
        return False

    if offer.price >= prev_price:
        return False

    drop = prev_price - offer.price
    drop_pct = drop / prev_price * 100 if prev_price else 0.0

    storage.upsert_best(
        conn,
        origin=cfg.origin,
        dest=cfg.destination,
        depart=pair.dep_iso,
        ret=pair.ret_iso,
        price=offer.price,
        currency=offer.currency,
    )

    if drop < cfg.alert_min_drop_abs or drop_pct < cfg.alert_min_drop_pct:
        log.info(
            "%s: new low %.0f %s but below alert thresholds (drop %.0f / %.1f%%)",
            pair.label(),
            offer.price,
            offer.currency,
            drop,
            drop_pct,
        )
        return False

    last_alert = storage.last_alert_ts(conn, cfg.origin, cfg.destination, pair.dep_iso, pair.ret_iso)
    if storage.in_cooldown(last_alert, cfg.alert_cooldown_h):
        log.info("%s: new low, but still inside the %.1fh alert cooldown", pair.label(), cfg.alert_cooldown_h)
        return False

    window_low = window_prev_price is None or offer.price < window_prev_price
    texts = notify.localized(cfg, notify.build_alert_text, pair, offer, prev_price, window_low)
    return send_alert(cfg, conn, notifier, pair, offer, prev_price, texts)


def run_cycle(cfg: Config, conn, notifier: notify.Notifier) -> tuple[int, int, int]:
    pairs = select_pairs(cfg, conn)
    run_id = storage.start_run(conn)
    log.info("poll #%d: checking %d date pair(s)", run_id, len(pairs))

    ok_count = 0
    error_count = 0
    alert_count = 0

    for index, pair in enumerate(pairs):
        try:
            offer = flights.fetch_offer(cfg, pair)
        except flights.FlightFetchError as exc:
            log.error("%s", exc)
            error_count += 1
        except Exception:  # noqa: BLE001 - one bad pair must not stop the cycle
            log.exception("unexpected error while checking %s", pair.label())
            error_count += 1
        else:
            ok_count += 1
            if offer is None:
                log.warning("%s: no priced round-trip itinerary returned", pair.label())
            else:
                log.info(
                    "%s: %s %.0f | %s",
                    pair.label(),
                    offer.currency,
                    offer.price,
                    notify.describe_offer("en", offer) or "-",
                )
                try:
                    if handle_offer(cfg, conn, notifier, run_id, pair, offer):
                        alert_count += 1
                except Exception:  # noqa: BLE001
                    log.exception("could not store/notify %s", pair.label())
                    error_count += 1

        if index < len(pairs) - 1 and cfg.request_delay_sec:
            time.sleep(cfg.request_delay_sec)

    storage.finish_run(conn, run_id, ok_count, error_count)
    log.info(
        "poll #%d done: %d ok, %d error(s), %d alert(s)", run_id, ok_count, error_count, alert_count
    )
    return ok_count, error_count, alert_count


# ------------------------------------------------------------------------- main


def sleep_with_jitter(minutes: float) -> None:
    total = max(30.0, minutes * 60)
    while total > 0:
        chunk = min(30.0, total)
        time.sleep(chunk)
        total -= chunk


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="trackfly",
        description="Track flight prices, alert via Signal, and serve a price chart.",
    )
    parser.add_argument("--once", action="store_true", help="run one polling cycle and exit")
    parser.add_argument("--no-web", action="store_true", help="do not start the dashboard")
    parser.add_argument("--web-only", action="store_true", help="only serve the dashboard")
    parser.add_argument("--test-notify", action="store_true", help="send a test Signal message")
    parser.add_argument("--list-pairs", action="store_true", help="print date pairs and Google Flights URLs")
    parser.add_argument("--env", type=Path, default=None, help="path to an .env file")
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        cfg = load_config(args.env)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    setup_logging(cfg, args.verbose)
    storage.init_db(cfg.db_path)
    conn = storage.connect(cfg.db_path)
    notifier = notify.Notifier(cfg)

    log.info(
        "trackFlyPrice %s | %s | %d date pair(s) | %s | db=%s",
        __version__,
        cfg.label(),
        len(cfg.pairs),
        cfg.currency,
        cfg.db_path,
    )

    try:
        if args.list_pairs:
            for pair in cfg.pairs:
                try:
                    url = flights.build_query(cfg, pair).url()
                except Exception as exc:  # noqa: BLE001
                    url = f"(could not build URL: {exc})"
                print(f"{pair.dep_iso} -> {pair.ret_iso}  {url}")
            return 0

        if args.test_notify:
            if not notifier.enabled:
                print("Signal is disabled (SIGNAL_MODE=none) or the sender is not available.")
                return 1
            text = f"✈️ trackFlyPrice test message\n{cfg.label()} | {len(cfg.pairs)} date pairs | {cfg.currency}"
            results = [
                (number, *notifier.send_to(number, text)) for number in cfg.signal_recipients
            ]
            for number, ok, detail in results:
                print(f"{notify.mask_number(number)}: {'sent' if ok else 'FAILED'} ({detail})")
            return 0 if all(ok for _, ok, _ in results) else 1

        if args.web_only:
            web.serve(cfg)  # blocking
            return 0

        if not args.no_web:
            threading.Thread(target=web.serve, args=(cfg,), daemon=True, name="dashboard").start()

        if args.once:
            run_cycle(cfg, conn, notifier)
            return 0

        if cfg.inbox_enabled:
            inbox.CommandInbox(cfg, notifier).start()
        else:
            log.info("Signal commands disabled (INBOX_ENABLED=false or no signal-cli)")

        log.info(
            "polling every %d min (±%d min), %s pairs/cycle, %s alert cooldown",
            cfg.poll_interval_min,
            cfg.poll_jitter_min,
            cfg.pairs_per_cycle or "all",
            f"{cfg.alert_cooldown_h:g}h",
        )
        log.info("Signal mode: %s, recipients: %d", cfg.signal_mode, len(cfg.signal_recipients))

        while True:
            started = time.monotonic()
            try:
                run_cycle(cfg, conn, notifier)
            except KeyboardInterrupt:
                raise
            except Exception:  # noqa: BLE001 - keep the watcher alive
                log.exception("polling cycle crashed")

            elapsed_min = (time.monotonic() - started) / 60
            wait_min = max(1.0, cfg.poll_interval_min + random.uniform(0, cfg.poll_jitter_min) - elapsed_min)
            log.info("next poll in ~%.0f min", wait_min)
            sleep_with_jitter(wait_min)

    except KeyboardInterrupt:
        log.info("stopped by user")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
