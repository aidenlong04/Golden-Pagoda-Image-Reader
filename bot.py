from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import os
import re
import sys
import time
import warnings
from collections.abc import Callable
from datetime import timedelta
from importlib import metadata as importlib_metadata
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

import discord  # noqa: E402
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


TARGET_CHANNEL_ID = _int_env("TARGET_CHANNEL_ID")
GUILD_ID = _int_env("GUILD_ID", 1361846841905381629)
PASS_INFO_CHANNEL_ID = _int_env("PASS_INFO_CHANNEL_ID", 1392582268769271950)
PASS_EXTRA_CHANNEL_ID = _int_env("PASS_EXTRA_CHANNEL_ID", 1361846842383663268)

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
PASS_REACTION_NAME = os.getenv("PASS_REACTION_NAME", "thumbsup")
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
FAIL_REACTION_EMOJI = _parse_reaction_emoji(FAIL_REACTION)
# Reaction cleared from the post when verification passes (e.g. a "pending"
# marker added upstream). Set to 0 to disable.
PENDING_REACTION_ID = _int_env("PENDING_REACTION_ID", 1459403163432910972)
PENDING_REACTION_NAME = os.getenv("PENDING_REACTION_NAME", "pending")

# Components V2 reply styling.
COMPONENTS_V2_FLAG = 1 << 15  # 32768 — IS_COMPONENTS_V2
ACCENT_PASS = _int_env("ACCENT_PASS", 0xD4A857)        # gold
ACCENT_FAIL = _int_env("ACCENT_FAIL", 0xED4245)        # red
ACCENT_INCOMPLETE = _int_env("ACCENT_INCOMPLETE", 0x99AAB5)  # grey

# Role granted to users whose screenshot was readable but couldn't be fully
# verified automatically (platform icon missing, unconfigured clan, etc).
# A staff member then manually completes verification.
INCOMPLETE_ROLE_ID = _int_env("INCOMPLETE_ROLE_ID", 1361846841905381632)

# Roles mentioned in the "Verification Incomplete" message so staff is pinged
# to follow up. Comma-separated list of role IDs.
OUTREACH_ROLE_IDS: list[int] = [
    int(x)
    for x in (
        os.getenv("OUTREACH_ROLE_IDS", "1361846841934610565,1361846841934610563")
    ).split(",")
    if x.strip().isdigit()
]

# Auto-delete bot replies after this many seconds (0 = keep forever).
REPLY_TTL_SECONDS = _int_env("REPLY_TTL_SECONDS", 180)

# Role removed from a member on successful verification (e.g. an "unverified"
# gate role). Set to 0 to disable.
VERIFY_REMOVE_ROLE_ID = _int_env("VERIFY_REMOVE_ROLE_ID", 1459326361968574555)

# Catch-up scan: process missed messages from recent history on startup.
CATCHUP_LOOKBACK_HOURS = _int_env("CATCHUP_LOOKBACK_HOURS", 24)
CATCHUP_STATE_PATH = Path(os.getenv("CATCHUP_STATE_PATH", "/app/data/catchup_state.json"))
CATCHUP_DELAY_SECONDS = float(os.getenv("CATCHUP_DELAY_SECONDS") or "1.0")

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


def _update_env_clan_slots(slots: list[ClanSlot]) -> bool:
    """Rewrite the CLAN_ROLE_{i}_NAME/_ID/_EMOJI entries in the .env file in place."""
    if not ENV_FILE_PATH.exists():
        return False

    by_slot = {s.slot: s for s in slots}
    lines = ENV_FILE_PATH.read_text().splitlines()
    seen: set[tuple[int, str]] = set()
    pattern = re.compile(r"^(\s*)CLAN_ROLE_(\d+)_(NAME|ID|EMOJI)\s*=.*$")

    for idx, line in enumerate(lines):
        m = pattern.match(line)
        if not m:
            continue
        indent, slot_num, field = m.group(1), int(m.group(2)), m.group(3)
        slot = by_slot.get(slot_num)
        if slot is None:
            continue
        value = _slot_field_value(slot, field)
        lines[idx] = f"{indent}CLAN_ROLE_{slot_num}_{field}={value}"
        seen.add((slot_num, field))

    missing: list[str] = []
    for i in sorted(by_slot):
        for field in ("NAME", "ID", "EMOJI"):
            if (i, field) in seen:
                continue
            value = _slot_field_value(by_slot[i], field)
            missing.append(f"CLAN_ROLE_{i}_{field}={value}")
    if missing:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(missing)

    _atomic_write_text(ENV_FILE_PATH, "\n".join(lines) + "\n")
    return True


CLAN_SLOTS: list[ClanSlot] = _load_clan_slots()


def _update_env_platform_ids(ids: dict[str, int | None]) -> bool:
    """Rewrite the PLATFORM_ROLE_*_ID entries in the .env file in place."""
    if not ENV_FILE_PATH.exists():
        return False

    key_to_platform = {v: k for k, v in PLATFORM_ROLE_ID_ENV_KEYS.items()}
    lines = ENV_FILE_PATH.read_text().splitlines()
    seen: set[str] = set()
    pattern = re.compile(r"^(\s*)(PLATFORM_ROLE_[A-Z]+_ID)\s*=.*$")

    for idx, line in enumerate(lines):
        m = pattern.match(line)
        if not m:
            continue
        indent, key = m.group(1), m.group(2)
        platform = key_to_platform.get(key)
        if platform is None:
            continue
        value = str(ids.get(platform)) if ids.get(platform) else ""
        lines[idx] = f"{indent}{key}={value}"
        seen.add(key)

    missing: list[str] = []
    for platform, key in PLATFORM_ROLE_ID_ENV_KEYS.items():
        if key in seen:
            continue
        value = str(ids.get(platform)) if ids.get(platform) else ""
        missing.append(f"{key}={value}")
    if missing:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(missing)

    _atomic_write_text(ENV_FILE_PATH, "\n".join(lines) + "\n")
    return True


def _update_env_id_list(env_key: str, ids: list[int]) -> bool:
    """Rewrite (or append) ``ENV_KEY=id1,id2,...`` in the .env file."""
    if not ENV_FILE_PATH.exists():
        return False
    value = ",".join(str(i) for i in ids)
    lines = ENV_FILE_PATH.read_text().splitlines()
    pattern = re.compile(rf"^(\s*){re.escape(env_key)}\s*=.*$")
    replaced = False
    for idx, line in enumerate(lines):
        m = pattern.match(line)
        if m:
            lines[idx] = f"{m.group(1)}{env_key}={value}"
            replaced = True
            break
    if not replaced:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(f"{env_key}={value}")
    _atomic_write_text(ENV_FILE_PATH, "\n".join(lines) + "\n")
    return True


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
    return data.get("last_message_id")


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
            if eid is not None and (eid == PASS_REACTION_ID or eid == fail_id):
                return True
        elif fail_str is not None and emoji == fail_str:
            return True
    return False


async def _health_task() -> None:
    while True:
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


def _normalize(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().lower())


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


# Tracks which OCR backend serviced the most recent _ocr() call so the
# caller can record an accurate engine label in analytics.
_LAST_OCR_ENGINE: str = "ocr.space"


def _ocr(
    image_bytes: bytes, filename: str, content_type: str
) -> tuple[str, list[tuple[str, tuple[int, int, int, int]]]]:
    global _LAST_OCR_ENGINE
    if OCR_API_KEY:
        try:
            result = _ocr_via_api(image_bytes, filename, content_type)
            _LAST_OCR_ENGINE = "ocr.space"
            return result
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
                    result = _ocr_via_api(
                        image_bytes, filename, content_type, engine="2"
                    )
                    _LAST_OCR_ENGINE = "ocr.space:e2"
                    return result
                except Exception as api_err2:
                    logger.warning(
                        "OCR.space engine 2 also failed (%s); falling back to local Tesseract",
                        api_err2.__class__.__name__,
                    )
            if pytesseract is None:
                raise
            _LAST_OCR_ENGINE = "tesseract"
            return _ocr_via_tesseract(image_bytes)
    if pytesseract is None:
        raise RuntimeError(
            "No OCR backend available: set OCR_API_KEY or install pytesseract."
        )
    _LAST_OCR_ENGINE = "tesseract"
    return _ocr_via_tesseract(image_bytes)


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
    try:
        img = Image.open(io.BytesIO(image_bytes))
        w, h = img.size
    except Exception:
        return ocr_text, ocr_words
    finally:
        if 'img' in locals():
            img.close()
    strip_h = max(40, int(h * _TITLE_STRIP_FRAC))
    # If we already have a #NNN word inside the title strip, no need to retry.
    for _text, (_x0, y0, _x1, y1) in ocr_words:
        if y0 < strip_h and _PROFILE_TOKEN_RE.search(_text or ""):
            return ocr_text, ocr_words
    try:
        strip = img.crop((0, 0, w, strip_h)).convert("L")
        # Upscale aggressively — title-bar glyphs are ~16-22px tall on a
        # 1080p screenshot, well below Tesseract's comfort zone.
        strip = strip.resize((strip.width * 3, strip.height * 3), Image.LANCZOS)
        strip = ImageOps.autocontrast(strip, cutoff=2)
        data = pytesseract.image_to_data(
            strip,
            config="--oem 3 --psm 7",
            output_type=pytesseract.Output.DICT,
        )
    except Exception:
        logger.exception("Title-bar Tesseract supplement failed")
        return ocr_text, ocr_words
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
        ocr_text_raw, ocr_words = await asyncio.to_thread(
            _ocr,
            image_bytes,
            attachment.filename,
            attachment.content_type or "image/png",
        )
        ocr_text = ocr_text_raw.strip()
        ocr_engine = _LAST_OCR_ENGINE
    except Exception:
        logger.exception("OCR failed for uploaded image")
        analytics.record_verification(
            outcome="ocr_error",
            ocr_engine=ocr_engine,
            ocr_latency_ms=int((time.monotonic() - ocr_started) * 1000),
            user_id=message.author.id,
            guild_id=message.guild.id,
        )
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
        analytics.record_verification(
            outcome="unreadable",
            clan=clan_name,
            ocr_engine=ocr_engine,
            ocr_latency_ms=ocr_latency_ms,
            user_id=message.author.id,
            guild_id=message.guild.id,
        )
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
        for (label, role_obj, _), result in zip(role_coros, results):
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
    # is still missing (Platform / MR / Syndicate). Platform is no longer
    # auto-assigned \u2014 the user picks it themselves in the help channel.
    effective_role_ids = {r.id for r in member.roles} | assigned_role_ids
    cats = _role_categories_for(effective_role_ids)
    have = sum(1 for _, ok in cats if ok)
    total = len(cats)
    extra_missing = [name for name, ok in cats if not ok]
    if extra_missing:
        issues.extend(f"Missing **{cat}** role." for cat in extra_missing)
        passed = False

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
        )
    except Exception:
        logger.warning("verify: progress card render failed", exc_info=True)

    # Fan out the user-visible work concurrently: reacting, removing the
    # opposite-state role, and posting the V2 reply all hit different
    # Discord endpoints and never depend on each other.
    nick_target = _nickname_suggestion(member, profile_name) if passed else None
    if passed:
        components = _pass_components(
            profile_name, clan_name, role_lines,
            clan_emoji=clan_emoji,
            mastery_rank=mastery_rank,
            link_buttons=_resolve_pass_link_buttons(message.guild, clan_name),
            progress_attachment="progress.png" if progress_png else None,
            nick_suggestion=nick_target,
            user_id=member.id,
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
        if emoji is None and PASS_REACTION_ID:
            emoji = discord.PartialEmoji(name=PASS_REACTION_NAME, id=PASS_REACTION_ID)
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
        ) or discord.PartialEmoji(name=PENDING_REACTION_NAME, id=PENDING_REACTION_ID)
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


def _container(accent: int, text: str) -> dict:
    """Minimal v2 container: one TextDisplay inside an accent-coloured container."""
    return {
        "type": 17,
        "accent_color": accent,
        "components": [{"type": 10, "content": text}],
    }


def _quote(text: str) -> str:
    """Render multi-line text as a Discord blockquote."""
    return "\n".join(f"> {line}" if line else ">" for line in text.splitlines() or [text])


CLAN_EMOJI = os.getenv("CLAN_EMOJI", "").strip() or "\U0001F6E1\uFE0F"  # 🛡️
ALLIANCE_EMOJI_RAW = os.getenv("ALLIANCE_EMOJI", "<:GoldenPagoda_Emblem:1416905638428020877>").strip()


def _emoji_to_button_payload(raw: str) -> dict | None:
    """Parse a `<:name:id>` (or `<a:name:id>`) string into the Discord
    button-emoji payload `{"id": str, "name": str, "animated": bool}`.
    Returns None for unicode emojis (callers can just prefix the label)."""
    if not raw:
        return None
    m = re.match(r"^<(a?):([A-Za-z0-9_]{2,}):(\d{15,25})>$", raw)
    if not m:
        return None
    return {"id": m.group(3), "name": m.group(2), "animated": m.group(1) == "a"}


ALLIANCE_EMOJI_PAYLOAD = _emoji_to_button_payload(ALLIANCE_EMOJI_RAW)


def _pass_components(
    profile: str,
    clan: str | None,
    *,
    clan_emoji: str | None = None,
    mastery_rank: str | None = None,
    link_buttons: list[tuple[str, str]] | None = None,
    progress_attachment: str | None = None,
    nick_suggestion: str | None = None,
    user_id: int | None = None,
) -> list[dict]:
    emoji = (clan_emoji or "").strip() or CLAN_EMOJI
    display_clan = _strip_clan_tag(clan) if clan else None
    clan_part = f"{emoji} **{display_clan}**" if display_clan else f"{emoji} *Unaffiliated*"
    # Profile names normally render without the #NNN discriminator for
    # cleaner display. The "Tenno #<member_count>" fallback (used when
    # OCR can't read the real handle) intentionally keeps the suffix so
    # the response shows something unique per server.
    if profile.startswith("Tenno #"):
        display_profile = profile
    else:
        display_profile = _strip_clan_tag(profile)
    # In-game name shown as the heading inside the container, with clan
    # directly underneath so the verified identity reads top-to-bottom.
    inner_lines = [f"### \u2705  `{display_profile}`", clan_part]
    if mastery_rank:
        inner_lines.append(f"-# > {mastery_rank}")
    inner = "\n".join(inner_lines)
    container_children: list[dict] = [{"type": 10, "content": inner}]
    if link_buttons:
        button_payloads: list[dict] = []
        for label, url in link_buttons[:5]:
            btn: dict = {"type": 2, "style": 5, "label": label, "url": url}
            if label == "Clan Chat" and ALLIANCE_EMOJI_PAYLOAD is not None:
                btn["emoji"] = ALLIANCE_EMOJI_PAYLOAD
            button_payloads.append(btn)
        container_children.append({"type": 1, "components": button_payloads})
    container = {
        "type": 17,
        "accent_color": ACCENT_PASS,
        "components": container_children,
    }
    top_level: list[dict] = []
    # Progress card pinned to the TOP of the message (above the verification
    # card) so the bar / count is the first thing the user sees.
    if progress_attachment:
        top_level.append({
            "type": 12,
            "items": [{"media": {"url": f"attachment://{progress_attachment}"}}],
        })
    top_level.append(container)
    # Inline nickname prompt as additional top-level components so the
    # whole verification flow lives in one message.
    if nick_suggestion and user_id is not None:
        top_level.extend(_nickname_prompt_top_level(nick_suggestion, user_id))
    return top_level


def _channel_url(guild_id: int, channel_id: int) -> str:
    return f"https://discord.com/channels/{guild_id}/{channel_id}"


def _resolve_pass_link_buttons(
    guild: discord.Guild | None, clan_name: str | None
) -> list[tuple[str, str]]:
    """Return [(label, url), ...] for pass response buttons:
    1. Clan general chat — found by matching the clan's category and a 'general' channel.
    2. Info channel — PASS_INFO_CHANNEL_ID (label = its channel name).
    """
    buttons: list[tuple[str, str]] = []
    if guild is None:
        return buttons

    if clan_name:
        clean = _strip_clan_tag(clan_name).lower().strip()
        category = next(
            (c for c in guild.categories if clean and clean in c.name.lower()),
            None,
        )
        if category is None:
            logger.info(
                "No clan category matched for clan=%r (clean=%r); available=%s",
                clan_name,
                clean,
                [c.name for c in guild.categories],
            )
        else:
            # Prefer channels named like a general/clan-chat hangout. Falls
            # back to the first text channel in the category if no semantic
            # match (so the button still works for non-standard setups).
            keywords = ("general", "clan-chat", "clan chat", "chat", "lounge", "main", "hangout")
            general = next(
                (
                    ch
                    for kw in keywords
                    for ch in category.text_channels
                    if kw in ch.name.lower()
                ),
                None,
            )
            if general is None and category.text_channels:
                general = category.text_channels[0]
            if general is None:
                logger.info(
                    "Clan category %r matched but no chat channel found; available=%s",
                    category.name,
                    [ch.name for ch in category.text_channels],
                )
            else:
                buttons.append(
                    ("Clan Chat", _channel_url(guild.id, general.id))
                )

    info = guild.get_channel(PASS_EXTRA_CHANNEL_ID)
    if info is not None:
        label = f"#{info.name}"
        buttons.append((label, _channel_url(guild.id, info.id)))
    return buttons


def _fail_components(headline: str, reason: str, *, image_url: str | None = None) -> list[dict]:
    header = {
        "type": 10,
        "content": "> Verification Failed",
    }
    children: list[dict] = [
        {"type": 10, "content": f"-# {reason}"},
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
    outreach = " / ".join(f"<@&{rid}>" for rid in OUTREACH_ROLE_IDS) or "staff"
    header = {
        "type": 10,
        "content": "### \u26A0\uFE0F  Verification Incomplete\n-# Manual review required",
    }
    children: list[dict] = [
        {"type": 10, "content": f"-# {reason}"},
        {
            "type": 10,
            "content": f"-# {outreach} will reach out to verify.",
        },
    ]
    if image_url:
        children.insert(
            1,
            {
                "type": 12,
                "items": [{"media": {"url": image_url}}],
            },
        )
    if link_buttons:
        # Discord caps action rows at 5 buttons; we never exceed that here.
        children.append({
            "type": 1,
            "components": [
                {"type": 2, "style": 5, "label": label, "url": url}
                for label, url in link_buttons[:5]
            ],
        })
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
    top_level.extend([header, container])
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


def _nickname_prompt_top_level(suggestion: str, user_id: int) -> list[dict]:
    """Return top-level V2 components for the in-game-name Yes/No prompt.

    Both yes/no custom IDs carry the URL-encoded suggestion so the
    handler can address the user by their in-game name on either branch
    without cross-message state.
    """
    from urllib.parse import quote

    # Discord custom_id cap is 100 chars. Reserve the wider prefix
    # ("nick:y:<uid>:") so both yes and no IDs fit identically.
    prefix_len = len(f"nick:y:{user_id}:")
    max_encoded_len = 100 - prefix_len
    truncated = suggestion[:max_encoded_len]
    encoded = quote(truncated, safe="")
    while len(encoded) > max_encoded_len and truncated:
        truncated = truncated[:-1]
        encoded = quote(truncated, safe="")
    yes_id = f"nick:y:{user_id}:{encoded}"
    no_id = f"nick:n:{user_id}:{encoded}"
    return [
        {"type": 14},  # Separator
        {
            "type": 10,
            "content": (
                f"### Use your in-game name?\n"
                f"-# Set your server nickname to **{suggestion}**."
            ),
        },
        {
            "type": 1,
            "components": [
                {
                    "type": 2, "style": 3, "label": "Yes",
                    "custom_id": yes_id,
                },
                {
                    "type": 2, "style": 4, "label": "No",
                    "custom_id": no_id,
                },
            ],
        },
    ]


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


def _missing_categories(role_ids: set[int]) -> list[str]:
    return [name for name, has in _role_categories_for(role_ids) if not has]


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
            import aiohttp

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
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, data=form, headers=headers) as resp:
                    if resp.status >= 400:
                        text = await resp.text()
                        raise discord.HTTPException(resp, text)  # type: ignore[arg-type]
                    data = await resp.json()
        else:
            data = await client.http.request(route, json=payload)
        if isinstance(data, dict) and data.get("id"):
            try:
                sent_id = int(data["id"])
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

    if sent_id and REPLY_TTL_SECONDS > 0:
        task = asyncio.create_task(
            _delete_after(reply_to.channel.id, sent_id, REPLY_TTL_SECONDS)
        )
        _BG_TASKS.add(task)
        task.add_done_callback(_BG_TASKS.discard)


async def _delete_after(channel_id: int, message_id: int, delay: float) -> None:
    from discord.http import Route

    try:
        await asyncio.sleep(delay)
        route = Route(
            "DELETE",
            "/channels/{channel_id}/messages/{message_id}",
            channel_id=channel_id,
            message_id=message_id,
        )
        await client.http.request(route)
    except discord.NotFound:
        pass
    except discord.HTTPException:
        logger.exception("Failed to auto-delete reply %s", message_id)


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

    sample_clan = CLAN_SLOTS[0].clan_name if CLAN_SLOTS and CLAN_SLOTS[0].clan_name else "Golden Tenno"
    sample_emoji = (CLAN_SLOTS[0].emoji if CLAN_SLOTS else None) or CLAN_EMOJI

    samples = [
        _pass_components(
            "GoldenTenno#200",
            sample_clan,
            clan_emoji=sample_emoji,
            mastery_rank="MR 30",
            link_buttons=_resolve_pass_link_buttons(interaction.guild, sample_clan),
        ),
        _fail_components(
            "Profile name not found",
            "Could not read your in-game name from the screenshot. "
            "Please post a clear shot of your Warframe profile page.",
        ),
        _incomplete_components(
            f"No role for clan **{sample_clan}**.",
        ),
    ]

    route = Route(
        "POST",
        "/channels/{channel_id}/messages",
        channel_id=PREVIEW_CHANNEL_ID,
    )
    sent = 0
    errors: list[str] = []
    tasks = [
        client.http.request(
            route,
            json={
                "flags": COMPONENTS_V2_FLAG,
                "components": components,
                "allowed_mentions": {"parse": []},
            },
        )
        for components in samples
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for r in results:
        if isinstance(r, Exception):
            errors.append(str(r))
        else:
            sent += 1

    msg = f"\u2705 Posted {sent}/{len(samples)} samples to <#{PREVIEW_CHANNEL_ID}>."
    if errors:
        msg += "\n" + "\n".join(f"\u274C {e}" for e in errors)
    await interaction.response.send_message(msg, ephemeral=True)


# ---------- /status (paginated, ephemeral, V2) ------------------------------


EPHEMERAL_FLAG = 1 << 6  # 64


def _health_age() -> int | None:
    try:
        return int(time.time() - os.path.getmtime(HEALTH_PATH))
    except OSError:
        return None


def _fmt_uptime(seconds: float) -> str:
    seconds = int(seconds)
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
    return " ".join(parts)


def _status_page_bot(interaction: discord.Interaction) -> str:
    user = client.user
    latency_ms = int(client.latency * 1000) if client.latency >= 0 else -1
    uptime = _fmt_uptime(time.time() - BOT_START_TIME)
    hb = _health_age()
    if hb is None:
        hb_line = "\u26A0\uFE0F unhealthy (no signal)"
    elif hb > 90:
        hb_line = f"\u274C unhealthy ({hb}s stale)"
    else:
        hb_line = f"\u2705 healthy ({hb}s ago)"
    guilds = len(client.guilds)
    members = sum(g.member_count or 0 for g in client.guilds)
    py = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    return (
        f"**Bot**\n"
        f"-# User: `{user}` (`{getattr(user, 'id', '?')}`)\n"
        f"-# Latency: `{latency_ms} ms`\n"
        f"-# Uptime: `{uptime}`\n"
        f"-# Health: `{hb_line}`\n"
        f"-# Guilds: `{guilds}` \u2022 Members: `{members}`\n"
        f"-# Python: `{py}` \u2022 discord.py: `{discord.__version__}`"
    )


def _status_page_roles(interaction: discord.Interaction) -> str:
    head, tail = _status_page_roles_split(interaction)
    return f"{head}\n\n{tail}"


def _status_page_roles_split(
    interaction: discord.Interaction,
) -> tuple[str, str]:
    """Return (clan_slots_section, platform+special_roles_section).

    Used by `_status_components` to wedge the "Emblems" button
    directly under the Clan slots block, between the two text blobs.
    """
    clan_lines = ["**Clan slots**"]
    _placeholder_re = re.compile(r"^place[\s\-_]*holder", re.IGNORECASE)

    def _sort_key(slot):
        name = (slot.clan_name or "").strip()
        is_placeholder = 1 if _placeholder_re.match(name) else 0
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

    rest_lines.append("")
    rest_lines.append("**Special roles**")
    inc = f"<@&{INCOMPLETE_ROLE_ID}>" if INCOMPLETE_ROLE_ID else "*(unset)*"
    rem = f"<@&{VERIFY_REMOVE_ROLE_ID}>" if VERIFY_REMOVE_ROLE_ID else "*(unset)*"
    out = ", ".join(f"<@&{rid}>" for rid in OUTREACH_ROLE_IDS) or "*(none)*"
    rest_lines.append(f"-# Incomplete: {inc}")
    rest_lines.append(f"-# Remove on pass: {rem}")
    rest_lines.append(f"-# Outreach: {out}")
    return "\n".join(clan_lines), "\n".join(rest_lines)


def _status_page_channels(interaction: discord.Interaction) -> str:
    def fmt(cid: int) -> str:
        if not cid:
            return "*(unset)*"
        return f"<#{cid}> `{cid}`"
    return (
        f"**Channels**\n"
        f"-# Target: {fmt(TARGET_CHANNEL_ID)}\n"
        f"-# Pass info button: {fmt(PASS_INFO_CHANNEL_ID)}\n"
        f"-# Pass extra button: {fmt(PASS_EXTRA_CHANNEL_ID)}\n"
        f"-# Preview channel: {fmt(PREVIEW_CHANNEL_ID)}\n"
        f"-# Guild ID: `{GUILD_ID}`"
    )


def _status_page_misc(interaction: discord.Interaction) -> str:
    ocr = "OCR.space (engine 3)" if OCR_API_KEY else (
        "Tesseract (local)" if pytesseract else "*(none configured)*"
    )
    try:
        pillow_ver = importlib_metadata.version("Pillow")
    except importlib_metadata.PackageNotFoundError:
        pillow_ver = "?"
    try:
        numpy_ver = importlib_metadata.version("numpy")
    except importlib_metadata.PackageNotFoundError:
        numpy_ver = "?"
    last_seen = _load_catchup_state()
    catchup = (
        f"`{CATCHUP_LOOKBACK_HOURS}h` lookback \u2022 last id: "
        f"`{last_seen}`" if last_seen else
        f"`{CATCHUP_LOOKBACK_HOURS}h` lookback \u2022 last id: *(none)*"
    )
    return (
        f"**OCR / Misc**\n"
        f"-# OCR: `{ocr}`\n"
        f"-# Reply TTL: `{REPLY_TTL_SECONDS}s`\n"
        f"-# OCR max upload: `{OCR_MAX_UPLOAD_BYTES} bytes`\n"
        f"-# Pass reaction: `:{PASS_REACTION_NAME}:` ({PASS_REACTION_ID or '-'})\n"
        f"-# Pending reaction: `:{PENDING_REACTION_NAME}:` ({PENDING_REACTION_ID or '-'})\n"
        f"-# Fail reaction: {FAIL_REACTION}\n"
        f"-# Catch-up: {catchup}\n"
        f"-# Pillow: `{pillow_ver}` \u2022 NumPy: `{numpy_ver}`"
    )


_STATUS_PAGES: list[tuple[str, str, str, Callable]] = [
    ("bot",       "\U0001F916 Bot",          "Bot identity, latency, uptime.",  lambda i, _s: _status_page_bot(i)),
    ("roles",     "\U0001F6E1\uFE0F Roles",  "Configured roles + slot map.",    lambda i, _s: _status_page_roles(i)),
    ("channels",  "\U0001F4FA Channels",     "Target/info/preview channels.",   lambda i, _s: _status_page_channels(i)),
    ("misc",      "\U0001F527 OCR / Misc",   "OCR backend, TTL, reactions.",    lambda i, _s: _status_page_misc(i)),
    ("stats",     "\U0001F4C8 Stats",        "Verification totals + windows.",  lambda _i, s: _stats_page_overview(s)),
    ("clans",     "\U0001F3F0 Clans",        "Configured clans + member counts.", lambda i, _s: _status_page_clans(i)),
    ("ocr",       "\u23F1\uFE0F OCR Latency","OCR latency p50/p95/avg.",        lambda _i, s: _stats_page_ocr(s)),
]
_STATUS_PAGE_INDEX: dict[str, int] = {key: idx for idx, (key, *_rest) in enumerate(_STATUS_PAGES)}


def _status_components(interaction: discord.Interaction, page: int) -> list[dict]:
    page = max(0, min(page, len(_STATUS_PAGES) - 1))
    key, title, _desc, builder = _STATUS_PAGES[page]
    snap = analytics.summary()  # cheap; reused for stats pages, ignored for live ones
    header = {
        "type": 10,
        "content": f"### \U0001F4CA  Status \u2014 {title}",
    }
    nav_buttons = [
        {"type": 2, "style": 2, "label": "\u25C0 Prev",
         "custom_id": f"status:{page - 1}", "disabled": page == 0},
        {"type": 2, "style": 2,
         "label": f"{page + 1}/{len(_STATUS_PAGES)}",
         "custom_id": "status:noop", "disabled": True},
        {"type": 2, "style": 2, "label": "Next \u25B6",
         "custom_id": f"status:{page + 1}",
         "disabled": page >= len(_STATUS_PAGES) - 1},
        {"type": 2, "style": 1, "label": "\U0001F504 Refresh",
         "custom_id": f"status:{page}"},
    ]
    if key == "roles":
        # Wedge the Emblems button between Clan slots and the rest
        # of the Roles page so it lives directly under the clan listing.
        clan_text, rest_text = _status_page_roles_split(interaction)
        assign_row = {
            "type": 1,
            "components": [
                {"type": 2, "style": 3,
                 "label": "Emblems",
                 "custom_id": "status:assign_emblems"},
            ],
        }
        container_components = [
            {"type": 10, "content": clan_text},
            assign_row,
            {"type": 10, "content": rest_text},
            {"type": 1, "components": nav_buttons},
        ]
    else:
        body = builder(interaction, snap)
        container_components = [
            {"type": 10, "content": body},
            {"type": 1, "components": nav_buttons},
        ]
    container = {
        "type": 17,
        "accent_color": ACCENT_PASS,
        "components": container_components,
    }
    return [header, container]


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
    components = _status_components(interaction, page)
    try:
        await _interaction_callback(interaction, 4, components)
    except Exception:
        logger.exception("status page %s failed", page)
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "\u274C Failed to render status.", ephemeral=True
            )


# /status — single ephemeral command. The Components V2 message paginates
# through every page (bot, roles, channels, misc, stats, platforms, clans,
# ocr) via the Prev/Next buttons, so subcommands are unnecessary.
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
_PROGRESS_CARD_H = 200
_PROGRESS_BG = (30, 31, 34)            # #1E1F22 Discord dark
_PROGRESS_BG_EDGE = (24, 25, 28)       # subtle inner shadow
_PROGRESS_TRACK = (43, 45, 49)         # #2B2D31
_PROGRESS_FILL_START = (93, 208, 243)  # cyan
_PROGRESS_FILL_END = (134, 230, 168)   # mint — gradient end
_PROGRESS_FILL_GOLD = (212, 168, 87)   # gold for finished bars
_PROGRESS_TEXT = (236, 238, 240)
_PROGRESS_MUTED = (163, 166, 170)
_PROGRESS_AVATAR_SIZE = 144
_PROGRESS_AVATAR_RING = (212, 168, 87)


def _load_font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont:
    """Load DejaVu Sans at ``size``; fall back to PIL default on any error.

    DejaVu ships with ``fonts-dejavu-core`` (Debian) and is installed in
    the container image. The fallback keeps unit tests + dev environments
    without the package from crashing.
    """
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    candidates = [
        f"/usr/share/fonts/truetype/dejavu/{name}",
        f"/usr/share/fonts/dejavu/{name}",
        name,
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size=size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()  # type: ignore[return-value]


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
            width=4,
        )
        avatar.alpha_composite(ring)
        return avatar
    finally:
        if needs_close:
            src.close()


def _gradient_bar(
    width: int, height: int, progress: float, *, complete: bool
) -> Image.Image:
    """Render the rounded progress bar with a horizontal colour gradient."""
    bar = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    radius = height // 2
    ImageDraw.Draw(bar).rounded_rectangle(
        (0, 0, width - 1, height - 1), radius=radius, fill=_PROGRESS_TRACK
    )
    if progress <= 0:
        return bar

    fill_w = max(height, int(round(width * progress)))

    grad = Image.new("RGBA", (fill_w, height), (0, 0, 0, 0))
    if complete:
        start, end = _PROGRESS_FILL_GOLD, _PROGRESS_FILL_END
    else:
        start, end = _PROGRESS_FILL_START, _PROGRESS_FILL_END
    pixels = grad.load()
    if fill_w > 1 and pixels is not None:
        for x in range(fill_w):
            t = x / (fill_w - 1)
            r = int(start[0] + (end[0] - start[0]) * t)
            g = int(start[1] + (end[1] - start[1]) * t)
            b = int(start[2] + (end[2] - start[2]) * t)
            for y in range(height):
                pixels[x, y] = (r, g, b, 255)

    fill_mask = Image.new("L", (fill_w, height), 0)
    ImageDraw.Draw(fill_mask).rounded_rectangle(
        (0, 0, fill_w - 1, height - 1), radius=radius, fill=255
    )
    bar.paste(grad, (0, 0), fill_mask)
    return bar


def _render_progress_card_png(
    *,
    avatar_bytes: bytes | None,
    display_name: str,
    count: int,
    target: int,
) -> bytes:
    """Render an 860x200 progress card and return PNG bytes.

    Composition: circular avatar (left) + gradient bar with overlay text
    on the right. The whole composition is a single PNG so the V2 message
    only needs one media gallery item.
    """
    progress = max(0.0, min(1.0, count / target)) if target > 0 else 0.0
    complete = count >= target

    canvas = Image.new(
        "RGBA", (_PROGRESS_CARD_W, _PROGRESS_CARD_H), _PROGRESS_BG + (255,)
    )
    ImageDraw.Draw(canvas).rounded_rectangle(
        (0, 0, _PROGRESS_CARD_W - 1, _PROGRESS_CARD_H - 1),
        radius=22, outline=_PROGRESS_BG_EDGE + (255,), width=2,
    )

    pad = 28
    avatar = _circular_avatar(avatar_bytes, _PROGRESS_AVATAR_SIZE)
    avatar_y = (_PROGRESS_CARD_H - _PROGRESS_AVATAR_SIZE) // 2

    # Soft drop shadow under the avatar.
    shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).ellipse(
        (
            pad + 6, avatar_y + _PROGRESS_AVATAR_SIZE - 10,
            pad + _PROGRESS_AVATAR_SIZE - 6,
            avatar_y + _PROGRESS_AVATAR_SIZE + 14,
        ),
        fill=(0, 0, 0, 120),
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(7))
    canvas.alpha_composite(shadow)
    canvas.alpha_composite(avatar, (pad, avatar_y))

    text_x = pad + _PROGRESS_AVATAR_SIZE + 28
    right_x = _PROGRESS_CARD_W - pad
    draw = ImageDraw.Draw(canvas)

    name_font = _load_font(34, bold=True)
    label_font = _load_font(18, bold=True)
    count_font = _load_font(26, bold=True)
    footer_font = _load_font(16)

    name = display_name or "Member"
    max_name_w = right_x - text_x - 10
    if draw.textlength(name, font=name_font) > max_name_w:
        while name and draw.textlength(
            name + "\u2026", font=name_font
        ) > max_name_w:
            name = name[:-1]
        name = name + "\u2026"
    draw.text((text_x, 24), name, font=name_font, fill=_PROGRESS_TEXT)

    count_text = f"{count} / {target}"
    count_w = draw.textlength(count_text, font=count_font)
    draw.text(
        (right_x - count_w, 78),
        count_text,
        font=count_font,
        fill=_PROGRESS_TEXT,
    )

    pct_label = f"{int(round(progress * 100))}%"
    draw.text(
        (text_x, 84),
        f"PROGRESS  \u2022  {pct_label}",
        font=label_font,
        fill=_PROGRESS_MUTED,
    )

    bar_h = 28
    bar_w = right_x - text_x
    bar_y = 124
    bar = _gradient_bar(bar_w, bar_h, progress, complete=complete)
    canvas.alpha_composite(bar, (text_x, bar_y))

    if complete:
        footer = "\u2605  Target reached!"
        footer_fill = _PROGRESS_AVATAR_RING
    else:
        remaining = max(0, target - count)
        footer = f"{remaining} more to reach the goal"
        footer_fill = _PROGRESS_MUTED
    draw.text(
        (text_x, bar_y + bar_h + 8),
        footer,
        font=footer_font,
        fill=footer_fill,
    )

    buf = io.BytesIO()
    canvas.convert("RGB").save(buf, format="PNG", optimize=True)
    return buf.getvalue()


async def _fetch_avatar_bytes(url: str) -> bytes | None:
    """Best-effort fetch of an avatar URL; returns None on any failure.

    Discord's CDN occasionally 403s requests that lack a recognisable
    User-Agent, so we send one explicitly. Returns the full response body
    (avatars are <512 KiB; aiohttp's resp.read() handles transfer-encoded
    chunks correctly where resp.content.read(n) can return short reads).
    """
    try:
        import aiohttp

        headers = {
            "User-Agent": "DiscordBot (https://github.com/aidenlong04/Golden-Pagoda-Image-Reader, 1.0)",
            "Accept": "image/png,image/webp,image/*;q=0.8",
        }
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.warning(
                        "progress: avatar fetch returned HTTP %s for %s",
                        resp.status, url,
                    )
                    return None
                data = await resp.read()
                return data or None
    except Exception:
        logger.warning("progress: avatar fetch failed", exc_info=True)
        return None


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
        icon = "\u26A0\uFE0F"
        body = "-# No verification categories are configured for this server."
        accent = ACCENT_INCOMPLETE
    else:
        icon = "\u26A0\uFE0F"
        bullets = ", ".join(f"**{m}**" for m in missing)
        body = f"-# Missing: {bullets}"
        accent = ACCENT_INCOMPLETE

    safe_name = _strip_clan_tag(display_name) or display_name
    header = {"type": 10, "content": f"### {icon}  `{safe_name}`"}
    container_children: list[dict] = [
        {"type": 10, "content": body},
        {"type": 10, "content": f"-# Progress: {have}/{total}"},
    ]
    if link_buttons and not complete:
        container_children.append({
            "type": 1,
            "components": [
                {"type": 2, "style": 5, "label": label, "url": url}
                for label, url in link_buttons[:5]
            ],
        })
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


async def _handle_nick_interaction(
    interaction: discord.Interaction, custom_id: str
) -> None:
    """Handle the in-game-name Yes/No buttons posted after verification.

    On either choice we UPDATE_MESSAGE the original verification reply so
    the prompt is replaced with a confirmation block addressing the user
    by their in-game name. Omitting ``attachments`` from the edit payload
    preserves the existing progress-card attachment.
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

    # Default username for the confirmation header is the in-game name
    # carried in the custom_id; fall back to the member's display name
    # if we don't have one.
    username = suggestion or interaction.user.display_name

    if action == "y":
        if (
            suggestion
            and interaction.guild
            and isinstance(interaction.user, discord.Member)
        ):
            try:
                await interaction.user.edit(
                    nick=suggestion,
                    reason="In-game name applied via verification prompt",
                )
            except discord.Forbidden:
                logger.info("nick: forbidden setting %s", suggestion)
            except discord.HTTPException:
                logger.exception("nick: edit failed")
        selection_line = "-# In-game alias selected"
        accent = ACCENT_PASS
    else:
        selection_line = "-# Base Discord alias selected"
        accent = ACCENT_INCOMPLETE

    text = f"### Understood Tenno - {username}\n{selection_line}"
    components = [
        {
            "type": 17,
            "accent_color": accent,
            "components": [{"type": 10, "content": text}],
        }
    ]
    try:
        # type 7 = UPDATE_MESSAGE; non-ephemeral so the original public
        # message is edited in place.
        await _interaction_callback(
            interaction, 7, components, ephemeral=False,
        )
    except Exception:
        logger.exception("nick: update message failed")


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
            f"### <:GoldenPagoda_Emblem:1416905638428020877>  Emblems\n"
            f"Click {mention} to set or clear a clan emoji.\n"
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


def _fmt_age(ts: int | None) -> str:
    if not ts:
        return "*(none)*"
    delta = max(0, int(time.time()) - int(ts))
    if delta < 60:
        return f"{delta}s ago"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


def _fmt_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 ** 2:
        return f"{n / 1024:.1f} KiB"
    return f"{n / 1024 ** 2:.1f} MiB"


def _stats_page_overview(s: dict) -> str:
    if not s.get("available"):
        return (
            "**Analytics**\n"
            "-# Storage unavailable.\n"
            f"-# DB path: `{s.get('db_path')}`\n"
            "-# Mount `/opt/golden-pagoda/data:/app/data` to enable."
        )
    total = s["total"]
    by = s["by_outcome"]
    p = by.get("pass", 0)
    f = by.get("fail", 0)
    inc = by.get("incomplete", 0)
    unr = by.get("unreadable", 0)
    err = by.get("ocr_error", 0)
    pct = lambda n: f"{(n / total * 100):.1f}%" if total else "-"  # noqa: E731

    win = s["windows"]
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
        f"-# Last 30d: `{win.get('30d', 0)}`\n"
        f"-# First seen: {_fmt_age(s.get('first_ts'))}\n"
        f"-# Last seen: {_fmt_age(s.get('last_ts'))}\n"
        f"-# DB size: `{_fmt_bytes(s.get('db_size_bytes', 0))}`"
    )


def _status_page_clans(interaction: discord.Interaction | None) -> str:
    guild = interaction.guild if interaction else None
    if guild is None:
        return "**Clans**\n-# No guild context."
    configured = [s for s in CLAN_SLOTS if s.clan_name]
    if not configured:
        return "**Clans**\n-# No clan slots configured."

    rows: list[tuple[str, str, int, bool]] = []  # (label, glyph, members, missing_role)
    for slot in configured:
        role = guild.get_role(slot.role_id) if slot.role_id else None
        members = len(role.members) if role else 0
        glyph = slot.emoji or "\u2022"
        rows.append((slot.clan_name, glyph, members, role is None))

    rows.sort(key=lambda r: (-r[2], r[0].lower()))

    lines = [f"**Clans** ({len(rows)} configured)"]
    for name, glyph, members, missing in rows:
        suffix = " \u26A0\uFE0F missing role" if missing else ""
        lines.append(f"-# {glyph} `{name}` \u2014 `{members}` members{suffix}")
    return "\n".join(lines)


def _stats_page_ocr(s: dict) -> str:
    ocr = s.get("ocr") or {}
    engines = ocr.get("engines") or []
    lines = ["**OCR latency** (last 500 events)"]
    if ocr.get("samples"):
        lines.append(f"-# Samples: `{ocr['samples']}`")
        lines.append(f"-# Avg: `{ocr['avg_ms']} ms`")
        lines.append(f"-# p50: `{ocr['p50_ms']} ms`")
        lines.append(f"-# p95: `{ocr['p95_ms']} ms`")
    else:
        lines.append("-# No samples yet.")
    lines.append("")
    lines.append("**Engines**")
    if engines:
        for name, count in engines:
            lines.append(f"-# `{name}` \u2014 `{count}`")
    else:
        lines.append("-# No data.")
    return "\n".join(lines)



if __name__ == "__main__":
    # log_handler=None disables discord.py's own setup_logging so we don't
    # get duplicate handlers on the root + `discord` loggers (which would
    # double every log line emitted by discord.py).
    client.run(DISCORD_TOKEN, log_handler=None)
