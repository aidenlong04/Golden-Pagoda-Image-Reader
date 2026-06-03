"""Lightweight analytics for verification outcomes.

Stores one row per verification attempt in a local SQLite DB. The DB path
defaults to ``/app/data/analytics.db`` so it can be mounted as a host volume
on Hetzner (``/opt/golden-pagoda/data:/app/data``). If the directory is not
writable the module silently degrades to a no-op so the bot can never crash
because of analytics.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = os.getenv("ANALYTICS_DB_PATH", "/app/data/analytics.db")

_lock = threading.Lock()
_initialized = False
_disabled = False
_db_path: Path | None = None


def _resolve_path() -> Path | None:
    global _db_path
    if _db_path is not None:
        return _db_path
    p = Path(DEFAULT_DB_PATH)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.warning("analytics: cannot create %s; disabling", p.parent)
        return None
    _db_path = p
    return p


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    p = _resolve_path()
    if p is None:
        raise RuntimeError("analytics disabled")
    conn = sqlite3.connect(str(p), timeout=2.0)
    try:
        conn.row_factory = sqlite3.Row
        yield conn
    finally:
        conn.close()


def _init() -> None:
    global _initialized, _disabled
    if _initialized or _disabled:
        return
    try:
        with _connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY,
                    ts INTEGER NOT NULL,
                    outcome TEXT NOT NULL,
                    platform TEXT,
                    clan TEXT,
                    ocr_engine TEXT,
                    ocr_latency_ms INTEGER,
                    user_id INTEGER,
                    guild_id INTEGER
                );
                CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
                CREATE INDEX IF NOT EXISTS idx_events_outcome ON events(outcome);
                CREATE TABLE IF NOT EXISTS submissions (
                    message_id INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    guild_id INTEGER,
                    channel_id INTEGER NOT NULL,
                    ts INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_subs_user_chan
                    ON submissions(user_id, channel_id);
                CREATE INDEX IF NOT EXISTS idx_subs_chan_ts
                    ON submissions(channel_id, ts);
                """
            )
            conn.commit()
            
            columns = {row[1] for row in conn.execute("PRAGMA table_info(events)")}
            if "platform_scores" not in columns:
                try:
                    conn.execute("ALTER TABLE events ADD COLUMN platform_scores TEXT")
                    conn.commit()
                    logger.info("analytics: added platform_scores column")
                except Exception:
                    logger.warning("analytics: failed to add platform_scores column", exc_info=True)
        _initialized = True
    except Exception:
        logger.exception("analytics: init failed; disabling")
        _disabled = True


def record_verification(
    *,
    outcome: str,
    platform: str | None = None,
    clan: str | None = None,
    ocr_engine: str | None = None,
    ocr_latency_ms: int | None = None,
    user_id: int | None = None,
    guild_id: int | None = None,
    platform_scores: dict | None = None,
) -> None:
    if _disabled:
        return
    with _lock:
        _init()
        if _disabled:
            return
        try:
            scores_json = json.dumps(platform_scores, sort_keys=True) if platform_scores else None
            with _connect() as conn:
                conn.execute(
                    "INSERT INTO events"
                    "(ts, outcome, platform, clan, ocr_engine, ocr_latency_ms, user_id, guild_id, platform_scores)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        int(time.time()),
                        outcome,
                        platform,
                        clan,
                        ocr_engine,
                        ocr_latency_ms,
                        user_id,
                        guild_id,
                        scores_json,
                    ),
                )
                conn.commit()
        except Exception:
            logger.exception("analytics: record failed")


def _percentile(values: list[int], pct: float) -> int:
    if not values:
        return 0
    s = sorted(values)
    k = max(0, min(len(s) - 1, round((pct / 100.0) * (len(s) - 1))))
    return s[k]


def summary() -> dict:
    """Return a snapshot summary used by /stats."""
    out: dict = {
        "available": False,
        "total": 0,
        "by_outcome": {},
        "by_platform": [],
        "by_clan": [],
        "windows": {},
        "ocr": {"engines": [], "avg_ms": 0, "p50_ms": 0, "p95_ms": 0, "samples": 0},
        "first_ts": None,
        "last_ts": None,
        "db_path": str(_db_path) if _db_path else DEFAULT_DB_PATH,
        "db_size_bytes": 0,
    }
    if _disabled:
        return out
    with _lock:
        _init()
        if _disabled:
            return out
        try:
            with _connect() as conn:
                row = conn.execute(
                    "SELECT COUNT(*) AS c, MIN(ts) AS mn, MAX(ts) AS mx FROM events"
                ).fetchone()
                out["available"] = True
                out["total"] = row["c"] or 0
                out["first_ts"] = row["mn"]
                out["last_ts"] = row["mx"]

                out["by_outcome"] = {
                    r["outcome"]: r["c"]
                    for r in conn.execute(
                        "SELECT outcome, COUNT(*) AS c FROM events GROUP BY outcome"
                    )
                }

                out["by_platform"] = [
                    (r["platform"] or "(unknown)", r["c"])
                    for r in conn.execute(
                        "SELECT platform, COUNT(*) AS c FROM events GROUP BY platform ORDER BY c DESC"
                    )
                ]

                out["by_clan"] = [
                    (r["clan"] or "(none)", r["c"])
                    for r in conn.execute(
                        "SELECT clan, COUNT(*) AS c FROM events GROUP BY clan ORDER BY c DESC LIMIT 10"
                    )
                ]

                now = int(time.time())
                for label, secs in (("24h", 86400), ("7d", 7 * 86400), ("30d", 30 * 86400)):
                    r = conn.execute(
                        "SELECT COUNT(*) AS c FROM events WHERE ts >= ?",
                        (now - secs,),
                    ).fetchone()
                    out["windows"][label] = r["c"] or 0

                lat = [
                    int(r["ocr_latency_ms"])
                    for r in conn.execute(
                        "SELECT ocr_latency_ms FROM events"
                        " WHERE ocr_latency_ms IS NOT NULL"
                        " ORDER BY ts DESC LIMIT 500"
                    )
                ]
                if lat:
                    out["ocr"]["samples"] = len(lat)
                    out["ocr"]["avg_ms"] = sum(lat) // len(lat)
                    out["ocr"]["p50_ms"] = _percentile(lat, 50)
                    out["ocr"]["p95_ms"] = _percentile(lat, 95)

                out["ocr"]["engines"] = [
                    (r["ocr_engine"] or "(unknown)", r["c"])
                    for r in conn.execute(
                        "SELECT ocr_engine, COUNT(*) AS c FROM events"
                        " GROUP BY ocr_engine ORDER BY c DESC"
                    )
                ]
        except Exception:
            logger.exception("analytics: summary failed")
            return out

    p = _resolve_path()
    if p is not None and p.exists():
        try:
            out["db_size_bytes"] = p.stat().st_size
        except OSError:
            pass
    return out


def record_submission(
    *,
    user_id: int,
    channel_id: int,
    message_id: int,
    guild_id: int | None = None,
    ts: int | None = None,
) -> None:
    """Record a single user submission in ``channel_id``.

    Idempotent on ``message_id`` (INSERT OR IGNORE) so catch-up scans or
    duplicate dispatches do not inflate counts. Fail-soft like the rest of
    the module — never raises.
    """
    if _disabled:
        return
    with _lock:
        _init()
        if _disabled:
            return
        try:
            with _connect() as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO submissions"
                    "(message_id, user_id, guild_id, channel_id, ts)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (
                        int(message_id),
                        int(user_id),
                        int(guild_id) if guild_id is not None else None,
                        int(channel_id),
                        int(ts if ts is not None else time.time()),
                    ),
                )
                conn.commit()
        except Exception:
            logger.exception("analytics: submission record failed")


def submission_count(*, user_id: int, channel_id: int) -> int:
    """Return the number of submissions ``user_id`` has made in ``channel_id``."""
    if _disabled:
        return 0
    with _lock:
        _init()
        if _disabled:
            return 0
        try:
            with _connect() as conn:
                row = conn.execute(
                    "SELECT COUNT(*) AS c FROM submissions"
                    " WHERE user_id = ? AND channel_id = ?",
                    (int(user_id), int(channel_id)),
                ).fetchone()
                return int(row["c"] or 0)
        except Exception:
            logger.exception("analytics: submission_count failed")
            return 0
