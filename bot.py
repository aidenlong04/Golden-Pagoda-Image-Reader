from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import os
import re
import time
from collections.abc import Callable
from pathlib import Path

import discord
import requests
from discord import app_commands
from PIL import Image

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
    detect_platform,
    find_clan_slot,
    load_default_references,
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

# Directory where reference platform icons are cached on disk.
PLATFORM_ICON_DIR = Path(os.getenv("PLATFORM_ICON_DIR", "icons"))

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
REPLY_TTL_SECONDS = _int_env("REPLY_TTL_SECONDS", 120)

# URL of an image shown in the failure card when the platform icon can't be
# detected (helps the user spot where the icon should appear).
ICON_EXAMPLE_URL = os.getenv(
    "ICON_EXAMPLE_URL",
    "https://ik.imagekit.io/qcxbyrkgu/image_2026-05-20_154003027.png",
).strip()

# Role removed from a member on successful verification (e.g. an "unverified"
# gate role). Set to 0 to disable.
VERIFY_REMOVE_ROLE_ID = _int_env("VERIFY_REMOVE_ROLE_ID", 1459326361968574555)

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

    ENV_FILE_PATH.write_text("\n".join(lines) + "\n")
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

    ENV_FILE_PATH.write_text("\n".join(lines) + "\n")
    return True


# ---------- Platform reference icons ----------------------------------------

try:
    PLATFORM_ICONS = load_default_references(PLATFORM_ICON_DIR)
    logger.info("Loaded %d platform reference icons", len(PLATFORM_ICONS))
except Exception:
    logger.exception("Failed to load platform reference icons")
    PLATFORM_ICONS = {}


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


async def _health_task() -> None:
    while True:
        try:
            with open(HEALTH_PATH, "w") as fh:
                fh.write(str(int(time.time())))
        except OSError:
            logger.exception("health write failed")
        await asyncio.sleep(HEALTH_INTERVAL)


@client.event
async def on_ready() -> None:
    logger.info("Logged in as %s", client.user)
    if not getattr(client, "_health_started", False):
        client.loop.create_task(_health_task())
        client._health_started = True  # type: ignore[attr-defined]
    _sync_clan_slots_from_guilds()
    _sync_platform_roles_from_guilds()
    try:
        synced = await tree.sync()
        logger.info("Synced %d slash command(s)", len(synced))
        _COMMAND_IDS.clear()
        for cmd in synced:
            _COMMAND_IDS[cmd.name] = cmd.id
    except Exception:
        logger.exception("Failed to sync slash commands")


def _sync_platform_roles_from_guilds() -> None:
    """Resolve each platform's role ID against the server's role list and
    write the IDs back to .env. Runs on every reconnect.
    """
    if not client.guilds:
        return
    changed = False
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
            changed = True
    if changed:
        try:
            _update_env_platform_ids(PLATFORM_ROLE_IDS)
        except Exception:
            logger.exception("Failed to update %s", ENV_FILE_PATH)


def _sync_clan_slots_from_guilds() -> None:
    """For each guild the bot is in, resolve clan slot names/IDs against the
    server's role list and update the slot cache + .env file. Runs every
    time the bot reconnects — zero manual intervention required.
    """
    if not client.guilds:
        return
    changed = False
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
            slot.clan_name = resolved.name
            slot.role_id = resolved.id
            changed = True
    if changed:
        try:
            _update_env_clan_slots(CLAN_SLOTS)
        except Exception:
            logger.exception("Failed to update %s", ENV_FILE_PATH)


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


def _find_platform_role(guild: discord.Guild, platform: str) -> discord.Role | None:
    role_id = PLATFORM_ROLE_IDS.get(platform)
    if role_id:
        role = guild.get_role(role_id)
        if role is not None:
            return role
    return _find_role(guild, *PLATFORM_ROLE_ALIASES.get(platform, (platform,)))


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
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=OCR_RECOMPRESS_QUALITY, optimize=True)
        shrunk = buf.getvalue()
    except Exception:
        logger.exception("Failed to recompress image for OCR; sending original")
        return image_bytes, filename, content_type or "image/png"
    base = filename.rsplit(".", 1)[0] or "screenshot"
    logger.info("Recompressed %s for OCR: %d -> %d bytes", filename, len(image_bytes), len(shrunk))
    return shrunk, f"{base}.jpg", "image/jpeg"


def _ocr_via_api(
    image_bytes: bytes, filename: str, content_type: str
) -> tuple[str, list[tuple[str, tuple[int, int, int, int]]]]:
    image_bytes, filename, content_type = _shrink_for_ocr(
        image_bytes, filename, content_type
    )
    response = requests.post(
        OCR_API_URL,
        headers={"apikey": OCR_API_KEY},
        data={
            "OCREngine": OCR_ENGINE,
            "language": OCR_LANGUAGE,
            "scale": "true",
            "isTable": "false",
            "detectOrientation": "true",
            "isOverlayRequired": "true",
        },
        files={"file": (filename, image_bytes, content_type or "image/png")},
        timeout=60,
    )
    response.raise_for_status()
    payload = response.json()
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


def _ocr(
    image_bytes: bytes, filename: str, content_type: str
) -> tuple[str, list[tuple[str, tuple[int, int, int, int]]]]:
    if OCR_API_KEY:
        return _ocr_via_api(image_bytes, filename, content_type)
    if pytesseract is None:
        raise RuntimeError(
            "No OCR backend available: set OCR_API_KEY or install pytesseract."
        )
    text = pytesseract.image_to_string(
        Image.open(io.BytesIO(image_bytes)), config=TESSERACT_CONFIG
    )
    return text, []


_PROFILE_TOKEN_RE = re.compile(r"#\d{2,4}")


def _profile_name_bbox(
    words: list[tuple[str, tuple[int, int, int, int]]],
) -> tuple[int, int, int, int] | None:
    """Return the union bbox of words forming the profile name (handle + #NNN)."""
    if not words:
        return None
    # Locate the word that contains the '#NNN' suffix; the handle may be split
    # across one or two adjacent words on the same line.
    for idx, (text, _bbox) in enumerate(words):
        if _PROFILE_TOKEN_RE.search(text or ""):
            cluster = [words[idx]]
            tail_top = words[idx][1][1]
            tail_bottom = words[idx][1][3]
            line_h = tail_bottom - tail_top
            for prev in reversed(words[:idx]):
                _ptext, pbbox = prev
                if abs(pbbox[1] - tail_top) > line_h:
                    break
                if pbbox[2] < cluster[0][1][0] - line_h * 2:
                    break
                cluster.insert(0, prev)
                if len(cluster) >= 3:
                    break
            xs = [b[0] for _, b in cluster] + [b[2] for _, b in cluster]
            ys = [b[1] for _, b in cluster] + [b[3] for _, b in cluster]
            return (min(xs), min(ys), max(xs), max(ys))
    return None


# ---------- Screenshot processing -------------------------------------------


def _first_image_attachment(message: discord.Message) -> discord.Attachment | None:
    for attachment in message.attachments:
        if attachment.content_type and attachment.content_type.startswith("image/"):
            return attachment
    return None


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
        emoji = FAIL_REACTION
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
    await _send_v2(
        message,
        _fail_components(headline, reason, image_url=image_url),
        mention_user=True,
    )


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


_PLATFORM_GLYPH_FALLBACK = {
    "PC": "\U0001F5A5\uFE0F",         # 🖥️
    "Xbox": "\U0001F7E2",             # 🟢
    "PlayStation": "\U0001F535",      # 🔵
    "Switch": "\U0001F534",           # 🔴
    "Mobile": "\U0001F4F1",           # 📱
}

_PLATFORM_EMOJI_ENV = {
    "PC": "PLATFORM_EMOJI_PC",
    "Xbox": "PLATFORM_EMOJI_XBOX",
    "PlayStation": "PLATFORM_EMOJI_PLAYSTATION",
    "Switch": "PLATFORM_EMOJI_SWITCH",
    "Mobile": "PLATFORM_EMOJI_MOBILE",
}

CLAN_EMOJI = os.getenv("CLAN_EMOJI", "").strip() or "\U0001F6E1\uFE0F"  # 🛡️


def _platform_glyph(platform: str | None) -> str:
    if not platform:
        return "\U0001F3AE"  # 🎮
    env_key = _PLATFORM_EMOJI_ENV.get(platform)
    custom = (os.getenv(env_key) or "").strip() if env_key else ""
    return custom or _PLATFORM_GLYPH_FALLBACK.get(platform, "\U0001F3AE")


def _pass_components(
    profile: str,
    platform: str | None,
    clan: str | None,
    role_lines: list[str],  # kept for API parity / preview
    *,
    clan_emoji: str | None = None,
    mastery_rank: str | None = None,
    link_buttons: list[tuple[str, str]] | None = None,
) -> list[dict]:
    emoji = (clan_emoji or "").strip() or CLAN_EMOJI
    display_clan = _strip_clan_tag(clan) if clan else None
    clan_part = f"{emoji} **{display_clan}**" if display_clan else f"{emoji} *Unaffiliated*"
    plat_emoji = _platform_glyph(platform)
    plat_part = (
        f"**{platform}** {plat_emoji}" if platform else f"*Unknown* {plat_emoji}"
    )
    display_profile = _strip_clan_tag(profile)
    header = {"type": 10, "content": f"### \u2705  `{display_profile}`"}
    inner_lines = [clan_part, f"> {plat_part}"]
    if mastery_rank:
        inner_lines.append(f"-# > {mastery_rank}")
    inner = "\n".join(inner_lines)
    container_children: list[dict] = [{"type": 10, "content": inner}]
    if link_buttons:
        container_children.append(
            {
                "type": 1,
                "components": [
                    {"type": 2, "style": 5, "label": label, "url": url}
                    for label, url in link_buttons[:5]
                ],
            }
        )
    container = {
        "type": 17,
        "accent_color": ACCENT_PASS,
        "components": container_children,
    }
    return [header, container]


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
        if category is not None:
            general = next(
                (
                    ch
                    for ch in category.text_channels
                    if "general" in ch.name.lower()
                ),
                None,
            )
            if general is not None:
                buttons.append(
                    ("Clan General Chat", _channel_url(guild.id, general.id))
                )

    info = guild.get_channel(PASS_INFO_CHANNEL_ID)
    if info is not None:
        label = f"#{info.name}"
        buttons.append((label, _channel_url(guild.id, info.id)))
    extra = guild.get_channel(PASS_EXTRA_CHANNEL_ID)
    if extra is not None:
        buttons.append((f"#{extra.name}", _channel_url(guild.id, extra.id)))
    return buttons


def _fail_components(headline: str, reason: str, *, image_url: str | None = None) -> list[dict]:
    header = {
        "type": 10,
        "content": f"### \u274C  Verification Failed\n-# {headline}",
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


def _incomplete_components(reason: str, *, image_url: str | None = None) -> list[dict]:
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
    return [
        header,
        {
            "type": 17,
            "accent_color": ACCENT_INCOMPLETE,
            "components": children,
        },
    ]


async def _send_v2(
    reply_to: discord.Message,
    components: list[dict],
    *,
    mention_user: bool = False,
    allow_role_mentions: bool = False,
) -> None:
    """Send a Components V2 message as a reply via raw HTTP (discord.py 2.x has no native v2)."""
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

    route = Route(
        "POST",
        "/channels/{channel_id}/messages",
        channel_id=reply_to.channel.id,
    )
    sent_id: int | None = None
    try:
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

    attachment = _first_image_attachment(message)
    if attachment is None:
        await _fail(message, "Not an image", "Upload a PNG/JPG screenshot of your Warframe profile.")
        return
    if message.guild is None:
        await _fail(message, "Server only", "I can only assign roles in a server channel.")
        return

    try:
        image_bytes = await attachment.read()
        image = Image.open(io.BytesIO(image_bytes))
    except Exception:
        logger.exception("Failed to read uploaded image")
        await _fail(message, "Invalid image", "Image could not be opened. Re-upload a valid PNG/JPG.")
        return

    ocr_engine = "ocr.space" if OCR_API_KEY else ("tesseract" if pytesseract else "none")
    ocr_started = time.monotonic()
    try:
        ocr_text_raw, ocr_words = _ocr(
            image_bytes,
            attachment.filename,
            attachment.content_type or "image/png",
        )
        ocr_text = ocr_text_raw.strip()
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

    profile_name = parse_profile_name(ocr_text)
    clan_name = parse_clan_name(ocr_text)
    mastery_rank = parse_mastery_rank(ocr_text)

    anchor_bbox = _profile_name_bbox(ocr_words) if profile_name else None
    platform, platform_scores = detect_platform(image, anchor_bbox)

    if not profile_name or not platform:
        analytics.record_verification(
            outcome="unreadable",
            platform=platform,
            clan=clan_name,
            ocr_engine=ocr_engine,
            ocr_latency_ms=ocr_latency_ms,
            user_id=message.author.id,
            guild_id=message.guild.id,
            platform_scores=platform_scores,
        )
        await _fail(
            message,
            "Profile not found",
            "Make sure your title bar (PlayerName#NNN) and platform icon are visible at the top.",
            image_url=ICON_EXAMPLE_URL or None,
        )
        return

    member = message.author if isinstance(message.author, discord.Member) else None
    if member is None:
        await _fail(message, "Not a member", "I can only assign roles to server members.")
        return

    role_lines: list[str] = []
    issues: list[str] = []
    passed = True

    role = _find_platform_role(message.guild, platform)
    if role is None:
        issues.append(f"No role for platform **{platform}**.")
        passed = False
    else:
        _, status = await _add_role(member, role, "Screenshot platform verification")
        role_lines.append(f"Platform: {status}")

    clan_emoji: str | None = None
    if clan_name:
        slot = find_clan_slot(CLAN_SLOTS, clan_name)
        if slot is not None:
            clan_emoji = slot.emoji
        role = _find_clan_role(message.guild, clan_name)
        if role is None:
            issues.append(f"No role for clan **{_strip_clan_tag(clan_name)}**.")
            passed = False
        else:
            _, status = await _add_role(member, role, "Screenshot clan verification")
            role_lines.append(f"Clan: {status}")
    else:
        issues.append("Clan shown as Unaffiliated — no matching server clan role.")
        passed = False

    await _react(message, "pass" if passed else "incomplete")
    if passed:
        await _remove_unverified_role(member)
        await _send_v2(
            message,
            _pass_components(
                profile_name, platform, clan_name, role_lines,
                clan_emoji=clan_emoji,
                mastery_rank=mastery_rank,
                link_buttons=_resolve_pass_link_buttons(message.guild, clan_name),
            ),
        )
    else:
        await _add_incomplete_role(member)
        components = _incomplete_components(" ".join(issues))
        await _send_v2(message, components, mention_user=True, allow_role_mentions=True)

    analytics.record_verification(
        outcome="pass" if passed else "incomplete",
        platform=platform,
        clan=clan_name,
        ocr_engine=ocr_engine,
        ocr_latency_ms=ocr_latency_ms,
        user_id=member.id,
        guild_id=message.guild.id,
        platform_scores=platform_scores,
    )


# ---------- Slash commands --------------------------------------------------

_CUSTOM_EMOJI_RE = re.compile(r"^<a?:[A-Za-z0-9_]{2,}:\d{15,25}>$")


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
    if _CUSTOM_EMOJI_RE.match(s):
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
)
@app_commands.default_permissions(manage_guild=True)
async def clan_emblems(
    interaction: discord.Interaction,
    role: discord.Role,
    emoji: str = "",
) -> None:
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
            "PC",
            sample_clan,
            [],
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
    return (
        f"**Bot**\n"
        f"-# User: `{user}` (`{getattr(user, 'id', '?')}`)\n"
        f"-# Latency: `{latency_ms} ms`\n"
        f"-# Uptime: `{uptime}`\n"
        f"-# Health: `{hb_line}`\n"
        f"-# Guilds: `{guilds}` \u2022 Members: `{members}`"
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
    for s in CLAN_SLOTS:
        name = s.clan_name or "*(unset)*"
        rid = s.role_id or 0
        emoji = s.emoji or ""
        mention = f"<@&{rid}>" if rid else "*(no role)*"
        clan_lines.append(f"-# {emoji} `{s.slot}` {name} \u2192 {mention}")

    rest_lines = ["**Platform roles**"]
    for plat in PLATFORM_ROLE_ID_ENV_KEYS:
        rid = PLATFORM_ROLE_IDS.get(plat) or 0
        glyph = _platform_glyph(plat)
        mention = f"<@&{rid}>" if rid else "*(unset)*"
        rest_lines.append(f"-# {glyph} {plat} \u2192 {mention}")

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
    icons = len(PLATFORM_ICONS)
    return (
        f"**OCR / Misc**\n"
        f"-# OCR: `{ocr}`\n"
        f"-# Reference icons loaded: `{icons}`\n"
        f"-# Reply TTL: `{REPLY_TTL_SECONDS}s`\n"
        f"-# OCR max upload: `{OCR_MAX_UPLOAD_BYTES} bytes`\n"
        f"-# Pass reaction: `:{PASS_REACTION_NAME}:` ({PASS_REACTION_ID or '-'})\n"
        f"-# Pending reaction: `:{PENDING_REACTION_NAME}:` ({PENDING_REACTION_ID or '-'})\n"
        f"-# Fail reaction: {FAIL_REACTION}"
    )


_STATUS_PAGES: list[tuple[str, str, str, Callable]] = [
    ("bot",       "\U0001F916 Bot",          "Bot identity, latency, uptime.",  lambda i, _s: _status_page_bot(i)),
    ("roles",     "\U0001F6E1\uFE0F Roles",  "Configured roles + slot map.",    lambda i, _s: _status_page_roles(i)),
    ("channels",  "\U0001F4FA Channels",     "Target/info/preview channels.",   lambda i, _s: _status_page_channels(i)),
    ("misc",      "\U0001F527 OCR / Misc",   "OCR backend, TTL, reactions.",    lambda i, _s: _status_page_misc(i)),
    ("stats",     "\U0001F4C8 Stats",        "Verification totals + windows.",  lambda _i, s: _stats_page_overview(s)),
    ("platforms", "\U0001F3AE Platforms",    "Verifications by platform.",      lambda _i, s: _stats_page_platforms(s)),
    ("clans",     "\U0001F3F0 Clans",        "Top clans by verification.",      lambda _i, s: _stats_page_clans(s)),
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
) -> None:
    from discord.http import Route

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
                "flags": EPHEMERAL_FLAG | COMPONENTS_V2_FLAG,
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


@client.event
async def on_interaction(interaction: discord.Interaction) -> None:
    if interaction.type != discord.InteractionType.component:
        return
    data = interaction.data or {}
    custom_id = str(data.get("custom_id", ""))

    # Backwards-compatible: accept legacy "stats:N" buttons too.
    if custom_id.startswith("stats:"):
        custom_id = "status:" + custom_id.split(":", 1)[1]
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


def _stats_page_platforms(s: dict) -> str:
    rows = s.get("by_platform") or []
    if not rows:
        return "**Platforms**\n-# No data yet."
    lines = ["**Platforms**"]
    for name, count in rows:
        glyph = _platform_glyph(name) if name and name != "(unknown)" else "?"
        lines.append(f"-# {glyph} `{name}` \u2014 `{count}`")
    return "\n".join(lines)


def _stats_page_clans(s: dict) -> str:
    rows = s.get("by_clan") or []
    if not rows:
        return "**Clans**\n-# No data yet."
    lines = ["**Clans (top 10)**"]
    for name, count in rows:
        slot = next((c for c in CLAN_SLOTS if c.clan_name and name and c.clan_name.lower() == name.lower()), None)
        glyph = (slot.emoji if slot else "") or "\u2022"
        lines.append(f"-# {glyph} `{name}` \u2014 `{count}`")
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
