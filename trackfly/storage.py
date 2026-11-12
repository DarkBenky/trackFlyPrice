"""SQLite persistence: raw quotes, per-pair bests, alerts and runs."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    ok_count    INTEGER NOT NULL DEFAULT 0,
    err_count   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS quotes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER NOT NULL REFERENCES runs(id),
    ts          TEXT NOT NULL,
    origin      TEXT NOT NULL,
    dest        TEXT NOT NULL,
    depart      TEXT NOT NULL,
    ret         TEXT NOT NULL,
    price       REAL NOT NULL,
    currency    TEXT NOT NULL,
    airlines    TEXT NOT NULL DEFAULT '',
    stops       INTEGER,
    duration_min INTEGER,
    legs        INTEGER,
    booking_url TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_quotes_pair ON quotes(origin, dest, depart, ret, ts);

CREATE TABLE IF NOT EXISTS bests (
    origin     TEXT NOT NULL,
    dest       TEXT NOT NULL,
    depart     TEXT NOT NULL,
    ret        TEXT NOT NULL,
    price      REAL NOT NULL,
    currency   TEXT NOT NULL,
    ts         TEXT NOT NULL,
    PRIMARY KEY (origin, dest, depart, ret)
);

CREATE TABLE IF NOT EXISTS alerts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    origin     TEXT NOT NULL,
    dest       TEXT NOT NULL,
    depart     TEXT NOT NULL,
    ret        TEXT NOT NULL,
    price      REAL NOT NULL,
    prev_price REAL,
    currency   TEXT NOT NULL,
    text       TEXT NOT NULL,
    channels   TEXT NOT NULL DEFAULT ''
);
"""


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)


# --------------------------------------------------------------------------- runs


def start_run(conn: sqlite3.Connection) -> int:
    cur = conn.execute("INSERT INTO runs (started_at) VALUES (?)", (utcnow_iso(),))
    conn.commit()
    return int(cur.lastrowid)


def finish_run(conn: sqlite3.Connection, run_id: int, ok_count: int, err_count: int) -> None:
    conn.execute(
        "UPDATE runs SET finished_at = ?, ok_count = ?, err_count = ? WHERE id = ?",
        (utcnow_iso(), ok_count, err_count, run_id),
    )
    conn.commit()


# ------------------------------------------------------------------------- quotes


def insert_quote(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    origin: str,
    dest: str,
    depart: str,
    ret: str,
    price: float,
    currency: str,
    airlines: Iterable[str] = (),
    stops: int | None = None,
    duration_min: int | None = None,
    legs: int | None = None,
    booking_url: str = "",
) -> None:
    conn.execute(
        """
        INSERT INTO quotes (run_id, ts, origin, dest, depart, ret, price, currency,
                            airlines, stops, duration_min, legs, booking_url)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            utcnow_iso(),
            origin,
            dest,
            depart,
            ret,
            price,
            currency,
            ", ".join(airlines),
            stops,
            duration_min,
            legs,
            booking_url,
        ),
    )
    conn.commit()


def pair_best(conn: sqlite3.Connection, origin: str, dest: str, depart: str, ret: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM bests WHERE origin = ? AND dest = ? AND depart = ? AND ret = ?",
        (origin, dest, depart, ret),
    ).fetchone()


def window_best(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM bests ORDER BY price ASC LIMIT 1").fetchone()


def upsert_best(
    conn: sqlite3.Connection,
    *,
    origin: str,
    dest: str,
    depart: str,
    ret: str,
    price: float,
    currency: str,
) -> None:
    conn.execute(
        """
        INSERT INTO bests (origin, dest, depart, ret, price, currency, ts)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(origin, dest, depart, ret) DO UPDATE SET
            price = excluded.price,
            currency = excluded.currency,
            ts = excluded.ts
        WHERE excluded.price < bests.price
        """,
        (origin, dest, depart, ret, price, currency, utcnow_iso()),
    )
    conn.commit()


# ------------------------------------------------------------------------- alerts


def last_alert_ts(conn: sqlite3.Connection, origin: str, dest: str, depart: str, ret: str) -> str | None:
    row = conn.execute(
        """
        SELECT ts FROM alerts
        WHERE origin = ? AND dest = ? AND depart = ? AND ret = ?
        ORDER BY ts DESC LIMIT 1
        """,
        (origin, dest, depart, ret),
    ).fetchone()
    return row["ts"] if row else None


def in_cooldown(last_ts: str | None, cooldown_hours: float) -> bool:
    if not last_ts or cooldown_hours <= 0:
        return False
    try:
        last = datetime.fromisoformat(last_ts)
    except ValueError:
        return False
    return datetime.now(timezone.utc) - last < timedelta(hours=cooldown_hours)


def add_alert(
    conn: sqlite3.Connection,
    *,
    origin: str,
    dest: str,
    depart: str,
    ret: str,
    price: float,
    prev_price: float | None,
    currency: str,
    text: str,
    channels: str,
) -> None:
    conn.execute(
        """
        INSERT INTO alerts (ts, origin, dest, depart, ret, price, prev_price, currency, text, channels)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (utcnow_iso(), origin, dest, depart, ret, price, prev_price, currency, text, channels),
    )
    conn.commit()


# -------------------------------------------------------------------- dashboard


def last_checked(conn: sqlite3.Connection, origin: str, dest: str) -> dict[tuple[str, str], str]:
    rows = conn.execute(
        """
        SELECT depart, ret, MAX(ts) AS ts FROM quotes
        WHERE origin = ? AND dest = ?
        GROUP BY depart, ret
        """,
        (origin, dest),
    ).fetchall()
    return {(row["depart"], row["ret"]): row["ts"] for row in rows}


def latest_per_pair(conn: sqlite3.Connection, origin: str, dest: str) -> list[sqlite3.Row]:
    rows = conn.execute(
        """
        WITH ranked AS (
            SELECT origin, dest, depart, ret, price, currency, ts, airlines, stops,
                   duration_min, booking_url,
                   ROW_NUMBER() OVER (PARTITION BY depart, ret ORDER BY ts DESC, id DESC) AS rn,
                   MIN(price)    OVER (PARTITION BY depart, ret) AS best_price,
                   COUNT(*)      OVER (PARTITION BY depart, ret) AS checks
            FROM quotes
            WHERE origin = ? AND dest = ?
        )
        SELECT * FROM ranked WHERE rn = 1 ORDER BY depart, ret
        """,
        (origin, dest),
    ).fetchall()
    return list(rows)


def series(
    conn: sqlite3.Connection,
    origin: str,
    dest: str,
    depart: str | None = None,
    ret: str | None = None,
    days: int = 90,
) -> list[dict[str, Any]]:
    """Minimum price per polling run, optionally for one date pair."""
    since = utc_iso(datetime.now(timezone.utc) - timedelta(days=max(1, days)))
    params: list[Any] = [origin, dest, since]
    pair_filter = ""
    if depart and ret:
        pair_filter = "AND q.depart = ? AND q.ret = ?"
        params.extend([depart, ret])

    rows = conn.execute(
        f"""
        SELECT r.started_at AS ts, MIN(q.price) AS price
        FROM quotes q
        JOIN runs r ON r.id = q.run_id
        WHERE q.origin = ? AND q.dest = ? AND r.started_at >= ? {pair_filter}
        GROUP BY q.run_id
        ORDER BY r.started_at
        """,
        params,
    ).fetchall()
    return [{"ts": row["ts"], "price": float(row["price"])} for row in rows]


def last_run(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        "runs": conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0],
        "quotes": conn.execute("SELECT COUNT(*) FROM quotes").fetchone()[0],
        "alerts": conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0],
    }


def recent_alerts(conn: sqlite3.Connection, limit: int = 10) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT ts, depart, ret AS "return", price, prev_price, currency, channels
        FROM alerts ORDER BY id DESC LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def summary(conn: sqlite3.Connection, origin: str, dest: str, currency: str) -> dict[str, Any]:
    pairs = latest_per_pair(conn, origin, dest)
    best_row = window_best(conn)
    run = last_run(conn)
    counters = counts(conn)

    def pair_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "depart": row["depart"],
            "return": row["ret"],
            "latest": float(row["price"]),
            "latest_ts": row["ts"],
            "best": float(row["best_price"]),
            "checks": int(row["checks"]),
            "airlines": row["airlines"],
            "stops": row["stops"],
            "duration_min": row["duration_min"],
            "url": row["booking_url"],
        }

    pair_list = [pair_dict(row) for row in pairs]
    current_lowest = min(pair_list, key=lambda item: item["latest"]) if pair_list else None

    return {
        "route": {"origin": origin, "destination": dest, "currency": currency},
        "generated_at": utcnow_iso(),
        "last_run": (
            {
                "started_at": run["started_at"],
                "finished_at": run["finished_at"],
                "ok": run["ok_count"],
                "errors": run["err_count"],
            }
            if run
            else None
        ),
        "current_lowest": current_lowest,
        "all_time_low": (
            {
                "price": float(best_row["price"]),
                "depart": best_row["depart"],
                "return": best_row["ret"],
                "observed_at": best_row["ts"],
            }
            if best_row
            else None
        ),
        "pairs": pair_list,
        "counters": counters,
        "recent_alerts": recent_alerts(conn),
    }
