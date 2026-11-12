"""Configuration loading and date-range expansion.

All settings come from the .env file next to this project (real environment
variables always win over .env values).
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


class ConfigError(RuntimeError):
    """Raised when .env is missing something or contains a bad value."""


# Google Flights accepts IATA airport / metro codes. Map common city names so
# that FROM=VIENNA works as well as FROM=VIE.
CITY_TO_IATA = {
    "AMSTERDAM": "AMS",
    "ATHENS": "ATH",
    "BANGKOK": "BKK",
    "BARCELONA": "BCN",
    "BELGRADE": "BEG",
    "BERLIN": "BER",
    "BOLOGNA": "BLQ",
    "BRATISLAVA": "BTS",
    "BRUSSELS": "BRU",
    "BUCHAREST": "OTP",
    "BUCURESTI": "OTP",
    "BUDAPEST": "BUD",
    "CAIRO": "CAI",
    "CASABLANCA": "CAS",
    "CHICAGO": "CHI",
    "COPENHAGEN": "CPH",
    "DUBLIN": "DUB",
    "DUBAI": "DXB",
    "DUSSELDORF": "DUS",
    "FRANKFURT": "FRA",
    "GENEVA": "GVA",
    "GDANSK": "GDN",
    "GOTHENBURG": "GOT",
    "GRAZ": "GRZ",
    "HAMBURG": "HAM",
    "HELSINKI": "HEL",
    "HONG KONG": "HKG",
    "INNSBRUCK": "INN",
    "ISTANBUL": "IST",
    "KLAGENFURT": "KLU",
    "KRAKOW": "KRK",
    "LISBON": "LIS",
    "LJUBLJANA": "LJU",
    "LONDON": "LON",
    "LOS ANGELES": "LAX",
    "LUXEMBOURG": "LUX",
    "MADRID": "MAD",
    "MARRAKECH": "RAK",
    "MEXICO CITY": "MEX",
    "MIAMI": "MIA",
    "MILAN": "MIL",
    "MUNICH": "MUC",
    "NAPLES": "NAP",
    "NEW YORK": "NYC",
    "OSLO": "OSL",
    "PARIS": "PAR",
    "PORTO": "OPO",
    "PRAGUE": "PRG",
    "REYKJAVIK": "REK",
    "RIGA": "RIX",
    "ROME": "ROM",
    "SALZBURG": "SZG",
    "SAN FRANCISCO": "SFO",
    "SARAJEVO": "SJJ",
    "SINGAPORE": "SIN",
    "SKOPJE": "SKP",
    "SOFIA": "SOF",
    "STOCKHOLM": "STO",
    "STUTTGART": "STR",
    "TALLINN": "TLL",
    "TEL AVIV": "TLV",
    "THESSALONIKI": "SKG",
    "TIRANA": "TIA",
    "TOKYO": "TYO",
    "TORONTO": "YTO",
    "TUNIS": "TUN",
    "VALENCIA": "VLC",
    "VENICE": "VCE",
    "VIENNA": "VIE",
    "VILNIUS": "VNO",
    "WARSAW": "WAW",
    "ZAGREB": "ZAG",
    "ZURICH": "ZRH",
    "ANTALYA": "AYT",
    "BASEL": "BSL",
    "COLOGNE": "CGN",
    "MARSEILLE": "MRS",
    "NICE": "NCE",
}


def load_env_file(path: Path) -> None:
    """Minimal .env loader (KEY=VALUE, # comments, optional quotes).

    Existing environment variables are never overwritten.
    """
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            # allow trailing comments for unquoted values: KEY=value   # note
            value = value.split(" #", 1)[0].rstrip()
        if key:
            os.environ.setdefault(key, value)


def _get(key: str, default: str = "") -> str:
    return (os.getenv(key) or default).strip()


def _int(key: str, default: int) -> int:
    raw = _get(key)
    if raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{key} must be an integer, got {raw!r}") from None


def _float(key: str, default: float) -> float:
    raw = _get(key)
    if raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        raise ConfigError(f"{key} must be a number, got {raw!r}") from None


def _bool(key: str, default: bool = False) -> bool:
    raw = _get(key).lower()
    if raw == "":
        return default
    return raw in {"1", "true", "yes", "y", "on"}


def _int_or_none(key: str) -> int | None:
    raw = _get(key)
    if raw == "":
        return None
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{key} must be an integer, got {raw!r}") from None


def _local_ip() -> str:
    """Best-effort LAN/public IP of this machine (no packets are sent)."""
    import socket

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def resolve_place(value: str, key: str) -> str:
    """Turn 'Vienna', 'vienna, austria' or 'vie' into 'VIE'."""
    cleaned = value.strip().upper()
    if not cleaned:
        raise ConfigError(f"{key} is empty - set it in .env (e.g. {key}=VIE)")
    short = cleaned.split(",")[0].strip()
    if short in CITY_TO_IATA:
        return CITY_TO_IATA[short]
    if len(short) == 3 and short.isalpha():
        return short
    raise ConfigError(
        f"{key}={value!r} is not a known city or IATA code. "
        f"Use a 3-letter airport code (e.g. {key}=VIE)."
    )


def expand_day_range(spec: str, month: int, year: int, key: str) -> list[date]:
    """Expand '26-30' (or a single '27') into concrete dates.

    A range whose end is lower than its start rolls over into the next month,
    so '28-3' with month=12 means 28 Dec .. 3 Jan.
    """
    spec = spec.strip()
    if not spec:
        raise ConfigError(f"{key} is empty - set it in .env")
    try:
        if "-" in spec:
            first, last = spec.split("-", 1)
            start_day, end_day = int(first), int(last)
        else:
            start_day = end_day = int(spec)
    except ValueError:
        raise ConfigError(f"{key}={spec!r} must look like '26-30' or '27'") from None

    try:
        start = date(year, month, start_day)
    except ValueError:
        raise ConfigError(f"{key}: day {start_day} does not exist in {month}/{year}") from None

    end_year, end_month = year, month
    if end_day < start_day:
        end_month += 1
        if end_month > 12:
            end_month = 1
            end_year += 1
    try:
        end = date(end_year, end_month, end_day)
    except ValueError:
        raise ConfigError(f"{key}: day {end_day} does not exist in {end_month}/{end_year}") from None

    days = (end - start).days
    return [start + timedelta(days=offset) for offset in range(days + 1)]


@dataclass(frozen=True)
class Pair:
    """One concrete round-trip date combination."""

    depart: date
    ret: date

    @property
    def dep_iso(self) -> str:
        return self.depart.isoformat()

    @property
    def ret_iso(self) -> str:
        return self.ret.isoformat()

    def label(self) -> str:
        return f"{self.depart:%d %b %Y} → {self.ret:%d %b %Y}"


@dataclass(frozen=True)
class Config:
    origin: str
    destination: str
    pairs: tuple[Pair, ...]
    currency: str
    adults: int
    seat: str
    max_stops: int | None
    proxy: str

    db_path: Path
    log_path: Path

    poll_interval_min: int
    poll_jitter_min: int
    pairs_per_cycle: int
    request_delay_sec: float
    retry_count: int
    retry_delay_sec: int

    alert_min_drop_abs: float
    alert_min_drop_pct: float
    alert_on_first_run: bool
    alert_cooldown_h: float

    signal_mode: str
    signal_account: str
    signal_api_url: str
    signal_token: str
    signal_recipients: tuple[str, ...]
    signal_cli: str

    web_host: str
    web_port: int
    web_user: str
    web_pass: str
    public_url: str
    inbox_enabled: bool
    language: str
    recipient_languages: dict[str, str]

    def label(self) -> str:
        return f"{self.origin} → {self.destination}"

    def language_for(self, number: str | None) -> str:
        """Language used when writing to `number` (falls back to LANGUAGE)."""
        if number and number in self.recipient_languages:
            return self.recipient_languages[number]
        return self.language


def load_config(env_file: Path | None = None) -> Config:
    env_path = env_file or BASE_DIR / ".env"
    load_env_file(env_path)

    dep_spec = _get("DEPARTURE_DAY_RANGE")
    ret_spec = _get("RETURN_DAY_RANGE")
    dep_month = _int("DEPARTURE_MONTH", 0)
    dep_year = _int("DEPARTURE_YEAR", 0)
    ret_month = _int("RETURN_MONTH", 0)
    ret_year = _int("RETURN_YEAR", 0)

    for key, value in (
        ("DEPARTURE_MONTH", dep_month),
        ("DEPARTURE_YEAR", dep_year),
        ("RETURN_MONTH", ret_month),
        ("RETURN_YEAR", ret_year),
    ):
        if not value:
            raise ConfigError(f"{key} is missing - set it in .env")

    dep_dates = expand_day_range(dep_spec, dep_month, dep_year, "DEPARTURE_DAY_RANGE")
    ret_dates = expand_day_range(ret_spec, ret_month, ret_year, "RETURN_DAY_RANGE")

    pairs = tuple(
        Pair(dep, ret)
        for dep in dep_dates
        for ret in ret_dates
        if ret > dep
    )
    if not pairs:
        raise ConfigError(
            "No valid date pairs: every return date is before the departure date."
        )

    recipients_raw = _get("SIGNAL_RECIPIENTS") or _get("SIGNAL_TO")
    recipients = tuple(
        part.strip() for part in recipients_raw.replace(";", ",").split(",") if part.strip()
    )

    signal_cli = _get("SIGNAL_CLI", "signal-cli")
    mode = _get("SIGNAL_MODE").lower()
    if not mode:
        if _get("SIGNAL_API_URL"):
            mode = "http"
        elif shutil.which(signal_cli):
            mode = "signal-cli"
        else:
            mode = "none"
    if mode not in {"http", "signal-cli", "none"}:
        raise ConfigError("SIGNAL_MODE must be one of: http, signal-cli, none")

    db_path = Path(_get("DB_PATH", "data/prices.db"))
    if not db_path.is_absolute():
        db_path = BASE_DIR / db_path

    log_path = Path(_get("LOG_PATH", "data/trackfly.log"))
    if not log_path.is_absolute():
        log_path = BASE_DIR / log_path

    web_port = _int("WEB_PORT", 8099)

    public_url = _get("PUBLIC_URL")
    if not public_url:
        public_url = f"http://{_local_ip()}:{web_port}/"
    if not public_url.endswith("/"):
        public_url += "/"

    inbox_raw = _get("INBOX_ENABLED").lower()
    if inbox_raw:
        inbox_enabled = inbox_raw in {"1", "true", "yes", "y", "on"}
    else:
        # only the signal-cli transport can read incoming messages
        inbox_enabled = mode == "signal-cli" and bool(recipients)

    language = _get("LANGUAGE", "en").lower()[:2] or "en"
    if language not in {"en", "ru"}:
        raise ConfigError("LANGUAGE must be 'en' or 'ru'")

    recipient_languages: dict[str, str] = {}
    for chunk in _get("SIGNAL_LANGUAGES").replace(";", ",").split(","):
        if "=" not in chunk and ":" not in chunk:
            continue
        separator = ":" if ":" in chunk else "="
        number, _, lang = chunk.partition(separator)
        number, lang = number.strip(), lang.strip().lower()[:2]
        if number and lang in {"en", "ru"}:
            recipient_languages[number] = lang

    cfg = Config(
        origin=resolve_place(_get("FROM"), "FROM"),
        destination=resolve_place(_get("TO"), "TO"),
        pairs=pairs,
        currency=_get("CURRENCY", "EUR").upper(),
        adults=max(1, _int("ADULTS", 1)),
        seat=_get("SEAT_CLASS", "economy").lower(),
        max_stops=_int_or_none("MAX_STOPS"),
        proxy=_get("PROXY"),
        db_path=db_path,
        log_path=log_path,
        poll_interval_min=max(5, _int("POLL_INTERVAL_MIN", 180)),
        poll_jitter_min=max(0, _int("POLL_JITTER_MIN", 10)),
        pairs_per_cycle=max(0, _int("POLL_PAIRS_PER_CYCLE", 0)),
        request_delay_sec=max(0.0, _float("REQUEST_DELAY_SEC", 3.0)),
        retry_count=max(0, _int("RETRY_COUNT", 2)),
        retry_delay_sec=max(1, _int("RETRY_DELAY_SEC", 20)),
        alert_min_drop_abs=max(0.0, _float("ALERT_MIN_DROP_ABS", 1.0)),
        alert_min_drop_pct=max(0.0, _float("ALERT_MIN_DROP_PCT", 0.0)),
        alert_on_first_run=_bool("ALERT_ON_FIRST_RUN", False),
        alert_cooldown_h=max(0.0, _float("ALERT_COOLDOWN_H", 3.0)),
        signal_mode=mode,
        signal_account=_get("SIGNAL_ACCOUNT"),
        signal_api_url=_get("SIGNAL_API_URL").rstrip("/"),
        signal_token=_get("SIGNAL_TOKEN"),
        signal_recipients=recipients,
        signal_cli=signal_cli,
        web_host=_get("WEB_HOST", "0.0.0.0"),
        web_port=web_port,
        web_user=_get("WEB_USER"),
        web_pass=_get("WEB_PASS"),
        public_url=public_url,
        inbox_enabled=inbox_enabled,
        language=language,
        recipient_languages=recipient_languages,
    )

    if cfg.signal_mode != "none":
        if not cfg.signal_recipients:
            raise ConfigError(
                "SIGNAL_MODE is enabled but SIGNAL_RECIPIENTS is empty. "
                "Set the phone numbers (with country code), or set SIGNAL_MODE=none."
            )
        if cfg.signal_mode == "http" and not cfg.signal_api_url:
            raise ConfigError("SIGNAL_MODE=http needs SIGNAL_API_URL, e.g. http://127.0.0.1:8080")
        if cfg.signal_mode == "signal-cli" and not cfg.signal_account:
            raise ConfigError("SIGNAL_MODE=signal-cli needs SIGNAL_ACCOUNT (the sender number)")

    return cfg
