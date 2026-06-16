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
        return True
    except Exception:
        logger.warning("failed to save records index to %s", p, exc_info=True)
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
    index = load_index(path)
    index_add(index, user_id, message_id, channel_id=channel_id)
    return save_index(index, path)


def get_record_message_ids(
    user_id: int, *, path: Path | str = RECORDS_INDEX_PATH
) -> list[int]:
    """Return the record message IDs for ``user_id`` (newest appended last)."""
    index = load_index(path)
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
    cid = load_index(path).get("channel_id")
    try:
        return int(cid) if cid is not None else None
    except (TypeError, ValueError):
        return None
