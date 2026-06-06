"""Lightweight analytics for verification outcomes.

Stores one row per verification attempt in a local SQLite DB. The DB path
defaults to ``/app/data/analytics.db`` so it can be mounted as a host volume
on Hetzner (``/opt/golden-pagoda/data:/app/data``). If the directory is not
writable the module silently degrades to a no-op so the bot can never crash
because of analytics.
"""

from __future__ import annotations

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
# Single long-lived connection. SQLite handles serialization fine when
# all callers share one connection guarded by ``_lock`` (the bot only
# writes from the main thread + the OCR worker thread). Reusing the
# connection avoids the open()/close() syscalls and per-call setup cost
# that dominated record_verification() on the CX22's slow disk.
_conn: sqlite3.Connection | None = None


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
    global _conn
    p = _resolve_path()
    if p is None:
        raise RuntimeError("analytics disabled")
    if _conn is None:
        _conn = sqlite3.connect(
            str(p), timeout=2.0, check_same_thread=False, isolation_level=None
        )
        _conn.row_factory = sqlite3.Row
        # WAL gives concurrent readers + a single writer without per-tx
        # fsync stalls; synchronous=NORMAL is safe in WAL mode.
        try:
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.Error:
            logger.warning("analytics: PRAGMA setup failed", exc_info=True)
    try:
        yield _conn
    except sqlite3.Error:
        # Drop the cached connection on hard errors so the next call
        # reopens cleanly instead of reusing a broken handle.
        try:
            _conn.close()
        except Exception:
            pass
        _conn = None
        raise


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

                CREATE TABLE IF NOT EXISTS member_profiles (
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    mastery_rank TEXT,
                    in_game_name TEXT,
                    platform TEXT,
                    clan TEXT,
                    last_verified_ts INTEGER,
                    updated_ts INTEGER NOT NULL,
                    PRIMARY KEY (guild_id, user_id)
                );

                CREATE TABLE IF NOT EXISTS member_titles (
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    title_key TEXT NOT NULL,
                    title TEXT NOT NULL,
                    reason TEXT,
                    event_name TEXT,
                    awarded_ts INTEGER NOT NULL,
                    PRIMARY KEY (guild_id, user_id, title_key)
                );
                CREATE INDEX IF NOT EXISTS idx_member_titles_user
                    ON member_titles(guild_id, user_id);
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
    clan: str | None = None,
    ocr_engine: str | None = None,
    ocr_latency_ms: int | None = None,
    user_id: int | None = None,
    guild_id: int | None = None,
) -> None:
    if _disabled:
        return
    with _lock:
        _init()
        if _disabled:
            return
        try:
            with _connect() as conn:
                conn.execute(
                    "INSERT INTO events"
                    "(ts, outcome, platform, clan, ocr_engine, ocr_latency_ms, user_id, guild_id, platform_scores)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        int(time.time()),
                        outcome,
                        None,
                        clan,
                        ocr_engine,
                        ocr_latency_ms,
                        user_id,
                        guild_id,
                        None,
                    ),
                )
                conn.commit()
        except Exception:
            logger.exception("analytics: record failed")


def upsert_member_profile(
    *,
    guild_id: int,
    user_id: int,
    mastery_rank: str | None = None,
    in_game_name: str | None = None,
    platform: str | None = None,
    clan: str | None = None,
    last_verified_ts: int | None = None,
) -> None:
    """Insert or update a durable per-member profile row.

    Only non-None fields overwrite existing values (COALESCE), so a
    partial update — e.g. the /profile mastery dropdown touching just
    ``mastery_rank`` — never wipes the snapshot captured at verify time.
    Fail-soft like :func:`record_verification`.
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
                    "INSERT INTO member_profiles"
                    " (guild_id, user_id, mastery_rank, in_game_name,"
                    "  platform, clan, last_verified_ts, updated_ts)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT(guild_id, user_id) DO UPDATE SET"
                    "  mastery_rank=COALESCE(excluded.mastery_rank, mastery_rank),"
                    "  in_game_name=COALESCE(excluded.in_game_name, in_game_name),"
                    "  platform=COALESCE(excluded.platform, platform),"
                    "  clan=COALESCE(excluded.clan, clan),"
                    "  last_verified_ts=COALESCE(excluded.last_verified_ts, last_verified_ts),"
                    "  updated_ts=excluded.updated_ts",
                    (
                        guild_id,
                        user_id,
                        mastery_rank,
                        in_game_name,
                        platform,
                        clan,
                        last_verified_ts,
                        int(time.time()),
                    ),
                )
                conn.commit()
        except Exception:
            logger.exception("analytics: upsert_member_profile failed")


def get_member_profile(guild_id: int, user_id: int) -> dict | None:
    """Return the stored per-member profile row as a dict, or None."""
    if _disabled:
        return None
    with _lock:
        _init()
        if _disabled:
            return None
        try:
            with _connect() as conn:
                row = conn.execute(
                    "SELECT mastery_rank, in_game_name, platform, clan,"
                    " last_verified_ts, updated_ts FROM member_profiles"
                    " WHERE guild_id=? AND user_id=?",
                    (guild_id, user_id),
                ).fetchone()
                return dict(row) if row is not None else None
        except Exception:
            logger.exception("analytics: get_member_profile failed")
            return None


def award_title(
    *,
    guild_id: int,
    user_id: int,
    title: str,
    reason: str | None = None,
    event_name: str | None = None,
    awarded_ts: int | None = None,
) -> None:
    """Award a cosmetic profile title to a member (idempotent per title).

    The stored ``title_key`` is the case-folded title, so re-awarding the
    same title refreshes its reason / event / timestamp instead of
    inserting a duplicate. Fail-soft like :func:`record_verification`.
    """
    title = (title or "").strip()
    if not title:
        return
    key = title.casefold()
    if _disabled:
        return
    with _lock:
        _init()
        if _disabled:
            return
        try:
            with _connect() as conn:
                conn.execute(
                    "INSERT INTO member_titles"
                    " (guild_id, user_id, title_key, title, reason,"
                    "  event_name, awarded_ts)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT(guild_id, user_id, title_key) DO UPDATE SET"
                    "  title=excluded.title,"
                    "  reason=COALESCE(excluded.reason, reason),"
                    "  event_name=COALESCE(excluded.event_name, event_name),"
                    "  awarded_ts=excluded.awarded_ts",
                    (
                        guild_id,
                        user_id,
                        key,
                        title,
                        reason,
                        event_name,
                        int(awarded_ts if awarded_ts is not None else time.time()),
                    ),
                )
                conn.commit()
        except Exception:
            logger.exception("analytics: award_title failed")


def list_member_titles(guild_id: int, user_id: int) -> list[dict]:
    """Return a member's awarded titles as dicts, newest first.

    Fail-soft: returns an empty list when analytics is disabled or errors.
    """
    if _disabled:
        return []
    with _lock:
        _init()
        if _disabled:
            return []
        try:
            with _connect() as conn:
                rows = conn.execute(
                    "SELECT title_key, title, reason, event_name, awarded_ts"
                    " FROM member_titles WHERE guild_id=? AND user_id=?"
                    " ORDER BY awarded_ts DESC, title ASC",
                    (guild_id, user_id),
                ).fetchall()
                return [dict(r) for r in rows]
        except Exception:
            logger.exception("analytics: list_member_titles failed")
            return []


def revoke_title(*, guild_id: int, user_id: int, title: str) -> bool:
    """Remove an awarded title, matched case-insensitively by ``title``.

    Returns True when a row was deleted. Fail-soft: returns False when
    disabled or on error.
    """
    title = (title or "").strip()
    if not title:
        return False
    key = title.casefold()
    if _disabled:
        return False
    with _lock:
        _init()
        if _disabled:
            return False
        try:
            with _connect() as conn:
                cur = conn.execute(
                    "DELETE FROM member_titles"
                    " WHERE guild_id=? AND user_id=? AND title_key=?",
                    (guild_id, user_id, key),
                )
                conn.commit()
                return cur.rowcount > 0
        except Exception:
            logger.exception("analytics: revoke_title failed")
            return False


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
