"""Google Flights access.

Google has no public API, so we request the same page the browser uses
(`?tfs=<base64 protobuf>`) and read the embedded `data:` payload that powers the
result list. The `SOCS=CAI` cookie skips the EU consent wall.

We parse the payload ourselves instead of using `fast_flights.parser.parse`
because that parser raises `IndexError` as soon as Google returns an itinerary
without a price (which happens on most searches).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass

import primp
from selectolax.lexbor import LexborHTMLParser

from .config import Config, Pair

log = logging.getLogger(__name__)

FLIGHTS_URL = "https://www.google.com/travel/flights"
CONSENT_COOKIES = {"SOCS": "CAI"}
IMPERSONATE = "chrome_145"


class FlightFetchError(RuntimeError):
    """Raised when a date pair could not be fetched after all retries."""


@dataclass
class Offer:
    price: float
    currency: str
    airlines: list[str]
    stops: int | None
    duration_min: int | None
    booking_url: str
    legs: int


def build_query(cfg: Config, pair: Pair):
    """Build a round-trip Google Flights query (protobuf -> ?tfs=...)."""
    from fast_flights import FlightQuery, Passengers, create_query

    legs = [
        FlightQuery(
            date=pair.dep_iso,
            from_airport=cfg.origin,
            to_airport=cfg.destination,
            max_stops=cfg.max_stops,
        ),
        FlightQuery(
            date=pair.ret_iso,
            from_airport=cfg.destination,
            to_airport=cfg.origin,
            max_stops=cfg.max_stops,
        ),
    ]
    return create_query(
        flights=legs,
        seat=cfg.seat,
        trip="round-trip",
        passengers=Passengers(adults=cfg.adults),
        language="en",
        currency=cfg.currency,
    )


# --------------------------------------------------------------------- fetching


def _client(cfg: Config) -> primp.Client:
    client = primp.Client(
        impersonate=IMPERSONATE,
        impersonate_os="macos",
        referer=True,
        cookie_store=True,
        proxy=cfg.proxy or None,
    )
    client.set_cookies(FLIGHTS_URL, CONSENT_COOKIES)
    return client


def fetch_html(cfg: Config, query) -> str:
    response = _client(cfg).get(FLIGHTS_URL, params=query.params())
    response.raise_for_status()
    html = response.text
    if "ds:1" not in html:
        raise FlightFetchError(
            "Google returned a page without flight data "
            f"(status {response.status_code}, {len(html)} bytes) - consent wall or bot check"
        )
    return html


# ---------------------------------------------------------------------- parsing


def _payload(html: str) -> list:
    node = LexborHTMLParser(html).css_first(r"script.ds\:1")
    if node is None:
        raise FlightFetchError("no embedded flight data found in the page")
    text = node.text()
    marker = "data:"
    index = text.find(marker)
    if index == -1:
        raise FlightFetchError("unexpected flight data script format")

    raw = text[index + len(marker) :].lstrip()
    # The script is `AF_initDataCallback({key: 'ds:1', hash: ..., data:[...],
    # sideChannel: ...})` - decode exactly one JSON value and ignore the rest,
    # instead of guessing where the payload ends (varies between pages).
    try:
        payload, _ = json.JSONDecoder().raw_decode(raw)
        return payload
    except json.JSONDecodeError:
        payload = json.loads(raw.rsplit(",", 1)[0])
        return payload


def _itineraries(payload: list):
    """Google puts the 'best' and 'other' results in payload[2] and payload[3]."""
    for section in payload[2:4]:
        if not section:
            continue
        items = section[0] or []
        for item in items:
            if isinstance(item, list) and len(item) >= 2:
                yield item


def _price_of(item: list) -> float | None:
    fare = item[1]
    if not isinstance(fare, list) or not fare:
        return None
    cheapest = fare[0]
    if not isinstance(cheapest, list) or len(cheapest) < 2:
        return None
    value = cheapest[1]
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _details_of(item: list) -> tuple[list[str], list[list]]:
    flight = item[0]
    if not isinstance(flight, list) or len(flight) < 3:
        return [], []
    airlines = [str(name) for name in (flight[1] or []) if name]
    segments = [segment for segment in (flight[2] or []) if isinstance(segment, list)]
    return airlines, segments


def _duration_of(segments: list[list]) -> int | None:
    total = 0
    for segment in segments:
        if len(segment) > 11 and isinstance(segment[11], (int, float)):
            total += int(segment[11])
    return total or None


def parse_offers(html: str, currency: str, booking_url: str) -> list[Offer]:
    payload = _payload(html)
    offers: list[Offer] = []

    for item in _itineraries(payload):
        try:
            price = _price_of(item)
            if price is None or price <= 0:
                continue
            airlines, segments = _details_of(item)
            offers.append(
                Offer(
                    price=price,
                    currency=currency,
                    airlines=airlines,
                    # Round-trip results list the outbound leg(s) only, so
                    # "segments - 1" is the number of outbound stops.
                    stops=max(0, len(segments) - 1) if segments else None,
                    duration_min=_duration_of(segments),
                    booking_url=booking_url,
                    legs=len(segments),
                )
            )
        except Exception:  # noqa: BLE001 - one odd itinerary must not kill the page
            log.debug("skipping malformed itinerary", exc_info=True)
    return offers


# ------------------------------------------------------------------------- api


def fetch_offer(cfg: Config, pair: Pair) -> Offer | None:
    """Cheapest offer for `pair`, retrying transient failures.

    Both exceptions and empty result pages are retried: Google occasionally
    serves a page where every itinerary is unpriced, and the same date pair has
    a price again a moment later.
    """
    query = build_query(cfg, pair)
    booking_url = query.url()
    last_error: Exception | None = None
    attempts = cfg.retry_count + 1

    for attempt in range(1, attempts + 1):
        try:
            offers = parse_offers(fetch_html(cfg, query), cfg.currency, booking_url)
            if offers:
                return min(offers, key=lambda offer: offer.price)
            last_error = None
            log.warning(
                "%s: no priced itinerary (%d/%d)", pair.label(), attempt, attempts
            )
        except Exception as exc:  # noqa: BLE001 - scraper raises assorted errors
            last_error = exc
            log.warning("%s: attempt %d/%d failed: %s", pair.label(), attempt, attempts, exc)

        if attempt < attempts:
            time.sleep(cfg.retry_delay_sec)

    if last_error is None:
        return None
    raise FlightFetchError(f"{pair.label()}: {last_error}") from last_error
