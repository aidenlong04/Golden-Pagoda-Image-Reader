"""Fast user_id → record message-id index for the member-records channel.

Treats the records channel as a queryable store: the source of truth is the
channel itself, this JSON file is just a lookup cache mapping each member's
Discord user ID to the message IDs of their record posts. Cheap O(1) lookups
(then ``fetch_message`` / a jump URL) instead of scanning or searching.

Shared by the one-time migration (``scripts/migrate_channel_to_records.py``)
and the live bot (``_post_member_record`` keeps it fresh going forward).

Design notes:
- stdlib only, fully fail-soft (a bad/missing file never raises).
- atomic writes (temp file + ``os.replace``).
- append-only + de-duplicated. Deletion is intentionally not tracked.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# Env-configurable to match the other ./data state paths (HEALTH_PATH,
# ANALYTICS_DB_PATH). Default lives under ./data.
RECORDS_INDEX_PATH = Path(
    os.getenv("RECORDS_INDEX_PATH", "./data/records_index.json")
)

_INDEX_VERSION = 1

# In-memory cache of parsed indexes keyed by path, validated by a cheap
# ``stat()`` signature so an external write (another process, a test fixture)
# is still picked up. Saves the full read + JSON parse that used to run on
# every ``get_record_message_ids`` / ``get_channel_id`` / ``add_record`` call
# — the hot path of every record read/write and ``/profile`` use.
_CACHE_MAX_ENTRIES = 8
_cache: dict[str, tuple[tuple[int, int] | None, dict]] = {}


def _stat_signature(p: Path) -> tuple[int, int] | None:
    """Return ``(mtime_ns, size)`` for ``p``, or None when unreadable."""
    try:
        st = p.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _invalidate_cache(p: Path) -> None:
    _cache.pop(str(p), None)


def _load_cached(path: Path | str) -> dict:
    """Return the parsed index for ``path``, reusing the in-memory copy while
    the file on disk is unchanged. Callers must treat the result as shared —
    mutations must go through ``add_record`` (which refreshes the cache)."""
    p = Path(path)
    key = str(p)
    sig = _stat_signature(p)
    cached = _cache.get(key)
    if cached is not None and cached[0] == sig:
        return cached[1]
    index = load_index(p)
    if len(_cache) >= _CACHE_MAX_ENTRIES:
        # Evict the oldest-inserted entry (dicts preserve insertion order);
        # in practice only tests ever use more than one path.
        _cache.pop(next(iter(_cache)))
    _cache[key] = (sig, index)
    return index


def _empty_index(channel_id: int | None = None) -> dict:
    return {"version": _INDEX_VERSION, "channel_id": channel_id, "users": {}}


def load_index(path: Path | str = RECORDS_INDEX_PATH) -> dict:
    """Load the index, returning a fresh empty one on any problem."""
    p = Path(path)
    if not p.exists():
        return _empty_index()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("index root is not an object")
        data.setdefault("version", _INDEX_VERSION)
        data.setdefault("channel_id", None)
        users = data.get("users")
        if not isinstance(users, dict):
            data["users"] = {}
        return data
    except Exception:
        logger.warning(
            "records index unreadable (%s); starting fresh", p, exc_info=True
        )
        return _empty_index()


def save_index(index: dict, path: Path | str = RECORDS_INDEX_PATH) -> bool:
    """Atomically write ``index`` to ``path``. Returns True on success."""
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(index, indent=2, sort_keys=True), encoding="utf-8"
        )
        os.replace(tmp, p)
        _invalidate_cache(p)
        return True
    except Exception:
        logger.warning("failed to save records index to %s", p, exc_info=True)
        _invalidate_cache(p)
        return False


def index_add(
    index: dict,
    user_id: int,
    message_id: int,
    *,
    channel_id: int | None = None,
) -> dict:
    """Append ``message_id`` to ``user_id``'s list in an in-memory ``index``.

    De-duplicates and preserves insertion (oldest-first) order. Use this for
    batch work (accumulate, then ``save_index`` once); use ``add_record`` for
    one-off incremental updates.
    """
    if channel_id is not None:
        index["channel_id"] = channel_id
    users = index.setdefault("users", {})
    lst = users.setdefault(str(user_id), [])
    if message_id not in lst:
        lst.append(message_id)
    return index


def add_record(
    user_id: int,
    message_id: int,
    *,
    channel_id: int | None = None,
    path: Path | str = RECORDS_INDEX_PATH,
) -> bool:
    """Load-merge-append-save a single record id. Fail-soft.

    Safe to call off the event loop (e.g. via ``asyncio.to_thread``) from the
    bot whenever a new record is posted.
    """
    index = _load_cached(path)
    index_add(index, user_id, message_id, channel_id=channel_id)
    ok = save_index(index, path)
    if ok:
        # Re-prime the cache with the just-written index so the next read
        # (typically the record edit that follows immediately) skips the
        # disk read + parse entirely.
        p = Path(path)
        _cache[str(p)] = (_stat_signature(p), index)
    return ok


def get_record_message_ids(
    user_id: int, *, path: Path | str = RECORDS_INDEX_PATH
) -> list[int]:
    """Return the record message IDs for ``user_id`` (newest appended last)."""
    index = _load_cached(path)
    raw = index.get("users", {}).get(str(user_id), [])
    out: list[int] = []
    for mid in raw:
        try:
            out.append(int(mid))
        except (TypeError, ValueError):
            continue
    return out


def get_channel_id(*, path: Path | str = RECORDS_INDEX_PATH) -> int | None:
    """Return the records channel ID stored in the index, if any."""
    cid = _load_cached(path).get("channel_id")
    try:
        return int(cid) if cid is not None else None
    except (TypeError, ValueError):
        return None
