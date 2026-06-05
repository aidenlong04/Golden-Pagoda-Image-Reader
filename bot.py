from __future__ import annotations

import asyncio
import contextlib
import functools
import io
import json
import logging
import os
import re
import sys
import time
import warnings
from collections import deque
from collections.abc import Callable
from datetime import timedelta

from pathlib import Path

# Suppress discord.py's audioop DeprecationWarning on Python 3.12. The bot
# never uses voice, but discord.player imports audioop unconditionally and
# stdlib audioop is slated for removal in 3.13. Filter before importing
# discord so the warning never reaches the log.
warnings.filterwarnings(
    "ignore",
    message=r"'audioop' is deprecated.*",
    category=DeprecationWarning,
)

import aiohttp  # noqa: E402
import discord  # noqa: E402
import numpy as np  # noqa: E402
import requests  # noqa: E402
from discord import app_commands  # noqa: E402
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps  # noqa: E402

try:
    import pytesseract  # optional local fallback
except ImportError:  # pragma: no cover
    pytesseract = None  # type: ignore[assignment]

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from logic import (
    ClanSlot,
    find_clan_slot,
    parse_clan_name,
    parse_mastery_rank,
    parse_profile_name,
)
import analytics

# Single root handler — discord.py's own setup_logging is disabled in
# client.run() below (log_handler=None) so every record is emitted exactly
# once. LOG_LEVEL is env-tunable (default INFO) for cheap runtime tuning.
logging.basicConfig(
    level=(os.getenv("LOG_LEVEL") or "INFO").upper(),
    format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# Quiet chatty third-party loggers so journald/json-file stay lean.
for _name, _lvl in (
    ("discord.http",          logging.WARNING),
    ("discord.gateway",       logging.WARNING),
    ("discord.state",         logging.WARNING),
    ("urllib3",               logging.WARNING),
    ("urllib3.connectionpool",logging.WARNING),
    ("PIL",                   logging.WARNING),
    ("PIL.PngImagePlugin",    logging.WARNING),
):
    logging.getLogger(_name).setLevel(_lvl)

# Silence the harmless "PyNaCl/davey not installed" warnings emitted on every
# connect (we don't use voice).
for _name in ("discord.client", "discord.voice_client"):
    logging.getLogger(_name).addFilter(
        lambda r: "voice will NOT be supported" not in r.getMessage()
    )

# ---------- Configuration ---------------------------------------------------

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")


def _int_env(name: str, default: int = 0) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        # base=0 accepts decimal, 0x hex, 0o octal, 0b binary literals.
        return int(raw, 0) if raw.lower().startswith(("0x", "0o", "0b")) else int(raw)
    except ValueError:
        logger.warning("Env var %s=%r is not a valid integer; using %d", name, raw, default)
        return default


def _float_env(name: str, default: float = 0.0) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("Env var %s=%r is not a valid float; using %s", name, raw, default)
        return default


TARGET_CHANNEL_ID = _int_env("TARGET_CHANNEL_ID")

# Platform name → list of acceptable Discord role-name aliases (case-insensitive).
PLATFORM_ROLE_ALIASES: dict[str, tuple[str, ...]] = {
    "PC": ("PC", "Windows", "Steam"),
    "Xbox": ("Xbox", "XBL", "Xbox Live"),
    "PlayStation": ("PlayStation", "PS", "PSN", "PS4", "PS5"),
    "Switch": ("Switch", "Nintendo", "Nintendo Switch"),
    "Mobile": ("Mobile", "iOS", "Android", "Apple"),
}

# Platform name → .env key for the resolved role ID.
PLATFORM_ROLE_ID_ENV_KEYS: dict[str, str] = {
    "PC": "PLATFORM_ROLE_PC_ID",
    "Xbox": "PLATFORM_ROLE_XBOX_ID",
    "PlayStation": "PLATFORM_ROLE_PLAYSTATION_ID",
    "Switch": "PLATFORM_ROLE_SWITCH_ID",
    "Mobile": "PLATFORM_ROLE_MOBILE_ID",
}

# Cached platform role IDs (auto-resolved + written back to .env on connect).
PLATFORM_ROLE_IDS: dict[str, int | None] = {
    platform: (_int_env(key) or None)
    for platform, key in PLATFORM_ROLE_ID_ENV_KEYS.items()
}

# Per-platform custom emoji literals (``<:Name:id>``), used when rendering the
# progress card so each platform row gets its own icon instead of the bullet.
PLATFORM_EMOJIS: dict[str, str | None] = {
    "PC": (os.getenv("PLATFORM_EMOJI_PC") or "").strip() or None,
    "Xbox": (os.getenv("PLATFORM_EMOJI_XBOX") or "").strip() or None,
    "PlayStation": (os.getenv("PLATFORM_EMOJI_PLAYSTATION") or "").strip() or None,
    "Switch": (os.getenv("PLATFORM_EMOJI_SWITCH") or "").strip() or None,
    "Mobile": (os.getenv("PLATFORM_EMOJI_MOBILE") or "").strip() or None,
}

# Clan slots (auto-synced with the .env file on connect).
CLAN_SLOT_COUNT = 7
ENV_FILE_PATH = Path(os.getenv("ENV_FILE_PATH", ".env"))

# OCR.space API — engine 3 per requirements. Falls back to local Tesseract
# (with --oem 3) if OCR_API_KEY is not set and pytesseract is available.
OCR_API_KEY = os.getenv("OCR_API_KEY", "").strip()
OCR_API_URL = os.getenv("OCR_API_URL", "https://api.ocr.space/parse/image")
OCR_ENGINE = os.getenv("OCR_ENGINE", "3")
OCR_LANGUAGE = os.getenv("OCR_LANGUAGE", "eng")
TESSERACT_CONFIG = "--oem 3 --psm 6"
# OCR.space free tier rejects uploads >1 MB. We re-encode oversize images as
# JPEG before sending so large PNG screenshots still get verified.
OCR_MAX_UPLOAD_BYTES = _int_env("OCR_MAX_UPLOAD_BYTES", 900_000)
OCR_RECOMPRESS_QUALITY = _int_env("OCR_RECOMPRESS_QUALITY", 70)

# Reactions added to the original screenshot message based on verification outcome.
PASS_REACTION_ID = _int_env("PASS_REACTION_ID", 1506744187096399882)
FAIL_REACTION = os.getenv("FAIL_REACTION", "\U0001F6A8")  # 🚨
# Parsed form: a PartialEmoji if FAIL_REACTION is a custom emoji literal
# (<:name:id> or <a:name:id>), otherwise the raw unicode string.
_CUSTOM_EMOJI_RE = re.compile(r"^<(a?):([A-Za-z0-9_~]+):(\d+)>$")
def _parse_reaction_emoji(raw: str):
    m = _CUSTOM_EMOJI_RE.match((raw or "").strip())
    if not m:
        return raw
    animated, name, eid = m.group(1) == "a", m.group(2), int(m.group(3))
    return discord.PartialEmoji(name=name, id=eid, animated=animated)


def _emoji_id_from_literal(raw: str | None) -> int | None:
    """Return the numeric ID from a ``<:Name:id>`` / ``<a:Name:id>`` literal."""
    if not raw:
        return None
    m = _CUSTOM_EMOJI_RE.match(raw.strip())
    if not m:
        return None
    try:
        return int(m.group(3))
    except ValueError:
        return None
FAIL_REACTION_EMOJI = _parse_reaction_emoji(FAIL_REACTION)
# Reaction cleared from the post when verification passes (e.g. a "pending"
# marker added upstream). Set to 0 to disable.
PENDING_REACTION_ID = _int_env("PENDING_REACTION_ID", 1459403163432910972)

# Components V2 reply styling.
COMPONENTS_V2_FLAG = 1 << 15  # 32768 — IS_COMPONENTS_V2
ACCENT_PASS = _int_env("ACCENT_PASS", 0xD4A857)        # gold
ACCENT_FAIL = _int_env("ACCENT_FAIL", 0xED4245)        # red
ACCENT_INCOMPLETE = _int_env("ACCENT_INCOMPLETE", 0x99AAB5)  # grey

# Role granted to users whose screenshot was readable but couldn't be fully
# verified automatically (platform icon missing, unconfigured clan, etc).
# A staff member then manually completes verification.
INCOMPLETE_ROLE_ID = _int_env("INCOMPLETE_ROLE_ID", 1361846841905381632)

# Auto-delete bot replies after this many seconds (0 = keep forever).
REPLY_TTL_SECONDS = _int_env("REPLY_TTL_SECONDS", 180)

# Role removed from a member on successful verification (e.g. an "unverified"
# gate role). Set to 0 to disable.
VERIFY_REMOVE_ROLE_ID = _int_env("VERIFY_REMOVE_ROLE_ID", 1459326361968574555)

# Catch-up scan: process missed messages from recent history on startup.
CATCHUP_LOOKBACK_HOURS = _int_env("CATCHUP_LOOKBACK_HOURS", 24)
CATCHUP_STATE_PATH = Path(os.getenv("CATCHUP_STATE_PATH", "/app/data/catchup_state.json"))
CATCHUP_DELAY_SECONDS = _float_env("CATCHUP_DELAY_SECONDS", 1.0)

# Role IDs that count as "has MR verified" / "has joined a syndicate" for
# the /progress completion check. Both accept a comma-separated list — a
# member counts as having the category if they hold ANY of the listed roles.
# Empty list disables the category (it stays at 0/0 and doesn't drag the
# completion percentage down).
#
# Operators normally configure these by NAME via MR_ROLE_NAMES /
# SYNDICATE_ROLE_NAMES: on every reconnect those names are resolved
# against each guild's role list and the IDs are written back to
# MR_ROLE_IDS / SYNDICATE_ROLE_IDS in .env. The _IDS vars are still
# the source of truth at runtime (and can be hand-edited as a fallback).
def _csv(name: str, default: str = "") -> list[str]:
    return [
        x.strip() for x in (os.getenv(name) or default).split(",") if x.strip()
    ]


def _csv_ids(name: str) -> list[int]:
    return [int(x) for x in _csv(name) if x.isdigit()]


MR_ROLE_NAMES: list[str] = _csv(
    "MR_ROLE_NAMES",
    "MR 1-10,MR 11-15,MR 16-22,MR 22-29,MR 30,LR 1-7",
)
SYNDICATE_ROLE_NAMES: list[str] = _csv(
    "SYNDICATE_ROLE_NAMES",
    "Steel Meridian,Arbiters of Hexis,Cephalon Suda,"
    "The Perrin Sequence,Red Veil,New Loka",
)
MR_ROLE_IDS: list[int] = _csv_ids("MR_ROLE_IDS")
SYNDICATE_ROLE_IDS: list[int] = _csv_ids("SYNDICATE_ROLE_IDS")
# Channel users are directed to when they're missing MR/Platform/Syndicate.
# Surfaces as a link button on the incomplete card.
HELP_CHANNEL_ID = _int_env("HELP_CHANNEL_ID", 1392582268769271950)


def _format_mastery_display(value: str | None) -> str:
    """Normalize a stored/OCR'd mastery rank to a card-ready value.

    The label on the card is already "Mastery Rank", so the value drops
    the redundant prefix: ``"MR 28" -> "28"``. Legendary ranks expand:
    ``"LR 3" -> "Legendary 3"``. Anything else (e.g. "Unranked") passes
    through unchanged. Shared by the verify card, the /profile gatherer,
    and the mastery editor so the formatting lives in one place.
    """
    if not value:
        return ""
    upper = value.upper()
    if upper.startswith("MR "):
        return value[3:].strip()
    if upper.startswith("LR "):
        return f"Legendary {value[3:].strip()}"
    return value


if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN is required")
if TARGET_CHANNEL_ID <= 0:
    raise RuntimeError("TARGET_CHANNEL_ID must be set to a valid channel ID")


# ---------- Clan slot loading + .env writeback ------------------------------


def _load_clan_slots() -> list[ClanSlot]:
    slots: list[ClanSlot] = []
    for i in range(1, CLAN_SLOT_COUNT + 1):
        name = (os.getenv(f"CLAN_ROLE_{i}_NAME") or "").strip() or None
        role_id = _int_env(f"CLAN_ROLE_{i}_ID") or None
        emoji = (os.getenv(f"CLAN_ROLE_{i}_EMOJI") or "").strip() or None
        slots.append(
            ClanSlot(slot=i, clan_name=name, role_id=role_id, emoji=emoji)
        )
    return slots


def _slot_field_value(slot: ClanSlot, field: str) -> str:
    if field == "NAME":
        return slot.clan_name or ""
    if field == "ID":
        return str(slot.role_id) if slot.role_id else ""
    if field == "EMOJI":
        return slot.emoji or ""
    return ""


def _atomic_write_text(path: Path, content: str) -> None:
    """Write to ``path`` atomically: stage in a sibling tempfile, then rename.

    Prevents a half-written .env if the process is killed mid-write (e.g.
    OOM under the 512m container cap), which would leave the bot unable
    to start on next boot.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content)
    os.replace(tmp, path)


def _rewrite_env_file(
    replace_line: Callable[[str], str | None],
    missing_lines: Callable[[], list[str]],
) -> bool:
    """Rewrite ``.env`` in place using a shared read→replace→append skeleton.

    ``replace_line`` is called for every existing line and returns either a
    replacement string or ``None`` to leave the line untouched. Any entries
    returned by ``missing_lines()`` are appended (after a blank separator).
    Returns ``False`` when the file doesn't exist. Centralises the logic the
    clan / platform / id-list writers previously duplicated.
    """
    if not ENV_FILE_PATH.exists():
        return False
    lines = ENV_FILE_PATH.read_text().splitlines()
    for idx, line in enumerate(lines):
        replacement = replace_line(line)
        if replacement is not None:
            lines[idx] = replacement
    missing = missing_lines()
    if missing:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(missing)
    _atomic_write_text(ENV_FILE_PATH, "\n".join(lines) + "\n")
    return True


def _update_env_clan_slots(slots: list[ClanSlot]) -> bool:
    """Rewrite the CLAN_ROLE_{i}_NAME/_ID/_EMOJI entries in the .env file in place."""
    by_slot = {s.slot: s for s in slots}
    seen: set[tuple[int, str]] = set()

    def replace(line: str) -> str | None:
        m = _ENV_CLAN_SLOT_RE.match(line)
        if not m:
            return None
        indent, slot_num, field = m.group(1), int(m.group(2)), m.group(3)
        slot = by_slot.get(slot_num)
        if slot is None:
            return None
        seen.add((slot_num, field))
        return f"{indent}CLAN_ROLE_{slot_num}_{field}={_slot_field_value(slot, field)}"

    def missing() -> list[str]:
        out: list[str] = []
        for i in sorted(by_slot):
            for field in ("NAME", "ID", "EMOJI"):
                if (i, field) not in seen:
                    out.append(f"CLAN_ROLE_{i}_{field}={_slot_field_value(by_slot[i], field)}")
        return out

    return _rewrite_env_file(replace, missing)


CLAN_SLOTS: list[ClanSlot] = _load_clan_slots()


def _update_env_platform_ids(ids: dict[str, int | None]) -> bool:
    """Rewrite the PLATFORM_ROLE_*_ID entries in the .env file in place."""
    key_to_platform = {v: k for k, v in PLATFORM_ROLE_ID_ENV_KEYS.items()}
    seen: set[str] = set()

    def replace(line: str) -> str | None:
        m = _ENV_PLATFORM_ID_RE.match(line)
        if not m:
            return None
        indent, key = m.group(1), m.group(2)
        platform = key_to_platform.get(key)
        if platform is None:
            return None
        seen.add(key)
        return f"{indent}{key}={str(ids.get(platform)) if ids.get(platform) else ''}"

    def missing() -> list[str]:
        return [
            f"{key}={str(ids.get(platform)) if ids.get(platform) else ''}"
            for platform, key in PLATFORM_ROLE_ID_ENV_KEYS.items()
            if key not in seen
        ]

    return _rewrite_env_file(replace, missing)


def _update_env_id_list(env_key: str, ids: list[int]) -> bool:
    """Rewrite (or append) ``ENV_KEY=id1,id2,...`` in the .env file."""
    value = ",".join(str(i) for i in ids)
    seen = False

    def replace(line: str) -> str | None:
        nonlocal seen
        if seen:
            return None
        m = _ENV_GENERIC_KEY_RE.match(line)
        if not m or m.group(2) != env_key:
            return None
        seen = True
        return f"{m.group(1)}{env_key}={value}"

    def missing() -> list[str]:
        return [] if seen else [f"{env_key}={value}"]

    return _rewrite_env_file(replace, missing)


# ---------- Discord client --------------------------------------------------

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

BOT_START_TIME = time.time()
HEALTH_PATH = os.getenv("HEALTH_PATH", "/tmp/gp_health")
HEALTH_INTERVAL = _int_env("HEALTH_INTERVAL", 20)

# Populated after tree.sync(); used to render clickable slash-command mentions
# (`</name:id>`) inside ephemeral replies fired from component buttons.
_COMMAND_IDS: dict[str, int] = {}

# Strong refs for fire-and-forget tasks. asyncio docs warn that
# create_task() return values must be kept alive or the task may be
# garbage-collected mid-await. We discard each task once it completes.
_BG_TASKS: set[asyncio.Task] = set()

# Sliding-window error tracking for the healthcheck. When >= 3 errors
# occur in a 60s window, the health-tick task stops writing the signal
# file so the watchdog sees the container as unhealthy and restarts it.
# This catches systemic functional failures (e.g. every screenshot raising
# TypeError) that the current "asyncio loop is alive" check misses.
_ERROR_TIMESTAMPS: deque[float] = deque(maxlen=10)
_ERROR_THRESHOLD = 3
_ERROR_WINDOW_SECONDS = 60
_HEALTH_STOPPED = False

# Bounded source_message_id -> (channel_id, bot_reply_id) map so we can
# tombstone the bot's verification reply when the user deletes their
# original screenshot post. OrderedDict + popitem(last=False) gives us
# O(1) FIFO eviction; cap is small because the only consumers are
# moderators clicking "Delete" within a single session, and the bot
# already auto-deletes replies after REPLY_TTL_SECONDS anyway.
from collections import OrderedDict as _OrderedDict  # noqa: E402
_REPLY_MAP_CAP = 512
_REPLY_MAP: "_OrderedDict[int, tuple[int, int]]" = _OrderedDict()


def _remember_reply(source_id: int, channel_id: int, reply_id: int) -> None:
    _REPLY_MAP[source_id] = (channel_id, reply_id)
    _REPLY_MAP.move_to_end(source_id)
    while len(_REPLY_MAP) > _REPLY_MAP_CAP:
        _REPLY_MAP.popitem(last=False)


# ---------- Catch-up state persistence --------------------------------------


def _load_catchup_state() -> int | None:
    """Load the last successfully-scanned message ID from disk."""
    try:
        data = json.loads(CATCHUP_STATE_PATH.read_text())
    except FileNotFoundError:
        return None
    except Exception:
        logger.warning("Failed to load catch-up state from %s", CATCHUP_STATE_PATH, exc_info=True)
        return None
    if not isinstance(data, dict):
        return None
    last = data.get("last_message_id")
    if not isinstance(last, int):
        return None
    return last


def _save_catchup_state(message_id: int) -> None:
    """Persist the last successfully-scanned message ID to disk."""
    try:
        CATCHUP_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CATCHUP_STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps({"last_message_id": message_id}))
        os.replace(tmp, CATCHUP_STATE_PATH)
    except Exception:
        logger.warning("Failed to save catch-up state to %s", CATCHUP_STATE_PATH, exc_info=True)


def _message_already_processed(message: discord.Message) -> bool:
    """Check if a message has already been processed by looking for the bot's
    reactions (pass/fail). Returns True if the message has any of the bot's
    outcome reactions, meaning it was already handled."""
    if not message.reactions or client.user is None:
        return False
    fail_id = FAIL_REACTION_EMOJI.id if isinstance(FAIL_REACTION_EMOJI, discord.PartialEmoji) else None
    fail_str = FAIL_REACTION_EMOJI if isinstance(FAIL_REACTION_EMOJI, str) else None
    for reaction in message.reactions:
        if not reaction.me:
            continue
        emoji = reaction.emoji
        if isinstance(emoji, (discord.Emoji, discord.PartialEmoji)):
            eid = emoji.id
            if eid is not None and eid in (PASS_REACTION_ID, fail_id):
                return True
        elif fail_str is not None and emoji == fail_str:
            return True
    return False


async def _health_task() -> None:
    while True:
        if not _HEALTH_STOPPED:
            try:
                with open(HEALTH_PATH, "w") as fh:
                    fh.write(str(int(time.time())))
            except OSError:
                logger.exception("health write failed")
        await asyncio.sleep(HEALTH_INTERVAL)


def _spawn_bg_task(coro) -> asyncio.Task:
    """Schedule a coroutine on the running loop and keep a strong reference
    so the GC can't reap it mid-await. Self-cleans on completion."""
    task = asyncio.create_task(coro)
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)
    return task


# Persistent aiohttp session for Discord REST + CDN fetches. Reusing a
# single session avoids paying the TLS handshake (~100-200ms on a CX22
# AMD Rome core) on every reply, progress edit, and avatar fetch.
_HTTP_SESSION = None  # type: ignore[var-annotated]
_HTTP_SESSION_LOCK = asyncio.Lock()


async def _get_http_session():
    """Return the process-wide aiohttp ClientSession, creating it lazily.

    The session is bound to the running event loop. Callers must await
    this from within the bot's loop (everywhere we use it already does).
    """
    global _HTTP_SESSION
    if _HTTP_SESSION is not None and not _HTTP_SESSION.closed:
        return _HTTP_SESSION

    async with _HTTP_SESSION_LOCK:
        if _HTTP_SESSION is None or _HTTP_SESSION.closed:
            connector = aiohttp.TCPConnector(
                limit=20, ttl_dns_cache=300, enable_cleanup_closed=True
            )
            _HTTP_SESSION = aiohttp.ClientSession(connector=connector)
    return _HTTP_SESSION


@client.event
async def on_ready() -> None:
    logger.info("Logged in as %s", client.user)
    if not getattr(client, "_health_started", False):
        _spawn_bg_task(_health_task())
        client._health_started = True  # type: ignore[attr-defined]
    _sync_clan_slots_from_guilds()
    _sync_platform_roles_from_guilds()
    _sync_named_role_lists_from_guilds()
    try:
        synced = await tree.sync()
        logger.info("Synced %d slash command(s)", len(synced))
        _COMMAND_IDS.clear()
        for cmd in synced:
            _COMMAND_IDS[cmd.name] = cmd.id
    except Exception:
        logger.exception("Failed to sync slash commands")

    # Run catch-up scan on first connect only (not on every reconnect).
    # Spawned as a background task so on_ready returns immediately — the
    # scan can take tens of seconds, and we don't want to block heartbeats
    # or delay the event loop from processing live messages.
    if not getattr(client, "_catchup_done", False):
        client._catchup_done = True  # type: ignore[attr-defined]
        _spawn_bg_task(_catchup_scan())


def _sync_platform_roles_from_guilds() -> list[str]:
    """Resolve each platform's role ID against the server's role list and
    write the IDs back to .env. Runs on every reconnect.

    Returns a list of human-readable change descriptions (empty if nothing
    changed).
    """
    if not client.guilds:
        return []
    changes: list[str] = []
    for platform, aliases in PLATFORM_ROLE_ALIASES.items():
        current = PLATFORM_ROLE_IDS.get(platform)
        resolved: discord.Role | None = None
        for guild in client.guilds:
            if current:
                role = guild.get_role(current)
                if role is not None:
                    resolved = role
                    break
            role = _find_role(guild, *aliases)
            if role is not None:
                resolved = role
                break
        if resolved is None:
            continue
        if current != resolved.id:
            logger.info(
                "platform role %s: %s → %s (%s)",
                platform,
                current,
                resolved.id,
                resolved.name,
            )
            PLATFORM_ROLE_IDS[platform] = resolved.id
            changes.append(f"{platform}: id {current} → {resolved.id} ({resolved.name})")
    if changes:
        try:
            _update_env_platform_ids(PLATFORM_ROLE_IDS)
        except Exception:
            logger.exception("Failed to update %s", ENV_FILE_PATH)
    return changes


def _resolve_named_roles(names: list[str]) -> list[int]:
    """Look up each role name across the connected guilds (case-insensitive,
    first match wins) and return the resolved IDs in input order. Names
    that don't match any guild role are silently skipped. Duplicates are
    removed while preserving order.
    """
    if not names or not client.guilds:
        return []
    ids: list[int] = []
    seen: set[int] = set()
    for name in names:
        for guild in client.guilds:
            role = _find_role(guild, name)
            if role is not None and role.id not in seen:
                ids.append(role.id)
                seen.add(role.id)
                break
    return ids


def _sync_named_role_lists_from_guilds() -> list[str]:
    """Resolve MR_ROLE_NAMES / SYNDICATE_ROLE_NAMES against the server's
    role list and write the resulting ID lists back to .env. Mirrors
    the platform/clan auto-resolve flow.
    """
    global MR_ROLE_IDS, SYNDICATE_ROLE_IDS
    if not client.guilds:
        return []
    changes: list[str] = []
    for label, names, env_id_key, current in (
        ("MR", MR_ROLE_NAMES, "MR_ROLE_IDS", MR_ROLE_IDS),
        ("Syndicate", SYNDICATE_ROLE_NAMES, "SYNDICATE_ROLE_IDS", SYNDICATE_ROLE_IDS),
    ):
        resolved = _resolve_named_roles(names)
        if not resolved or resolved == current:
            continue
        logger.info(
            "%s role IDs: %s → %s (from %s)", label, current, resolved, names,
        )
        if label == "MR":
            MR_ROLE_IDS = resolved
        else:
            SYNDICATE_ROLE_IDS = resolved
        try:
            _update_env_id_list(env_id_key, resolved)
        except Exception:
            logger.exception("Failed to update %s in %s", env_id_key, ENV_FILE_PATH)
        changes.append(f"{label}: {current} → {resolved}")
    return changes


def _sync_clan_slots_from_guilds() -> list[str]:
    """For each guild the bot is in, resolve clan slot names/IDs against the
    server's role list and update the slot cache + .env file. Runs every
    time the bot reconnects — zero manual intervention required.

    Returns a list of human-readable change descriptions (empty if nothing
    changed).
    """
    if not client.guilds:
        return []
    changes: list[str] = []
    for slot in CLAN_SLOTS:
        resolved: discord.Role | None = None
        for guild in client.guilds:
            if slot.role_id:
                role = guild.get_role(slot.role_id)
                if role is not None:
                    resolved = role
                    break
            if slot.clan_name:
                want = _normalize(slot.clan_name)
                role = discord.utils.find(
                    lambda r, w=want: _normalize(r.name) == w, guild.roles
                )
                if role is not None:
                    resolved = role
                    break
        if resolved is None:
            continue
        if slot.role_id != resolved.id or slot.clan_name != resolved.name:
            logger.info(
                "clan slot %d: %r/%s → %r/%s",
                slot.slot,
                slot.clan_name,
                slot.role_id,
                resolved.name,
                resolved.id,
            )
            old_name = slot.clan_name
            old_id = slot.role_id
            slot.clan_name = resolved.name
            slot.role_id = resolved.id
            changes.append(
                f"slot {slot.slot}: {old_name!r}/{old_id} → {resolved.name!r}/{resolved.id}"
            )
    if changes:
        try:
            _update_env_clan_slots(CLAN_SLOTS)
        except Exception:
            logger.exception("Failed to update %s", ENV_FILE_PATH)
    return changes


# ---------- Role lookup helpers --------------------------------------------

_CLAN_TAG_SUFFIX_RE = re.compile(r"#\d+\s*$")
_WS_RE = re.compile(r"\s+")
_ENV_CLAN_SLOT_RE = re.compile(r"^(\s*)CLAN_ROLE_(\d+)_(NAME|ID|EMOJI)\s*=.*$")
_ENV_PLATFORM_ID_RE = re.compile(r"^(\s*)(PLATFORM_ROLE_[A-Z]+_ID)\s*=.*$")
_ENV_GENERIC_KEY_RE = re.compile(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*)\s*=.*$")
_PLACEHOLDER_NAME_RE = re.compile(r"^place[\s\-_]*holder", re.IGNORECASE)


def _normalize(name: str) -> str:
    return _WS_RE.sub(" ", name.strip().lower())


def _strip_clan_tag(clan_name: str) -> str:
    """'Grand Warhorde#245' -> 'Grand Warhorde'."""
    return _CLAN_TAG_SUFFIX_RE.sub("", clan_name).strip()


def _find_role(guild: discord.Guild, *candidates: str) -> discord.Role | None:
    """Case-insensitive lookup against the server's role list."""
    wanted = {_normalize(c) for c in candidates if c}
    if not wanted:
        return None
    for role in guild.roles:
        if _normalize(role.name) in wanted:
            return role
    return None


def _find_clan_role(guild: discord.Guild, clan_name: str) -> discord.Role | None:
    # 1. Configured slot match (case-insensitive, ignores trailing #NNN).
    slot = find_clan_slot(CLAN_SLOTS, clan_name)
    if slot is not None and slot.role_id:
        role = guild.get_role(slot.role_id)
        if role is not None:
            return role
    # 2. Direct role-name fallback (handles unconfigured clans).
    return _find_role(guild, clan_name, _strip_clan_tag(clan_name))


# ---------- OCR -------------------------------------------------------------


def _shrink_for_ocr(
    image_bytes: bytes, filename: str, content_type: str
) -> tuple[bytes, str, str]:
    """Re-encode oversize images as JPEG so they fit the OCR.space 1 MB limit."""
    if len(image_bytes) <= OCR_MAX_UPLOAD_BYTES:
        return image_bytes, filename, content_type or "image/png"
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        try:
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=OCR_RECOMPRESS_QUALITY, optimize=True)
            shrunk = buf.getvalue()
        finally:
            img.close()
    except Exception:
        logger.exception("Failed to recompress image for OCR; sending original")
        return image_bytes, filename, content_type or "image/png"
    base = filename.rsplit(".", 1)[0] or "screenshot"
    logger.info("Recompressed %s for OCR: %d -> %d bytes", filename, len(image_bytes), len(shrunk))
    return shrunk, f"{base}.jpg", "image/jpeg"


def _ocr_via_api(
    image_bytes: bytes,
    filename: str,
    content_type: str,
    *,
    engine: str | None = None,
) -> tuple[str, list[tuple[str, tuple[int, int, int, int]]]]:
    image_bytes, filename, content_type = _shrink_for_ocr(
        image_bytes, filename, content_type
    )
    # OCR.space returns transient 5xx; one quick retry usually clears it.
    last_err: Exception | None = None
    for attempt in (1, 2):
        try:
            response = requests.post(
                OCR_API_URL,
                headers={"apikey": OCR_API_KEY},
                data={
                    "OCREngine": engine or OCR_ENGINE,
                    "language": OCR_LANGUAGE,
                    "scale": "true",
                    "isTable": "false",
                    "detectOrientation": "true",
                    "isOverlayRequired": "true",
                },
                files={"file": (filename, image_bytes, content_type or "image/png")},
                timeout=60,
            )
            if 500 <= response.status_code < 600 and attempt == 1:
                logger.info("OCR.space %d on attempt 1; retrying once", response.status_code)
                time.sleep(1.0)
                continue
            response.raise_for_status()
            payload = response.json()
            break
        except (requests.HTTPError, requests.ConnectionError, requests.Timeout) as e:
            last_err = e
            if attempt == 1:
                logger.info("OCR.space %s on attempt 1; retrying once", e.__class__.__name__)
                time.sleep(1.0)
                continue
            raise
    else:
        raise last_err if last_err else RuntimeError("OCR.space failed")
    if payload.get("IsErroredOnProcessing"):
        raise RuntimeError(f"OCR API error: {payload.get('ErrorMessage') or payload}")
    parsed = payload.get("ParsedResults") or []
    text = "\n".join(item.get("ParsedText", "") for item in parsed)
    words: list[tuple[str, tuple[int, int, int, int]]] = []
    for item in parsed:
        overlay = item.get("TextOverlay") or {}
        for line in overlay.get("Lines") or []:
            for word in line.get("Words") or []:
                try:
                    left = int(word.get("Left", 0))
                    top = int(word.get("Top", 0))
                    width = int(word.get("Width", 0))
                    height = int(word.get("Height", 0))
                except (TypeError, ValueError):
                    continue
                if width <= 0 or height <= 0:
                    continue
                words.append(
                    (str(word.get("WordText", "")), (left, top, left + width, top + height))
                )
    return text, words


def _preprocess_for_tesseract(image_bytes: bytes) -> Image.Image:
    """Upscale + grayscale + autocontrast. Tesseract is dramatically more
    accurate on Warframe's stylized UI font when the input is enlarged and
    contrast-normalized first.

    Caller is responsible for closing the returned Image.
    """
    img = Image.open(io.BytesIO(image_bytes))
    if img.mode != "L":
        img = ImageOps.grayscale(img)
    # Upscale only when the source is modestly sized; very large screenshots
    # are already legible and 2x would blow past Tesseract's memory budget.
    if max(img.size) < 2400:
        img = img.resize((img.width * 2, img.height * 2), Image.LANCZOS)
    img = ImageOps.autocontrast(img, cutoff=2)
    return img


def _ocr_via_tesseract(
    image_bytes: bytes,
) -> tuple[str, list[tuple[str, tuple[int, int, int, int]]]]:
    if pytesseract is None:
        raise RuntimeError("pytesseract not installed")
    try:
        prepared = _preprocess_for_tesseract(image_bytes)
    except Exception:
        logger.exception("Tesseract preprocess failed; falling back to raw image")
        prepared = Image.open(io.BytesIO(image_bytes))
    try:
        text = pytesseract.image_to_string(prepared, config=TESSERACT_CONFIG)
    finally:
        prepared.close()
    return text, []


# Engine label returned alongside (text, words) so the caller can record
# an accurate analytics tag without relying on shared mutable state
# (concurrent _ocr() invocations would otherwise race).
def _ocr(
    image_bytes: bytes, filename: str, content_type: str
) -> tuple[str, list[tuple[str, tuple[int, int, int, int]]], str]:
    if OCR_API_KEY:
        try:
            text, words = _ocr_via_api(image_bytes, filename, content_type)
            return text, words, "ocr.space"
        except Exception as api_err:
            logger.warning(
                "OCR.space engine %s failed (%s); trying engine 2",
                OCR_ENGINE,
                api_err.__class__.__name__,
            )
            # Engine 2 sometimes succeeds where engine 3 (multilang) 500s
            # on the same upload. Skip the second attempt if we were already
            # configured for engine 2.
            if OCR_ENGINE != "2":
                try:
                    text, words = _ocr_via_api(
                        image_bytes, filename, content_type, engine="2"
                    )
                    return text, words, "ocr.space:e2"
                except Exception as api_err2:
                    logger.warning(
                        "OCR.space engine 2 also failed (%s); falling back to local Tesseract",
                        api_err2.__class__.__name__,
                    )
            if pytesseract is None:
                raise
            text, words = _ocr_via_tesseract(image_bytes)
            return text, words, "tesseract"
    if pytesseract is None:
        raise RuntimeError(
            "No OCR backend available: set OCR_API_KEY or install pytesseract."
        )
    text, words = _ocr_via_tesseract(image_bytes)
    return text, words, "tesseract"


_PROFILE_TOKEN_RE = re.compile(r"#\d{2,4}")
_TITLE_NAME_RE = re.compile(r"[A-Za-z0-9_\-\.\[\]]{2,}#\d{2,4}")
# How much of the image height counts as "title bar" for the supplement pass.
_TITLE_STRIP_FRAC = 0.14


def _supplement_title_bar_ocr(
    image_bytes: bytes,
    ocr_text: str,
    ocr_words: list[tuple[str, tuple[int, int, int, int]]],
) -> tuple[str, list[tuple[str, tuple[int, int, int, int]]]]:
    """If the main OCR missed the title-bar PlayerName#NNN token, retry just
    the top strip with Tesseract. OCR.space engine 3 routinely drops the
    small, low-contrast title-bar line on Warframe profile screenshots even
    when the rest of the page reads cleanly. Tesseract on an upscaled,
    autocontrasted top-strip crop recovers it reliably.

    Returns the (possibly augmented) (text, words) tuple. No-op if pytesseract
    is unavailable or the original OCR already contains a #NNN token in the
    top portion of the image.
    """
    if pytesseract is None:
        return ocr_text, ocr_words
    img = None
    try:
        img = Image.open(io.BytesIO(image_bytes))
        w, h = img.size
        strip_h = max(40, int(h * _TITLE_STRIP_FRAC))
        # If we already have a #NNN word inside the title strip, no need to retry.
        for _text, (_x0, y0, _x1, _y1) in ocr_words:
            if y0 < strip_h and _PROFILE_TOKEN_RE.search(_text or ""):
                return ocr_text, ocr_words
        try:
            strip = img.crop((0, 0, w, strip_h)).convert("L")
            try:
                # Upscale aggressively — title-bar glyphs are ~16-22px tall on a
                # 1080p screenshot, well below Tesseract's comfort zone.
                strip = strip.resize((strip.width * 3, strip.height * 3), Image.LANCZOS)
                strip = ImageOps.autocontrast(strip, cutoff=2)
                data = pytesseract.image_to_data(
                    strip,
                    config="--oem 3 --psm 7",
                    output_type=pytesseract.Output.DICT,
                )
            finally:
                strip.close()
        except Exception:
            logger.exception("Title-bar Tesseract supplement failed")
            return ocr_text, ocr_words
    except Exception:
        return ocr_text, ocr_words
    finally:
        if img is not None:
            img.close()
    new_words: list[tuple[str, tuple[int, int, int, int]]] = []
    found_token = False
    n = len(data.get("text") or [])
    for i in range(n):
        txt = (data["text"][i] or "").strip()
        if not txt:
            continue
        try:
            left = int(data["left"][i]) // 3
            top = int(data["top"][i]) // 3
            width = int(data["width"][i]) // 3
            height = int(data["height"][i]) // 3
        except (TypeError, ValueError, KeyError):
            continue
        if width <= 0 or height <= 0:
            continue
        new_words.append((txt, (left, top, left + width, top + height)))
        if _PROFILE_TOKEN_RE.search(txt):
            found_token = True
    if not found_token:
        # Also try matching the joined line — Tesseract sometimes splits
        # "Senseiwom#241" into "Senseiwom" + "#241" tokens.
        joined = " ".join(t for t, _ in new_words)
        if _TITLE_NAME_RE.search(joined):
            found_token = True
    if not found_token:
        joined = " ".join(t for t, _ in new_words)
        logger.info(
            "Title-bar OCR supplement found no #NNN token "
            "(strip=%dpx, tokens=%d, text=%r)",
            strip_h,
            len(new_words),
            joined[:200],
        )
        return ocr_text, ocr_words
    logger.info(
        "Title-bar OCR supplement recovered %d token(s): %r",
        len(new_words),
        " ".join(t for t, _ in new_words)[:120],
    )
    # Prepend the recovered title-bar text so parse_profile_name sees it
    # before any CLAN-section content.
    supplement_line = " ".join(t for t, _ in new_words)
    augmented_text = supplement_line + "\n" + ocr_text
    return augmented_text, new_words + ocr_words


# ---------- Screenshot processing -------------------------------------------


def _first_image_attachment(message: discord.Message) -> discord.Attachment | None:
    for attachment in message.attachments:
        if attachment.content_type and attachment.content_type.startswith("image/"):
            return attachment
    return None


async def _process_screenshot(message: discord.Message) -> None:
    """Core screenshot verification logic. Extracted from on_message to support
    both live processing and catch-up scanning."""
    global _HEALTH_STOPPED
    try:
        await _process_screenshot_impl(message)
    except Exception as e:
        # Top-level catch-all: log systemic bugs at ERROR and track them
        # in the sliding window. Transient user errors (e.g. discord.Forbidden
        # on role assignment) are already caught + logged deeper in the stack
        # and don't propagate here.
        if isinstance(e, (TypeError, AttributeError, NameError, KeyError, ValueError)):
            logger.error(
                "_process_screenshot: unexpected exception (systemic bug): %s",
                e.__class__.__name__, exc_info=True,
            )
            _ERROR_TIMESTAMPS.append(time.time())
            # If we have >= ERROR_THRESHOLD errors in the last ERROR_WINDOW_SECONDS,
            # stop the health signal so the watchdog restarts the container.
            now = _ERROR_TIMESTAMPS[-1]
            recent = sum(1 for ts in _ERROR_TIMESTAMPS if now - ts < _ERROR_WINDOW_SECONDS)
            if recent >= _ERROR_THRESHOLD:
                if not _HEALTH_STOPPED:
                    logger.critical(
                        "_process_screenshot: %d errors in %ds window; stopping health signal",
                        recent, _ERROR_WINDOW_SECONDS,
                    )
                    _HEALTH_STOPPED = True
        else:
            # Transient network / Discord API errors — log but don't count
            # toward the systemic-failure threshold.
            logger.exception("_process_screenshot: transient error")
        raise


async def _process_screenshot_impl(message: discord.Message) -> None:
    """Core screenshot verification logic implementation (wrapped by error-tracking)."""
    attachment = _first_image_attachment(message)
    if attachment is None:
        await _fail(message, "Not an image", "Upload a PNG/JPG screenshot of your Warframe profile.")
        return
    if message.guild is None:
        await _fail(message, "Server only", "I can only assign roles in a server channel.")
        return

    try:
        image_bytes = await attachment.read()
        # Probe-decode to fail fast on corrupt uploads before paying OCR cost.
        probe = Image.open(io.BytesIO(image_bytes))
        try:
            probe.verify()
        finally:
            probe.close()
    except Exception:
        logger.exception("Failed to read uploaded image")
        await _fail(message, "Invalid image", "Image could not be opened. Re-upload a valid PNG/JPG.")
        return

    ocr_engine = "ocr.space" if OCR_API_KEY else ("tesseract" if pytesseract else "none")
    ocr_started = time.monotonic()
    try:
        # OCR involves blocking HTTP (up to 60s) and subprocess work — run it
        # in a worker thread so the event loop keeps servicing heartbeats and
        # other messages while a single screenshot is being verified.
        ocr_text_raw, ocr_words, ocr_engine = await asyncio.to_thread(
            _ocr,
            image_bytes,
            attachment.filename,
            attachment.content_type or "image/png",
        )
        ocr_text = ocr_text_raw.strip()
    except Exception:
        logger.exception("OCR failed for uploaded image")
        _spawn_bg_task(asyncio.to_thread(
            analytics.record_verification,
            outcome="ocr_error",
            ocr_engine=ocr_engine,
            ocr_latency_ms=int((time.monotonic() - ocr_started) * 1000),
            user_id=message.author.id,
            guild_id=message.guild.id,
        ))
        await _fail(message, "Not readable", "No text could be read. Upload a clearer screenshot.")
        return
    ocr_latency_ms = int((time.monotonic() - ocr_started) * 1000)

    # OCR.space often drops the small title-bar text; rerun Tesseract on the
    # top strip to recover the PlayerName#NNN token when it's missing.
    try:
        ocr_text, ocr_words = await asyncio.to_thread(
            _supplement_title_bar_ocr, image_bytes, ocr_text, ocr_words
        )
    except Exception:
        logger.exception("Title-bar OCR supplement raised")

    profile_name = parse_profile_name(ocr_text)
    clan_name = parse_clan_name(ocr_text)
    mastery_rank = parse_mastery_rank(ocr_text)

    # Fallback name when OCR can't read the profile handle: use the
    # guild's current member count as a pseudo-discriminator so the
    # pass response still shows something meaningful (e.g. "Tenno #1234").
    profile_name_fallback_used = False
    if not profile_name:
        member_count = getattr(message.guild, "member_count", None) or 0
        profile_name = f"Tenno #{member_count}"
        profile_name_fallback_used = True

    if not clan_name:
        # Without a clan name we can't assign the only role the bot
        # auto-grants from the screenshot. Surface a clean failure.
        snippet = " ".join(ocr_text.split())[:240]
        logger.warning(
            "Unreadable: engine=%s profile=%r clan=%r mastery=%r ocr=%r",
            ocr_engine,
            profile_name,
            clan_name,
            mastery_rank,
            snippet,
        )
        _spawn_bg_task(asyncio.to_thread(
            analytics.record_verification,
            outcome="unreadable",
            clan=clan_name,
            ocr_engine=ocr_engine,
            ocr_latency_ms=ocr_latency_ms,
            user_id=message.author.id,
            guild_id=message.guild.id,
        ))
        await _fail(
            message,
            "Profile not found",
            "Make sure your title bar (PlayerName#NNN) and CLAN are visible at the top.",
        )
        return

    member = message.author if isinstance(message.author, discord.Member) else None
    if member is None:
        await _fail(message, "Not a member", "I can only assign roles to server members.")
        return

    role_lines: list[str] = []
    issues: list[str] = []
    passed = True

    if profile_name_fallback_used:
        logger.info(
            "Profile name OCR failed; using member-count fallback %r", profile_name
        )

    # Resolve which Discord role coroutines to fire concurrently. Building
    # them up front lets us issue role-add HTTP calls in parallel via
    # asyncio.gather instead of paying sequential Discord round-trips.
    role_coros: list[tuple[str, "discord.Role", "asyncio.Future"]] = []

    clan_emoji: str | None = None
    slot = find_clan_slot(CLAN_SLOTS, clan_name)
    if slot is not None:
        clan_emoji = slot.emoji
    role = _find_clan_role(message.guild, clan_name)
    if role is None:
        issues.append(f"No role for clan **{_strip_clan_tag(clan_name)}**.")
        passed = False
    else:
        role_coros.append(("Clan", role, _add_role(member, role, "Screenshot clan verification")))

    assigned_role_ids: set[int] = set()
    if role_coros:
        results = await asyncio.gather(
            *(c for _, _, c in role_coros), return_exceptions=True
        )
        for (label, role_obj, _), result in zip(role_coros, results, strict=True):
            if isinstance(result, BaseException):
                logger.exception("%s role assignment failed", label, exc_info=result)
                role_lines.append(f"{label}: error assigning role")
                passed = False
            else:
                _, status = result
                role_lines.append(f"{label}: {status}")
                # discord.py's add_roles updates member.roles via a gateway
                # event that may not have arrived yet; track the assignment
                # locally so the post-verify role check sees fresh state.
                assigned_role_ids.add(role_obj.id)

    # Post-verify category check: list every required category the member
    # is still missing (Platform / MR / Syndicate). These are surfaced for
    # the user's awareness but do NOT block a pass: as long as the clan
    # role was successfully assigned (env-recognised clan), we run the
    # full pass procedure (reaction, unverified-role removal, pass embed)
    # so the user is properly verified. Platform/MR/Syndicate are picked
    # up later via the user's own self-service flow.
    effective_role_ids = {r.id for r in member.roles} | assigned_role_ids
    cats = _role_categories_for(effective_role_ids)
    if passed and mastery_rank:
        # On a pass we render the OCR-read Mastery Rank as its own row, so
        # count it as a satisfied category here too. Otherwise the bar
        # (driven by role possession) and the "Missing" pill (which hides
        # MR once it's been shown) disagree — e.g. a 2/4 bar that lists
        # only one missing field.
        cats = [
            (name, True if name == "Mastery Rank" else ok)
            for name, ok in cats
        ]
    have = sum(1 for _, ok in cats if ok)
    total = len(cats)
    extra_missing = [name for name, ok in cats if not ok]
    if extra_missing and not passed:
        # Only surface missing categories on the incomplete embed; on a
        # pass the user already gets the progress bar showing them.
        issues.extend(f"Missing **{cat}** role." for cat in extra_missing)

    # On a pass, "Missing Data" mirrors the bar exactly (it derives from
    # the same cats), so the number of missing fields always equals
    # total − have.
    pass_missing: list[str] = list(extra_missing) if passed else []

    # Build the labeled rows rendered beneath the progress bar on a
    # passing card: profile name, platform, clan, mastery rank, missing
    # data. Clan + platform rows carry their custom emoji bytes (fetched
    # below) so the card renders the same icons the env defines.
    member_platform: str | None = None
    for plat, rid in PLATFORM_ROLE_IDS.items():
        if rid and rid in effective_role_ids:
            member_platform = plat
            break

    clan_emoji_bytes: bytes | None = None
    platform_emoji_bytes: bytes | None = None
    profile_emoji_bytes: bytes | None = None
    mastery_emoji_bytes: bytes | None = None
    missing_emoji_bytes: bytes | None = None
    if passed:
        if clan_emoji:
            clan_emoji_bytes = await _fetch_emoji_bytes(clan_emoji)
        if member_platform:
            platform_emoji_bytes = await _fetch_emoji_bytes(
                PLATFORM_EMOJIS.get(member_platform)
            )
        # Profile / mastery / missing rows reuse the same custom emojis the
        # text embed renders (operator, mastery sigil, warning) so the card
        # icons match the bot's established identity. Each falls back to the
        # bullet glyph inside the renderer when its emoji can't be fetched.
        profile_emoji_bytes = await _fetch_emoji_bytes(OPERATOR_EMOJI_RAW)
        mastery_emoji_bytes = await _fetch_emoji_bytes(MASTERY_RANK_EMOJI_RAW)
        missing_emoji_bytes = await _fetch_emoji_bytes(WARNING_EMOJI_RAW)

    info_lines: list[tuple] = []
    if passed:
        # Order matters: the card grid fills row-major (index 0 = top-left,
        # 1 = top-right, 2 = bottom-left, 3 = bottom-right), so this order
        # lays out as:  Clan | Mastery Rank  /  Profile | Platform  —
        # keeping Profile directly under Clan in the left column.
        if clan_name:
            info_lines.append(
                ("Clan", _strip_clan_tag(clan_name), clan_emoji_bytes)
            )
        if mastery_rank:
            info_lines.append(
                ("Mastery Rank", _format_mastery_display(mastery_rank),
                 mastery_emoji_bytes)
            )
        if profile_name:
            display_profile = (
                profile_name if profile_name.startswith("Tenno #")
                else _strip_clan_tag(profile_name)
            )
            info_lines.append(
                ("Profile", display_profile, profile_emoji_bytes)
            )
        if member_platform:
            info_lines.append(
                ("Platform", member_platform, platform_emoji_bytes)
            )
        if pass_missing:
            info_lines.append(
                ("Missing Data", ", ".join(pass_missing), missing_emoji_bytes)
            )

    # Render the progress card once; both pass and incomplete embeds attach it.
    progress_png: bytes | None = None
    try:
        avatar_url = (member.display_avatar or member.default_avatar).replace(
            size=256, format="png"
        ).url
        avatar_bytes = await _fetch_avatar_bytes(avatar_url)
        progress_png = await asyncio.to_thread(
            _render_progress_card_png,
            avatar_bytes=avatar_bytes,
            display_name=member.display_name,
            count=have,
            target=total,
            info_lines=info_lines if info_lines else None,
        )
    except Exception:
        logger.warning("verify: progress card render failed", exc_info=True)

    # Fan out the user-visible work concurrently: reacting, removing the
    # opposite-state role, and posting the V2 reply all hit different
    # Discord endpoints and never depend on each other.
    nick_target = _nickname_suggestion(member, profile_name)
    if passed:
        # _pass_components owns the entire pass reply: the progress card
        # image on top, then ONE gold container holding the call-sign
        # choices. Hand it the nick suggestion directly rather than
        # appending a separate prompt.
        components = _pass_components(
            profile_name, clan_name,
            clan_emoji=clan_emoji,
            mastery_rank=mastery_rank,
            progress_attachment="progress.png" if progress_png else None,
            nick_suggestion=nick_target,
            user_id=member.id,
            current_nick=member.display_name or "",
            missing_categories=pass_missing,
        )
        outbound = [
            _react(message, "pass"),
            _remove_unverified_role(member),
            _send_v2(
                message, components,
                file_bytes=progress_png,
                file_name="progress.png",
            ),
        ]
    else:
        components = _incomplete_components(
            " ".join(issues),
            link_buttons=_help_link_buttons(message.guild),
            progress_attachment="progress.png" if progress_png else None,
        )
        if nick_target:
            try:
                components.extend(_nickname_prompt_components(
                    nick_target, member.id,
                    current_nick=member.display_name or "",
                ))
            except Exception:
                logger.exception("nick prompt build failed")
        outbound = [
            _react(message, "incomplete"),
            _add_incomplete_role(member),
            _send_v2(
                message, components,
                mention_user=True,
                allow_role_mentions=True,
                file_bytes=progress_png,
                file_name="progress.png",
            ),
        ]
    for result in await asyncio.gather(*outbound, return_exceptions=True):
        if isinstance(result, BaseException):
            logger.exception("post-verification action failed", exc_info=result)

    # Push analytics to a background task so the SQLite write never adds to
    # the user-visible response time. record_verification is fail-soft, so
    # losing one event on shutdown is acceptable.
    _spawn_bg_task(asyncio.to_thread(
        analytics.record_verification,
        outcome="pass" if passed else "incomplete",
        clan=clan_name,
        ocr_engine=ocr_engine,
        ocr_latency_ms=ocr_latency_ms,
        user_id=member.id,
        guild_id=message.guild.id,
    ))

    # Durable per-member snapshot (in-game name, platform, clan, exact
    # mastery rank, last-verified). Survives restarts + role changes and
    # backs the /profile card. Fail-soft + off the event loop; partial
    # (COALESCE) so unread fields don't clobber a previous good snapshot.
    _spawn_bg_task(asyncio.to_thread(
        analytics.upsert_member_profile,
        guild_id=message.guild.id,
        user_id=member.id,
        mastery_rank=mastery_rank,
        in_game_name=None if profile_name_fallback_used else profile_name,
        platform=member_platform,
        clan=_strip_clan_tag(clan_name) if clan_name else None,
        last_verified_ts=int(time.time()),
    ))


# Hard cap on history fetched per scan. Guards against a corrupt state file
# or extreme lookback values driving an unbounded API walk.
_CATCHUP_SCAN_LIMIT = 1000


async def _catchup_scan() -> None:
    """Scan recent message history in TARGET_CHANNEL_ID for unprocessed
    screenshots and verify them. Runs once on startup after on_ready."""
    if CATCHUP_LOOKBACK_HOURS <= 0:
        logger.info("Catch-up scan disabled (CATCHUP_LOOKBACK_HOURS=%d)", CATCHUP_LOOKBACK_HOURS)
        return

    channel = client.get_channel(TARGET_CHANNEL_ID)
    if not isinstance(channel, discord.TextChannel):
        logger.warning("Catch-up scan: TARGET_CHANNEL_ID=%s not found or not a text channel", TARGET_CHANNEL_ID)
        return

    last_seen_id = _load_catchup_state()
    cutoff = discord.utils.utcnow() - timedelta(hours=CATCHUP_LOOKBACK_HOURS)
    # Prefer resuming from last-seen snowflake; otherwise let Discord skip
    # everything older than the lookback window server-side.
    after: discord.Object | object
    after = discord.Object(id=last_seen_id) if last_seen_id else cutoff

    logger.info(
        "Starting catch-up scan: channel=%s lookback=%dh last_seen=%s limit=%d",
        channel.name, CATCHUP_LOOKBACK_HOURS, last_seen_id, _CATCHUP_SCAN_LIMIT,
    )

    allowed_types = (discord.MessageType.default, discord.MessageType.reply)
    found = 0
    processed = 0
    skipped = 0
    errors = 0
    latest_id: int | None = None

    try:
        async for message in channel.history(
            limit=_CATCHUP_SCAN_LIMIT,
            after=after,
            oldest_first=True,
        ):
            latest_id = message.id
            if (
                message.author.bot
                or message.webhook_id is not None
                or message.type not in allowed_types
            ):
                continue
            if not _first_image_attachment(message):
                continue

            found += 1

            if _message_already_processed(message):
                skipped += 1
                continue

            try:
                logger.info("Catch-up: processing message %s from %s", message.id, message.author)
                await _process_screenshot(message)
                processed += 1
                _save_catchup_state(message.id)
                await asyncio.sleep(CATCHUP_DELAY_SECONDS)
            except Exception:
                errors += 1
                logger.exception("Catch-up: failed to process message %s", message.id)

        if latest_id is not None:
            _save_catchup_state(latest_id)

        logger.info(
            "Catch-up scan complete: found=%d processed=%d skipped=%d errors=%d",
            found, processed, skipped, errors,
        )
    except Exception:
        logger.exception("Catch-up scan failed")


async def _add_role(
    member: discord.Member, role: discord.Role, reason: str
) -> tuple[bool, str]:
    if role in member.roles:
        return False, f"already has **{role.name}**"
    try:
        await member.add_roles(role, reason=reason)
        return True, f"assigned **{role.name}**"
    except discord.Forbidden:
        return False, f"missing permission to assign **{role.name}**"
    except discord.HTTPException:
        logger.exception("Failed to assign role %s", role.name)
        return False, f"error assigning **{role.name}**"


async def _react(message: discord.Message, status: str) -> None:
    """Add a reaction based on outcome and clear the pending marker.

    ``status`` is one of ``"pass"``, ``"fail"``, or ``"incomplete"``. The
    incomplete state intentionally adds **no** reaction and leaves the
    upstream pending marker alone (a human still needs to follow up).
    """
    if status == "incomplete":
        return

    if status == "pass":
        emoji: discord.Emoji | discord.PartialEmoji | str | None = (
            client.get_emoji(PASS_REACTION_ID) if PASS_REACTION_ID else None
        )
        # Fallback when the emoji isn't in cache yet (e.g. mid-startup).
        # Discord matches reactions by ID; "r" is a placeholder name.
        if emoji is None and PASS_REACTION_ID:
            emoji = discord.PartialEmoji(name="r", id=PASS_REACTION_ID)
        if emoji is None:
            emoji = "\U0001F44D"  # 👍 fallback
    else:  # fail
        emoji = FAIL_REACTION_EMOJI
    try:
        await message.add_reaction(emoji)
    except discord.HTTPException:
        logger.exception("Failed to add %s reaction", status)

    # Clear the upstream "pending" marker on resolved outcomes only.
    if PENDING_REACTION_ID:
        pending: discord.Emoji | discord.PartialEmoji | None = client.get_emoji(
            PENDING_REACTION_ID
        ) or discord.PartialEmoji(name="r", id=PENDING_REACTION_ID)
        try:
            await message.clear_reaction(pending)
        except discord.NotFound:
            pass  # nothing to clear
        except discord.Forbidden:
            logger.warning(
                "Missing Manage Messages permission to clear pending reaction %s",
                PENDING_REACTION_ID,
            )
        except discord.HTTPException:
            logger.exception("Failed to clear pending reaction")


async def _fail(
    message: discord.Message,
    headline: str,
    reason: str,
    *,
    image_url: str | None = None,
) -> None:
    await _react(message, "fail")
    logger.info(
        "_fail: sending V2 reply headline=%r reason=%r msg=%s chan=%s",
        headline, reason, message.id, message.channel.id,
    )
    try:
        await _send_v2(
            message,
            _fail_components(headline, reason, image_url=image_url),
            mention_user=True,
        )
    except Exception:
        logger.exception("_fail: _send_v2 raised")
        raise


async def _remove_unverified_role(member: discord.Member) -> None:
    if not VERIFY_REMOVE_ROLE_ID:
        return
    role = member.guild.get_role(VERIFY_REMOVE_ROLE_ID)
    if role is None or role not in member.roles:
        return
    try:
        await member.remove_roles(role, reason="Screenshot verification passed")
    except discord.Forbidden:
        logger.warning("Missing permission to remove role %s", role.name)
    except discord.HTTPException:
        logger.exception("Failed to remove role %s", role.name)


async def _add_incomplete_role(member: discord.Member) -> None:
    if not INCOMPLETE_ROLE_ID:
        return
    role = member.guild.get_role(INCOMPLETE_ROLE_ID)
    if role is None or role in member.roles:
        return
    try:
        await member.add_roles(role, reason="Screenshot verification incomplete — awaiting manual review")
    except discord.Forbidden:
        logger.warning("Missing permission to add incomplete role %s", role.name)
    except discord.HTTPException:
        logger.exception("Failed to add incomplete role %s", role.name)


# ---------- Components V2 reply -------------------------------------------


CLAN_EMOJI = os.getenv("CLAN_EMOJI", "").strip() or "\U0001F6E1\ufe0f"  # 🛡️
OPERATOR_EMOJI_RAW = os.getenv(
    "OPERATOR_EMOJI", "<:operator:1467922510908494098>"
).strip()
MASTERY_RANK_EMOJI_RAW = os.getenv(
    "MASTERY_RANK_EMOJI", "<:mastery:1511640736318226553>"
).strip()
SYNDICATE_EMOJI_RAW = os.getenv("SYNDICATE_EMOJI", "").strip()
WARNING_EMOJI_RAW = os.getenv(
    "WARNING_EMOJI", "<:WarningStatus:1512253042270142634>"
).strip()

# Per-faction syndicate styling for the /profile card: each canonical
# Warframe syndicate maps to (env-key suffix, accent colour). A member's
# syndicate role name is matched case-insensitively; the accent tints the
# faction name on the card and an optional custom emoji is read from
# SYNDICATE_EMOJI_<KEY> (<:name:id>). Colours are the in-game faction
# accents and can be tweaked freely.
_SYNDICATE_FACTIONS: dict[str, tuple[str, tuple[int, int, int]]] = {
    "steel meridian": ("STEEL_MERIDIAN", (198, 70, 56)),
    "arbiters of hexis": ("ARBITERS_OF_HEXIS", (200, 205, 210)),
    "cephalon suda": ("CEPHALON_SUDA", (58, 150, 221)),
    "the perrin sequence": ("THE_PERRIN_SEQUENCE", (38, 198, 176)),
    "red veil": ("RED_VEIL", (176, 38, 42)),
    "new loka": ("NEW_LOKA", (124, 185, 73)),
}
SYNDICATE_FACTION_EMOJIS: dict[str, str] = {
    name: (os.getenv(f"SYNDICATE_EMOJI_{key}") or "").strip()
    for name, (key, _color) in _SYNDICATE_FACTIONS.items()
}


def _syndicate_style(
    role_name: str,
) -> tuple[tuple[int, int, int] | None, str | None]:
    """Map a syndicate role name to its ``(accent_colour, emoji_literal)``.

    Matches ``role_name`` case-insensitively against the canonical Warframe
    factions. Returns the faction accent + its ``SYNDICATE_EMOJI_<KEY>``
    literal; falls back to ``(None, shared SYNDICATE_EMOJI)`` for an
    unrecognised name so callers can apply the role's own colour + the
    shared icon.
    """
    key = (role_name or "").strip().lower()
    meta = _SYNDICATE_FACTIONS.get(key)
    if meta is None:
        return None, (SYNDICATE_EMOJI_RAW or None)
    _env_key, color = meta
    emoji = SYNDICATE_FACTION_EMOJIS.get(key) or SYNDICATE_EMOJI_RAW or None
    return color, emoji


def _link_button(label: str, url: str) -> dict:
    """Build a Components V2 Link button (style 5)."""
    return {"type": 2, "style": 5, "label": label, "url": url}


def _link_button_row(
    link_buttons: list[tuple[str, str]] | None,
) -> dict | None:
    """Build a single action row (type 1) of Link buttons, capped at
    Discord's 5-per-row limit, or None when there are none to render."""
    if not link_buttons:
        return None
    return {
        "type": 1,
        "components": [_link_button(lbl, url) for lbl, url in link_buttons[:5]],
    }


def _pass_components(
    profile: str,
    clan: str | None,
    *,
    clan_emoji: str | None = None,
    mastery_rank: str | None = None,
    progress_attachment: str | None = None,
    nick_suggestion: str | None = None,
    user_id: int | None = None,
    current_nick: str = "",
    missing_categories: list[str] | None = None,
) -> list[dict]:
    emoji = (clan_emoji or "").strip() or CLAN_EMOJI

    # Normal path: the profile / clan / mastery / missing bullets are
    # rendered INTO the progress PNG, so the reply is just the image on
    # top (a top-level media gallery, OUTSIDE any container) followed by a
    # SINGLE gold container holding the in-game-name call-sign choices
    # (when there's a name worth suggesting). Folding the nickname prompt
    # in here keeps the whole pass reply to one image + one container.
    if progress_attachment:
        top_components: list[dict] = [{
            "type": 12,
            "items": [{"media": {"url": f"attachment://{progress_attachment}"}}],
        }]
        caption, row_buttons = _callsign_buttons(
            nick_suggestion, user_id, current_nick
        )
        children = list(caption)
        if row_buttons:
            children.append({"type": 1, "components": row_buttons[:5]})
        if children:
            top_components.append({
                "type": 17,
                "accent_color": ACCENT_PASS,
                "components": children,
            })
        return top_components

    # Fallback path (progress card render failed): the bullets live in
    # text instead of the image, but the shape stays identical — one gold
    # container holding the bullet list and the call-sign prompt, so
    # there's never a stray container or loose action row.
    display_clan = _strip_clan_tag(clan) if clan else None
    clan_part = (
        f"> * {emoji} **`{display_clan}`**"
        if display_clan
        else f"> * {emoji} *Unaffiliated*"
    )
    # The "Tenno #<member_count>" fallback (used when OCR can't read the
    # real handle) keeps its #NNN suffix so the response stays unique per
    # server; real handles render without the discriminator.
    if profile.startswith("Tenno #"):
        display_profile = profile
    else:
        display_profile = _strip_clan_tag(profile)
    inner_lines = [
        f"> * {OPERATOR_EMOJI_RAW} **`{display_profile}`**",
        clan_part,
    ]
    if mastery_rank:
        # The "Mastery Rank" label already names the field, so drop the
        # redundant "MR "/"LR " prefix via the shared formatter (which also
        # expands Legendary ranks, e.g. "LR 3" -> "Legendary 3").
        mr_value = _format_mastery_display(mastery_rank)
        mr_prefix = f"{MASTERY_RANK_EMOJI_RAW} " if MASTERY_RANK_EMOJI_RAW else ""
        inner_lines.append(f"> * -# {mr_prefix}Mastery Rank: `{mr_value}`")
    if missing_categories:
        joined = ", ".join(f"**`{c}`**" for c in missing_categories)
        warn_prefix = f"{WARNING_EMOJI_RAW} " if WARNING_EMOJI_RAW else ""
        inner_lines.append(f"> * -# {warn_prefix}Missing Data: {joined}")
        inner_lines.append("please assign the missing roles here")

    caption, row_buttons = _callsign_buttons(
        nick_suggestion, user_id, current_nick
    )
    children = [{"type": 10, "content": "\n".join(inner_lines)}, *caption]
    if row_buttons:
        children.append({"type": 1, "components": row_buttons[:5]})
    return [{
        "type": 17,
        "accent_color": ACCENT_PASS,
        "components": children,
    }]


def _fail_components(headline: str, reason: str, *, image_url: str | None = None) -> list[dict]:
    header = {
        "type": 10,
        "content": "> Verification Failed",
    }
    children: list[dict] = [
        {"type": 10, "content": f"### {headline}\n-# {reason}"},
    ]
    if image_url:
        children.append(
            {
                "type": 12,  # MediaGallery
                "items": [{"media": {"url": image_url}}],
            }
        )
    return [
        header,
        {
            "type": 17,
            "accent_color": ACCENT_FAIL,
            "components": children,
        },
    ]


def _incomplete_components(
    reason: str,
    *,
    image_url: str | None = None,
    link_buttons: list[tuple[str, str]] | None = None,
    progress_attachment: str | None = None,
) -> list[dict]:
    warn_icon = WARNING_EMOJI_RAW or "\u26a0\ufe0f"
    children: list[dict] = [
        {
            "type": 10,
            # Custom emoji don't render inside markdown headings (Discord
            # falls back to the `:name:` shortcode), so keep the heading
            # plain and lead the reason subtext with the warning icon.
            "content": (
                f"### Please select the missing roles\n"
                f"-# {warn_icon}  {reason}"
            ),
        },
    ]
    if image_url:
        children.append({
            "type": 12,
            "items": [{"media": {"url": image_url}}],
        })
    button_row = _link_button_row(link_buttons)
    if button_row:
        children.append(button_row)
    container = {
        "type": 17,
        "accent_color": ACCENT_INCOMPLETE,
        "components": children,
    }
    top_level: list[dict] = []
    if progress_attachment:
        top_level.append({
            "type": 12,
            "items": [{"media": {"url": f"attachment://{progress_attachment}"}}],
        })
    top_level.append(container)
    return top_level


# ---------- Nickname suggestion --------------------------------------------


def _nickname_suggestion(
    member: discord.Member, profile_name: str | None
) -> str | None:
    """Return a cleaned in-game nickname worth suggesting, or None.

    Skips the "Tenno #N" fallback (which is synthetic, not OCR'd) and
    cases where the member's current display name already matches.
    Discord nicknames cap at 32 characters.
    """
    if not profile_name or profile_name.startswith("Tenno #"):
        return None
    suggestion = _strip_clan_tag(profile_name).strip()[:32]
    if not suggestion:
        return None
    if suggestion.lower() == (member.display_name or "").strip().lower():
        return None
    return suggestion


def _nick_custom_ids(suggestion: str, user_id: int) -> tuple[str, str]:
    """Return (yes_id, no_id) for the nick prompt, URL-encoding the
    suggestion and truncating to fit Discord's 100-char custom_id cap.

    Both IDs use the wider ``nick:y:<uid>:`` prefix length so the two
    branches stay symmetric. Truncation is bounded by ``len(suggestion)``
    iterations because each pass shortens ``truncated`` by one char.
    """
    from urllib.parse import quote

    prefix_len = len(f"nick:y:{user_id}:")
    max_encoded_len = max(0, 100 - prefix_len)
    truncated = suggestion[:max_encoded_len]
    encoded = quote(truncated, safe="")
    while len(encoded) > max_encoded_len and truncated:
        truncated = truncated[:-1]
        encoded = quote(truncated, safe="")
    return (
        f"nick:y:{user_id}:{encoded}",
        f"nick:n:{user_id}:{encoded}",
    )


NICK_PROMPT_INGAME_EMOJI_ID = os.getenv(
    "NICK_PROMPT_INGAME_EMOJI_ID", "1467922510908494098"
).strip()
NICK_PROMPT_SERVER_EMOJI_ID = os.getenv(
    "NICK_PROMPT_SERVER_EMOJI_ID", "1511640752424222760"
).strip()

# Caption shown above the call-sign buttons. Defined once so the pass
# reply (which folds the prompt into its gold container) and
# _strip_nick_prompt (which removes it after a choice is made) agree on
# the exact text.
_CALLSIGN_CAPTION = "-# Operator, pick your call sign!"

# Accent colour of the standalone in-game-name prompt container (the
# incomplete flow appends one). _strip_nick_prompt keys off this to drop
# the whole prompt once the member picks a call sign. Distinct from
# ACCENT_PASS so the pass container (which folds the prompt in) is left
# in place and only has its call-sign bits stripped.
_NICK_PROMPT_ACCENT = 0xD4AF37


def _nick_button(label: str, custom_id: str, emoji_id: str) -> dict:
    """Build a secondary (style 2) call-sign button, attaching the
    configured custom emoji when one is provided."""
    btn: dict = {
        "style": 2,
        "type": 2,
        "label": label,
        "custom_id": custom_id,
    }
    if emoji_id:
        btn["emoji"] = {"id": emoji_id, "name": "unknown", "animated": False}
    return btn


def _callsign_buttons(
    suggestion: str | None, user_id: int | None, current_nick: str,
) -> tuple[list[dict], list[dict]]:
    """Return ``(caption_components, callsign_buttons)`` for the in-game
    name prompt, or ``([], [])`` when there's no suggestion worth
    offering.

    Single source of truth shared by the pass reply (which folds these
    into its gold container) and the standalone incomplete-flow prompt,
    so the caption text and the two ``nick:`` buttons are defined once.
    """
    if not (suggestion and user_id is not None):
        return [], []
    yes_id, no_id = _nick_custom_ids(suggestion, user_id)
    caption = [{"type": 10, "content": _CALLSIGN_CAPTION}]
    buttons = [
        _nick_button(
            (current_nick or "Current nickname")[:80],
            no_id, NICK_PROMPT_SERVER_EMOJI_ID,
        ),
        _nick_button(
            (suggestion or "In-game name")[:80],
            yes_id, NICK_PROMPT_INGAME_EMOJI_ID,
        ),
    ]
    return caption, buttons


def _nickname_prompt_components(
    suggestion: str, user_id: int, *, current_nick: str = "",
) -> list[dict]:
    """Standalone V2 message for the in-game-name selection prompt.

    Layout: one gold-accent container with a "pick your call sign!"
    caption and an action row of the server-nick + in-game-name buttons.

    The two callsign buttons reuse the existing ``nick:y:<uid>:<encoded>``
    / ``nick:n:<uid>:<encoded>`` custom_id scheme so
    ``_handle_nick_interaction`` can edit this whole prompt message in
    place via UPDATE_MESSAGE.
    """
    caption, row_buttons = _callsign_buttons(suggestion, user_id, current_nick)

    # Caption AND buttons live INSIDE one gold container so the prompt
    # reads as a single embedded block (no stray action row floating
    # outside the container).
    return [{
        "type": 17,
        "accent_color": _NICK_PROMPT_ACCENT,
        "components": [
            *caption,
            {"type": 1, "components": row_buttons[:5]},
        ],
    }]


def _nickname_resolved_components(text: str, accent: int) -> list[dict]:
    return [{
        "type": 17,
        "accent_color": accent,
        "components": [{"type": 10, "content": text}],
    }]


# ---------- Role category tracking -----------------------------------------


# Categories that contribute to a member's verification "completion %".
# Each tuple is (display_name, callable -> list[int] of role IDs that
# count for that category). The list is recomputed per call because
# CLAN_SLOTS / PLATFORM_ROLE_IDS can change at runtime via /clan-emblems
# resync.
def _role_categories() -> list[tuple[str, list[int]]]:
    return [
        ("Platform", [rid for rid in PLATFORM_ROLE_IDS.values() if rid]),
        ("Clan", [s.role_id for s in CLAN_SLOTS if s.role_id]),
        ("Mastery Rank", list(MR_ROLE_IDS)),
        ("Syndicate", list(SYNDICATE_ROLE_IDS)),
    ]


def _role_categories_for(role_ids: set[int]) -> list[tuple[str, bool]]:
    """Return (name, has) for each *enabled* category given a member's roles.

    A category is enabled when its role-id list is non-empty. Disabled
    categories don't appear in /progress totals, so an unconfigured server
    won't show 0% forever.
    """
    out: list[tuple[str, bool]] = []
    for name, ids in _role_categories():
        if not ids:
            continue
        out.append((name, any(rid in role_ids for rid in ids)))
    return out


async def _member_profile_info_lines(
    member: discord.Member,
) -> list[tuple]:
    """Gather a member's role-derived verification data for the /profile
    card, one entry per category configured on the server.

    Values come straight from the member's roles (no OCR), so the card
    reflects exactly what they hold. Clan/Platform/Mastery Rank are
    ``(label, value, emoji_bytes[, color])`` rows (an em-dash value when
    not earned); Syndicate is special-cased to ``("Syndicate", [(name,
    accent_rgb|None, emoji_bytes), ...])`` so the card can colour each
    faction and show its icon. Consumed by :func:`_render_profile_card_png`.
    """
    role_ids = {r.id for r in member.roles}
    rows: list[tuple] = []

    # Durable per-member store: the exact picked/OCR'd Mastery Rank lives
    # here (Discord roles only carry coarse buckets), so prefer it for the
    # Mastery Rank row when present.
    stored = await asyncio.to_thread(
        analytics.get_member_profile, member.guild.id, member.id
    )
    mastery_override = (stored or {}).get("mastery_rank")

    # Clan — match the member's clan role to its slot for name + emoji.
    if any(s.role_id for s in CLAN_SLOTS):
        slot = next(
            (s for s in CLAN_SLOTS if s.role_id and s.role_id in role_ids),
            None,
        )
        if slot is not None:
            clan_role = member.guild.get_role(slot.role_id)
            clan_color = (
                clan_role.color.to_rgb()
                if clan_role is not None and clan_role.color.value
                else None
            )
            rows.append((
                "Clan",
                _strip_clan_tag(slot.clan_name or "") or "\u2014",
                await _fetch_emoji_bytes(slot.emoji),
                clan_color,
            ))
        else:
            rows.append(("Clan", "\u2014", None))

    # Platform — first configured platform role the member holds.
    platforms = {p: rid for p, rid in PLATFORM_ROLE_IDS.items() if rid}
    if platforms:
        member_platform = next(
            (p for p, rid in platforms.items() if rid in role_ids), None
        )
        if member_platform is not None:
            rows.append((
                "Platform",
                member_platform,
                await _fetch_emoji_bytes(PLATFORM_EMOJIS.get(member_platform)),
            ))
        else:
            rows.append(("Platform", "\u2014", None))

    # Mastery Rank — prefer the exact stored rank; otherwise fall back to
    # the coarse role-bucket name(s) the member holds.
    if MR_ROLE_IDS or mastery_override:
        if mastery_override:
            mr_value = _format_mastery_display(mastery_override) or "\u2014"
        else:
            mr_ids = set(MR_ROLE_IDS)
            mr_names = [r.name for r in member.roles if r.id in mr_ids]
            mr_value = ", ".join(mr_names) if mr_names else "\u2014"
        rows.append((
            "Mastery Rank",
            mr_value,
            await _fetch_emoji_bytes(MASTERY_RANK_EMOJI_RAW),
        ))

    # Syndicate — members may pledge to several. Emit a per-faction list of
    # (name, accent_rgb, emoji_bytes): the canonical Warframe palette +
    # per-faction SYNDICATE_EMOJI_<KEY>, falling back to the role's own
    # colour + the shared SYNDICATE_EMOJI for an unrecognised name.
    if SYNDICATE_ROLE_IDS:
        syn_ids = set(SYNDICATE_ROLE_IDS)
        factions: list[tuple] = []
        for r in member.roles:
            if r.id not in syn_ids:
                continue
            color, emoji_literal = _syndicate_style(r.name)
            if color is None and r.color.value:
                color = r.color.to_rgb()
            factions.append((
                r.name, color, await _fetch_emoji_bytes(emoji_literal),
            ))
        rows.append(("Syndicate", factions))

    return rows


def _help_link_buttons(guild: "discord.Guild | None") -> list[tuple[str, str]]:
    """Build a Link button row pointing to the help channel.

    Discord channel jump-links are ``/channels/<guild>/<channel>`` URLs.
    Returns an empty list when either the guild or channel is unset so
    callers can safely splat into ``link_buttons=``.
    """
    if not (guild and HELP_CHANNEL_ID):
        return []
    return [(
        "How to get your roles",
        f"https://discord.com/channels/{guild.id}/{HELP_CHANNEL_ID}",
    )]


async def _send_v2(
    reply_to: discord.Message,
    components: list[dict],
    *,
    mention_user: bool = False,
    allow_role_mentions: bool = False,
    file_bytes: bytes | None = None,
    file_name: str = "attachment.png",
    file_content_type: str = "image/png",
) -> None:
    """Send a Components V2 message as a reply via raw HTTP (discord.py 2.x has no native v2).

    When ``file_bytes`` is provided, the message is posted as multipart so a
    top-level media-gallery entry referencing ``attachment://<file_name>``
    resolves to the attached file in the same Discord message.
    """
    from discord.http import Route

    parse: list[str] = []
    if mention_user:
        parse.append("users")
    if allow_role_mentions:
        parse.append("roles")

    payload: dict = {
        "flags": COMPONENTS_V2_FLAG,
        "components": components,
        "allowed_mentions": {
            "parse": parse,
            "replied_user": mention_user,
        },
        "message_reference": {
            "message_id": reply_to.id,
            "channel_id": reply_to.channel.id,
            "fail_if_not_exists": False,
        },
    }
    if reply_to.guild is not None:
        payload["message_reference"]["guild_id"] = reply_to.guild.id
    if file_bytes is not None:
        payload["attachments"] = [{"id": 0, "filename": file_name}]

    route = Route(
        "POST",
        "/channels/{channel_id}/messages",
        channel_id=reply_to.channel.id,
    )
    sent_id: int | None = None
    try:
        if file_bytes is not None:
            # discord.py's HTTPClient.request can't cleanly post arbitrary
            # multipart bodies for V2, so post directly with aiohttp using
            # the bot token. Per-channel rate limits apply but a single
            # reply is well within the bucket.
            form = aiohttp.FormData()
            form.add_field(
                "payload_json", json.dumps(payload),
                content_type="application/json",
            )
            form.add_field(
                "files[0]", file_bytes,
                filename=file_name, content_type=file_content_type,
            )
            url = (
                f"https://discord.com/api/v10/channels/"
                f"{reply_to.channel.id}/messages"
            )
            headers = {
                "Authorization": f"Bot {DISCORD_TOKEN}",
                "User-Agent": "GoldenPagoda (https://github.com/aidenlong04, 1.0)",
            }
            timeout = aiohttp.ClientTimeout(total=15)
            session = await _get_http_session()
            async with session.post(
                url, data=form, headers=headers, timeout=timeout
            ) as resp:
                if resp.status >= 400:
                    text = await resp.text()
                    raise discord.HTTPException(resp, text)  # type: ignore[arg-type]
                data = await resp.json()
        else:
            data = await client.http.request(route, json=payload)
        if isinstance(data, dict):
            raw_id = data.get("id")
            if isinstance(raw_id, (str, int)):
                try:
                    sent_id = int(raw_id)
                except (TypeError, ValueError):
                    sent_id = None
    except discord.HTTPException:
        logger.exception("v2 component reply failed; falling back to plain text")
        text = next(
            (
                block.get("content", "")
                for c in components
                for block in c.get("components", [])
                if block.get("type") == 10
            ),
            "Verification update",
        )
        try:
            sent = await reply_to.reply(text)
            sent_id = sent.id
        except discord.HTTPException:
            logger.exception("plain-text fallback also failed")

    if sent_id:
        _remember_reply(reply_to.id, reply_to.channel.id, sent_id)
        if REPLY_TTL_SECONDS > 0:
            _spawn_bg_task(
                _delete_after(reply_to.channel.id, sent_id, REPLY_TTL_SECONDS)
            )


async def _edit_message_v2_with_file(
    *,
    channel_id: int,
    message_id: int,
    components: list[dict],
    file_bytes: bytes,
    file_name: str = "progress.png",
    file_content_type: str = "image/png",
) -> None:
    """PATCH an existing V2 message and replace its single attachment.

    UPDATE_MESSAGE (interaction callback type 7) is JSON-only and can't
    swap attachments, so refreshing the progress card requires a direct
    multipart PATCH to /channels/{cid}/messages/{mid}. The new attachment
    keeps the same filename so any ``attachment://progress.png``
    references inside components resolve to the fresh file.
    """
    payload = {
        "components": components,
        "attachments": [{"id": 0, "filename": file_name}],
        "allowed_mentions": {"parse": []},
    }
    form = aiohttp.FormData()
    form.add_field(
        "payload_json", json.dumps(payload),
        content_type="application/json",
    )
    form.add_field(
        "files[0]", file_bytes,
        filename=file_name, content_type=file_content_type,
    )
    url = (
        f"https://discord.com/api/v10/channels/"
        f"{channel_id}/messages/{message_id}"
    )
    headers = {
        "Authorization": f"Bot {DISCORD_TOKEN}",
        "User-Agent": "GoldenPagoda (https://github.com/aidenlong04, 1.0)",
    }
    timeout = aiohttp.ClientTimeout(total=15)
    session = await _get_http_session()
    async with session.patch(
        url, data=form, headers=headers, timeout=timeout
    ) as resp:
        if resp.status >= 400:
            text = await resp.text()
            raise RuntimeError(
                f"PATCH message {message_id} failed: {resp.status} {text}"
            )


async def _delete_message(channel_id: int, message_id: int) -> None:
    """Issue a single DELETE /channels/{cid}/messages/{mid}. NotFound is
    swallowed (already gone); other HTTP errors are logged."""
    from discord.http import Route

    try:
        await client.http.request(Route(
            "DELETE",
            "/channels/{channel_id}/messages/{message_id}",
            channel_id=channel_id,
            message_id=message_id,
        ))
    except discord.NotFound:
        pass
    except discord.HTTPException:
        logger.exception("Failed to delete message %s", message_id)


async def _delete_after(channel_id: int, message_id: int, delay: float) -> None:
    try:
        await asyncio.sleep(delay)
    except asyncio.CancelledError:
        return
    await _delete_message(channel_id, message_id)


@client.event
async def on_raw_message_delete(payload: discord.RawMessageDeleteEvent) -> None:
    """When the original screenshot post is deleted, also delete our reply.

    Uses the raw event so this still fires when the source message isn't
    cached (e.g. older than the bot's start). Scoped to TARGET_CHANNEL_ID
    so we never touch messages in unrelated channels.
    """
    if payload.channel_id != TARGET_CHANNEL_ID:
        return
    entry = _REPLY_MAP.pop(payload.message_id, None)
    if entry is None:
        return
    channel_id, reply_id = entry
    _spawn_bg_task(_delete_message(channel_id, reply_id))


@client.event
async def on_message(message: discord.Message) -> None:
    if (
        message.author.bot
        or (client.user is not None and message.author.id == client.user.id)
        or message.webhook_id is not None
        or message.type not in (discord.MessageType.default, discord.MessageType.reply)
    ):
        return
    if message.channel.id != TARGET_CHANNEL_ID:
        return

    await _process_screenshot(message)


# ---------- Slash commands --------------------------------------------------

# Stricter than the module-level `_CUSTOM_EMOJI_RE` used for reaction parsing:
# this one is the validator for user-supplied `/clan-emblems` input, so it
# rejects fancy unicode in the name and bounds the snowflake length.
_CUSTOM_EMOJI_INPUT_RE = re.compile(r"^<a?:[A-Za-z0-9_]{2,}:\d{15,25}>$")


def _normalize_emoji_input(raw: str) -> str | None:
    """Validate and normalize an emoji input. Accepts:
      - Discord custom emoji: <:name:id> or <a:name:id>
      - A bare numeric ID (assumed custom; caller must look up name)
      - A unicode emoji string
      - Empty string → returns "" to clear
    Returns the canonical string to store, or None if invalid.
    """
    s = (raw or "").strip()
    if not s:
        return ""
    if _CUSTOM_EMOJI_INPUT_RE.match(s):
        return s
    # Bare ID is ambiguous (no name), reject — user should paste full <:name:id>.
    if s.isdigit():
        return None
    # Treat anything else short as a unicode emoji.
    if len(s) <= 8:
        return s
    return None


@tree.command(
    name="clan-emblems",
    description="Set the emoji shown next to a clan in verification messages.",
)
@app_commands.describe(
    role="The clan role to set the emoji for.",
    emoji="A custom emoji (<:name:id>) or unicode emoji. Leave blank to clear.",
    resync_clans="Re-resolve clan + platform role names/IDs from the server (ignores role/emoji).",
)
@app_commands.default_permissions(manage_guild=True)
async def clan_emblems(
    interaction: discord.Interaction,
    role: discord.Role | None = None,
    emoji: str = "",
    resync_clans: bool = False,
) -> None:
    if resync_clans:
        clan_changes = _sync_clan_slots_from_guilds()
        platform_changes = _sync_platform_roles_from_guilds()
        logger.info(
            "clan-emblems resync: clan_changes=%d platform_changes=%d",
            len(clan_changes), len(platform_changes),
        )
        if not clan_changes and not platform_changes:
            await interaction.response.send_message(
                "✅ Resync complete — no changes (everything was already up to date).",
                ephemeral=True,
            )
            return
        lines = ["✅ **Resync complete**"]
        if clan_changes:
            lines.append("")
            lines.append("**Clans**")
            for c in clan_changes:
                lines.append(f"- `{c}`")
        if platform_changes:
            lines.append("")
            lines.append("**Platforms**")
            for c in platform_changes:
                lines.append(f"- `{c}`")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)
        return

    if role is None:
        await interaction.response.send_message(
            "❌ Provide a `role` to set its emoji, or pass `resync_clans:true` to re-sync names/IDs.",
            ephemeral=True,
        )
        return

    slot = next((s for s in CLAN_SLOTS if s.role_id == role.id), None)
    if slot is None:
        await interaction.response.send_message(
            f"\u274C **{role.name}** is not a configured clan role. "
            f"Configured slots: "
            + ", ".join(
                f"{s.slot}={s.clan_name}" for s in CLAN_SLOTS if s.clan_name
            ),
            ephemeral=True,
        )
        return

    normalized = _normalize_emoji_input(emoji)
    if normalized is None:
        await interaction.response.send_message(
            "\u274C Invalid emoji. Use a custom emoji like `<:name:1234567890>` "
            "or a unicode emoji.",
            ephemeral=True,
        )
        return

    slot.emoji = normalized or None
    env_key = f"CLAN_ROLE_{slot.slot}_EMOJI"
    os.environ[env_key] = normalized

    persisted = False
    try:
        persisted = _update_env_clan_slots(CLAN_SLOTS)
    except Exception:
        logger.exception("Failed to persist %s to %s", env_key, ENV_FILE_PATH)

    logger.info(
        "clan-emblems: slot=%s role=%s emoji=%r persisted=%s env=%s",
        slot.slot, role.name, normalized, persisted, ENV_FILE_PATH,
    )
    display = normalized if normalized else "*(cleared)*"
    suffix = "" if persisted else " (in-memory only — `.env` not writable)"
    await interaction.response.send_message(
        f"\u2705 Slot **{slot.slot}** ({slot.clan_name}) emoji \u2192 {display}{suffix}",
        ephemeral=True,
    )


PREVIEW_CHANNEL_ID = 1378199771428163765


@tree.command(
    name="preview-responses",
    description="(temp) Post pass/fail/incomplete sample responses to the test channel.",
)
@app_commands.default_permissions(manage_guild=True)
async def preview_responses(interaction: discord.Interaction) -> None:
    from discord.http import Route

    sample_clan = (
        CLAN_SLOTS[0].clan_name
        if CLAN_SLOTS and CLAN_SLOTS[0].clan_name
        else "Golden Tenno"
    )
    sample_emoji = (CLAN_SLOTS[0].emoji if CLAN_SLOTS else None) or CLAN_EMOJI
    sample_uid = interaction.user.id

    samples: list[tuple[str, list[dict]]] = [
        (
            "PASS — mastery + missing categories",
            _pass_components(
                "GoldenTenno#200",
                sample_clan,
                clan_emoji=sample_emoji,
                mastery_rank="MR 28",
                missing_categories=["Platform", "Syndicate"],
            ),
        ),
        (
            "PASS — mastery only, fully verified",
            _pass_components(
                "MonguPrime002#661",
                sample_clan,
                clan_emoji=sample_emoji,
                mastery_rank="MR 30",
            ),
        ),
        (
            "FAIL — Not an image",
            _fail_components(
                "Not an image",
                "Upload a PNG/JPG screenshot of your Warframe profile.",
            ),
        ),
        (
            "FAIL — Invalid image",
            _fail_components(
                "Invalid image",
                "Image could not be opened. Re-upload a valid PNG/JPG.",
            ),
        ),
        (
            "FAIL — Not readable",
            _fail_components(
                "Not readable",
                "No text could be read. Upload a clearer screenshot.",
            ),
        ),
        (
            "FAIL — Profile not found",
            _fail_components(
                "Profile not found",
                "Make sure your title bar (PlayerName#NNN) and platform "
                "icon are visible at the top.",
            ),
        ),
        (
            "INCOMPLETE — unknown clan",
            _incomplete_components(
                f"No role for clan **{sample_clan}**.",
            ),
        ),
        (
            "NICKNAME PROMPT (standalone)",
            _nickname_prompt_components(
                "GoldenTenno",
                sample_uid,
                current_nick=(
                    interaction.user.display_name
                    if isinstance(interaction.user, discord.Member)
                    else "OldNick"
                ),
            ),
        ),
    ]

    route = Route(
        "POST",
        "/channels/{channel_id}/messages",
        channel_id=PREVIEW_CHANNEL_ID,
    )
    # Defer first so we don't hit the 3s interaction timeout while
    # posting 8 messages sequentially. Sequential keeps the channel
    # ordering deterministic (pass → fail → incomplete → nick prompt).
    await interaction.response.defer(ephemeral=True, thinking=True)
    sent = 0
    errors: list[str] = []
    for label, components in samples:
        payload = {
            "flags": COMPONENTS_V2_FLAG,
            "components": components,
            "allowed_mentions": {"parse": []},
        }
        try:
            await client.http.request(route, json=payload)
            sent += 1
        except Exception as exc:
            errors.append(f"{label}: {exc}")
            logger.exception("preview-responses: failed sending %s", label)
        await asyncio.sleep(0.3)

    msg = (
        f"\u2705 Posted {sent}/{len(samples)} samples to "
        f"<#{PREVIEW_CHANNEL_ID}>."
    )
    if errors:
        msg += "\n" + "\n".join(f"\u274C {e}" for e in errors)
    await interaction.followup.send(msg, ephemeral=True)


# ---------- /status (paginated, ephemeral, V2) ------------------------------


EPHEMERAL_FLAG = 1 << 6  # 64


def _status_page_bot(_interaction: discord.Interaction, _snap: dict) -> str:
    user = client.user
    latency_ms = int(client.latency * 1000) if client.latency >= 0 else -1

    # Uptime
    seconds = int(time.time() - BOT_START_TIME)
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if d:
        parts.append(f"{d}d")
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    parts.append(f"{s}s")
    uptime = " ".join(parts)

    # Health signal age
    try:
        hb: int | None = int(time.time() - os.path.getmtime(HEALTH_PATH))
    except OSError:
        hb = None
    if hb is None:
        hb_line = "\u26a0\ufe0f unhealthy (no signal)"
    elif hb > 90:
        hb_line = f"\u274C unhealthy ({hb}s stale)"
    else:
        hb_line = f"\u2705 healthy ({hb}s ago)"

    guilds = len(client.guilds)
    members = sum(g.member_count or 0 for g in client.guilds)
    py = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"

    # Resident set size from /proc/self/status (no psutil dep).
    rss_label = "`?`"
    try:
        with open("/proc/self/status", "r") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    rss_label = f"`{int(line.split()[1]) // 1024} MiB`"
                    break
    except OSError:
        pass

    return (
        f"**Bot**\n"
        f"-# User: `{user}` (`{getattr(user, 'id', '?')}`)\n"
        f"-# Health: `{hb_line}`\n"
        f"-# Uptime: `{uptime}` \u2022 Latency: `{latency_ms} ms`\n"
        f"-# Guilds: `{guilds}` \u2022 Members: `{members}`\n"
        f"-# Python: `{py}` \u2022 RSS: {rss_label}"
    )


def _status_page_roles_split() -> tuple[str, str]:
    """Return (clan_slots_section, platform_roles_section).

    Used by `_status_components` to wedge the "Emblems" button
    directly under the Clan slots block, between the two text blobs.
    """
    clan_lines = ["**Clan slots**"]

    def _sort_key(slot):
        name = (slot.clan_name or "").strip()
        is_placeholder = 1 if _PLACEHOLDER_NAME_RE.match(name) else 0
        has_emoji = 0 if (slot.emoji or "").strip() else 1
        # Order: named+emoji, named+no-emoji, placeholders (then slot index).
        return (is_placeholder, has_emoji, slot.slot)

    for s in sorted(CLAN_SLOTS, key=_sort_key):
        name = s.clan_name or "*(unset)*"
        rid = s.role_id or 0
        emoji = s.emoji or ""
        mention = f"<@&{rid}>" if rid else "*(no role)*"
        clan_lines.append(f"-# {emoji} `{s.slot}` {name} \u2192 {mention}")

    rest_lines = ["**Platform roles**"]
    for plat in PLATFORM_ROLE_ID_ENV_KEYS:
        rid = PLATFORM_ROLE_IDS.get(plat) or 0
        mention = f"<@&{rid}>" if rid else "*(unset)*"
        rest_lines.append(f"-# {plat} \u2192 {mention}")

    return "\n".join(clan_lines), "\n".join(rest_lines)


def _status_page_channels(
    _interaction: discord.Interaction, _snap: dict
) -> str:
    def fmt(cid: int) -> str:
        return f"<#{cid}> `{cid}`" if cid else "*(unset)*"

    def fmt_reaction(rid: int | None) -> str:
        if not rid:
            return "*(unset)*"
        # Pull the rendered form from the bot's cache so animated
        # custom emojis get <a:name:id> (and the correct display
        # name) automatically. Falls back to a bare id reference for
        # the rare window before the cache populates.
        live = client.get_emoji(rid) if client else None
        if live is not None:
            return str(live)
        return f"`{rid}`"

    last_seen = _load_catchup_state()
    last = f"`{last_seen}`" if last_seen else "*(none)*"

    return (
        f"**Channels**\n"
        f"-# Target: {fmt(TARGET_CHANNEL_ID)}\n"
        f"-# Preview: {fmt(PREVIEW_CHANNEL_ID)}\n"
        f"\n**Reactions**\n"
        f"-# Pass: {fmt_reaction(PASS_REACTION_ID)}\n"
        f"-# Pending: {fmt_reaction(PENDING_REACTION_ID)}\n"
        f"-# Fail: {FAIL_REACTION or '*(unset)*'}\n"
        f"\n**Messaging**\n"
        f"-# Reply TTL: `{REPLY_TTL_SECONDS}s`\n"
        f"-# Catch-up: `{CATCHUP_LOOKBACK_HOURS}h` lookback \u2022 last id: {last}"
    )


def _status_page_ocr(_interaction: discord.Interaction, snap: dict) -> str:
    backend = "OCR.space (engine 3)" if OCR_API_KEY else (
        "Tesseract (local)" if pytesseract else "*(none configured)*"
    )
    lines = [
        "**OCR**",
        f"-# Backend: `{backend}`",
        "",
        "**Latency** (last 500 events)",
    ]
    ocr = snap.get("ocr") or {}
    if ocr.get("samples"):
        lines.append(f"-# Samples: `{ocr['samples']}`")
        lines.append(f"-# Avg: `{ocr['avg_ms']} ms`")
        lines.append(f"-# p50: `{ocr['p50_ms']} ms` \u2022 p95: `{ocr['p95_ms']} ms`")
    else:
        lines.append("-# No samples yet.")
    return "\n".join(lines)


def _status_page_stats(_interaction: discord.Interaction, snap: dict) -> str:
    if not snap.get("available"):
        return (
            "**Analytics**\n"
            "-# Storage unavailable.\n"
            f"-# DB path: `{snap.get('db_path')}`\n"
            "-# Mount `/opt/golden-pagoda/data:/app/data` to enable."
        )
    total = snap["total"]
    by = snap["by_outcome"]
    p = by.get("pass", 0)
    f = by.get("fail", 0)
    inc = by.get("incomplete", 0)
    unr = by.get("unreadable", 0)
    err = by.get("ocr_error", 0)
    pct = lambda n: f"{(n / total * 100):.1f}%" if total else "-"  # noqa: E731

    win = snap["windows"]
    return (
        f"**Verifications**\n"
        f"-# Total: `{total}`\n"
        f"-# Pass: `{p}` ({pct(p)})\n"
        f"-# Incomplete: `{inc}` ({pct(inc)})\n"
        f"-# Fail: `{f}` ({pct(f)})\n"
        f"-# Unreadable: `{unr}` ({pct(unr)})\n"
        f"-# OCR error: `{err}` ({pct(err)})\n"
        f"\n**Windows**\n"
        f"-# Last 24h: `{win.get('24h', 0)}`\n"
        f"-# Last 7d: `{win.get('7d', 0)}`\n"
        f"-# Last 30d: `{win.get('30d', 0)}`"
    )


def _status_page_clans(interaction: discord.Interaction, _snap: dict) -> str:
    guild = interaction.guild if interaction else None
    if guild is None:
        return "**Clans**\n-# No guild context."
    configured = [s for s in CLAN_SLOTS if s.clan_name]
    if not configured:
        return "**Clans**\n-# No clan slots configured."

    rows: list[tuple[str, str, int, bool]] = []
    for slot in configured:
        role = guild.get_role(slot.role_id) if slot.role_id else None
        members = len(role.members) if role else 0
        glyph = slot.emoji or "\u2022"
        rows.append((slot.clan_name, glyph, members, role is None))

    rows.sort(key=lambda r: (-r[2], r[0].lower()))

    lines = [f"**Clans** ({len(rows)} configured)"]
    for name, glyph, members, missing in rows:
        suffix = " \u26a0\ufe0f missing role" if missing else ""
        lines.append(f"-# {glyph} `{name}` \u2014 `{members}` members{suffix}")
    return "\n".join(lines)


# Each entry: (key, title, builder). Builders take (interaction, snap) and
# return a Markdown body. The "roles" page is special-cased in
# `_status_components` to wedge the Emblems button between sections, so its
# builder slot is None.
_StatusBuilder = Callable[[discord.Interaction, dict], str]
_STATUS_PAGES: list[tuple[str, str, _StatusBuilder | None]] = [
    ("bot",      "Bot",       _status_page_bot),
    ("roles",    "Roles",     None),
    ("channels", "Channels",  _status_page_channels),
    ("ocr",      "OCR",       _status_page_ocr),
    ("stats",    "Stats",     _status_page_stats),
    ("clans",    "Clans",     _status_page_clans),
]

# Pages that consume `analytics.summary()`. Computing the snapshot fires
# ~7 SQL queries, so we only do it when the active page needs it.
_PAGES_NEEDING_SNAPSHOT = frozenset({"ocr", "stats"})


def _status_nav_row(page: int) -> dict:
    last = len(_STATUS_PAGES) - 1
    return {
        "type": 1,
        "components": [
            {"type": 2, "style": 2, "label": "\u25C0 Prev",
             "custom_id": f"status:{page - 1}", "disabled": page == 0},
            {"type": 2, "style": 2,
             "label": f"{page + 1}/{len(_STATUS_PAGES)}",
             "custom_id": "status:noop", "disabled": True},
            {"type": 2, "style": 2, "label": "Next \u25B6",
             "custom_id": f"status:{page + 1}", "disabled": page >= last},
            {"type": 2, "style": 1,
             "emoji": {"name": "\U0001F504"},
             "custom_id": f"status:{page}"},
        ],
    }


def _status_components(interaction: discord.Interaction, page: int) -> list[dict]:
    page = max(0, min(page, len(_STATUS_PAGES) - 1))
    key, title, builder = _STATUS_PAGES[page]
    nav_row = _status_nav_row(page)

    if key == "roles":
        # Wedge the Emblems button between Clan slots and Platform roles
        # so it lives directly under the clan listing.
        clan_text, rest_text = _status_page_roles_split()
        container_components = [
            {"type": 10, "content": clan_text},
            {"type": 1, "components": [
                {"type": 2, "style": 3, "label": "Emblems",
                 "custom_id": "status:assign_emblems"},
            ]},
            {"type": 10, "content": rest_text},
            nav_row,
        ]
    else:
        snap = analytics.summary() if key in _PAGES_NEEDING_SNAPSHOT else {}
        assert builder is not None  # only the "roles" slot is None
        container_components = [
            {"type": 10, "content": builder(interaction, snap)},
            nav_row,
        ]

    return [
        {"type": 10, "content": f"### \U0001F4CA  Status \u2014 {title}"},
        {
            "type": 17,
            "accent_color": ACCENT_PASS,
            "components": container_components,
        },
    ]


async def _interaction_callback(
    interaction: discord.Interaction,
    callback_type: int,
    components: list[dict],
    *,
    ephemeral: bool = True,
) -> None:
    from discord.http import Route

    flags = COMPONENTS_V2_FLAG
    if ephemeral:
        flags |= EPHEMERAL_FLAG
    route = Route(
        "POST",
        "/interactions/{interaction_id}/{interaction_token}/callback",
        interaction_id=interaction.id,
        interaction_token=interaction.token,
    )
    await client.http.request(
        route,
        json={
            "type": callback_type,
            "data": {
                "flags": flags,
                "components": components,
                "allowed_mentions": {"parse": []},
            },
        },
    )
    with contextlib.suppress(AttributeError):
        interaction.response._responded = True  # type: ignore[attr-defined]


async def _send_status_page(interaction: discord.Interaction, page: int) -> None:
    try:
        components = _status_components(interaction, page)
        await _interaction_callback(interaction, 4, components)
    except Exception:
        logger.exception("status page %s failed", page)
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "\u274C Failed to render status.", ephemeral=True
            )


# /status — single ephemeral command. The Components V2 message paginates
# through every page (bot, roles, channels, ocr, stats, clans) via the
# Prev/Next buttons, so subcommands are unnecessary.
@tree.command(
    name="status",
    description="Bot status & verification analytics (paginated, ephemeral).",
)
@app_commands.default_permissions(manage_guild=True)
async def status_cmd(interaction: discord.Interaction) -> None:
    await _send_status_page(interaction, 0)


# ---------- /progress (submission tracker, V2 with attached PNG) ------------

# Visual styling for the rendered progress card. The card composites the
# member's avatar (circular, left) with a rounded gradient progress bar
# and inline text overlay so a single PNG carries the whole message.
_PROGRESS_CARD_W = 860
_PROGRESS_RADIUS = 24                   # rounded panel corners
# Warframe-inspired slate panel: a faint vertical gradient from a lighter
# top to a darker base gives the card depth instead of a flat fill.
_PROGRESS_BG_TOP = (38, 41, 47)        # lighter slate (top edge)
_PROGRESS_BG = (30, 31, 34)            # #1E1F22 base panel
_PROGRESS_BG_BOTTOM = (21, 22, 25)     # darker slate (bottom edge)
_PROGRESS_BG_EDGE = (18, 19, 22)       # dividers / inner shadow
_PROGRESS_BORDER = (58, 62, 70)        # crisp outer hairline
_PROGRESS_TRACK = (43, 45, 49)         # #2B2D31 bar track
_PROGRESS_TRACK_EDGE = (24, 25, 28)    # track rim for definition
_PROGRESS_FILL_START = (93, 208, 243)  # Warframe energy cyan
_PROGRESS_FILL_END = (134, 230, 168)   # mint — gradient mid-stop
_PROGRESS_FILL_GOLD = (208, 162, 80)   # Orokin gold for finished bars
_PROGRESS_FILL_GOLD_END = (240, 214, 140)  # warm gold highlight end
_PROGRESS_TEXT = (236, 238, 240)
_PROGRESS_MUTED = (163, 166, 170)
_PROGRESS_ACCENT = (212, 168, 87)      # gold accent (footer / pct)
_PROGRESS_MISSING = (236, 170, 92)     # amber — 'missing data' callout
_PROGRESS_AVATAR_SIZE = 112
_PROGRESS_AVATAR_RING = (212, 168, 87)
# Supersample factor: the card is laid out in logical units then rendered
# at this multiple so text, icons, and the bar stay crisp on Discord's
# HiDPI clients (the previous 1x output looked soft when scaled).
_PROGRESS_SS = 2


def _load_font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont:
    """Load DejaVu Sans at ``size``; fall back to PIL default on any error.

    Results are memoized — fonts are immutable for a given (size, bold)
    pair and the truetype open is the single hottest call inside
    ``_render_progress_card_png`` (invoked 4-5 times per render).
    """
    return _load_font_cached(size, bold)


@functools.lru_cache(maxsize=32)
def _load_font_cached(size: int, bold: bool) -> ImageFont.FreeTypeFont:
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    candidates = [
        f"/usr/share/fonts/truetype/dejavu/{name}",
        f"/usr/share/fonts/dejavu/{name}",
        name,
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size=size)
        except OSError:
            continue
    return ImageFont.load_default()  # type: ignore[return-value]


def _vertical_gradient(
    width: int, height: int,
    top: tuple[int, int, int], bottom: tuple[int, int, int],
) -> Image.Image:
    """Return an RGBA image filled with a top→bottom colour gradient."""
    t = np.linspace(0.0, 1.0, height, dtype=np.float32)[:, None]  # (h, 1)
    top_a = np.array(top, dtype=np.float32)[None, :]              # (1, 3)
    bot_a = np.array(bottom, dtype=np.float32)[None, :]           # (1, 3)
    col = top_a + (bot_a - top_a) * t                            # (h, 3)
    arr = np.empty((height, width, 4), dtype=np.uint8)
    arr[..., :3] = col[:, None, :].astype(np.uint8)
    arr[..., 3] = 255
    return Image.fromarray(arr, mode="RGBA")


def _rounded_mask(width: int, height: int, radius: int) -> Image.Image:
    """Antialiased rounded-rectangle alpha mask (rendered at 2x).

    The card is already drawn at ``_PROGRESS_SS`` so a 2x mask is plenty
    of edge antialiasing without the memory cost of a 4x buffer on the
    full panel.
    """
    scale = 2
    mask = Image.new("L", (width * scale, height * scale), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, width * scale - 1, height * scale - 1),
        radius=radius * scale, fill=255,
    )
    return mask.resize((width, height), Image.LANCZOS)


def _circular_avatar(
    avatar_bytes: bytes | None, size: int
) -> Image.Image:
    """Return an RGBA avatar cropped into a circle with a thin gold ring.

    If ``avatar_bytes`` fails to decode (or is None) a flat gold disc is
    used as a graceful placeholder.
    """
    diameter = size
    if avatar_bytes:
        try:
            src = Image.open(io.BytesIO(avatar_bytes))
            src.load()  # force full decode now to surface truncation
            src = src.convert("RGBA")
        except Exception:
            logger.warning(
                "progress: avatar decode failed (%d bytes)",
                len(avatar_bytes), exc_info=True,
            )
            src = Image.new(
                "RGBA", (diameter, diameter), _PROGRESS_AVATAR_RING + (255,)
            )
    else:
        src = Image.new(
            "RGBA", (diameter, diameter), _PROGRESS_AVATAR_RING + (255,)
        )

    # src from Image.open needs to be closed after processing.
    needs_close = avatar_bytes is not None
    try:
        src = ImageOps.fit(src, (diameter, diameter), Image.LANCZOS)

        # Antialiased circular mask: render at 4x and downsample.
        scale = 4
        mask = Image.new("L", (diameter * scale, diameter * scale), 0)
        ImageDraw.Draw(mask).ellipse(
            (0, 0, diameter * scale - 1, diameter * scale - 1), fill=255
        )
        mask = mask.resize((diameter, diameter), Image.LANCZOS)

        avatar = Image.new("RGBA", (diameter, diameter), (0, 0, 0, 0))
        avatar.paste(src, (0, 0), mask)

        ring = Image.new("RGBA", (diameter, diameter), (0, 0, 0, 0))
        ImageDraw.Draw(ring).ellipse(
            (0, 0, diameter - 1, diameter - 1),
            outline=_PROGRESS_AVATAR_RING + (255,),
            width=max(2, diameter // 28),
        )
        avatar.alpha_composite(ring)
        return avatar
    finally:
        if needs_close:
            src.close()


def _segmented_bar(
    width: int, height: int, count: int, target: int, *,
    complete: bool, scale: int = 1,
) -> Image.Image:
    """Render a segmented progress bar: one rounded segment per
    verification category.

    The first ``count`` segments carry the glassy energy gradient —
    Warframe energy cyan flowing through mint into Orokin gold, the gold
    growing more pronounced toward the filled edge (fully gold when
    complete) — cropped from a single full-width fill so the light flows
    continuously across the bar; the remaining segments are
    recessed track with a faint amber "pending" tint so unmet categories
    read as outstanding. Small gaps separate the segments, and the last
    filled segment gets a soft leading-edge glow while in progress.
    ``scale`` keeps strokes proportional under supersampling.
    """
    bar = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    n = max(1, target)
    count = max(0, min(count, n))
    radius = height // 2
    line_w = max(1, int(round(1.5 * scale)))
    gap = max(2, int(round(4 * scale)))

    # Horizontal gradient stops. In progress the bar flows from the
    # Warframe energy cyan through mint and into Orokin gold, with the
    # gold weighted toward the filled (right) edge so it grows more
    # pronounced the further a member progresses — easing into the
    # all-gold complete state. Filled segments crop their slice from this
    # single full-width fill so the light flows continuously rather than
    # restarting per segment.
    if complete:
        stops = ((0.0, _PROGRESS_FILL_GOLD), (1.0, _PROGRESS_FILL_GOLD_END))
    else:
        stops = (
            (0.0, _PROGRESS_FILL_START),
            (0.40, _PROGRESS_FILL_END),
            (1.0, _PROGRESS_FILL_GOLD),
        )
    end = stops[-1][1]

    t = np.linspace(0.0, 1.0, max(1, width), dtype=np.float32)
    xp = np.array([p for p, _ in stops], dtype=np.float32)
    grad_row = np.stack(
        [
            np.interp(
                t, xp,
                np.array([c[ch] for _, c in stops], dtype=np.float32),
            )
            for ch in range(3)
        ],
        axis=1,
    ).astype(np.float32)
    grad_base = np.repeat(grad_row[None, :, :], height, axis=0)
    shade = np.linspace(1.16, 0.80, height, dtype=np.float32)[:, None, None]
    fill_arr = np.empty((height, width, 4), dtype=np.uint8)
    fill_arr[..., :3] = np.clip(grad_base * shade, 0, 255).astype(np.uint8)
    fill_arr[..., 3] = 255
    fill_img = Image.fromarray(fill_arr, mode="RGBA")
    gloss_a = np.linspace(118.0, 0.0, height, dtype=np.float32)
    gloss_a[int(height * 0.55):] = 0.0
    gloss = np.zeros((height, width, 4), dtype=np.uint8)
    gloss[..., :3] = 255
    gloss[..., 3] = np.clip(gloss_a[:, None], 0, 255).astype(np.uint8)
    fill_img.alpha_composite(Image.fromarray(gloss, mode="RGBA"))
    trace = tuple(min(255, int(c * 1.18)) for c in end[:3])

    # Empty 'pending' segments: track nudged toward amber (neutral when
    # complete) so outstanding categories catch the eye.
    if complete:
        empty_fill = _PROGRESS_TRACK
    else:
        empty_fill = tuple(
            int(round(tc * 0.82 + mc * 0.18))
            for tc, mc in zip(_PROGRESS_TRACK, _PROGRESS_MISSING, strict=True)
        )

    seg_w = (width - gap * (n - 1)) / n
    last_filled = count - 1
    for i in range(n):
        x0 = int(round(i * (seg_w + gap)))
        x1 = width if i == n - 1 else int(round(x0 + seg_w))
        sw = x1 - x0
        if sw <= 0:
            continue
        seg_mask = _rounded_mask(sw, height, radius)
        if i < count:
            bar.paste(
                fill_img.crop((x0, 0, x0 + sw, height)), (x0, 0), seg_mask
            )
            # Traced brighter outline around the filled segment.
            ImageDraw.Draw(bar).rounded_rectangle(
                (x0, 0, x1 - 1, height - 1), radius=radius,
                outline=trace + (220,), width=line_w,
            )
            # Leading-edge glow on the last filled segment while in progress.
            if not complete and i == last_filled and count < n:
                glow = Image.new("RGBA", (sw, height), (0, 0, 0, 0))
                gx = sw - max(2, int(round(3 * scale)))
                ImageDraw.Draw(glow).line(
                    (gx, line_w, gx, height - line_w - 1),
                    fill=(255, 255, 255, 150),
                    width=max(1, int(round(2 * scale))),
                )
                glow = glow.filter(
                    ImageFilter.GaussianBlur(max(1, int(round(1.4 * scale))))
                )
                ga = np.asarray(glow.getchannel("A"), dtype=np.float32) * (
                    np.asarray(seg_mask, dtype=np.float32) / 255.0
                )
                glow.putalpha(Image.fromarray(ga.astype(np.uint8), mode="L"))
                bar.alpha_composite(glow, (x0, 0))
        else:
            seg_layer = Image.new("RGBA", (sw, height), (0, 0, 0, 0))
            sd = ImageDraw.Draw(seg_layer)
            sd.rounded_rectangle(
                (0, 0, sw - 1, height - 1), radius=radius, fill=empty_fill
            )
            # Inset top shadow so the empty segment reads as a recessed groove.
            tm = np.asarray(seg_mask, dtype=np.float32) / 255.0
            inset_a = np.linspace(70.0, 0.0, height, dtype=np.float32)
            inset = np.zeros((height, sw, 4), dtype=np.uint8)
            inset[..., 3] = np.clip(
                inset_a[:, None] * tm, 0, 255
            ).astype(np.uint8)
            seg_layer.alpha_composite(Image.fromarray(inset, mode="RGBA"))
            sd.rounded_rectangle(
                (0, 0, sw - 1, height - 1), radius=radius,
                outline=_PROGRESS_TRACK_EDGE + (255,), width=line_w,
            )
            bar.alpha_composite(seg_layer, (x0, 0))

    return bar


def _paste_emoji_icon(
    canvas: Image.Image, emoji_bytes: bytes | None,
    x: int, cy: int, icon_px: int, *, label: str = "",
) -> bool:
    """Composite an emoji PNG onto ``canvas`` with its left edge at ``x``
    and vertically centred on ``cy``.

    The art is aspect-preserved (``ImageOps.contain``) and centred inside
    a square ``icon_px`` box so non-square emoji never stretch. Returns
    True when drawn, False on empty input or a decode error so callers can
    fall back to a bullet glyph.
    """
    if not emoji_bytes:
        return False
    try:
        src = Image.open(io.BytesIO(emoji_bytes))
        src.load()
        src = src.convert("RGBA")
        src = ImageOps.contain(src, (icon_px, icon_px), Image.LANCZOS)
        box = Image.new("RGBA", (icon_px, icon_px), (0, 0, 0, 0))
        box.alpha_composite(
            src, ((icon_px - src.width) // 2, (icon_px - src.height) // 2)
        )
        canvas.alpha_composite(box, (x, cy - icon_px // 2))
        box.close()
        src.close()
        return True
    except Exception:
        logger.warning(
            "progress: emoji decode failed for %r", label, exc_info=True
        )
        return False


def _draw_info_grid(
    canvas: Image.Image,
    *,
    norm_rows: list[tuple[str, str, bytes | None]],
    width: int,
    divider_y: int,
    pad: int,
    row_h: int,
    scale: int,
) -> None:
    """Draw the two-column reference grid shared by the progress and
    profile cards.

    Renders a hairline divider at ``divider_y`` then fills each
    ``(label, value, emoji_bytes)`` row into a cell as "[icon]  Label:
    Value" (icon aspect-preserved, bullet fallback), pulling the data
    outward across the full card width. Items fill left-to-right,
    top-to-bottom. ``divider_y`` and ``width`` are in supersampled pixels;
    ``pad`` / ``row_h`` are logical units scaled by ``scale``.
    """
    s = scale

    def sc(v: float) -> int:
        return int(round(v * s))

    draw = ImageDraw.Draw(canvas)
    W = width
    info_label_font = _load_font(sc(16), bold=True)
    info_value_font = _load_font(sc(18), bold=True)
    draw.line(
        [(sc(pad + 8), divider_y), (W - sc(pad + 8), divider_y)],
        fill=_PROGRESS_BG_EDGE,
        width=max(1, sc(1)),
    )

    leading_pad = sc(8)
    icon_gap = sc(8)

    def _draw_cell(
        x0: int, cy: int, label: str, value: str,
        emoji_bytes: bytes | None, lf: ImageFont.FreeTypeFont,
        vf: ImageFont.FreeTypeFont, icon_px: int, right_edge: int,
    ) -> None:
        # Draw "[icon]  Label: Value" anchored at x0, vertically
        # centred on cy and clipped/ellipsized to right_edge.
        if _paste_emoji_icon(
            canvas, emoji_bytes, x0, cy, icon_px, label=label
        ):
            text_x0 = x0 + icon_px + icon_gap
        else:
            bullet = "\u2022 "
            draw.text(
                (x0, cy), bullet, font=lf,
                fill=_PROGRESS_MUTED, anchor="lm",
            )
            text_x0 = x0 + int(draw.textlength(bullet, font=lf))

        label_text = f"{label}: "
        draw.text(
            (text_x0, cy), label_text, font=lf,
            fill=_PROGRESS_MUTED, anchor="lm",
        )
        value_x = text_x0 + draw.textlength(label_text, font=lf)
        max_value_w = right_edge - value_x
        v = value or ""
        if draw.textlength(v, font=vf) > max_value_w:
            while v and draw.textlength(
                v + "\u2026", font=vf
            ) > max_value_w:
                v = v[:-1]
            v = v + "\u2026"
        draw.text(
            (value_x, cy), v, font=vf,
            fill=_PROGRESS_TEXT, anchor="lm",
        )

    # Two-column grid: pull the reference data outward to use the full
    # card width. Items fill left-to-right, top-to-bottom.
    left_x = sc(pad) + leading_pad
    inner_right = W - sc(pad) - leading_pad
    col_gap = sc(24)
    mid_x = (left_x + inner_right) // 2
    left_col_right = mid_x - col_gap // 2
    right_col_x = mid_x + col_gap // 2
    icon_px = sc(22)
    row_y = divider_y + sc(14)
    for i in range(0, len(norm_rows), 2):
        cy = row_y + sc(row_h) // 2
        lbl, val, emj = norm_rows[i]
        _draw_cell(
            left_x, cy, lbl, val, emj, info_label_font,
            info_value_font, icon_px, left_col_right,
        )
        if i + 1 < len(norm_rows):
            lbl2, val2, emj2 = norm_rows[i + 1]
            _draw_cell(
                right_col_x, cy, lbl2, val2, emj2, info_label_font,
                info_value_font, icon_px, inner_right,
            )
        row_y += sc(row_h)


def _render_progress_card_png(
    *,
    avatar_bytes: bytes | None,
    display_name: str,
    count: int,
    target: int,
    info_lines: list[tuple] | None = None,
) -> bytes:
    """Render the progress card and return PNG bytes.

    Composition: circular avatar (left) + gradient bar with overlay text
    on the right. When ``info_lines`` is provided, the card grows
    vertically and renders each ``(label, value)`` pair as a bulleted row
    beneath the bar (used to inline profile / clan / mastery / missing
    info on the verification PASS embed).

    ``info_lines`` entries may be 2-tuples ``(label, value)`` or 3-tuples
    ``(label, value, emoji_png_bytes)``. When emoji bytes are supplied,
    they are composited at the start of the row in place of the bullet.
    The whole card is laid out in logical units and rendered at
    ``_PROGRESS_SS``x so text and icons stay sharp on HiDPI clients.
    """
    progress = max(0.0, min(1.0, count / target)) if target > 0 else 0.0
    complete = target > 0 and count >= target
    s = _PROGRESS_SS

    def sc(v: float) -> int:
        return int(round(v * s))

    # Normalize entries to (label, value, emoji_bytes|None). The
    # "Missing Data" line is pulled out so it can render as an amber
    # callout pill directly beneath the bar (its contextual home) rather
    # than as a row in the reference grid below.
    norm_rows: list[tuple[str, str, bytes | None]] = []
    missing_row: tuple[str, str, bytes | None] | None = None
    for entry in info_lines or []:
        label = entry[0]
        value = entry[1]
        emoji = entry[2] if len(entry) >= 3 else None
        if str(label).strip().lower().startswith("missing"):
            missing_row = (label, value, emoji)
        else:
            norm_rows.append((label, value, emoji))

    # Logical layout sizes (1x); everything below is scaled via sc().
    pad = 22
    row_h = 30
    # Fixed header zone (avatar + name + percent + bar). The avatar is
    # centred in this zone so it never drifts as the footer/grid grow. A
    # status line (the gold "complete" note or the amber missing-data
    # pill) sits just beneath the bar, so reserve extra footer height
    # only when one is shown.
    header_h = 130
    has_status = complete or bool(missing_row)
    base_h = header_h + (42 if has_status else 14)
    info_block_h = 0
    # Regular rows render in two columns so the reference data spreads
    # across the full width instead of bunching on the left; the block
    # height therefore scales with the number of GRID rows (ceil(n / 2)).
    n_grid_rows = (len(norm_rows) + 1) // 2
    if norm_rows:
        info_block_h = 14 + n_grid_rows * row_h + 14
    card_h = base_h + info_block_h

    W, H = sc(_PROGRESS_CARD_W), sc(card_h)
    canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    # Slate panel: vertical gradient clipped to rounded, transparent
    # corners so the card blends into Discord's message background.
    panel = _vertical_gradient(W, H, _PROGRESS_BG_TOP, _PROGRESS_BG_BOTTOM)
    panel_mask = _rounded_mask(W, H, sc(_PROGRESS_RADIUS))
    canvas.paste(panel, (0, 0), panel_mask)
    # Crisp hairline border tracing the rounded panel edge.
    ImageDraw.Draw(canvas).rounded_rectangle(
        (0, 0, W - 1, H - 1),
        radius=sc(_PROGRESS_RADIUS), outline=_PROGRESS_BORDER + (255,),
        width=sc(2),
    )

    avatar_px = sc(_PROGRESS_AVATAR_SIZE)
    avatar = _circular_avatar(avatar_bytes, avatar_px)
    avatar_y = sc((header_h - _PROGRESS_AVATAR_SIZE) // 2)

    # Soft drop shadow under the avatar.
    shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).ellipse(
        (
            sc(pad) + sc(6), avatar_y + avatar_px - sc(10),
            sc(pad) + avatar_px - sc(6), avatar_y + avatar_px + sc(14),
        ),
        fill=(0, 0, 0, 120),
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(sc(7)))
    canvas.alpha_composite(shadow)
    canvas.alpha_composite(avatar, (sc(pad), avatar_y))

    text_x = sc(pad) + avatar_px + sc(22)
    right_x = W - sc(pad)
    draw = ImageDraw.Draw(canvas)

    name_font = _load_font(sc(28), bold=True)
    label_font = _load_font(sc(16), bold=True)
    count_font = _load_font(sc(22), bold=True)
    footer_font = _load_font(sc(14))

    name = display_name or "Member"
    max_name_w = right_x - text_x - sc(10)
    if draw.textlength(name, font=name_font) > max_name_w:
        while name and draw.textlength(
            name + "\u2026", font=name_font
        ) > max_name_w:
            name = name[:-1]
        name = name + "\u2026"
    draw.text((text_x, sc(18)), name, font=name_font, fill=_PROGRESS_TEXT)

    count_text = f"{count} / {target}"
    count_w = draw.textlength(count_text, font=count_font)
    draw.text(
        (right_x - count_w, sc(60)),
        count_text,
        font=count_font,
        fill=_PROGRESS_TEXT,
    )

    pct_label = f"{int(round(progress * 100))}%"
    pct_prefix = "PROGRESS  \u2022  "
    draw.text((text_x, sc(64)), pct_prefix, font=label_font,
              fill=_PROGRESS_MUTED)
    prefix_w = draw.textlength(pct_prefix, font=label_font)
    draw.text(
        (text_x + prefix_w, sc(64)), pct_label, font=label_font,
        fill=_PROGRESS_ACCENT if complete else _PROGRESS_TEXT,
    )

    bar_h = sc(24)
    bar_w = right_x - text_x
    bar_y = sc(98)
    bar = _segmented_bar(
        bar_w, bar_h, count, target, complete=complete, scale=s
    )
    # Soft drop shadow under the bar for depth.
    bshadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ImageDraw.Draw(bshadow).rounded_rectangle(
        (text_x, bar_y + sc(3), text_x + bar_w - 1, bar_y + bar_h - 1 + sc(3)),
        radius=bar_h // 2, fill=(0, 0, 0, 90),
    )
    bshadow = bshadow.filter(ImageFilter.GaussianBlur(sc(4)))
    canvas.alpha_composite(bshadow)
    canvas.alpha_composite(bar, (text_x, bar_y))

    # Status line directly beneath the bar — the contextual home for the
    # "what's left" message. Complete shows the gold success note; an
    # incomplete pass with leftover categories shows an amber "Missing"
    # pill (relocated here from a previously orphaned bottom grid row so
    # it reads as the call to action tied to the progress bar).
    status_y = bar_y + bar_h + sc(9)
    if complete:
        draw.text(
            (text_x, status_y),
            "\u2605  Operator, all roles have been registered!",
            font=footer_font, fill=_PROGRESS_ACCENT,
        )
    elif missing_row:
        amber = _PROGRESS_MISSING
        badge_font = _load_font(sc(15), bold=True)
        badge_text = f"Missing: {missing_row[1]}"
        b_icon_px = sc(17)
        b_gap = sc(7)
        b_pad_x = sc(11)
        b_pad_y = sc(5)
        b_asc, b_desc = badge_font.getmetrics()
        b_has_icon = bool(missing_row[2])
        b_content_h = max(b_icon_px if b_has_icon else 0, b_asc + b_desc)
        b_text_w = int(draw.textlength(badge_text, font=badge_font))
        b_content_w = (b_icon_px + b_gap if b_has_icon else 0) + b_text_w
        badge_w = b_content_w + b_pad_x * 2
        badge_h = b_content_h + b_pad_y * 2
        badge = Image.new("RGBA", (badge_w, badge_h), (0, 0, 0, 0))
        ImageDraw.Draw(badge).rounded_rectangle(
            (0, 0, badge_w - 1, badge_h - 1), radius=badge_h // 2,
            fill=amber + (38,), outline=amber + (175,), width=max(1, sc(1)),
        )
        canvas.alpha_composite(badge, (text_x, status_y))
        cx = text_x + b_pad_x
        cy = status_y + badge_h // 2
        if _paste_emoji_icon(
            canvas, missing_row[2], cx, cy, b_icon_px, label="Missing Data"
        ):
            cx += b_icon_px + b_gap
        draw.text(
            (cx, cy), badge_text, font=badge_font, fill=amber, anchor="lm",
        )

    # Render the labeled reference rows beneath the divider in a
    # two-column grid (clan / mastery / profile / platform). Each cell is
    # "[icon]  Label: Value" with the icon aspect-preserved (no stretch)
    # and vertically centred. The missing-data callout lives above, under
    # the bar, so the grid is purely the data that was read.
    if norm_rows:
        _draw_info_grid(
            canvas, norm_rows=norm_rows, width=W, divider_y=sc(base_h),
            pad=pad, row_h=row_h, scale=s,
        )

    buf = io.BytesIO()
    # Keep the alpha channel so the rounded corners stay transparent and
    # blend into Discord's message background instead of showing as black.
    canvas.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _render_profile_card_png(
    *,
    avatar_bytes: bytes | None,
    display_name: str,
    info_lines: list[tuple] | None = None,
) -> bytes:
    """Render the "user profile" card and return PNG bytes.

    A sibling of :func:`_render_progress_card_png` with the progress bar
    removed. The header stacks a gold "USER PROFILE" eyebrow, the member
    name, and a row of icons beneath it (the platform icon with a soft
    gold glow, trailed by the syndicate flags — a lone syndicate shows
    its icon + faction-coloured name, two or more collapse to icon-only)
    on the left of the circular avatar, with the Clan as a gold callout
    on the right. Beneath the header the Mastery Rank sits in a gold
    capsule badge. ``info_lines`` come from
    :func:`_member_profile_info_lines`. Rendered at ``_PROGRESS_SS``x for
    crisp HiDPI output.
    """
    s = _PROGRESS_SS

    def sc(v: float) -> int:
        return int(round(v * s))

    # Pull the categories the profile card cares about out of info_lines.
    # Clan/Platform/Mastery are single rows; Syndicate is a per-faction
    # list of (name, accent_rgb|None, emoji_bytes). Tolerant of legacy
    # 3-tuple rows and a plain-string Syndicate value (older callers/tests).
    clan_row: tuple[str, str, bytes | None] | None = None
    clan_color: tuple[int, int, int] | None = None
    platform_row: tuple[str, str, bytes | None] | None = None
    mastery_row: tuple[str, str, bytes | None] | None = None
    syndicate_factions: list[tuple[str, tuple[int, int, int] | None, bytes | None]] = []
    for entry in info_lines or []:
        label = entry[0]
        if label == "Clan":
            clan_row = (label, entry[1], entry[2] if len(entry) >= 3 else None)
            clan_color = entry[3] if len(entry) >= 4 else None
        elif label == "Platform":
            platform_row = (
                label, entry[1], entry[2] if len(entry) >= 3 else None
            )
        elif label == "Mastery Rank":
            mastery_row = (
                label, entry[1], entry[2] if len(entry) >= 3 else None
            )
        elif label == "Syndicate" and len(entry) >= 2 and isinstance(
            entry[1], list
        ):
            syndicate_factions = [
                (str(f[0]), f[1] if len(f) >= 2 else None,
                 f[2] if len(f) >= 3 else None)
                for f in entry[1]
            ]

    pad = 22
    # Header zone holds the avatar + eyebrow + name + platform icon.
    header_h = 140
    mr_pill_h = 32

    # Lay out the stacked sections so the canvas height is known before we
    # draw: the header (which now also carries the platform + syndicate
    # flag icons) then the optional Mastery capsule.
    content_bottom = header_h
    mr_pill_top = None
    if mastery_row is not None:
        mr_pill_top = header_h + 10
        content_bottom = mr_pill_top + mr_pill_h
    card_h = content_bottom + 16

    W, H = sc(_PROGRESS_CARD_W), sc(card_h)
    canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    panel = _vertical_gradient(W, H, _PROGRESS_BG_TOP, _PROGRESS_BG_BOTTOM)
    panel_mask = _rounded_mask(W, H, sc(_PROGRESS_RADIUS))
    canvas.paste(panel, (0, 0), panel_mask)
    ImageDraw.Draw(canvas).rounded_rectangle(
        (0, 0, W - 1, H - 1),
        radius=sc(_PROGRESS_RADIUS), outline=_PROGRESS_BORDER + (255,),
        width=sc(2),
    )

    avatar_px = sc(_PROGRESS_AVATAR_SIZE)
    avatar = _circular_avatar(avatar_bytes, avatar_px)
    avatar_y = sc((header_h - _PROGRESS_AVATAR_SIZE) // 2)

    shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).ellipse(
        (
            sc(pad) + sc(6), avatar_y + avatar_px - sc(10),
            sc(pad) + avatar_px - sc(6), avatar_y + avatar_px + sc(14),
        ),
        fill=(0, 0, 0, 120),
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(sc(7)))
    canvas.alpha_composite(shadow)
    canvas.alpha_composite(avatar, (sc(pad), avatar_y))

    text_x = sc(pad) + avatar_px + sc(22)
    right_x = W - sc(pad)
    draw = ImageDraw.Draw(canvas)

    eyebrow_font = _load_font(sc(14), bold=True)
    name_font = _load_font(sc(28), bold=True)

    # Two header rows (eyebrow / name) anchored above the avatar's midline,
    # with the platform icon on a third row beneath the name.
    cy = sc(header_h) // 2
    eyebrow_cy = cy - sc(28)
    name_cy = cy - sc(2)
    plat_cy = cy + sc(34)

    # Thin gold rule between the avatar and the identity text — a refined
    # divider spanning the eyebrow + name rows.
    rule_x = sc(pad) + avatar_px + sc(11)
    draw.rounded_rectangle(
        (rule_x, eyebrow_cy - sc(9), rule_x + sc(3), name_cy + sc(13)),
        radius=sc(2), fill=_PROGRESS_ACCENT + (235,),
    )

    # Clan callout on the right of the header: "CLAN" eyebrow over the
    # clan emoji + name, aligned to the same two rows as the name block.
    name_right_bound = right_x
    if clan_row is not None:
        clan_label_font = _load_font(sc(12), bold=True)
        clan_value_font = _load_font(sc(16), bold=True)
        c_icon_px = sc(22)
        c_gap = sc(9)
        clan_val = clan_row[1] or "\u2014"
        clan_emoji = clan_row[2]
        # Budget the callout to the right ~45% of the header so a long
        # clan name can't crowd the member name; ellipsize to fit.
        clan_budget = int((right_x - text_x) * 0.45)
        icon_w = c_icon_px + c_gap if clan_emoji else 0
        max_clan_w = clan_budget - icon_w
        if draw.textlength(clan_val, font=clan_value_font) > max_clan_w:
            while clan_val and draw.textlength(
                clan_val + "\u2026", font=clan_value_font
            ) > max_clan_w:
                clan_val = clan_val[:-1]
            clan_val = clan_val + "\u2026"
        clan_text_w = draw.textlength(clan_val, font=clan_value_font)
        block_left = int(right_x - icon_w - clan_text_w)
        clan_name_fill = clan_color or _PROGRESS_ACCENT
        draw.text(
            (right_x, eyebrow_cy), "CLAN", font=clan_label_font,
            fill=_PROGRESS_ACCENT, anchor="rm",
        )
        if clan_emoji and _paste_emoji_icon(
            canvas, clan_emoji, block_left, name_cy, c_icon_px,
            label="Clan",
        ):
            draw.text(
                (block_left + c_icon_px + c_gap, name_cy), clan_val,
                font=clan_value_font, fill=clan_name_fill, anchor="lm",
            )
        else:
            draw.text(
                (right_x, name_cy), clan_val, font=clan_value_font,
                fill=clan_name_fill, anchor="rm",
            )
        name_right_bound = block_left - sc(20)

    draw.text(
        (text_x, eyebrow_cy), "USER PROFILE", font=eyebrow_font,
        fill=_PROGRESS_ACCENT, anchor="lm",
    )

    name = display_name or "Member"
    max_name_w = name_right_bound - text_x
    if draw.textlength(name, font=name_font) > max_name_w:
        while name and draw.textlength(
            name + "\u2026", font=name_font
        ) > max_name_w:
            name = name[:-1]
        name = name + "\u2026"
    draw.text(
        (text_x, name_cy), name, font=name_font,
        fill=_PROGRESS_TEXT, anchor="lm",
    )

    # Platform + syndicate flags share one row beneath the name. The
    # platform icon leads with a soft gold glow; a lone syndicate trails
    # it as icon + faction-coloured name, while two or more collapse to
    # icon-only flags (faction-coloured dot fallback when an emoji is
    # unset).
    row_cx = text_x
    if platform_row is not None and platform_row[2]:
        plat_icon_px = sc(26)
        glow_d = plat_icon_px * 2
        gcx = row_cx + plat_icon_px // 2
        glow = Image.new("RGBA", (glow_d, glow_d), (0, 0, 0, 0))
        ImageDraw.Draw(glow).ellipse(
            (glow_d * 0.2, glow_d * 0.2, glow_d * 0.8, glow_d * 0.8),
            fill=_PROGRESS_ACCENT + (95,),
        )
        glow = glow.filter(ImageFilter.GaussianBlur(sc(7)))
        canvas.alpha_composite(
            glow, (gcx - glow_d // 2, plat_cy - glow_d // 2)
        )
        _paste_emoji_icon(
            canvas, platform_row[2], row_cx, plat_cy, plat_icon_px,
            label="Platform",
        )
        row_cx += plat_icon_px + sc(10)
    if len(syndicate_factions) == 1:
        sname, scolor, sbytes = syndicate_factions[0]
        syn_icon_px = sc(24)
        if sbytes and _paste_emoji_icon(
            canvas, sbytes, row_cx, plat_cy, syn_icon_px, label="Syndicate"
        ):
            row_cx += syn_icon_px + sc(9)
        else:
            r = syn_icon_px // 3
            ImageDraw.Draw(canvas).ellipse(
                (
                    row_cx + syn_icon_px // 2 - r, plat_cy - r,
                    row_cx + syn_icon_px // 2 + r, plat_cy + r,
                ),
                fill=(scolor or _PROGRESS_MUTED) + (255,),
            )
            row_cx += syn_icon_px + sc(9)
        syn_font = _load_font(sc(15), bold=True)
        nm = sname or ""
        max_w = name_right_bound - row_cx
        if draw.textlength(nm, font=syn_font) > max_w:
            while nm and draw.textlength(
                nm + "\u2026", font=syn_font
            ) > max_w:
                nm = nm[:-1]
            nm = nm + "\u2026"
        draw.text(
            (row_cx, plat_cy), nm, font=syn_font,
            fill=scolor or _PROGRESS_TEXT, anchor="lm",
        )
    elif syndicate_factions:
        syn_icon_px = sc(24)
        syn_gap = sc(4)
        for _sname, scolor, sbytes in syndicate_factions:
            if row_cx + syn_icon_px > name_right_bound:
                break
            if not (sbytes and _paste_emoji_icon(
                canvas, sbytes, row_cx, plat_cy, syn_icon_px,
                label="Syndicate",
            )):
                r = syn_icon_px // 3
                ImageDraw.Draw(canvas).ellipse(
                    (
                        row_cx + syn_icon_px // 2 - r, plat_cy - r,
                        row_cx + syn_icon_px // 2 + r, plat_cy + r,
                    ),
                    fill=(scolor or _PROGRESS_MUTED) + (255,),
                )
            row_cx += syn_icon_px + syn_gap

    # Mastery Rank capsule badge beneath the header (gold-tinted fill +
    # hairline gold border), sized to its content.
    if mastery_row is not None and mr_pill_top is not None:
        mr_label_font = _load_font(sc(13), bold=True)
        mr_value_font = _load_font(sc(14), bold=True)
        mr_icon_px = sc(18)
        mr_icon_gap = sc(8)
        badge_pad_x = sc(14)
        label_txt = "Mastery Rank: "
        value_txt = mastery_row[1] or "\u2014"
        has_icon = bool(mastery_row[2])
        lbl_w = draw.textlength(label_txt, font=mr_label_font)
        val_w = draw.textlength(value_txt, font=mr_value_font)
        icon_w = (mr_icon_px + mr_icon_gap) if has_icon else 0
        pill_w = int(icon_w + lbl_w + val_w + badge_pad_x * 2)
        pill_h = sc(mr_pill_h)
        pill_x0 = sc(pad) + sc(8)
        pill_y0 = sc(mr_pill_top)
        overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        ImageDraw.Draw(overlay).rounded_rectangle(
            (pill_x0, pill_y0, pill_x0 + pill_w, pill_y0 + pill_h),
            radius=pill_h // 2, fill=_PROGRESS_ACCENT + (14,),
            outline=_PROGRESS_ACCENT + (105,), width=max(1, sc(1)),
        )
        canvas.alpha_composite(overlay)
        mcy = pill_y0 + pill_h // 2
        mx = pill_x0 + badge_pad_x
        if has_icon and _paste_emoji_icon(
            canvas, mastery_row[2], mx, mcy, mr_icon_px, label="Mastery Rank"
        ):
            mx += mr_icon_px + mr_icon_gap
        draw.text(
            (mx, mcy), label_txt, font=mr_label_font,
            fill=_PROGRESS_ACCENT, anchor="lm",
        )
        draw.text(
            (mx + lbl_w, mcy), value_txt, font=mr_value_font,
            fill=_PROGRESS_FILL_GOLD_END, anchor="lm",
        )

    buf = io.BytesIO()
    canvas.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


async def _fetch_cdn_bytes(
    url: str, *, accept: str = "image/png,image/*;q=0.8"
) -> bytes | None:
    """GET ``url`` via the shared session; return the body or None on failure.

    Discord's CDN 403s requests without a recognisable User-Agent, so we
    always send one. Shared by the avatar and emoji fetchers.
    """
    try:
        headers = {
            "User-Agent": "DiscordBot (https://github.com/aidenlong04/Golden-Pagoda-Image-Reader, 1.0)",
            "Accept": accept,
        }
        timeout = aiohttp.ClientTimeout(total=8)
        session = await _get_http_session()
        async with session.get(url, headers=headers, timeout=timeout) as resp:
            if resp.status != 200:
                logger.warning("cdn fetch returned HTTP %s for %s", resp.status, url)
                return None
            data = await resp.read()
            return data or None
    except Exception:
        logger.warning("cdn fetch failed for %s", url, exc_info=True)
        return None


async def _fetch_avatar_bytes(url: str) -> bytes | None:
    """Best-effort avatar fetch (avatars are <512 KiB); None on any failure."""
    return await _fetch_cdn_bytes(url, accept="image/png,image/webp,image/*;q=0.8")


# Decoded emoji PNG bytes keyed by Discord emoji ID. Emojis are immutable
# for the life of the process, so a simple dict cache is enough — avoids
# refetching the same clan/platform icons on every verification.
_EMOJI_BYTES_CACHE: dict[int, bytes | None] = {}


async def _fetch_emoji_bytes(literal: str | None) -> bytes | None:
    """Fetch a custom Discord emoji as PNG bytes from the CDN.

    ``literal`` is the raw ``<:Name:id>`` / ``<a:Name:id>`` form used in
    the env. Returns ``None`` for unicode/empty input or any network
    failure. Results are cached per emoji ID for the process lifetime.
    """
    eid = _emoji_id_from_literal(literal)
    if eid is None:
        return None
    if eid not in _EMOJI_BYTES_CACHE:
        url = f"https://cdn.discordapp.com/emojis/{eid}.png?size=128&quality=lossless"
        _EMOJI_BYTES_CACHE[eid] = await _fetch_cdn_bytes(url)
    return _EMOJI_BYTES_CACHE[eid]


def _progress_components(
    *, display_name: str, have: int, total: int, missing: list[str],
    link_buttons: list[tuple[str, str]] | None = None,
) -> list[dict]:
    """Mirror the _pass_components shape so /progress feels identical to the
    verification reply: header with the member's name + status icon, then a
    container with the completion summary and an optional help-link button.
    """
    complete = have >= total and total > 0
    if complete:
        icon = "\u2705"
        body = "\u2605 Verification complete \u2014 all roles assigned."
        accent = ACCENT_PASS
    elif total == 0:
        icon = "\u26a0\ufe0f"
        body = "-# No verification categories are configured for this server."
        accent = ACCENT_INCOMPLETE
    else:
        icon = "\u26a0\ufe0f"
        bullets = ", ".join(f"**{m}**" for m in missing)
        body = f"-# Missing: {bullets}"
        accent = ACCENT_INCOMPLETE

    safe_name = _strip_clan_tag(display_name) or display_name
    header = {"type": 10, "content": f"### {icon}  `{safe_name}`"}
    container_children: list[dict] = [
        {"type": 10, "content": body},
        {"type": 10, "content": f"-# Progress: {have}/{total}"},
    ]
    if not complete:
        button_row = _link_button_row(link_buttons)
        if button_row:
            container_children.append(button_row)
    return [header, {
        "type": 17, "accent_color": accent,
        "components": container_children,
    }]


@tree.command(
    name="progress",
    description="Show your verification role progress (0-100% complete).",
)
@app_commands.describe(
    user="View another member's progress (defaults to yourself).",
    ephemeral="Only you can see the reply when true (default: false).",
)
async def progress_cmd(
    interaction: discord.Interaction,
    user: discord.Member | None = None,
    ephemeral: bool = False,
) -> None:
    target = user or interaction.user
    if not isinstance(target, discord.Member):
        await interaction.response.send_message(
            "\u274C /progress can only be used in a server.", ephemeral=True
        )
        return

    display_name = target.display_name
    avatar_asset = target.display_avatar or target.default_avatar
    avatar_url = avatar_asset.replace(size=256, format="png").url

    role_ids = {r.id for r in target.roles}
    cats = _role_categories_for(role_ids)
    total = len(cats)
    have = sum(1 for _, ok in cats if ok)
    missing = [name for name, ok in cats if not ok]

    avatar_bytes = await _fetch_avatar_bytes(avatar_url)
    png = await asyncio.to_thread(
        _render_progress_card_png,
        avatar_bytes=avatar_bytes,
        display_name=display_name,
        count=have,
        target=total,
    )

    components = _progress_components(
        display_name=display_name, have=have, total=total, missing=missing,
        link_buttons=_help_link_buttons(interaction.guild),
    )

    # Mirror the verification flow exactly:
    # 1. V2 card as the initial response (same shape as _pass_components).
    # 2. The progress card PNG as a separate plain followup message — its
    #    own attachment bubble, no media-gallery wrapper.
    try:
        await _interaction_callback(
            interaction, 4, components, ephemeral=ephemeral,
        )
        await interaction.followup.send(
            file=discord.File(io.BytesIO(png), filename="progress.png"),
            ephemeral=ephemeral,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except Exception:
        logger.exception("/progress failed")
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "\u274C Failed to render progress.", ephemeral=True
            )


# ---------- /profile mastery-rank editor ------------------------------------
#
# The /profile card can carry an opt-in dropdown that lets a member set
# their *displayed* Mastery Rank (MR 1-30 / Legendary 1-8). Picking a value
# both swaps the member's coarse MR role bucket (MR 1-10, 11-15, ... ) to
# the matching one AND persists the exact rank to the durable per-member
# store so the card shows it. This uses discord.py's native ui.View/Select
# (rather than the raw V2 path) because a select menu attached to an
# uploaded image plus per-pick re-render is exactly what Views handle well;
# the on_interaction listener ignores their auto-generated custom_ids.

_MR_BUCKET_DIGITS_RE = re.compile(r"\d+")


def _parse_mr_bucket_range(name: str) -> tuple[str, int, int] | None:
    """Parse an MR role *name* into ``(kind, lo, hi)``.

    Handles the configured bucket names (e.g. ``"MR 1-10"`` ->
    ``("MR", 1, 10)``, ``"MR 30"`` -> ``("MR", 30, 30)``, ``"LR 1-7"`` ->
    ``("LR", 1, 7)``). ``kind`` is ``"MR"`` or ``"LR"``. Returns None when
    the name has no recognizable kind or number.
    """
    upper = name.upper()
    if "LR" in upper or "LEGEND" in upper:
        kind = "LR"
    elif "MR" in upper or "MASTER" in upper:
        kind = "MR"
    else:
        return None
    nums = [int(x) for x in _MR_BUCKET_DIGITS_RE.findall(name)]
    if not nums:
        return None
    lo, hi = nums[0], nums[-1]
    if hi < lo:
        lo, hi = hi, lo
    return (kind, lo, hi)


def _mr_bucket_role_for(
    guild: discord.Guild, kind: str, value: int
) -> "discord.Role | None":
    """Return the configured MR bucket role whose range covers ``value``.

    Resolves against the live role names (``MR_ROLE_IDS`` is not guaranteed
    index-aligned with ``MR_ROLE_NAMES`` since unresolved names are
    skipped), so the mapping stays correct regardless of how the buckets
    were configured.
    """
    for rid in MR_ROLE_IDS:
        role = guild.get_role(rid)
        if role is None:
            continue
        parsed = _parse_mr_bucket_range(role.name)
        if parsed and parsed[0] == kind and parsed[1] <= value <= parsed[2]:
            return role
    return None


async def _apply_mastery_bucket(
    member: discord.Member, kind: str, value: int
) -> str:
    """Swap ``member``'s MR bucket role to the one covering ``(kind, value)``.

    Returns ``"assigned"`` on success (incl. already-correct), ``"no_match"``
    when no configured bucket covers that rank, or ``"error"`` when the
    role edit fails (missing perms / role hierarchy).
    """
    target = _mr_bucket_role_for(member.guild, kind, value)
    if target is None:
        return "no_match"
    mr_ids = set(MR_ROLE_IDS)
    have_ids = {r.id for r in member.roles}
    to_remove = [
        r for r in member.roles if r.id in mr_ids and r.id != target.id
    ]
    try:
        if to_remove:
            await member.remove_roles(
                *to_remove, reason="Mastery rank self-service edit"
            )
        if target.id not in have_ids:
            await member.add_roles(
                target, reason="Mastery rank self-service edit"
            )
        return "assigned"
    except discord.HTTPException:
        logger.exception("mastery bucket role swap failed")
        return "error"


def _mastery_select_options() -> tuple[list, list]:
    """Build the two grouped option lists for the mastery editor.

    Discord caps a select menu at 25 options, so MR 1-30 + Legendary 1-8
    (38 values) is split: 1-25 in the first menu, 26-30 + Legendary 1-8 in
    the second. Option values encode ``"<kind>:<n>"`` (e.g. ``"MR:28"``,
    ``"LR:3"``).
    """
    first = [
        discord.SelectOption(label=f"Mastery Rank {n}", value=f"MR:{n}")
        for n in range(1, 26)
    ]
    second = [
        discord.SelectOption(label=f"Mastery Rank {n}", value=f"MR:{n}")
        for n in range(26, 31)
    ]
    second += [
        discord.SelectOption(label=f"Legendary {n}", value=f"LR:{n}")
        for n in range(1, 9)
    ]
    return first, second


class _MasterySelect(discord.ui.Select):
    def __init__(self, editor: "_MasteryEditorView", *, placeholder: str,
                 options: list) -> None:
        super().__init__(
            placeholder=placeholder, min_values=1, max_values=1,
            options=options,
        )
        # NB: store the back-reference under a private name that does NOT
        # collide with discord.py's reserved ``Item._parent`` (used by
        # ``Item._run_checks`` for V2 layout nesting). Clobbering ``_parent``
        # with the View breaks interaction dispatch (AttributeError: View has
        # no ``_run_checks``).
        self._editor = editor

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._editor.handle_pick(interaction, self.values[0])


class _MasteryEditorView(discord.ui.View):
    """Self-service Mastery Rank editor attached to a /profile card.

    Only the profile owner can use it (``interaction_check``). On a pick we
    swap the member's MR role bucket, persist the exact rank, re-render the
    card, and edit the message in place.
    """

    def __init__(
        self, *, member: discord.Member, owner_id: int,
        avatar_bytes: bytes | None, display_name: str,
    ) -> None:
        super().__init__(timeout=300)
        self.member = member
        self.owner_id = owner_id
        self.avatar_bytes = avatar_bytes
        self.display_name = display_name
        first, second = _mastery_select_options()
        self.add_item(_MasterySelect(
            self, placeholder="Set Mastery Rank (1\u201325)", options=first,
        ))
        self.add_item(_MasterySelect(
            self,
            placeholder="Set Mastery Rank 26\u201330 / Legendary 1\u20138",
            options=second,
        ))

    async def interaction_check(
        self, interaction: discord.Interaction
    ) -> bool:
        if interaction.user.id != self.owner_id:
            with contextlib.suppress(discord.HTTPException):
                await interaction.response.send_message(
                    "These controls aren't yours.", ephemeral=True
                )
            return False
        return True

    async def handle_pick(
        self, interaction: discord.Interaction, raw: str
    ) -> None:
        kind, _, num = raw.partition(":")
        try:
            value = int(num)
        except ValueError:
            with contextlib.suppress(discord.HTTPException):
                await interaction.response.defer()
            return

        status = await _apply_mastery_bucket(self.member, kind, value)
        # Persist synchronously (off-loop) so the re-render below reads the
        # freshly stored override instead of racing the write.
        await asyncio.to_thread(
            analytics.upsert_member_profile,
            guild_id=self.member.guild.id,
            user_id=self.member.id,
            mastery_rank=f"{kind} {value}",
        )
        info = await _member_profile_info_lines(self.member)
        png = await asyncio.to_thread(
            _render_profile_card_png,
            avatar_bytes=self.avatar_bytes,
            display_name=self.display_name,
            info_lines=info,
        )
        await interaction.response.edit_message(
            attachments=[
                discord.File(io.BytesIO(png), filename="profile.png")
            ],
            view=self,
        )
        if status == "no_match":
            with contextlib.suppress(discord.HTTPException):
                await interaction.followup.send(
                    "Saved your displayed rank. No matching rank role is "
                    "configured here, so your Discord role was left "
                    "unchanged.",
                    ephemeral=True,
                )
        elif status == "error":
            with contextlib.suppress(discord.HTTPException):
                await interaction.followup.send(
                    "Saved your displayed rank, but I couldn't change your "
                    "Discord role \u2014 check my Manage Roles permission and "
                    "role position.",
                    ephemeral=True,
                )


@tree.command(
    name="profile",
    description="Show a member's Warframe verification profile card.",
)
@app_commands.describe(
    user="View another member's profile (defaults to yourself).",
    ephemeral="Hide the reply so only you see it (default: true).",
    edit_mastery="Attach a dropdown to set your Mastery Rank (only on your own profile).",
)
async def profile_cmd(
    interaction: discord.Interaction,
    user: discord.Member | None = None,
    ephemeral: bool = True,
    edit_mastery: bool = False,
) -> None:
    target = user or interaction.user
    if not isinstance(target, discord.Member):
        await interaction.response.send_message(
            "\u274C /profile can only be used in a server.", ephemeral=True
        )
        return

    display_name = target.display_name
    avatar_asset = target.display_avatar or target.default_avatar
    avatar_url = avatar_asset.replace(size=256, format="png").url

    # Gather role-derived data first, then render off the event loop. The
    # card carries the same reference grid as /progress (Clan / Platform /
    # Mastery / Syndicate) without the progress bar.
    try:
        await interaction.response.defer(ephemeral=ephemeral)
        info = await _member_profile_info_lines(target)
        avatar_bytes = await _fetch_avatar_bytes(avatar_url)
        png = await asyncio.to_thread(
            _render_profile_card_png,
            avatar_bytes=avatar_bytes,
            display_name=display_name,
            info_lines=info,
        )
        send_kwargs: dict = dict(
            file=discord.File(io.BytesIO(png), filename="profile.png"),
            ephemeral=ephemeral,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        # The mastery editor is opt-in and only ever attached to a member's
        # own profile (it edits their roles + stored rank).
        if edit_mastery and target.id == interaction.user.id:
            send_kwargs["view"] = _MasteryEditorView(
                member=target,
                owner_id=interaction.user.id,
                avatar_bytes=avatar_bytes,
                display_name=display_name,
            )
        await interaction.followup.send(**send_kwargs)
    except Exception:
        logger.exception("/profile failed")
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "\u274C Failed to render profile.", ephemeral=True
            )


async def _handle_nick_interaction(
    interaction: discord.Interaction, custom_id: str
) -> None:
    """Handle the in-game-name buttons embedded at the bottom of the
    verification reply.

    On click we apply the nickname (if Yes) and then edit the message in
    place via UPDATE_MESSAGE (callback type 7), stripping just the
    nick-prompt components from the bottom so the verification card +
    progress bar above remain intact.
    """
    from urllib.parse import unquote

    parts = custom_id.split(":", 3)
    # ["nick", "y"|"n", uid, encoded_name?]
    if len(parts) < 3:
        return
    action = parts[1]
    try:
        target_uid = int(parts[2])
    except ValueError:
        return

    suggestion = ""
    if len(parts) >= 4:
        suggestion = unquote(parts[3])[:32].strip()

    if interaction.user.id != target_uid:
        # Wrong user clicked: ephemeral notice, no visible message change.
        try:
            await _interaction_callback(
                interaction, 4,
                _nickname_resolved_components(
                    "-# Only the verified member can use these buttons.",
                    ACCENT_FAIL,
                ),
            )
        except Exception:
            logger.exception("nick: deny ack failed")
        return

    if action not in ("y", "n"):
        return

    if action == "y" and suggestion:
        if interaction.guild and isinstance(interaction.user, discord.Member):
            try:
                await interaction.user.edit(
                    nick=suggestion,
                    reason="In-game name applied via verification prompt",
                )
            except discord.Forbidden:
                logger.info("nick: forbidden setting %s", suggestion)
            except discord.HTTPException:
                logger.exception("nick: edit failed")

    # Build the trimmed component list: keep everything from the original
    # message EXCEPT the nick-prompt block at the bottom (banner image,
    # gold container header, two button sections, and the separator
    # between them). discord.py 2.x doesn't parse V2 sub-components, so
    # fetch the raw message JSON to get authoritative component data.
    raw: list[dict] = []
    msg_attachments: list[dict] = []
    try:
        if interaction.message is not None:
            data = await client.http.get_message(
                interaction.channel_id, interaction.message.id,
            )
            raw = data.get("components") or []
            msg_attachments = data.get("attachments") or []
    except Exception:
        logger.exception("nick: fetch original message failed")
    trimmed = _strip_nick_prompt(raw)
    if not trimmed:
        # Nothing left to show; fall back to a silent ACK so Discord
        # doesn't display "interaction failed".
        try:
            from discord.http import Route
            route = Route(
                "POST",
                "/interactions/{interaction_id}/{interaction_token}/callback",
                interaction_id=interaction.id,
                interaction_token=interaction.token,
            )
            await client.http.request(route, json={"type": 6})
        except Exception:
            logger.exception("nick: deferred ack failed")
        return

    # If the original verification reply included the progress card
    # attachment, re-render it from the member's CURRENT roles so the
    # bar reflects any changes since the message was first posted
    # (clan/platform/MR/syndicate roles picked up via self-service,
    # plus any unverified-role removal). When refreshed, we must edit
    # via PATCH+multipart so the new PNG replaces the old attachment;
    # the type:9/type:12 component still references attachment://progress.png.
    has_progress = any(
        att.get("filename") == "progress.png" for att in msg_attachments
    )
    refreshed_png: bytes | None = None
    if (
        has_progress
        and interaction.guild
        and isinstance(interaction.user, discord.Member)
    ):
        try:
            member = interaction.user
            role_ids = {r.id for r in member.roles}
            cats = _role_categories_for(role_ids)
            have = sum(1 for _, ok in cats if ok)
            total = len(cats)
            avatar_url = (
                member.display_avatar or member.default_avatar
            ).replace(size=256, format="png").url
            avatar_bytes = await _fetch_avatar_bytes(avatar_url)
            refreshed_png = await asyncio.to_thread(
                _render_progress_card_png,
                avatar_bytes=avatar_bytes,
                display_name=member.display_name,
                count=have,
                target=total,
            )
        except Exception:
            logger.exception("nick: progress card refresh failed")
            refreshed_png = None

    if refreshed_png is not None:
        # Deferred-update ACK, then PATCH the message with multipart so
        # the new attachment supersedes the old one. UPDATE_MESSAGE
        # (type 7) is JSON-only so we can't use it for attachment swaps.
        try:
            from discord.http import Route
            route = Route(
                "POST",
                "/interactions/{interaction_id}/{interaction_token}/callback",
                interaction_id=interaction.id,
                interaction_token=interaction.token,
            )
            await client.http.request(route, json={"type": 6})
        except Exception:
            logger.exception("nick: deferred ack (refresh) failed")
            return
        try:
            await _edit_message_v2_with_file(
                channel_id=interaction.channel_id,
                message_id=interaction.message.id,
                components=trimmed,
                file_bytes=refreshed_png,
                file_name="progress.png",
            )
        except Exception:
            logger.exception("nick: message PATCH with refreshed card failed")
        return

    try:
        await _interaction_callback(
            interaction, 7, trimmed, ephemeral=False,
        )
    except Exception:
        logger.exception("nick: update message failed")


def _strip_nick_prompt(components: list[dict]) -> list[dict]:
    """Return ``components`` with the in-game-name prompt removed.

    Two shapes are produced by the bot and both are handled here,
    regardless of position:

      * the incomplete flow appends a STANDALONE gold container (accent
        ``_NICK_PROMPT_ACCENT``) — drop it wholesale;
      * the pass flow folds the caption + ``nick:`` buttons INTO its
        ``ACCENT_PASS`` container — reach in and remove just those,
        dropping the container only if nothing survives.
    """
    result: list[dict] = []
    for comp in components:
        # Standalone nick-prompt container (incomplete flow): drop it.
        if (
            comp.get("type") == 17
            and comp.get("accent_color") == _NICK_PROMPT_ACCENT
        ):
            continue
        # Any other container may have the call-sign prompt folded in
        # (the pass reply): strip just the caption + nick: buttons.
        if comp.get("type") == 17 and isinstance(comp.get("components"), list):
            kept: list[dict] = []
            for child in comp["components"]:
                ct = child.get("type")
                if ct == 10 and child.get("content") == _CALLSIGN_CAPTION:
                    continue
                if ct == 1:
                    btns = [
                        b for b in (child.get("components") or [])
                        if not str(b.get("custom_id", "")).startswith("nick:")
                    ]
                    if not btns:
                        continue
                    child = {**child, "components": btns}
                kept.append(child)
            if not kept:
                continue
            comp = {**comp, "components": kept}
        result.append(comp)
    return result


@client.event
async def on_interaction(interaction: discord.Interaction) -> None:
    if interaction.type != discord.InteractionType.component:
        return
    data = interaction.data or {}
    custom_id = str(data.get("custom_id", ""))

    # Backwards-compatible: accept legacy "stats:N" buttons too.
    if custom_id.startswith("stats:"):
        custom_id = "status:" + custom_id.split(":", 1)[1]
    if custom_id.startswith("nick:"):
        await _handle_nick_interaction(interaction, custom_id)
        return
    if not custom_id.startswith("status:"):
        return

    parts = custom_id.split(":", 1)
    if len(parts) != 2 or parts[1] == "noop":
        try:
            await _interaction_callback(interaction, 6, [])  # DEFERRED_UPDATE
        except Exception:
            logger.exception("noop ack failed")
        return
    if parts[1] == "assign_emblems":
        cmd_id = _COMMAND_IDS.get("clan-emblems")
        mention = (
            f"</clan-emblems:{cmd_id}>" if cmd_id else "`/clan-emblems`"
        )
        body = (
            # Custom emoji don't render inside markdown headings; keep the
            # heading plain and render the emblem inline on the body line.
            f"### Emblems\n"
            f"<:GoldenPagoda_Emblem:1416905638428020877>  Click {mention} "
            f"to set or clear a clan emoji.\n"
            f"-# > Select clan member role and then input clan emblem emoji. "
            f"Leave emoji blank to clear"
        )
        container = {
            "type": 17,
            "accent_color": ACCENT_PASS,
            "components": [{"type": 10, "content": body}],
        }
        try:
            await _interaction_callback(interaction, 4, [container])  # CHANNEL_MESSAGE_WITH_SOURCE
        except Exception:
            logger.exception("assign_emblems hint failed")
        return
    try:
        page = int(parts[1])
    except ValueError:
        return
    components = _status_components(interaction, page)
    try:
        await _interaction_callback(interaction, 7, components)  # UPDATE_MESSAGE
    except Exception:
        logger.exception("pagination failed")



if __name__ == "__main__":
    # uvloop is a drop-in C event loop; ~2-4x faster than asyncio default
    # for I/O-heavy workloads. Optional dependency — fall back silently
    # if it isn't installed (dev shells, ARM builds without wheels, etc.).
    try:
        import uvloop  # type: ignore

        uvloop.install()
        logger.info("uvloop event loop installed")
    except ImportError:
        pass
    # log_handler=None disables discord.py's own setup_logging so we don't
    # get duplicate handlers on the root + `discord` loggers (which would
    # double every log line emitted by discord.py).
    client.run(DISCORD_TOKEN, log_handler=None)
