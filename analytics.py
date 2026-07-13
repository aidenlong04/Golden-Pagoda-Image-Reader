"""Lightweight analytics for verification outcomes.

Stores one row per verification attempt in a local SQLite DB. The DB path
defaults to ``./data/analytics.db`` (relative to the working directory). If the
directory is not writable the module silently degrades to a no-op so the bot
can never crash because of analytics.

Performance enhancements:
- ``summary()`` result is cached for ``ANALYTICS_SUMMARY_TTL`` seconds
  (default 30) to avoid re-running ~7 SQL queries on every /status refresh.
  Call ``invalidate_summary_cache()`` to force a fresh snapshot.
- A separate read-only SQLite connection is used for analytics reads so
  ``summary()`` never contends with the write lock.
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

DEFAULT_DB_PATH = os.getenv("ANALYTICS_DB_PATH", "./data/analytics.db")

# Summary cache TTL in seconds (tunable via env var).
try:
    ANALYTICS_SUMMARY_TTL: float = float(
        (os.getenv("ANALYTICS_SUMMARY_TTL") or "30").strip()
    )
except ValueError:
    ANALYTICS_SUMMARY_TTL = 30.0

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

# Separate read-only connection for analytics queries (summary, profile
# reads). WAL mode allows concurrent readers without blocking the writer
# at all, so a dedicated reader never delays writes.
_read_conn: sqlite3.Connection | None = None

# Summary snapshot cache — avoids re-running ~7 SQL queries on every
# /status refresh.  Stored as a single ``(result, timestamp)`` tuple so
# the fast-path read is one atomic reference load (no split between cache
# object and its timestamp).  Invalidated after writes and on TTL expiry.
_summary_snapshot: tuple[dict, float] | None = None


def invalidate_summary_cache() -> None:
    """Force the next ``summary()`` call to recompute from the database."""
    global _summary_snapshot
    _summary_snapshot = None


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
        except sqlite3.Error:
            logger.debug("analytics: error closing broken write conn", exc_info=True)
        _conn = None
        raise


@contextmanager
def _connect_read() -> Iterator[sqlite3.Connection]:
    """Open (or reuse) a read-only connection for analytics queries.

    Uses ``uri=True`` with ``mode=ro`` so SQLite refuses accidental writes.
    Falls back to the write connection on platforms that don't support URI
    filenames (rare, but guards against edge cases).
    """
    global _read_conn
    p = _resolve_path()
    if p is None:
        raise RuntimeError("analytics disabled")
    if _read_conn is None:
        try:
            uri = f"file:{p}?mode=ro"
            _read_conn = sqlite3.connect(
                uri, uri=True, timeout=2.0,
                check_same_thread=False, isolation_level=None,
            )
            _read_conn.row_factory = sqlite3.Row
        except sqlite3.OperationalError:
            # DB file doesn't exist yet (first run before any writes).
            # Yield the write connection so callers get an empty result
            # set rather than a crash.
            with _connect() as c:
                yield c
            return
    try:
        yield _read_conn
    except sqlite3.Error:
        try:
            _read_conn.close()
        except sqlite3.Error:
            logger.debug("analytics: error closing broken read conn", exc_info=True)
        _read_conn = None
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
                CREATE INDEX IF NOT EXISTS idx_events_user_guild
                    ON events(guild_id, user_id);

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

                CREATE TABLE IF NOT EXISTS onboarding_prompts (
                    id INTEGER PRIMARY KEY,
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    posted_ts INTEGER NOT NULL,
                    completed INTEGER NOT NULL DEFAULT 0,
                    reprompt_count INTEGER NOT NULL DEFAULT 0,
                    ocr_fail_count INTEGER NOT NULL DEFAULT 0,
                    UNIQUE (guild_id, user_id)
                );
                CREATE INDEX IF NOT EXISTS idx_onboarding_pending
                    ON onboarding_prompts(completed, posted_ts)
                    WHERE completed = 0;

                CREATE TABLE IF NOT EXISTS member_profile (
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    in_game_name TEXT,
                    mastery_rank TEXT,
                    platform TEXT,
                    clan TEXT,
                    updated_ts INTEGER NOT NULL,
                    PRIMARY KEY (guild_id, user_id)
                );

                CREATE TABLE IF NOT EXISTS fish_catches (
                    id INTEGER PRIMARY KEY,
                    guild_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    user_id INTEGER,
                    fish_key TEXT NOT NULL,
                    fish_name TEXT NOT NULL,
                    weight REAL NOT NULL,
                    unit TEXT NOT NULL,
                    caught_ts INTEGER NOT NULL,
                    UNIQUE (guild_id, message_id)
                );
                CREATE INDEX IF NOT EXISTS idx_fish_catches_top
                    ON fish_catches(guild_id, fish_key, weight DESC);
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
            invalidate_summary_cache()
        except Exception:
            logger.exception("analytics: record failed")


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


def record_fish_catch(
    *,
    guild_id: int,
    channel_id: int,
    message_id: int,
    user_id: int,
    fish: str,
    weight: float,
    unit: str,
    caught_ts: int | None = None,
) -> None:
    """Log one passing fish-watch submission's measured catch.

    Idempotent per ``(guild_id, message_id)`` — re-processing the same
    screenshot message refreshes the row instead of duplicating it.
    Fail-soft like :func:`record_verification`.
    """
    fish = (fish or "").strip()
    if not fish or weight is None:
        return
    if _disabled:
        return
    with _lock:
        _init()
        if _disabled:
            return
        try:
            with _connect() as conn:
                conn.execute(
                    "INSERT INTO fish_catches"
                    " (guild_id, channel_id, message_id, user_id,"
                    "  fish_key, fish_name, weight, unit, caught_ts)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT(guild_id, message_id) DO UPDATE SET"
                    "  fish_key=excluded.fish_key,"
                    "  fish_name=excluded.fish_name,"
                    "  weight=excluded.weight,"
                    "  unit=excluded.unit",
                    (
                        guild_id,
                        channel_id,
                        message_id,
                        user_id,
                        fish.casefold(),
                        fish,
                        float(weight),
                        unit,
                        int(caught_ts if caught_ts is not None else time.time()),
                    ),
                )
                conn.commit()
        except Exception:
            logger.exception("analytics: record_fish_catch failed")


def top_fish_catches(guild_id: int, per_fish: int = 3) -> dict[str, list[dict]]:
    """The heaviest recorded catches per species for the /watch leaderboard.

    Returns ``{fish_key: [row, ...]}`` with up to ``per_fish`` rows per
    species ordered heaviest-first. Ties break oldest-first — first by
    ``caught_ts`` (the submission's post time), then by ``message_id``
    (a Discord snowflake, so exact-second ties still go to the earlier
    submission) — the first member to reach a weight keeps the higher
    rank. Each row carries ``fish_name`` / ``weight`` / ``unit`` /
    ``user_id`` / ``channel_id`` / ``message_id`` / ``caught_ts``.
    Fail-soft: empty dict when disabled.
    """
    if _disabled:
        return {}
    with _lock:
        _init()
        if _disabled:
            return {}
        try:
            with _connect() as conn:
                rows = conn.execute(
                    "SELECT fish_key, fish_name, weight, unit, user_id,"
                    " channel_id, message_id, caught_ts FROM ("
                    "  SELECT *, ROW_NUMBER() OVER ("
                    "   PARTITION BY fish_key"
                    "   ORDER BY weight DESC, caught_ts ASC, message_id ASC"
                    "  ) AS rn FROM fish_catches WHERE guild_id=?"
                    " ) WHERE rn <= ?"
                    " ORDER BY fish_key, weight DESC, caught_ts ASC,"
                    "  message_id ASC",
                    (guild_id, int(per_fish)),
                ).fetchall()
        except Exception:
            logger.exception("analytics: top_fish_catches failed")
            return {}
    out: dict[str, list[dict]] = {}
    for row in rows:
        out.setdefault(row["fish_key"], []).append(dict(row))
    return out


def get_member_profile(guild_id: int, user_id: int) -> dict | None:
    """Return a member's durable profile snapshot, or ``None`` when absent.

    The ``member_profile`` table is the source of truth for the two profile
    fields that are not recoverable from Discord roles — the in-game handle
    and the exact Mastery Rank — plus a role-derived ``platform`` / ``clan``
    snapshot kept for the /manage console. The returned dict mirrors the old
    records-channel read shape (``in_game_name`` / ``mastery_rank`` /
    ``platform`` / ``clan`` / ``last_verified_ts``), with empty fields omitted.
    Fail-soft: returns ``None`` when disabled or on error.
    """
    if _disabled:
        return None
    with _lock:
        _init()
        if _disabled:
            return None
        try:
            with _connect() as conn:
                row = conn.execute(
                    "SELECT in_game_name, mastery_rank, platform, clan,"
                    " updated_ts FROM member_profile"
                    " WHERE guild_id=? AND user_id=?",
                    (guild_id, user_id),
                ).fetchone()
        except Exception:
            logger.exception("analytics: get_member_profile failed")
            return None
    if row is None:
        return None
    out: dict = {}
    if row["in_game_name"]:
        out["in_game_name"] = row["in_game_name"]
    if row["mastery_rank"]:
        out["mastery_rank"] = row["mastery_rank"]
    if row["platform"]:
        out["platform"] = row["platform"]
    if row["clan"]:
        out["clan"] = row["clan"]
    if row["updated_ts"]:
        out["last_verified_ts"] = int(row["updated_ts"])
    return out or None


# Sentinel marking "caller did not supply this field" so an upsert can tell
# "leave the stored value untouched" (preserve) from "clear this value".
_PRESERVE_EXISTING = object()

# The only columns an upsert may write — a fixed allowlist so the dynamic
# column interpolation below can never reach an unexpected identifier.
_MEMBER_PROFILE_COLUMNS = ("in_game_name", "mastery_rank", "platform", "clan")


def upsert_member_profile(
    *,
    guild_id: int,
    user_id: int,
    in_game_name: object = _PRESERVE_EXISTING,
    mastery_rank: object = _PRESERVE_EXISTING,
    platform: object = _PRESERVE_EXISTING,
    clan: object = _PRESERVE_EXISTING,
    updated_ts: int | None = None,
) -> None:
    """Insert or update a member's durable profile snapshot.

    Each field is only written when supplied: passing a value sets it (``None``
    clears it), while omitting an argument preserves whatever is stored. This
    lets a role-derived refresh update ``platform`` / ``clan`` without
    clobbering the OCR-only ``in_game_name`` / ``mastery_rank`` that a role
    change can't recover. Fail-soft like the rest of the module.
    """
    if _disabled:
        return
    ts = int(updated_ts if updated_ts is not None else time.time())
    supplied = {
        "in_game_name": in_game_name,
        "mastery_rank": mastery_rank,
        "platform": platform,
        "clan": clan,
    }
    with _lock:
        _init()
        if _disabled:
            return
        try:
            with _connect() as conn:
                # Build the INSERT with the supplied columns; omitted columns
                # keep their stored value on conflict. Column names come only
                # from the fixed ``_MEMBER_PROFILE_COLUMNS`` allowlist, never
                # from caller input, so the interpolation below is safe.
                set_clauses = ["updated_ts=excluded.updated_ts"]
                cols = ["guild_id", "user_id", "updated_ts"]
                vals: list[object] = [guild_id, user_id, ts]
                for name in _MEMBER_PROFILE_COLUMNS:
                    value = supplied[name]
                    if value is _PRESERVE_EXISTING:
                        continue
                    # Defence-in-depth: identifiers only ever come from the
                    # fixed allowlist, never from caller input.
                    assert name in _MEMBER_PROFILE_COLUMNS
                    cols.append(name)
                    vals.append(value)
                    set_clauses.append(f"{name}=excluded.{name}")
                placeholders = ", ".join("?" for _ in cols)
                conn.execute(
                    f"INSERT INTO member_profile ({', '.join(cols)})"
                    f" VALUES ({placeholders})"
                    " ON CONFLICT(guild_id, user_id) DO UPDATE SET "
                    + ", ".join(set_clauses),
                    vals,
                )
                conn.commit()
        except Exception:
            logger.exception("analytics: upsert_member_profile failed")


def delete_member_data(*, guild_id: int, user_id: int) -> dict:
    """Erase a member's durable data when they leave the server.

    Scoped strictly to ``(guild_id, user_id)``. Drops every awarded title and
    the durable profile snapshot, then anonymises their verification telemetry
    by NULLing ``user_id`` on the ``events`` rows (the aggregate /status stats
    only ever group by outcome/clan, so the counts stay exact while the
    personal reference is gone). Also deletes any pending onboarding prompt.
    The member's screenshot still lives in the records channel, not here.
    Fail-soft like the rest of the module.

    Returns a small audit dict ``{"titles", "events_anonymized",
    "onboarding", "profile", "fish_catches_anonymized"}`` with the number of
    rows touched in each table (all zero when disabled).
    """
    out = {
        "titles": 0, "events_anonymized": 0, "onboarding": 0, "profile": 0,
        "fish_catches_anonymized": 0,
    }
    if _disabled:
        return out
    with _lock:
        _init()
        if _disabled:
            return out
        try:
            with _connect() as conn:
                # The connection is autocommit (isolation_level=None), so an
                # explicit transaction is required to make the purge atomic —
                # otherwise a crash mid-delete could drop the titles while
                # leaving the member's telemetry behind.
                conn.execute("BEGIN")
                try:
                    cur = conn.execute(
                        "DELETE FROM member_titles"
                        " WHERE guild_id=? AND user_id=?",
                        (guild_id, user_id),
                    )
                    titles = cur.rowcount or 0
                    cur = conn.execute(
                        "UPDATE events SET user_id=NULL"
                        " WHERE guild_id=? AND user_id=?",
                        (guild_id, user_id),
                    )
                    events = cur.rowcount or 0
                    cur = conn.execute(
                        "DELETE FROM onboarding_prompts"
                        " WHERE guild_id=? AND user_id=?",
                        (guild_id, user_id),
                    )
                    onboarding = cur.rowcount or 0
                    cur = conn.execute(
                        "DELETE FROM member_profile"
                        " WHERE guild_id=? AND user_id=?",
                        (guild_id, user_id),
                    )
                    profile = cur.rowcount or 0
                    cur = conn.execute(
                        "UPDATE fish_catches SET user_id=NULL"
                        " WHERE guild_id=? AND user_id=?",
                        (guild_id, user_id),
                    )
                    fish_catches = cur.rowcount or 0
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
                # Only report counts once the whole purge has committed.
                out["titles"] = titles
                out["events_anonymized"] = events
                out["onboarding"] = onboarding
                out["profile"] = profile
                out["fish_catches_anonymized"] = fish_catches
            if out["events_anonymized"] or out["titles"]:
                invalidate_summary_cache()
        except Exception:
            logger.exception("analytics: delete_member_data failed")
        return out


def upsert_onboarding_prompt(
    *,
    guild_id: int,
    user_id: int,
    channel_id: int,
    message_id: int,
    posted_ts: int,
) -> None:
    """Insert or replace an onboarding prompt row.

    On conflict (same guild+user) the existing row is replaced so a
    re-prompt always reflects the latest message_id and posted_ts while
    resetting ``completed`` to 0 and incrementing ``reprompt_count``.
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
                    "INSERT INTO onboarding_prompts"
                    " (guild_id, user_id, channel_id, message_id, posted_ts,"
                    "  completed, reprompt_count, ocr_fail_count)"
                    " VALUES (?, ?, ?, ?, ?, 0, 0, 0)"
                    " ON CONFLICT(guild_id, user_id) DO UPDATE SET"
                    "  channel_id=excluded.channel_id,"
                    "  message_id=excluded.message_id,"
                    "  posted_ts=excluded.posted_ts,"
                    "  completed=0,"
                    "  reprompt_count=reprompt_count + 1,"
                    "  ocr_fail_count=0",
                    (guild_id, user_id, channel_id, message_id, posted_ts),
                )
                conn.commit()
        except Exception:
            logger.exception("analytics: upsert_onboarding_prompt failed")


def get_onboarding_prompt(guild_id: int, user_id: int) -> dict | None:
    """Return the active onboarding prompt row as a dict, or None."""
    if _disabled:
        return None
    with _lock:
        _init()
        if _disabled:
            return None
        try:
            with _connect() as conn:
                row = conn.execute(
                    "SELECT guild_id, user_id, channel_id, message_id,"
                    " posted_ts, completed, reprompt_count, ocr_fail_count"
                    " FROM onboarding_prompts"
                    " WHERE guild_id=? AND user_id=?",
                    (guild_id, user_id),
                ).fetchone()
                return dict(row) if row is not None else None
        except Exception:
            logger.exception("analytics: get_onboarding_prompt failed")
            return None


def complete_onboarding_prompt(guild_id: int, user_id: int) -> None:
    """Mark an onboarding prompt as completed. Fail-soft."""
    if _disabled:
        return
    with _lock:
        _init()
        if _disabled:
            return
        try:
            with _connect() as conn:
                conn.execute(
                    "UPDATE onboarding_prompts SET completed=1"
                    " WHERE guild_id=? AND user_id=?",
                    (guild_id, user_id),
                )
                conn.commit()
        except Exception:
            logger.exception("analytics: complete_onboarding_prompt failed")


def delete_onboarding_prompt(guild_id: int, user_id: int) -> None:
    """Delete an onboarding prompt row. Fail-soft."""
    if _disabled:
        return
    with _lock:
        _init()
        if _disabled:
            return
        try:
            with _connect() as conn:
                conn.execute(
                    "DELETE FROM onboarding_prompts"
                    " WHERE guild_id=? AND user_id=?",
                    (guild_id, user_id),
                )
                conn.commit()
        except Exception:
            logger.exception("analytics: delete_onboarding_prompt failed")


def list_pending_onboarding_prompts() -> list[dict]:
    """Return all incomplete onboarding prompt rows as dicts.

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
                    "SELECT guild_id, user_id, channel_id, message_id,"
                    " posted_ts, completed, reprompt_count, ocr_fail_count"
                    " FROM onboarding_prompts"
                    " WHERE completed=0"
                    " ORDER BY posted_ts ASC",
                ).fetchall()
                return [dict(r) for r in rows]
        except Exception:
            logger.exception("analytics: list_pending_onboarding_prompts failed")
            return []


def increment_onboarding_ocr_fail(guild_id: int, user_id: int) -> int:
    """Increment the OCR fail counter for an onboarding prompt.

    Returns the new fail count, or 0 on error / when the row is gone.
    Fail-soft.
    """
    if _disabled:
        return 0
    with _lock:
        _init()
        if _disabled:
            return 0
        try:
            with _connect() as conn:
                conn.execute(
                    "UPDATE onboarding_prompts"
                    " SET ocr_fail_count=ocr_fail_count+1"
                    " WHERE guild_id=? AND user_id=?",
                    (guild_id, user_id),
                )
                conn.commit()
                row = conn.execute(
                    "SELECT ocr_fail_count FROM onboarding_prompts"
                    " WHERE guild_id=? AND user_id=?",
                    (guild_id, user_id),
                ).fetchone()
                return int(row[0]) if row else 0
        except Exception:
            logger.exception("analytics: increment_onboarding_ocr_fail failed")
            return 0


def _percentile(values: list[int], pct: float) -> int:
    if not values:
        return 0
    s = sorted(values)
    k = max(0, min(len(s) - 1, round((pct / 100.0) * (len(s) - 1))))
    return s[k]


def _empty_summary() -> dict:
    """Return a fresh, zeroed summary dict (the shape /status expects)."""
    return {
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


def _compute_summary() -> dict:
    """Run the actual SQL queries and return a fresh summary dict.

    Uses ``_connect_read`` (a dedicated read-only connection; WAL lets it run
    concurrently with the writer), so callers must NOT hold the write
    ``_lock`` while this runs — holding it would block every telemetry write
    for the duration of the ~7 aggregation queries.
    """
    out: dict = _empty_summary()
    try:
        with _connect_read() as conn:
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
        logger.exception("analytics: summary query failed")
        return out

    p = _resolve_path()
    if p is not None and p.exists():
        try:
            out["db_size_bytes"] = p.stat().st_size
        except OSError:
            pass
    return out


def summary() -> dict:
    """Return a snapshot summary used by the /status stats page.

    Results are cached for ``ANALYTICS_SUMMARY_TTL`` seconds (default 30)
    to avoid re-running ~7 SQL queries on every /status page refresh.
    Writes (``record_verification``, ``delete_member_data``) invalidate the
    cache immediately.  Call ``invalidate_summary_cache()`` to force a fresh
    read at any time.
    """
    global _summary_snapshot

    # Fast path: one atomic reference load — both cache result and timestamp
    # are read from the same tuple, eliminating the split-read race condition.
    snap = _summary_snapshot
    if snap is not None and (time.time() - snap[1]) < ANALYTICS_SUMMARY_TTL:
        return snap[0]

    if _disabled:
        return _empty_summary()

    # Slow path: take the write lock only for init + the snapshot
    # double-check, then run the read-only aggregation queries OUTSIDE it
    # (they use the dedicated read connection; WAL lets them run concurrently
    # with writes). Two threads may occasionally both recompute and race the
    # snapshot store — last-writer-wins is fine (both results are valid and
    # the assignment is a single atomic reference store) — but telemetry
    # writes are never blocked behind the ~7 queries.
    with _lock:
        _init()
        if _disabled:
            return _empty_summary()
        # Double-check under the lock in case another thread already refreshed.
        snap = _summary_snapshot
        if snap is not None and (time.time() - snap[1]) < ANALYTICS_SUMMARY_TTL:
            return snap[0]
    result = _compute_summary()
    _summary_snapshot = (result, time.time())
    return result
