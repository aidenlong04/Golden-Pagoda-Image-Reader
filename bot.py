from __future__ import annotations

import asyncio
import io
import logging
import os
import re
from pathlib import Path

import discord
import requests
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
    detect_platform_from_image,
    detect_platform_near_anchor,
    find_clan_slot,
    load_default_references,
    parse_clan_name,
    parse_profile_name,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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
        slots.append(ClanSlot(slot=i, clan_name=name, role_id=role_id))
    return slots


def _update_env_clan_slots(slots: list[ClanSlot]) -> bool:
    """Rewrite the CLAN_ROLE_{i}_NAME/_ID entries in the .env file in place."""
    if not ENV_FILE_PATH.exists():
        return False

    by_slot = {s.slot: s for s in slots}
    lines = ENV_FILE_PATH.read_text().splitlines()
    seen: set[tuple[int, str]] = set()
    pattern = re.compile(r"^(\s*)CLAN_ROLE_(\d+)_(NAME|ID)\s*=.*$")

    for idx, line in enumerate(lines):
        m = pattern.match(line)
        if not m:
            continue
        indent, slot_num, field = m.group(1), int(m.group(2)), m.group(3)
        slot = by_slot.get(slot_num)
        if slot is None:
            continue
        if field == "NAME":
            value = slot.clan_name or ""
        else:
            value = str(slot.role_id) if slot.role_id else ""
        lines[idx] = f"{indent}CLAN_ROLE_{slot_num}_{field}={value}"
        seen.add((slot_num, field))

    missing: list[str] = []
    for i in sorted(by_slot):
        for field in ("NAME", "ID"):
            if (i, field) in seen:
                continue
            slot = by_slot[i]
            if field == "NAME":
                value = slot.clan_name or ""
            else:
                value = str(slot.role_id) if slot.role_id else ""
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


@client.event
async def on_ready() -> None:
    logger.info("Logged in as %s", client.user)
    _sync_clan_slots_from_guilds()
    _sync_platform_roles_from_guilds()


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
                    lambda r: _normalize(r.name) == want, guild.roles
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
                ptext, pbbox = prev
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
    role_lines: list[str],  # noqa: ARG001 — kept for API parity / preview
) -> list[dict]:
    plat = (
        f"{_platform_glyph(platform)} **{platform}**" if platform
        else f"{_platform_glyph(None)} *Unknown*"
    )
    clan_part = f"{CLAN_EMOJI} **{clan}**" if clan else f"{CLAN_EMOJI} *Unaffiliated*"
    body = (
        f"### \u2705  `{profile}`\n"
        f"{clan_part}  \u2022  {plat}"
    )
    return [_container(ACCENT_PASS, body)]


def _fail_components(headline: str, reason: str, *, image_url: str | None = None) -> list[dict]:
    children: list[dict] = [
        {"type": 10, "content": f"## Verification Failed\n{_quote(headline)}"},
        {"type": 14, "divider": True, "spacing": 1},
        {"type": 10, "content": f"```\n{reason}\n```"},
    ]
    if image_url:
        children.append(
            {
                "type": 12,  # MediaGallery
                "items": [{"media": {"url": image_url}}],
            }
        )
    return [
        {
            "type": 17,
            "accent_color": ACCENT_FAIL,
            "components": children,
        }
    ]


def _incomplete_components(reason: str, *, image_url: str | None = None) -> list[dict]:
    outreach = " / ".join(f"<@&{rid}>" for rid in OUTREACH_ROLE_IDS) or "staff"
    children: list[dict] = [
        {"type": 10, "content": f"## Verification Incomplete\n{_quote('Manual review required')}"},
        {"type": 14, "divider": True, "spacing": 1},
        {"type": 10, "content": f"```\n{reason}\n```"},
        {
            "type": 10,
            "content": f"{outreach} will reach out to verify.",
        },
    ]
    if image_url:
        children.insert(
            3,
            {
                "type": 12,
                "items": [{"media": {"url": image_url}}],
            },
        )
    return [
        {
            "type": 17,
            "accent_color": ACCENT_INCOMPLETE,
            "components": children,
        }
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
        asyncio.create_task(
            _delete_after(reply_to.channel.id, sent_id, REPLY_TTL_SECONDS)
        )


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

    try:
        ocr_text_raw, ocr_words = _ocr(
            image_bytes,
            attachment.filename,
            attachment.content_type or "image/png",
        )
        ocr_text = ocr_text_raw.strip()
    except Exception:
        logger.exception("OCR failed for uploaded image")
        await _fail(message, "Not readable", "No text could be read. Upload a clearer screenshot.")
        return

    profile_name = parse_profile_name(ocr_text)
    clan_name = parse_clan_name(ocr_text)

    anchor_bbox = _profile_name_bbox(ocr_words) if profile_name else None
    platform: str | None = None
    if anchor_bbox is not None:
        platform = detect_platform_near_anchor(image, anchor_bbox)
    if platform is None:
        platform = detect_platform_from_image(image, PLATFORM_ICONS or None)

    if not profile_name or not platform:
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

    if clan_name:
        role = _find_clan_role(message.guild, clan_name)
        if role is None:
            issues.append(f"No role for clan {clan_name}.")
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
        await _send_v2(message, _pass_components(profile_name, platform, clan_name, role_lines))
    else:
        await _add_incomplete_role(member)
        components = _incomplete_components(" ".join(issues))
        await _send_v2(message, components, mention_user=True, allow_role_mentions=True)


if __name__ == "__main__":
    client.run(DISCORD_TOKEN)
