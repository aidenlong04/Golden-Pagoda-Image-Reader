from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import os
import random
import re
import sys
import time
import warnings
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from typing import NamedTuple

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
from discord import app_commands  # noqa: E402
from PIL import Image  # noqa: E402

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
    _format_mastery_display,
    _mastery_label_value,
    find_clan_slot,
    parse_clan_name,
    parse_mastery_rank,
    parse_profile_name,
)
import analytics
import records_index
from config import _csv, _csv_ids, _float_env, _int_env
from gpbot.bootstrap import build_client_tree
from gpbot.components_v2 import (
    DISCORD_API_BASE as _DISCORD_API_BASE,
    interaction_callback as _v2_interaction_callback,
    interaction_edit_original_v2 as _v2_interaction_edit_original,
    v2_multipart_request as _v2_multipart_http,
)
from gpbot.concurrency import (
    get_or_create_lock,
    run_heavy_job,
    spawn_bg_task as _spawn_bg_task_impl,
)
from gpbot.discord_http import discord_call_with_retry
from gpbot.onboarding import (
    RepromptDecision,
    parse_onboard_custom_id,
    reprompt_decision,
)
from gpbot.records import (
    DISCORD_EPOCH_MS as _DISCORD_EPOCH_MS,
    RECORD_LINE_RE as _RECORD_LINE_RE,
    RECORD_PROFILE_LABELS as _RECORD_PROFILE_LABELS,
    collect_v2_text as _collect_v2_text,
    is_exact_mastery_rank as _is_exact_mastery_rank,
    parse_record_embed as _parse_record_embed,
    parse_record_profile_text as _parse_record_profile_text,
    snowflake_ts as _snowflake_ts,
)
from gpbot.routing import CustomIDRouter
from gpbot.verify import (
    VerifyResult as _VerifyResult,
    VerifyState,
    parse_mastery_token,
    validate_image_bytes,
)
from ocr_engine import OCR_API_KEY, OLLAMA_OCR_MODEL, _ocr
from envstore import (  # noqa: F401  (some re-exported for tests via bot.*)
    ENV_FILE_PATH,
    PLATFORM_ROLE_ID_ENV_KEYS,
    _rewrite_env_file,
    _update_env_clan_slots,
    _update_env_id_list,
    _update_env_platform_ids,
    _update_env_value,
)
# Card rendering lives in cards.py; re-exported for internal callers + tests.
from cards import (  # noqa: F401  (test-facing re-exports resolved as bot.*)
    _card_backdrop,
    _card_backdrop_cached,
    _circular_avatar,
    _ellipsize,
    _load_font,
    _pagoda_silhouette,
    _radial_gradient,
    _render_profile_card_png,
    _vignette,
    warm_font_cache,
)
from utils.metrics import heavy_semaphore_metrics, metrics_snapshot, ocr_latency

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


TARGET_CHANNEL_ID = _int_env("TARGET_CHANNEL_ID")

# Dedicated onboarding channel where the join-welcome prompt is posted.
# Defaults to TARGET_CHANNEL_ID when unset (backwards-compatible).
ONBOARDING_CHANNEL_ID = _int_env("ONBOARDING_CHANNEL_ID") or _int_env("TARGET_CHANNEL_ID")

# Channel where a V2 record (the member's uploaded screenshot) is posted after
# a member completes onboarding verification. 0 disables the record post.
MEMBER_RECORDS_CHANNEL_ID = _int_env("MEMBER_RECORDS_CHANNEL_ID")

# Platform name → list of acceptable Discord role-name aliases (case-insensitive).
PLATFORM_ROLE_ALIASES: dict[str, tuple[str, ...]] = {
    "PC": ("PC", "Windows", "Steam"),
    "Xbox": ("Xbox", "XBL", "Xbox Live"),
    "PlayStation": ("PlayStation", "PS", "PSN", "PS4", "PS5"),
    "Switch": ("Switch", "Nintendo", "Nintendo Switch"),
    "Mobile": ("Mobile", "iOS", "Android", "Apple"),
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


# Matches a custom Discord emoji literal: <:name:id> or <a:name:id>.
_CUSTOM_EMOJI_RE = re.compile(r"^<(a?):([A-Za-z0-9_~]+):(\d+)>$")


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

# Components V2 reply styling.
COMPONENTS_V2_FLAG = 1 << 15  # 32768 — IS_COMPONENTS_V2
# Suppress all automatic pings; callers add explicit user IDs when needed.
_ALLOWED_MENTIONS_NONE = {"parse": []}
ACCENT_PASS = _int_env("ACCENT_PASS", 0xD4A857)        # gold
ACCENT_FAIL = _int_env("ACCENT_FAIL", 0xED4245)        # red
ACCENT_INCOMPLETE = _int_env("ACCENT_INCOMPLETE", 0x99AAB5)  # grey

# Role granted to users whose screenshot was readable but couldn't be fully
# verified automatically (platform icon missing, unconfigured clan, etc).
# A staff member then manually completes verification.
INCOMPLETE_ROLE_ID = _int_env("INCOMPLETE_ROLE_ID", 1381447170229538917)

# Auto-delete bot replies after this many seconds (0 = keep forever).
REPLY_TTL_SECONDS = _int_env("REPLY_TTL_SECONDS", 180)

# Role removed from a member on successful verification (e.g. an "unverified"
# gate role). Set to 0 to disable.
VERIFY_REMOVE_ROLE_ID = _int_env("VERIFY_REMOVE_ROLE_ID", 1459326361968574555)

# Onboarding flow: welcome prompt + screenshot verification for new joins.
# Hours before a pending welcome prompt triggers a re-prompt (default: 5h).
ONBOARDING_REPROMPT_HOURS = _float_env("ONBOARDING_REPROMPT_HOURS", 5.0)
# Maximum number of re-prompts before giving up on a pending member
# (default: 3).
ONBOARDING_MAX_REPROMPTS = _int_env("ONBOARDING_MAX_REPROMPTS", 3)
# OCR submission failures before routing to manual review (default: 3).
ONBOARDING_MAX_OCR_FAILS = _int_env("ONBOARDING_MAX_OCR_FAILS", 3)
# How often (in seconds) the background reprompt loop polls for expired prompts.
ONBOARDING_POLL_SECONDS = _int_env("ONBOARDING_POLL_SECONDS", 600)

# Role IDs that count as "has MR verified" / "has joined a syndicate" for
# the progress completion check. Both accept a comma-separated list — a
# member counts as having the category if they hold ANY of the listed roles.
# Empty list disables the category (it stays at 0/0 and doesn't drag the
# completion percentage down).
#
# Operators normally configure these by NAME via MR_ROLE_NAMES /
# SYNDICATE_ROLE_NAMES: on every reconnect those names are resolved
# against each guild's role list and the IDs are written back to
# MR_ROLE_IDS / SYNDICATE_ROLE_IDS in .env. The _IDS vars are still
# the source of truth at runtime (and can be hand-edited as a fallback).
MR_ROLE_NAMES: list[str] = _csv(
    "MR_ROLE_NAMES",
    ",".join(
        [f"MR {n}" for n in range(1, 31)]
        + [f"Legendary {n}" for n in range(1, 9)]
    ),
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

# Custom emoji shown on the "Assign <category>" link buttons that appear on a
# /profile card when the member is missing Platform / Mastery Rank / Syndicate.
ASSIGN_ROLE_EMOJI_ID = _int_env("ASSIGN_ROLE_EMOJI_ID", 1416857287166918827)

# Roles permitted to make a /profile reply public (i.e. flip the `ephemeral`
# toggle off); server managers are always allowed. /profile itself is open to
# everyone and anyone may target any member — only the `ephemeral` toggle is
# gated, so members without one of these roles always get an ephemeral reply.
# The `edit_mastery` option is self-only regardless. Comma-separated; falls
# back to the baked-in IDs when unset.
PROFILE_OPTIONS_ROLE_IDS: list[int] = _csv_ids("PROFILE_OPTIONS_ROLE_IDS") or [
    1361846841934610563,
    1361846841934610564,
    1361846841934610565,
]


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


CLAN_SLOTS: list[ClanSlot] = _load_clan_slots()


# ---------- Discord client --------------------------------------------------

client, tree = build_client_tree()

BOT_START_TIME = time.time()
HEALTH_PATH = os.getenv("HEALTH_PATH", "./data/gp_health")
HEALTH_INTERVAL = _int_env("HEALTH_INTERVAL", 20)

# Populated after tree.sync(); used to render clickable slash-command mentions
# (`</name:id>`) inside ephemeral replies fired from component buttons.
_COMMAND_IDS: dict[str, int] = {}
_CUSTOM_ID_ROUTER = CustomIDRouter()
_CUSTOM_ID_ROUTES_REGISTERED = False

# Strong refs for fire-and-forget tasks. asyncio docs warn that
# create_task() return values must be kept alive or the task may be
# garbage-collected mid-await. We discard each task once it completes.
_BG_TASKS: set[asyncio.Task] = set()

# Caps how many heavy image jobs (supersampled card renders + OCR upscales)
# run concurrently. On the CX22 (512MB / 2-3 cores) several simultaneous
# verifications could otherwise each allocate tens of MB of RGBA buffers at
# once and spike toward the memory limit.
# Tunable via HEAVY_JOB_CONCURRENCY (default 2). Raise to 3-4 only after
# profiling confirms memory headroom; the semaphore_metrics snapshot on
# /status will show if requests are queuing.
HEAVY_JOB_CONCURRENCY: int = _int_env("HEAVY_JOB_CONCURRENCY", 2)
_HEAVY_JOB_SEMAPHORE = asyncio.Semaphore(HEAVY_JOB_CONCURRENCY)


async def _run_heavy(func, /, *args, **kwargs):
    """Run a CPU/memory-heavy callable in a worker thread, bounded by
    ``_HEAVY_JOB_SEMAPHORE`` so concurrent renders/OCR can't pile up and
    blow the 512MB container budget.

    Instruments ``heavy_semaphore_metrics`` (from utils.metrics) so the
    /status page can surface current/peak/queued counts and average wait.
    """
    return await run_heavy_job(
        _HEAVY_JOB_SEMAPHORE,
        heavy_semaphore_metrics,
        func,
        *args,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Discord API retry helper
# ---------------------------------------------------------------------------
# discord.py surfaces rate-limit errors as discord.HTTPException with status
# 429 and populates `retry_after`. The helper below wraps role-assignment /
# removal calls (the most common API calls in the hot path) with exponential
# back-off and jitter so a transient rate-limit never loses a role assignment.

_DISCORD_RETRY_MAX = _int_env("DISCORD_RETRY_MAX", 3)
_DISCORD_RETRY_BASE = 1.0
_DISCORD_RETRY_MAX_DELAY = 30.0


async def _discord_call_with_retry(coro_factory, /, *, label: str = "discord call") -> None:
    """Retry a Discord API call with exponential back-off.

    Parameters
    ----------
    coro_factory:
        Zero-argument async callable that creates the coroutine on each
        attempt (needed because a consumed coroutine cannot be re-awaited).
    label:
        Human-readable description for logging.
    """
    await discord_call_with_retry(
        coro_factory,
        label=label,
        logger=logger,
        max_attempts=_DISCORD_RETRY_MAX,
        base_delay=_DISCORD_RETRY_BASE,
        max_delay=_DISCORD_RETRY_MAX_DELAY,
    )

async def _health_task() -> None:
    # Liveness signal: touch HEALTH_PATH every HEALTH_INTERVAL seconds. The
    # watchdog treats a stale (>90s) file as unhealthy.
    while True:
        try:
            with open(HEALTH_PATH, "w") as fh:
                fh.write(str(int(time.time())))
        except OSError:
            logger.exception("health write failed")
        await asyncio.sleep(HEALTH_INTERVAL)


def _bg_task_done(task: asyncio.Task) -> None:
    """Done-callback for background tasks: drop the strong reference and,
    crucially, *retrieve* the task result so an exception can't vanish
    silently (a bare ``set.discard`` callback never calls ``.result()``,
    so a crashed background task only surfaces as a late "Task exception
    was never retrieved" warning at GC time). Cancellation is expected on
    shutdown and is not logged as an error."""
    _BG_TASKS.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error(
            "background task %s failed: %r",
            task.get_name(), exc, exc_info=exc,
        )


def _spawn_bg_task(coro) -> asyncio.Task:
    """Schedule a coroutine on the running loop and keep a strong reference
    so the GC can't reap it mid-await. Self-cleans on completion and logs
    any unhandled exception via ``_bg_task_done``."""
    return _spawn_bg_task_impl(coro, task_set=_BG_TASKS, on_done=_bg_task_done)


async def _gather_fail_soft(label: str, *coros) -> list:
    """Run independent I/O steps concurrently, logging (not raising) any
    failure so one step can't kill its siblings. Returns the raw results
    (exceptions included) positionally."""
    results = await asyncio.gather(*coros, return_exceptions=True)
    for res in results:
        if isinstance(res, BaseException):
            logger.error(
                "%s: concurrent step failed: %r", label, res, exc_info=res,
            )
    return results


async def _gather_role_ops(*ops) -> None:
    """Run independent per-role add/remove calls concurrently; re-raise the
    first failure after every op has settled (so a sibling op is never left
    running un-awaited)."""
    results = await asyncio.gather(*ops, return_exceptions=True)
    for res in results:
        if isinstance(res, BaseException):
            raise res


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

    # Onboarding reprompt loop: start once. On startup it also handles
    # prompts whose 5-hour window elapsed while the bot was offline.
    if not getattr(client, "_reprompt_started", False):
        client._reprompt_started = True  # type: ignore[attr-defined]
        # Run an immediate reconciliation sweep before entering the loop
        # so offline-elapsed prompts are handled within seconds of boot.
        _spawn_bg_task(_onboarding_reprompt_task_startup())

    # Pre-warm the render caches (truetype fonts + configured emoji icons)
    # so the first verify/profile reply doesn't pay the cold-start cost.
    if not getattr(client, "_caches_warmed", False):
        client._caches_warmed = True  # type: ignore[attr-defined]
        _spawn_bg_task(_prewarm_caches())


async def _prewarm_caches() -> None:
    """Warm the font cache and prefetch every configured emoji icon.

    Runs once in the background after connect: fonts load in a worker thread
    (filesystem probes), emoji icons fetch concurrently from the CDN into
    ``_EMOJI_BYTES_CACHE``. Fail-soft — a cold cache only costs latency.
    """
    try:
        await asyncio.to_thread(warm_font_cache)
    except Exception:
        logger.debug("cache pre-warm: font warm failed", exc_info=True)
    literals: set[str] = {s.emoji for s in CLAN_SLOTS if s.emoji}
    literals.update(v for v in PLATFORM_EMOJIS.values() if v)
    literals.update(v for v in SYNDICATE_FACTION_EMOJIS.values() if v)
    for raw in (
        MASTERY_RANK_EMOJI_RAW, OPERATOR_EMOJI_RAW,
        SYNDICATE_EMOJI_RAW, WARNING_EMOJI_RAW,
    ):
        if raw:
            literals.add(raw)
    try:
        await asyncio.gather(
            *(_fetch_emoji_bytes(lit) for lit in literals)
        )
    except Exception:
        logger.debug("cache pre-warm: emoji prefetch failed", exc_info=True)


async def _onboarding_reprompt_task_startup() -> None:
    """Run an immediate sweep then hand off to the periodic loop."""
    try:
        await _onboarding_reprompt_sweep()
    except Exception:
        logger.exception("onboarding: startup reprompt sweep failed")
    await _onboarding_reprompt_task()


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
        resolved_guild: discord.Guild | None = None
        for guild in client.guilds:
            if slot.role_id:
                role = guild.get_role(slot.role_id)
                if role is not None:
                    resolved = role
                    resolved_guild = guild
                    break
            if slot.clan_name:
                want = _normalize(slot.clan_name)
                role = discord.utils.find(
                    lambda r, w=want: _normalize(r.name) == w, guild.roles
                )
                if role is not None:
                    resolved = role
                    resolved_guild = guild
                    break
        if resolved is None:
            continue
        resolved_emoji = slot.emoji
        # If the slot has no configured emoji, try to auto-resolve a custom
        # emoji whose name matches the clan/role name (across all guilds).
        if not resolved_emoji:
            resolved_emoji = _resolve_clan_emoji_literal(
                slot.clan_name, resolved.name, primary_guild=resolved_guild
            )
        if (
            slot.role_id != resolved.id
            or slot.clan_name != resolved.name
            or slot.emoji != resolved_emoji
        ):
            logger.info(
                "clan slot %d: %r/%s/%r → %r/%s/%r",
                slot.slot,
                slot.clan_name,
                slot.role_id,
                slot.emoji,
                resolved.name,
                resolved.id,
                resolved_emoji,
            )
            old_name = slot.clan_name
            old_id = slot.role_id
            old_emoji = slot.emoji
            slot.clan_name = resolved.name
            slot.role_id = resolved.id
            slot.emoji = resolved_emoji
            changes.append(
                f"slot {slot.slot}: {old_name!r}/{old_id}/{old_emoji!r} → "
                f"{resolved.name!r}/{resolved.id}/{resolved_emoji!r}"
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
_PLACEHOLDER_NAME_RE = re.compile(r"^place[\s\-_]*holder", re.IGNORECASE)
_EMOJI_NAME_RE = re.compile(r"[^a-z0-9]+")


def _normalize(name: str) -> str:
    return _WS_RE.sub(" ", name.strip().lower())


def _strip_clan_tag(clan_name: str) -> str:
    """'Grand Warhorde#245' -> 'Grand Warhorde'."""
    return _CLAN_TAG_SUFFIX_RE.sub("", clan_name).strip()


def _normalize_emoji_name(name: str) -> str:
    return _EMOJI_NAME_RE.sub("", (name or "").strip().lower())


def _emoji_literal(emoji: discord.Emoji) -> str:
    prefix = "a" if emoji.animated else ""
    return f"<{prefix}:{emoji.name}:{emoji.id}>" if prefix else f"<:{emoji.name}:{emoji.id}>"


# Short connector/filler words dropped when deriving clan-name match keys, so
# a word like "of" in "Church of Slua" never matches a stray server emoji.
_CLAN_NAME_STOPWORDS = frozenset({"of", "the", "and", "clan", "for"})


def _clan_emoji_match_keys(*names: str) -> list[str]:
    """Normalized emoji-match keys for clan/role names, most specific first.

    Yields the full alphanumeric name (e.g. ``kavatraiders``) before each
    significant (>=4 char, non-stopword) word (e.g. ``kavat``, ``raiders``)
    so a full-name match always beats a single-word match.
    """
    keys: list[str] = []
    seen: set[str] = set()
    # Full names first (most specific).
    for name in names:
        full = _normalize_emoji_name(_strip_clan_tag(name or ""))
        if full and full not in seen:
            seen.add(full)
            keys.append(full)
    # Then significant individual words.
    for name in names:
        for word in re.split(r"[^A-Za-z0-9]+", _strip_clan_tag(name or "")):
            key = _normalize_emoji_name(word)
            if len(key) >= 4 and key not in _CLAN_NAME_STOPWORDS and key not in seen:
                seen.add(key)
                keys.append(key)
    return keys


def _resolve_clan_emoji_literal(
    clan_name: str | None,
    role_name: str | None = None,
    *,
    primary_guild: discord.Guild | None = None,
) -> str | None:
    """Auto-pull a custom emoji for a clan slot that has none configured.

    Searches every guild the bot is in (``primary_guild`` first so a local
    match wins) for a custom emoji whose normalized name matches the clan.
    Match order: the full clan/role name, then any significant word (e.g.
    emoji ``slua`` for ``Church of Slua``), then a >=4-char prefix of the full
    name (e.g. ``kavat`` for ``Kavat Raiders``). Returns the ``<:name:id>``
    literal or None.
    """
    keys = _clan_emoji_match_keys(clan_name or "", role_name or "")
    if not keys:
        return None
    guilds: list[discord.Guild] = []
    if primary_guild is not None:
        guilds.append(primary_guild)
    guilds.extend(g for g in client.guilds if g is not primary_guild)
    full = keys[0]
    for guild in guilds:
        emojis = [(_normalize_emoji_name(e.name), e) for e in guild.emojis]
        # Exact match on any key (full name before words).
        for key in keys:
            for norm, emoji in emojis:
                if norm and norm == key:
                    return _emoji_literal(emoji)
        # Emoji name that *extends* the full clan key, e.g. clan
        # "kavatraiders" -> emoji "KavatRaiders_Emblem" ("kavatraidersemblem").
        # Handles the "<Clan>_Emblem" naming used for clan emblems; the
        # shortest extending name (closest to the clan) wins.
        if len(full) >= 4:
            best_ext: tuple[str, discord.Emoji] | None = None
            for norm, emoji in emojis:
                if norm.startswith(full):
                    if best_ext is None or len(norm) < len(best_ext[0]):
                        best_ext = (norm, emoji)
            if best_ext is not None:
                return _emoji_literal(best_ext[1])
        # Prefix of the full name (e.g. "kavat" -> "kavatraiders"); longest wins.
        best: tuple[str, discord.Emoji] | None = None
        for norm, emoji in emojis:
            if len(norm) >= 4 and full.startswith(norm):
                if best is None or len(norm) > len(best[0]):
                    best = (norm, emoji)
        if best is not None:
            return _emoji_literal(best[1])
    return None


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


# ---------- Screenshot processing -------------------------------------------


def _first_image_attachment(message: discord.Message) -> discord.Attachment | None:
    for attachment in message.attachments:
        if attachment.content_type and attachment.content_type.startswith("image/"):
            return attachment
    return None


class _OcrProfileFields(NamedTuple):
    """Parsed fields from OCR-ing a Warframe profile screenshot.

    ``ok`` is False only when the OCR call itself raised; an OCR that ran but
    read nothing returns ``ok=True`` with None fields. ``engine`` and
    ``latency_ms`` are always populated so the caller can record analytics
    even on failure.
    """

    ok: bool
    profile_name: str | None
    clan_name: str | None
    mastery_rank: str | None
    ocr_text: str
    engine: str
    latency_ms: int


async def _ocr_profile_fields(
    image_bytes: bytes, filename: str, content_type: str,
) -> _OcrProfileFields:
    """OCR an already-decoded profile screenshot and parse the in-game name,
    clan name, and mastery rank from it.

    A pure pipeline (no Discord I/O) shared by the modal verification flows
    (onboarding + ``_verify_member_from_screenshot``). The blocking OCR +
    title-bar supplement run in worker threads so the event loop keeps
    servicing heartbeats. The caller owns image validation (probe-decode)
    and every response / role / analytics side effect.
    """
    engine = (
        "ollama" if OLLAMA_OCR_MODEL
        else "ocr.space" if OCR_API_KEY
        else ("tesseract" if pytesseract else "none")
    )
    started = time.monotonic()
    try:
        # OCR involves blocking HTTP (up to 60s) and subprocess work. The
        # title-bar Tesseract supplement is folded into _ocr (and its result
        # cache), so a single worker hop covers both passes.
        ocr_text_raw, _ocr_words, engine = await _run_heavy(
            _ocr, image_bytes, filename, content_type,
        )
        ocr_text = (ocr_text_raw or "").strip()
    except Exception:
        logger.exception("OCR failed for uploaded image")
        return _OcrProfileFields(
            False, None, None, None, "", engine,
            int((time.monotonic() - started) * 1000),
        )
    latency_ms = int((time.monotonic() - started) * 1000)
    # Record to in-process metrics for the /status Latency page.
    ocr_latency.record(latency_ms)

    return _OcrProfileFields(
        True,
        parse_profile_name(ocr_text),
        parse_clan_name(ocr_text),
        parse_mastery_rank(ocr_text),
        ocr_text,
        engine,
        latency_ms,
    )


async def _add_role(
    member: discord.Member, role: discord.Role, reason: str
) -> tuple[bool, str]:
    if role in member.roles:
        return False, f"already has **{role.name}**"
    try:
        await _discord_call_with_retry(
            lambda: member.add_roles(role, reason=reason),
            label=f"add_role:{role.name}",
        )
        return True, f"assigned **{role.name}**"
    except discord.Forbidden:
        return False, f"missing permission to assign **{role.name}**"
    except discord.HTTPException:
        logger.exception("Failed to assign role %s", role.name)
        return False, f"error assigning **{role.name}**"


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


# ---------- Role category tracking -----------------------------------------


# Categories whose roles make up a member's verifiable profile data. Each
# tuple is (display_name, list[int] of role IDs that count for that
# category). Recomputed per call because CLAN_SLOTS / PLATFORM_ROLE_IDS can
# change at runtime via /clan-emblems resync. Drives the on_member_update
# change detector that keeps each member's record message in sync.
def _role_categories() -> list[tuple[str, list[int]]]:
    return [
        ("Platform", [rid for rid in PLATFORM_ROLE_IDS.values() if rid]),
        ("Clan", [s.role_id for s in CLAN_SLOTS if s.role_id]),
        ("Mastery Rank", list(MR_ROLE_IDS)),
        ("Syndicate", list(SYNDICATE_ROLE_IDS)),
    ]


def _tracked_role_ids() -> set[int]:
    """Return the flat set of every role ID that contributes to a member's
    record (clan / platform / mastery / syndicate). Used by
    ``on_member_update`` to decide whether a role change touched any
    profile-relevant role."""
    return {rid for _name, ids in _role_categories() for rid in ids}


def _member_mastery_roles(member: discord.Member) -> list["discord.Role"]:
    """Return the member's mastery-bucket roles.

    Prefers the configured ``MR_ROLE_IDS``; when the member holds none of
    those (the list is unconfigured, or the server's role names don't match
    ``MR_ROLE_NAMES`` exactly so the name->ID resolution never populated it),
    falls back to any role whose *name* parses as an MR/LR bucket via
    :func:`_parse_mr_bucket_range`. This keeps the Mastery Rank row resolving
    from the member's actual roles even when exact-name config hasn't matched
    — the same way Platform/Syndicate resolve from their ID lists.
    """
    mr_ids = set(MR_ROLE_IDS)
    by_id = [r for r in member.roles if r.id in mr_ids]
    if by_id:
        return by_id
    return [
        r for r in member.roles
        if _parse_mr_bucket_range(r.name) is not None
    ]


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

    # Prefetch every emoji icon a row might need in one concurrent burst —
    # each literal is an independent CDN fetch (cached per emoji ID), so
    # paying their latency back-to-back on a cold cache is pure waste.
    slot = next(
        (s for s in CLAN_SLOTS if s.role_id and s.role_id in role_ids),
        None,
    )
    platforms = {p: rid for p, rid in PLATFORM_ROLE_IDS.items() if rid}
    member_platform = next(
        (p for p, rid in platforms.items() if rid in role_ids), None
    )
    syn_ids = set(SYNDICATE_ROLE_IDS)
    syndicate_roles = [r for r in member.roles if r.id in syn_ids]
    emoji_literals: set[str | None] = {
        slot.emoji if slot is not None else None,
        PLATFORM_EMOJIS.get(member_platform) if member_platform else None,
        MASTERY_RANK_EMOJI_RAW,
        *(_syndicate_style(r.name)[1] for r in syndicate_roles),
    }
    emoji_literals.discard(None)
    literal_list = sorted(emoji_literals)
    emoji_bytes_list = await asyncio.gather(
        *(_fetch_emoji_bytes(lit) for lit in literal_list)
    )
    emoji_bytes: dict[str | None, bytes | None] = dict(
        zip(literal_list, emoji_bytes_list)
    )
    emoji_bytes[None] = None

    # The records channel is the source of truth for the exact picked/OCR'd
    # Mastery Rank (Discord roles only carry coarse buckets), so prefer it for
    # the Mastery Rank row when present. The record read (HTTP/cache) and the
    # titles query (SQLite in a worker thread) are independent I/O, so kick
    # them off concurrently rather than paying their latency back-to-back.
    stored, title_rows = await asyncio.gather(
        _member_profile_from_records(member.guild.id, member.id),
        asyncio.to_thread(
            analytics.list_member_titles, member.guild.id, member.id
        ),
    )
    mastery_override = (stored or {}).get("mastery_rank")

    # Clan — match the member's clan role to its slot for name + emoji.
    if any(s.role_id for s in CLAN_SLOTS):
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
                emoji_bytes.get(slot.emoji),
                clan_color,
            ))
        else:
            rows.append(("Clan", "\u2014", None))

    # Platform — first configured platform role the member holds.
    if platforms:
        if member_platform is not None:
            rows.append((
                "Platform",
                member_platform,
                emoji_bytes.get(PLATFORM_EMOJIS.get(member_platform)),
            ))
        else:
            rows.append(("Platform", "\u2014", None))

    # Mastery Rank — prefer the exact stored rank; otherwise fall back to
    # the per-rank role name(s) the member holds. Exception: if the
    # member holds a Legendary (LR) role, that wins even over a
    # lower stored rank — the Legendary role is the source of truth for
    # legendary status (a stale OCR'd "MR n" shouldn't hide it).
    member_mr_roles = _member_mastery_roles(member)
    if MR_ROLE_IDS or member_mr_roles or mastery_override:
        legendary_role_name = next(
            (
                r.name for r in member_mr_roles
                if (_parse_mr_bucket_range(r.name) or ("", 0, 0))[0] == "LR"
            ),
            None,
        )
        override_is_legendary = bool(mastery_override) and (
            _parse_mr_bucket_range(mastery_override) or ("", 0, 0)
        )[0] == "LR"
        if legendary_role_name and not override_is_legendary:
            mr_value = legendary_role_name
        elif mastery_override:
            mr_value = _format_mastery_display(mastery_override) or "\u2014"
        else:
            mr_names = [r.name for r in member_mr_roles]
            mr_value = ", ".join(mr_names) if mr_names else "\u2014"
        rows.append((
            "Mastery Rank",
            mr_value,
            emoji_bytes.get(MASTERY_RANK_EMOJI_RAW),
        ))

    # Syndicate — members may pledge to several. Emit a per-faction list of
    # (name, accent_rgb, emoji_bytes): the canonical Warframe palette +
    # per-faction SYNDICATE_EMOJI_<KEY>, falling back to the role's own
    # colour + the shared SYNDICATE_EMOJI for an unrecognised name.
    if SYNDICATE_ROLE_IDS:
        factions: list[tuple] = []
        for r in syndicate_roles:
            color, emoji_literal = _syndicate_style(r.name)
            if color is None and r.color.value:
                color = r.color.to_rgb()
            factions.append((
                r.name, color, emoji_bytes.get(emoji_literal),
            ))
        rows.append(("Syndicate", factions))

    # Titles — cosmetic achievement labels awarded via /titles, newest
    # first (fetched concurrently with the record read above). Surfaced as
    # compact gold chips on the profile card.
    if title_rows:
        rows.append(("Titles", [r["title"] for r in title_rows]))

    return rows


async def _member_in_game_name(member: discord.Member) -> str | None:
    """Return the member's in-game handle (e.g. ``PlayerName#123``) from their
    record in the records channel, or ``None`` when no readable scan has been
    recorded. Used as the /profile card headline (the server nickname becomes
    a small subtitle beneath it)."""
    stored = await _member_profile_from_records(member.guild.id, member.id)
    name = (stored or {}).get("in_game_name")
    return name.strip() if isinstance(name, str) and name.strip() else None


# Categories a member can self-assign via the help channel; surfaced as
# "Assign <category>" link buttons on a /profile card when unearned.
_ASSIGNABLE_CATEGORIES = ("Platform", "Mastery Rank", "Syndicate")


def _missing_assignable_categories(info_lines: list[tuple] | None) -> list[str]:
    """Return the subset of Platform / Mastery Rank / Syndicate the member
    hasn't earned, read from the gathered /profile rows (em-dash value or
    empty faction list). Categories not configured on the server are
    skipped — there's nothing to assign."""
    rows = {entry[0]: entry for entry in (info_lines or [])}
    missing: list[str] = []
    for label in _ASSIGNABLE_CATEGORIES:
        entry = rows.get(label)
        if entry is None:
            continue
        if label == "Syndicate":
            earned = bool(entry[1]) if len(entry) >= 2 and isinstance(
                entry[1], list
            ) else False
        else:
            value = entry[1] if len(entry) >= 2 else "\u2014"
            earned = bool(value) and value != "\u2014"
        if not earned:
            missing.append(label)
    return missing


def _assign_role_buttons(
    guild: "discord.Guild | None", missing: list[str]
) -> list["discord.ui.Button"]:
    """Build an "Assign <category>" Link button per missing category, each
    jumping to the help channel. Empty when the guild/channel is unset or
    nothing's missing."""
    if not (guild and HELP_CHANNEL_ID and missing):
        return []
    url = f"https://discord.com/channels/{guild.id}/{HELP_CHANNEL_ID}"
    emoji = (
        discord.PartialEmoji(name="assign", id=ASSIGN_ROLE_EMOJI_ID)
        if ASSIGN_ROLE_EMOJI_ID
        else None
    )
    return [
        discord.ui.Button(
            style=discord.ButtonStyle.link,
            label=f"Assign {label}",
            url=url,
            emoji=emoji,
        )
        for label in missing
    ]


# Components V2 multipart bodies bypass discord.py's HTTPClient (it can't
# cleanly post arbitrary V2 multipart) and go straight out via aiohttp with
# the bot token. One base URL + sender keeps the POST (new reply) and PATCH
# (attachment swap) paths in lock-step.


async def _v2_multipart_request(
    method: str,
    url: str,
    *,
    payload: dict,
    file_bytes: bytes,
    file_name: str,
    file_content_type: str = "image/png",
    extra_files: list[tuple[bytes, str]] | None = None,
) -> dict | None:
    """Send a multipart Components-V2 body (``payload_json`` + one or more
    files) to ``url`` via the shared aiohttp session, authenticated with the
    bot token.

    The primary file is ``files[0]``; any ``extra_files`` (``(bytes, name)``
    tuples, e.g. a circular avatar alongside the screenshot) are appended as
    ``files[1]``, ``files[2]``… so the payload's ``attachments`` ids line up.
    Returns the parsed JSON response when the server sends one, else None.
    Raises ``discord.HTTPException`` on any 4xx/5xx so callers can fall back
    or log it (e.g. ``_post_channel_embed`` / ``_edit_channel_embed``).
    """
    session = await _get_http_session()
    holder: dict[str, object] = {}

    async def _request():
        holder["data"] = await _v2_multipart_http(
            session,
            method=method,
            url=url,
            bot_token=DISCORD_TOKEN,
            payload=payload,
            file_bytes=file_bytes,
            file_name=file_name,
            file_content_type=file_content_type,
            extra_files=extra_files,
        )

    await _discord_call_with_retry(_request, label=f"v2 multipart {method}")
    return holder.get("data")


async def _delete_message(channel_id: int, message_id: int) -> None:
    """Issue a single DELETE /channels/{cid}/messages/{mid}. NotFound is
    swallowed (already gone); other HTTP errors are logged."""
    from discord.http import Route

    route = Route(
        "DELETE",
        "/channels/{channel_id}/messages/{message_id}",
        channel_id=channel_id,
        message_id=message_id,
    )
    try:
        await _discord_call_with_retry(
            lambda: client.http.request(route),
            label=f"delete message {message_id}",
        )
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


def _extract_message_id(data: object) -> int | None:
    """Pull the integer message ID out of a Discord HTTP response body.

    Returns None when ``data`` isn't a dict or carries no parseable ``id``.
    """
    if isinstance(data, dict):
        raw_id = data.get("id")
        if isinstance(raw_id, (str, int)):
            try:
                return int(raw_id)
            except (TypeError, ValueError):
                pass
    return None


async def _post_channel_v2(
    channel_id: int,
    components: list[dict],
    *,
    mention_user_ids: list[int] | None = None,
) -> int | None:
    """POST a standalone Components V2 message to ``channel_id``.

    Unlike ``_send_v2`` (which posts as a reply), this sends a plain channel
    message with no ``message_reference``. Returns the posted message ID, or
    None on failure. ``mention_user_ids`` controls which user IDs Discord
    actually pings when they appear as ``<@uid>`` inside component text.
    """
    from discord.http import Route

    users: list[str] = [str(uid) for uid in (mention_user_ids or [])]
    payload: dict = {
        "flags": COMPONENTS_V2_FLAG,
        "components": components,
        "allowed_mentions": {"parse": [], "users": users},
    }
    route = Route(
        "POST",
        "/channels/{channel_id}/messages",
        channel_id=channel_id,
    )
    try:
        holder: dict[str, object] = {}

        async def _request():
            holder["data"] = await client.http.request(route, json=payload)

        await _discord_call_with_retry(
            _request,
            label=f"post channel v2 {channel_id}",
        )
        data = holder.get("data")
        return _extract_message_id(data)
    except discord.HTTPException:
        logger.exception("_post_channel_v2: failed to post to channel %s", channel_id)
    return None


def _record_attachment_plan(
    file_bytes: bytes | None,
    file_name: str,
    avatar_bytes: bytes | None,
) -> tuple[bytes | None, str, list[tuple[bytes, str]], list[dict]]:
    """Order the record's attachments (screenshot then avatar) for a multipart
    upload.

    Returns ``(primary_bytes, primary_name, extra_files, attachments)`` where
    ``attachments`` are the ``{id, filename}`` descriptors in upload order so
    each ``attachment://`` reference in the embed resolves. ``primary_bytes``
    is None when there's nothing to upload (caller sends plain JSON).
    """
    files: list[tuple[bytes, str]] = []
    if file_bytes is not None:
        files.append((file_bytes, file_name))
    if avatar_bytes is not None:
        files.append((avatar_bytes, "avatar.png"))
    if not files:
        return None, file_name, [], []
    primary_bytes, primary_name = files[0]
    extra_files = files[1:]
    attachments = [
        {"id": i, "filename": name} for i, (_, name) in enumerate(files)
    ]
    return primary_bytes, primary_name, extra_files, attachments


async def _post_channel_embed(
    channel_id: int,
    embed: dict,
    *,
    file_bytes: bytes | None = None,
    file_name: str = "record.png",
    avatar_bytes: bytes | None = None,
) -> int | None:
    """POST a plain (non-V2) message carrying a single rich ``embed`` to
    ``channel_id``, optionally with a screenshot and/or a circular avatar.

    Records are sent as real embeds (not Components V2) so they render as the
    gold ``/status``-styled card and can be re-styled from
    ``scripts/record_layout.json``. ``file_bytes`` is the screenshot
    (``attachment://record.png``); ``avatar_bytes`` is the /profile-style
    circular avatar (``attachment://avatar.png``). Whichever are present are
    uploaded multipart with their ``attachments`` ids in upload order. Returns
    the posted message id or None on failure.
    """
    url = f"{_DISCORD_API_BASE}/channels/{channel_id}/messages"
    payload: dict = {"embeds": [embed], "allowed_mentions": _ALLOWED_MENTIONS_NONE}
    primary_bytes, primary_name, extra_files, attachments = (
        _record_attachment_plan(file_bytes, file_name, avatar_bytes)
    )
    try:
        if primary_bytes is not None:
            payload["attachments"] = attachments
            data = await _v2_multipart_request(
                "POST", url, payload=payload,
                file_bytes=primary_bytes, file_name=primary_name,
                extra_files=extra_files or None,
            )
        else:
            from discord.http import Route

            data = await client.http.request(
                Route(
                    "POST", "/channels/{channel_id}/messages",
                    channel_id=channel_id,
                ),
                json=payload,
            )
        return _extract_message_id(data)
    except discord.HTTPException:
        logger.exception(
            "_post_channel_embed: failed to post to channel %s", channel_id
        )
    return None


async def _edit_channel_embed(
    channel_id: int,
    message_id: int,
    embed: dict,
    *,
    file_bytes: bytes | None = None,
    file_name: str = "record.png",
    avatar_bytes: bytes | None = None,
    keep_attachment_ids: list[int] | None = None,
) -> None:
    """PATCH an existing record message's ``embed`` in place.

    With ``file_bytes`` and/or ``avatar_bytes`` the attachments are re-uploaded
    multipart (screenshot then circular avatar) and the embed's
    ``attachment://`` references resolve to the new uploads. Without any new
    file only the embed JSON is rewritten; pass ``keep_attachment_ids`` to
    retain already-attached files (screenshot + avatar). Fail-soft.
    """
    payload: dict = {"embeds": [embed], "allowed_mentions": _ALLOWED_MENTIONS_NONE}
    primary_bytes, primary_name, extra_files, attachments = (
        _record_attachment_plan(file_bytes, file_name, avatar_bytes)
    )
    try:
        if primary_bytes is not None:
            url = (
                f"{_DISCORD_API_BASE}/channels/"
                f"{channel_id}/messages/{message_id}"
            )
            payload["attachments"] = attachments
            await _v2_multipart_request(
                "PATCH", url, payload=payload,
                file_bytes=primary_bytes, file_name=primary_name,
                extra_files=extra_files or None,
            )
            return
        from discord.http import Route

        if keep_attachment_ids is not None:
            payload["attachments"] = [
                {"id": int(a)} for a in keep_attachment_ids
            ]
        await client.http.request(
            Route(
                "PATCH", "/channels/{channel_id}/messages/{message_id}",
                channel_id=channel_id, message_id=message_id,
            ),
            json=payload,
        )
    except discord.HTTPException:
        logger.exception(
            "_edit_channel_embed: failed to edit message %s", message_id
        )


async def _clear_member_data_on_leave(
    guild_id: int, user_id: int, label: str
) -> None:
    """Erase a member's durable data after they leave (the "on-leave data
    clear" policy). Runs off the event loop and never raises so a gateway
    member-remove event can't crash the bot.

    Deletes their awarded titles + onboarding prompt and anonymises their
    verification telemetry (see :func:`analytics.delete_member_data`). Their
    profile fields live in the records channel, and the rendered cards hold
    no persisted state of their own, so this leaves nothing dangling here.
    """
    try:
        result = await asyncio.to_thread(
            analytics.delete_member_data,
            guild_id=guild_id,
            user_id=user_id,
        )
    except Exception:
        logger.exception(
            "on-leave clear failed for %s (guild %s)", user_id, guild_id
        )
        return
    if result.get("titles") or result.get("events_anonymized") or result.get(
        "onboarding"
    ):
        logger.info(
            "on-leave clear: %s (%s) left guild %s \u2014 removed "
            "titles=%d onboarding=%d, anonymised events=%d",
            label,
            user_id,
            guild_id,
            result.get("titles", 0),
            result.get("onboarding", 0),
            result.get("events_anonymized", 0),
        )


@client.event
async def on_member_remove(member: discord.Member) -> None:
    """Apply the on-leave data clear when a member leaves, is kicked, or is
    banned (all surface as a member-remove gateway event).

    We only have the member's guild + user IDs to work with reliably here,
    so the clear is scoped strictly to that ``(guild_id, user_id)`` pair.
    """
    guild = member.guild
    if guild is None:
        return
    label = getattr(member, "display_name", None) or getattr(
        member, "name", "member"
    )
    # Drop the member's cached screenshot bytes (privacy + memory).
    _evict_cached_screenshot(member.id)
    _spawn_bg_task(
        _clear_member_data_on_leave(guild.id, member.id, str(label))
    )


@client.event
async def on_member_update(
    before: discord.Member, after: discord.Member
) -> None:
    """Keep a member's canonical record in sync when their profile-relevant
    roles change (clan / platform / mastery bucket / syndicate).

    Editing the record message doesn't itself fire ``on_member_update``, so
    there's no feedback loop. Only fires when a *tracked* role actually
    changed, and only refreshes an **existing** record — a bare role edit
    never creates one (records are born at verify / onboarding time).
    Fail-soft.
    """
    before_ids = {r.id for r in before.roles}
    after_ids = {r.id for r in after.roles}

    # If the member just became "settled" — gaining the verified-member role
    # (normal verify / a manual grant) or the manual-review Honoured Friends
    # grant — finalize their onboarding (mark complete + strip dropdown) AND
    # ensure they have a record (role-derived, partial). Checked before the
    # tracked-role gate because neither settled role is itself a tracked
    # profile category. Spawned as a background task.
    settled = {
        rid for rid in (_VERIFIED_ROLE_IDS + _MREVIEW_APPROVE_ROLE_IDS) if rid
    }
    if settled & after_ids and not (settled & before_ids):
        _spawn_bg_task(_finalize_onboarding_with_record(after))

    tracked = _tracked_role_ids()
    if not tracked:
        return
    if (before_ids & tracked) == (after_ids & tracked):
        return
    try:
        ids = await asyncio.to_thread(
            records_index.get_record_message_ids, after.id
        )
    except Exception:
        ids = []
    if not ids:
        return
    _spawn_bg_task(_edit_or_create_member_record(after))


# ---------- Member onboarding flow ------------------------------------------

# Timestamp of the last welcome posted per guild to debounce join storms.
# Maps guild_id → last-post timestamp. Cleared after ONBOARDING_POLL_SECONDS.
_JOIN_LAST_POST: dict[int, float] = {}
# Minimum seconds between welcomes in the same guild (join-storm guard).
_JOIN_DEBOUNCE_SECONDS = 2.0

# Animated banners shown at the top of the onboarding welcome; one is picked at
# random each time the prompt is built so repeat joins/reprompts vary.
_ONBOARDING_WELCOME_GIFS = (
    "https://i.imgur.com/tSUNiNB.gif",
    "https://i.imgur.com/L5gdLhn.gif",
    "https://i.imgur.com/EYEX2uw.gif",
    "https://i.imgur.com/jUVIpox.gif",
    "https://orig00.deviantart.net/fa45/f/2018/167/1/b/warframe_umbra_sprite_gif_animation_by_masich2d-dceknro.gif",
    "https://orig00.deviantart.net/a07a/f/2018/009/2/d/mesa_peacemaker_animation_gif_by_masich2d-dbzdz5p.gif",
)


# Matches default/unconfigured clan slot names like "Place Holder 2 Clan"
# (case-insensitive, flexible spacing) so they never surface in onboarding.
_PLACEHOLDER_CLAN_RE = re.compile(r"^\s*place\s*holder\b", re.IGNORECASE)


def _is_placeholder_clan_name(name: str | None) -> bool:
    """True for default placeholder clan names (e.g. "Place Holder 2 Clan")."""
    return bool(name) and _PLACEHOLDER_CLAN_RE.match(name or "") is not None


def _onboarding_welcome_components(
    member_id: int, guild: discord.Guild | None = None
) -> list[dict]:
    """Build the Components V2 welcome payload for a new member.

    Renders a randomly-chosen animated banner (type 12 media gallery), a
    welcome line, and a single string-select dropdown with one option per
    real clan slot (the select custom_id encodes the target member's user id
    so picks survive bot restarts) and a "Not Affiliated" option (value
    ``none``) that routes to manual review.

    Uses the same source/filter as the /status clans page: only slots with a
    configured ``clan_name`` whose role actually resolves in ``guild`` are
    offered, so placeholder/unconfigured clans never appear. When ``guild`` is
    None we fall back to the env-level ``clan_name and role_id`` check.
    Placeholder slots (e.g. "Place Holder 2 Clan") are skipped by name.
    """
    options: list[dict] = []
    for slot in CLAN_SLOTS:
        if not slot.clan_name or not slot.role_id:
            continue
        # Skip unconfigured placeholder slots by name.
        if _is_placeholder_clan_name(slot.clan_name):
            continue
        # Mirror /status: a slot is only "real" if its role exists in the guild.
        if guild is not None and guild.get_role(slot.role_id) is None:
            continue
        emoji_dict = _button_emoji_from_literal(slot.emoji)
        opt: dict = {
            "label": _strip_clan_tag(slot.clan_name or "")[:100],
            "value": str(slot.slot),
        }
        if emoji_dict:
            opt["emoji"] = emoji_dict
        options.append(opt)
    options.append({
        "label": "Not Affiliated",
        "value": "none",
        "description": "I am not in one of the above clans",
        "emoji": {"id": "1467922510908494098", "name": "warframe", "animated": False},
    })
    select_row = {
        "type": 1,
        "components": [
            {
                "type": 3,
                "custom_id": f"onboard:{member_id}:clanselect",
                "placeholder": "Are you a member of the alliance?",
                "min_values": 1,
                "max_values": 1,
                "options": options,
            }
        ],
    }

    banner = {
        "type": 12,
        "items": [{"media": {"url": random.choice(_ONBOARDING_WELCOME_GIFS)}}],
    }
    welcome_text = {"type": 10, "content": f"> Welcome <@{member_id}>"}
    return [
        banner,
        welcome_text,
        select_row,
    ]


# Component types that are "select menu" variants inside a Discord action row.
_SELECT_COMPONENT_TYPES = frozenset({3, 5, 6, 7, 8})


async def _patch_message_v2_components(
    channel_id: int,
    message_id: int,
    components: list[dict],
    *,
    debug_msg: str,
) -> None:
    """PATCH a Components V2 message's component tree in place. Fail-soft.

    ``debug_msg`` is logged (with traceback) if the request fails.
    """
    from discord.http import Route

    try:
        await client.http.request(
            Route(
                "PATCH",
                "/channels/{channel_id}/messages/{message_id}",
                channel_id=channel_id,
                message_id=message_id,
            ),
            json={"components": components, "flags": COMPONENTS_V2_FLAG},
        )
    except discord.HTTPException:
        logger.debug(debug_msg, exc_info=True)


def _strip_select_rows(components: list[dict]) -> list[dict]:
    """Return ``components`` with any action row containing a select removed."""
    out: list[dict] = []
    for comp in components:
        if comp.get("type") == 1 and any(
            child.get("type") in _SELECT_COMPONENT_TYPES
            for child in comp.get("components", [])
        ):
            continue
        out.append(comp)
    return out


async def _remove_onboarding_dropdown(guild_id: int, user_id: int) -> None:
    """Strip the clan-select dropdown from the member's onboarding welcome
    message in place, preserving the rest (banner + welcome line).

    Called when onboarding completes (pass) or routes to manual review so the
    original prompt can no longer be re-submitted. Fetches the live message and
    removes only the action row holding the select, leaving everything else
    untouched. Fail-soft — any error just leaves the message as-is.
    """
    from discord.http import Route

    row = await asyncio.to_thread(
        analytics.get_onboarding_prompt, guild_id, user_id
    )
    if not row:
        return
    channel_id = row.get("channel_id")
    message_id = row.get("message_id")
    if not channel_id or not message_id:
        return
    try:
        data = await client.http.request(Route(
            "GET",
            "/channels/{channel_id}/messages/{message_id}",
            channel_id=channel_id,
            message_id=message_id,
        ))
    except discord.HTTPException:
        logger.debug(
            "onboarding: couldn't fetch welcome message to strip dropdown",
            exc_info=True,
        )
        return
    components = data.get("components") if isinstance(data, dict) else None
    if not isinstance(components, list):
        return
    stripped = _strip_select_rows(components)
    if stripped == components:
        return  # no select row present (already stripped)
    await _patch_message_v2_components(
        channel_id, message_id, stripped,
        debug_msg="onboarding: failed to strip dropdown from welcome message",
    )


async def _finalize_onboarding_with_record(member: discord.Member) -> None:
    """Finalize a member's onboarding when they gain a verified/honoured role.

    Called from ``on_member_update`` when a member gains a verified/honoured
    role outside the self-serve screenshot pipeline (e.g., another bot granted
    verified, or a manual role). Marks onboarding complete and strips the
    welcome dropdown.

    It deliberately does **not** create a record: a member record is born only
    from a screenshot upload (onboarding verify / ``/profile`` verify /
    ``/manage`` screenshot), so a bare verified-role grant for a member who
    never uploaded one must not leave an empty, screenshot-less "Member Record"
    log behind. When the member already has a record, its role-derived fields
    are refreshed in place (the screenshot + OCR-only fields are preserved).
    Idempotent + fail-soft: no-ops gracefully. Spawned as a background task.
    """
    guild_id = member.guild.id
    user_id = member.id
    # Mark onboarding complete + strip dropdown + look up the record index —
    # three independent I/O steps, run concurrently.
    _complete_res, _dropdown_res, ids_res = await _gather_fail_soft(
        "onboarding finalize with record",
        asyncio.to_thread(
            analytics.complete_onboarding_prompt, guild_id, user_id
        ),
        _remove_onboarding_dropdown(guild_id, user_id),
        asyncio.to_thread(
            records_index.get_record_message_ids, user_id
        ),
    )
    ids = ids_res if not isinstance(ids_res, BaseException) else []
    if ids:
        await _edit_or_create_member_record(member)


async def _finalize_onboarding(guild_id: int, user_id: int) -> None:
    """Mark a member's onboarding complete and strip the welcome dropdown.

    Idempotent + fail-soft: no-ops when the member has no pending prompt.
    Called from modals + admin endpoints where the record is already written
    or the caller doesn't have the member object. Use
    ``_finalize_onboarding_with_record`` when you have the member and want to
    ensure a record exists.
    """
    await asyncio.to_thread(
        analytics.complete_onboarding_prompt, guild_id, user_id
    )
    await _remove_onboarding_dropdown(guild_id, user_id)


async def _reset_onboarding_select(guild_id: int, user_id: int) -> None:
    """Re-render the onboarding welcome's clan-select to a fresh, unselected
    state so the member can pick again.

    Discord won't re-fire a string select when a member re-picks the value
    they already chose, so after every pick we reset the select — letting a
    member retry the *same* clan after a failed OCR attempt. No-ops once
    onboarding is complete (the dropdown is gone by then). Fail-soft.
    """
    row = await asyncio.to_thread(
        analytics.get_onboarding_prompt, guild_id, user_id
    )
    if not row or row.get("completed"):
        return
    channel_id = row.get("channel_id")
    message_id = row.get("message_id")
    if not channel_id or not message_id:
        return
    guild = client.get_guild(guild_id)
    components = _onboarding_welcome_components(user_id, guild)
    await _patch_message_v2_components(
        channel_id, message_id, components,
        debug_msg="onboarding: failed to reset clan select",
    )


# Hard-coded assets for the public "verified" welcome posted on a successful
# onboarding pass / admin manual verify (mirrors the design JSON supplied by
# the maintainer).
_PASS_WELCOME_ACCENT = 13938486
_PASS_WELCOME_GIF = "https://i.imgur.com/a3aDbfQ.gif"
_PASS_WELCOME_STAFF_ROLE_ID = 1361846841934610565
_PASS_WELCOME_SELF_ROLES_EMOJI = {
    "id": "1416857239599317022", "name": "ExcalNod", "animated": True,
}
# Animated emoji shown on the manual-review "Verify" button.
_MREVIEW_VERIFY_EMOJI = {
    "id": "1459403163432910972", "name": "Processing", "animated": True,
}
# Seconds the normal onboarding-complete welcome stays up before it
# auto-deletes. The manual-review variant is exempt — it persists until
# staff approve it (then _handle_mreview_interaction deletes it).
_ONBOARDING_PASS_WELCOME_TTL = 180


def _onboarding_pass_welcome_components(
    member_id: int, *, manual_review: bool = False
) -> list[dict]:
    """Build the public "Welcome to the Golden Pagoda" message.

    Shared by the onboarding-complete (pass) path and the manual-review path;
    only the middle line differs:
    - ``manual_review=False`` (normal onboarding completed): the lore line.
    - ``manual_review=True``: a note that staff will reach out to confirm the
      member's clan details and assign roles.
    """
    if manual_review:
        body_line = (
            f"\n> Our <@&{_PASS_WELCOME_STAFF_ROLE_ID}> will reach out to "
            "confirm your clan details.\n"
        )
    else:
        body_line = (
            "\n> *Each clan within the alliance walks a different path — but "
            "all serve the Origin System.*\n"
        )
    # Alias select: replaces the old Self Roles link button. "IGN" sets the
    # member's server nickname to the in-game name read from their record;
    # "Server name" opens a modal to pick a custom nickname. The custom_id
    # encodes the target member id + the card variant so the select can be
    # re-rendered in place after a pick.
    variant = "mr" if manual_review else "std"
    alias_rows: list[dict] = [
        {
            "type": 1,
            "components": [
                {
                    "type": 3,
                    "custom_id": f"alias:{member_id}:{variant}",
                    "placeholder": "Choose your server alias",
                    "min_values": 1,
                    "max_values": 1,
                    "options": [
                        {
                            "label": "IGN",
                            "value": "ign",
                            "description": "Continue as your Tenno Alias",
                            "emoji": _PASS_WELCOME_SELF_ROLES_EMOJI,
                        },
                        {
                            "label": "Server name",
                            "value": "nick",
                            "description": "Pick a new Alias",
                        },
                    ],
                }
            ],
        }
    ]
    if manual_review:
        # Staff-only "Verify" button that grants the verified roles right
        # from this card (a select occupies a full row, so it gets its own).
        alias_rows.append({
            "type": 1,
            "components": [{
                "type": 2,
                "style": 3,
                "label": "Verify",
                "emoji": _MREVIEW_VERIFY_EMOJI,
                "custom_id": f"mreview:{member_id}:approve",
            }],
        })
    return [
        {
            "type": 17,
            "accent_color": _PASS_WELCOME_ACCENT,
            "spoiler": False,
            "components": [
                {
                    "type": 10,
                    "content": (
                        f"## Welcome to the Golden Pagoda, <@{member_id}>."
                    ),
                },
                {
                    "type": 12,
                    "items": [{"media": {"url": _PASS_WELCOME_GIF}}],
                },
                {
                    "type": 10,
                    "content": "```The Lotus has guided you here. ```",
                },
                {"type": 14, "spacing": 1},
                {
                    "type": 10,
                    "content": body_line,
                },
                {"type": 14, "divider": True, "spacing": 2},
                {
                    "type": 10,
                    "content": (
                        "-# *\"The Tenno are warriors, but they are not alone.\"*"
                        " — The Lotus"
                    ),
                },
                *alias_rows,
            ],
        }
    ]


async def _post_onboarding_pass_welcome(
    member: discord.Member, *, manual_review: bool = False
) -> None:
    """Post the public verified-welcome to the server-entry channel.

    The normal onboarding-complete variant auto-deletes after
    ``_ONBOARDING_PASS_WELCOME_TTL`` seconds. The manual-review variant
    persists until staff approve it (then it's deleted in
    :func:`_handle_mreview_interaction`).
    """
    if ONBOARDING_CHANNEL_ID <= 0:
        return
    components = _onboarding_pass_welcome_components(
        member.id, manual_review=manual_review
    )
    msg_id: int | None = None
    with contextlib.suppress(Exception):
        msg_id = await _post_channel_v2(
            ONBOARDING_CHANNEL_ID,
            components,
            mention_user_ids=[member.id],
        )
    if msg_id and not manual_review and _ONBOARDING_PASS_WELCOME_TTL > 0:
        _spawn_bg_task(
            _delete_after(
                ONBOARDING_CHANNEL_ID, msg_id, _ONBOARDING_PASS_WELCOME_TTL
            )
        )


async def _post_onboarding_welcome(member: discord.Member) -> bool:
    """Post a public welcome prompt in ONBOARDING_CHANNEL_ID and record it in the DB.

    Returns True when the welcome was posted (and the prompt recorded), False
    on any failure so callers (e.g. the admin /onboard trigger) can surface it.
    """
    components = _onboarding_welcome_components(member.id, member.guild)
    try:
        msg_id = await _post_channel_v2(
            ONBOARDING_CHANNEL_ID,
            components,
            mention_user_ids=[member.id],
        )
    except Exception:
        logger.exception("onboarding: failed to post welcome for %s", member.id)
        return False
    if msg_id is None:
        logger.warning("onboarding: welcome post returned no message id for %s", member.id)
        return False
    await asyncio.to_thread(
        analytics.upsert_onboarding_prompt,
        guild_id=member.guild.id,
        user_id=member.id,
        channel_id=ONBOARDING_CHANNEL_ID,
        message_id=msg_id,
        posted_ts=int(time.time()),
    )
    logger.info(
        "onboarding: posted welcome for %s (msg=%s) in guild %s",
        member.id, msg_id, member.guild.id,
    )
    return True


@client.event
async def on_member_join(member: discord.Member) -> None:
    """Post a public welcome prompt when a member joins the server."""
    if ONBOARDING_CHANNEL_ID <= 0:
        return
    # Debounce: skip if another welcome was posted in this guild within the
    # last _JOIN_DEBOUNCE_SECONDS to prevent a raid from hammering the channel.
    guild_id = member.guild.id
    now = time.monotonic()
    last = _JOIN_LAST_POST.get(guild_id, 0.0)
    if now - last < _JOIN_DEBOUNCE_SECONDS:
        logger.debug(
            "onboarding: debouncing join for %s in guild %s (%.1fs since last)",
            member.id, guild_id, now - last,
        )
        await asyncio.sleep(max(0.0, _JOIN_DEBOUNCE_SECONDS - (now - last)))
    _JOIN_LAST_POST[guild_id] = time.monotonic()
    _spawn_bg_task(_post_onboarding_welcome(member))


async def _onboarding_route_manual_review(
    interaction: discord.Interaction,
    member: discord.Member,
    reason: str,
    *,
    image_bytes: bytes | None = None,
    in_game_name: str | None = None,
    mastery_rank: str | None = None,
) -> None:
    """Assign the incomplete review role, notify the help channel, and ack."""
    # Ack the triggering interaction up front so it can't time out during the
    # role/analytics/HTTP work below (the modal path already deferred; the
    # dropdown path needs a fresh DEFERRED_UPDATE ack).
    with contextlib.suppress(Exception):
        if not interaction.response.is_done():
            await _interaction_callback(interaction, 6, [])  # DEFERRED_UPDATE
    logger.info(
        "onboarding: routing %s (%s) to manual review: %s",
        member, member.id, reason,
    )
    # These four steps are independent I/O (role add, two SQLite writes, the
    # welcome-prompt dropdown strip) — run them concurrently.
    await _gather_fail_soft(
        "onboarding manual-review route",
        _add_incomplete_role(member),
        asyncio.to_thread(
            analytics.record_verification,
            outcome="incomplete",
            guild_id=member.guild.id,
            user_id=member.id,
        ),
        asyncio.to_thread(
            analytics.complete_onboarding_prompt,
            guild_id=member.guild.id,
            user_id=member.id,
        ),
        _remove_onboarding_dropdown(member.guild.id, member.id),
    )
    # Maintain the member's canonical record (the uploaded screenshot, if any)
    # in the records channel so staff have a record of the pending case.
    _spawn_bg_task(
        _edit_or_create_member_record(
            member,
            in_game_name=in_game_name,
            mastery_rank=mastery_rank,
            extra_lines=[f"Manual review pending — {reason}"],
            image_bytes=image_bytes,
        )
    )
    # Member-facing response: post the public welcome card (a duplicate of the
    # onboarding-complete card, with the manual-review text variant) — it now
    # carries the staff-only "Verify" button inline.
    await _post_onboarding_pass_welcome(member, manual_review=True)


# When a staff member approves a manual-review case via the "Verify" button on
# the alert, the pending-review + unverified gate roles are removed and the
# Honoured Friends role is granted.
_MREVIEW_REMOVE_ROLE_IDS = (1459326361968574555, 1381447170229538917)
_MREVIEW_APPROVE_ROLE_IDS = (1361846841905381632,)

# The "verified member" role — owned and assigned by a SEPARATE bot; this bot
# never grants it. We only *observe* it: a member is "settled" (onboarding can
# be finalized) once they gain this OR the Honoured Friends manual-review grant.
_VERIFIED_ROLE_IDS = (1392585653971062815,)


def _member_is_verified(member: discord.Member) -> bool:
    """True when the member holds the (separate-bot-owned) verified role.

    Used to suppress verification prompts (the onboarding clan-select and the
    /profile "Verify Profile Data" button) for members who are already
    verified — those prompts only make sense for members still onboarding.
    """
    verified = {rid for rid in _VERIFIED_ROLE_IDS if rid}
    if not verified:
        return False
    return any(r.id in verified for r in member.roles)


async def _handle_mreview_interaction(
    interaction: discord.Interaction, custom_id: str
) -> None:
    """Staff-only approve button on a manual-review alert.

    ``custom_id`` format: ``mreview:<user_id>:approve``. Grants the
    verified-member roles to the target and edits the alert to reflect who
    approved. Requires Manage Server.
    """
    parts = custom_id.split(":")
    try:
        target_id = int(parts[1])
    except (IndexError, ValueError):
        return

    user = interaction.user
    guild = interaction.guild

    # Staff gate.
    if not (
        isinstance(user, discord.Member)
        and user.guild_permissions.manage_guild
    ):
        with contextlib.suppress(Exception):
            await _interaction_callback(
                interaction, 4,
                [{"type": 17, "accent_color": ACCENT_FAIL,
                  "components": [{"type": 10, "content": (
                      "-# Only staff can approve reviews, Operator."
                  )}]}],
            )
        return

    if guild is None:
        return

    member = guild.get_member(target_id)
    if member is None:
        with contextlib.suppress(Exception):
            await _interaction_callback(
                interaction, 4,
                [{"type": 17, "accent_color": ACCENT_INCOMPLETE,
                  "components": [{"type": 10, "content": (
                      "-# That member is no longer in the server."
                  )}]}],
            )
        return

    # Ack the click up front (DEFERRED_UPDATE) before any slow work (OCR can
    # take seconds) so the interaction doesn't time out. Capture the message
    # first — we delete it once approval completes.
    msg = interaction.message
    with contextlib.suppress(Exception):
        await _interaction_callback(interaction, 6, [])  # DEFERRED_UPDATE

    # Run the verification that the "Not Affiliated" submission deferred: OCR
    # the member's cached screenshot now, assigning whatever roles it can read
    # and refreshing their record. Manual approval (role grant below) proceeds
    # regardless of the OCR outcome — the moderator has vouched for them.
    image_bytes = _get_cached_screenshot(target_id)
    if image_bytes is not None:
        try:
            result = await _verify_member_from_screenshot(
                member,
                image_bytes=image_bytes,
                filename="profile.png",
                content_type="image/png",
            )
            if result.summary:
                # Re-fetch so the record write sees freshly assigned roles.
                with contextlib.suppress(discord.HTTPException):
                    member = await guild.fetch_member(target_id)
                await _edit_or_create_member_record(
                    member,
                    in_game_name=result.in_game_name,
                    mastery_rank=result.mastery_rank,
                    image_bytes=image_bytes,
                )
        except Exception:
            logger.exception(
                "mreview: verification from cached screenshot failed for %s",
                target_id,
            )

    reason = f"Manual review approved by {user}"

    async def _mreview_remove_one(role: discord.Role) -> "str | None":
        try:
            await _discord_call_with_retry(
                lambda: member.remove_roles(role, reason=reason),
                label=f"mreview_remove:{role.name}",
            )
            return role.name
        except discord.Forbidden:
            logger.warning(
                "mreview: missing permission to remove %s", role.name
            )
        except discord.HTTPException:
            logger.exception("mreview: failed to remove %s", role.name)
        return None

    # Each removal is an independent per-role DELETE; run them concurrently.
    to_remove = [
        role for rid in _MREVIEW_REMOVE_ROLE_IDS
        if (role := guild.get_role(rid)) is not None and role in member.roles
    ]
    removed = [
        name for name in await asyncio.gather(
            *(_mreview_remove_one(role) for role in to_remove)
        )
        if name
    ]

    granted: list[str] = []
    for rid in _MREVIEW_APPROVE_ROLE_IDS:
        role = guild.get_role(rid)
        if role is None:
            continue
        ok, _ = await _add_role(member, role, reason)
        if ok:
            granted.append(role.name)

    logger.info(
        "mreview: %s approved %s; removed=%s granted=%s",
        user.id, member.id, removed, granted,
    )

    # The manual-review welcome persists until approval. Now that roles are
    # granted, delete it (the click was already acked up front).
    if msg is not None:
        _spawn_bg_task(_delete_message(msg.channel.id, msg.id))
    # Ephemeral confirmation to the approving staff member — plain-text
    # ✅ + -# subtext, matching /titles and /clan-emblems.
    confirm_lines = [f"\u2705 Verified **{member.display_name}**."]
    if granted:
        confirm_lines.append(
            f"-# Granted: {', '.join(f'**{n}**' for n in granted)}"
        )
    if removed:
        confirm_lines.append(
            f"-# Removed: {', '.join(f'**{n}**' for n in removed)}"
        )
    with contextlib.suppress(Exception):
        await interaction.followup.send(
            "\n".join(confirm_lines), ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class _GPModal(discord.ui.Modal):
    """Shared modal base: report unexpected ``on_submit`` failures to the
    user instead of dying silently.

    discord.py's default ``Modal.on_error`` only logs — the member sees the
    modal close and nothing else. Every deferred interaction must surface its
    failure, so route it through the followup webhook (the submit handlers
    defer immediately) with a plain-response fallback.
    """

    async def on_error(
        self, interaction: discord.Interaction, error: Exception, /
    ) -> None:
        logger.exception(
            "modal %s failed", type(self).__name__, exc_info=error
        )
        msg = "\u274C Something went wrong \u2014 please try again."
        with contextlib.suppress(Exception):
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)


# Discord caps server nicknames at 32 characters.
_NICKNAME_MAX_LEN = 32


async def _set_member_nickname(
    member: discord.Member, nickname: str, *, reason: str
) -> tuple[bool, str]:
    """Set a member's server nickname. Fail-soft.

    The nickname is display-only — it never touches the member's record, so
    the recorded in-game name (the logging source of truth) is unchanged.
    Returns ``(ok, message)`` for the ephemeral reply.
    """
    nickname = nickname.strip()[:_NICKNAME_MAX_LEN]
    if not nickname:
        return False, "\u274C No name to set, Operator."
    try:
        await _discord_call_with_retry(
            lambda: member.edit(nick=nickname, reason=reason),
            label="alias nickname edit",
        )
    except discord.Forbidden:
        return False, (
            "\u274C I can't change your nickname, Operator — "
            "please set it yourself or ask a staff member."
        )
    except discord.HTTPException:
        logger.exception("alias: failed to set nickname for %s", member.id)
        return False, "\u274C Something went wrong \u2014 please try again."
    return True, f"\u2705 Your server alias is now **{nickname}**."


class _AliasNicknameModal(_GPModal):
    """Modal opened from the verified-welcome alias select's "Server name"
    option. Lets the member pick a custom server nickname — nothing else: no
    OCR, no roles, and no record write (the recorded IGN stays untouched).
    """

    def __init__(self, *, member: discord.Member) -> None:
        super().__init__(title="Pick a new Alias", timeout=600)
        self._gp_member = member
        self.nickname = discord.ui.TextInput(
            label="Server nickname",
            placeholder="Your new alias",
            default=member.nick or None,
            required=True,
            max_length=_NICKNAME_MAX_LEN,
        )
        self.add_item(self.nickname)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        member = self._gp_member
        _ok, msg = await _set_member_nickname(
            member, self.nickname.value or "",
            reason="Alias select: member picked a server name",
        )
        with contextlib.suppress(Exception):
            await interaction.response.send_message(
                msg, ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )


async def _reset_alias_select(
    interaction: discord.Interaction, member_id: int, *, manual_review: bool
) -> None:
    """Re-render the verified-welcome's alias select to a fresh, unselected
    state so the member can pick again (Discord suppresses a string-select
    re-fire on an unchanged value). Fail-soft.
    """
    msg = interaction.message
    if msg is None:
        return
    components = _onboarding_pass_welcome_components(
        member_id, manual_review=manual_review
    )
    await _patch_message_v2_components(
        msg.channel.id, msg.id, components,
        debug_msg="alias: failed to reset alias select",
    )


async def _handle_alias_interaction(
    interaction: discord.Interaction, custom_id: str
) -> None:
    """Dispatch the alias select on the verified-welcome card.

    ``custom_id`` format: ``alias:<user_id>:<variant>`` where ``variant`` is
    ``std`` (normal pass welcome) or ``mr`` (manual-review card). The picked
    option is in the interaction's ``values``:
    - ``ign``: set the member's server nickname to the in-game name read from
      their record (the profile card/log headline).
    - ``nick``: open a modal to pick a custom server nickname.

    Nickname changes are display-only — the recorded IGN used in logging is
    never replaced. Ownership check: only the target member may interact.
    """
    parts = custom_id.split(":")
    try:
        target_id = int(parts[1])
    except (IndexError, ValueError):
        return
    manual_review = len(parts) > 2 and parts[2] == "mr"
    values = (interaction.data or {}).get("values") or []
    value = str(values[0]) if values else ""
    user = interaction.user
    guild = interaction.guild

    # Ownership gate — reject any other user with an ephemeral.
    if user.id != target_id:
        with contextlib.suppress(Exception):
            await _interaction_callback(
                interaction, 4,
                [{"type": 17, "accent_color": ACCENT_FAIL,
                  "components": [{"type": 10, "content": (
                      "-# This prompt isn't for you, Operator."
                  )}]}],
            )
        return

    if guild is None:
        return

    member = guild.get_member(target_id)
    if member is None:
        with contextlib.suppress(Exception):
            await _interaction_callback(
                interaction, 4,
                [{"type": 17, "accent_color": ACCENT_INCOMPLETE,
                  "components": [{"type": 10, "content": (
                      "-# Your session has expired. Please re-join the server."
                  )}]}],
            )
        return

    if value == "nick":
        try:
            await interaction.response.send_modal(
                _AliasNicknameModal(member=member)
            )
        except Exception:
            logger.exception("alias: failed to send nickname modal")
        else:
            _spawn_bg_task(_reset_alias_select(
                interaction, target_id, manual_review=manual_review,
            ))
        return

    if value == "ign":
        ign = await _member_in_game_name(member)
        if not ign:
            msg = (
                "\u274C I couldn't find your in-game name, Operator — "
                "verify your profile first."
            )
        else:
            _ok, msg = await _set_member_nickname(
                member, ign,
                reason="Alias select: member chose their IGN",
            )
        with contextlib.suppress(Exception):
            await _interaction_callback(
                interaction, 4,
                [{"type": 17,
                  "accent_color": ACCENT_PASS if msg.startswith("\u2705")
                  else ACCENT_FAIL,
                  "components": [{"type": 10, "content": msg}]}],
            )
        _spawn_bg_task(_reset_alias_select(
            interaction, target_id, manual_review=manual_review,
        ))


class _OnboardingVerifyModal(_GPModal):
    """Modal opened from the onboarding welcome prompt when a member clicks a
    clan button. Accepts a Warframe profile screenshot, OCRs it, and validates
    that the OCR-detected clan matches the one the member claimed via the button.

    Differs from ``_ScreenshotVerifyModal`` in that it:
    - Has no source profile card to refresh.
    - Validates the claimed clan vs. the OCR clan before assigning roles.
    - On repeated failures routes to the manual-review branch.
    - Posts ephemeral results so screenshots never appear publicly.
    """

    def __init__(
        self, *, member: discord.Member, claimed_slot: "ClanSlot",
    ) -> None:
        super().__init__(title="Submit Profile Screenshot", timeout=600)
        self._gp_member = member
        self._gp_claimed_slot = claimed_slot
        self.screenshot = discord.ui.FileUpload(
            min_values=1, max_values=1, required=True,
        )
        self.add_item(discord.ui.Label(
            text="Profile screenshot",
            description=(
                "Upload a screenshot of your Warframe profile "
                "(title bar + CLAN section visible)."
            ),
            component=self.screenshot,
        ))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        files = list(self.screenshot.values or [])
        if not files:
            with contextlib.suppress(discord.HTTPException):
                await interaction.response.defer()
            return
        attachment = files[0]
        with contextlib.suppress(discord.HTTPException):
            await interaction.response.defer(ephemeral=True)

        member = self._gp_member
        guild = member.guild

        try:
            image_bytes = await attachment.read()
        except Exception:
            logger.warning("onboarding OCR: attachment read failed", exc_info=True)
            with contextlib.suppress(discord.HTTPException):
                await interaction.followup.send(
                    "Couldn't read that upload \u2014 try again.", ephemeral=True
                )
            return

        # Prefer the cached member (no REST round-trip) to keep the submit
        # responsive; the member handed to the modal already carries current
        # roles. Only the cache is consulted, so there's no extra latency.
        member = guild.get_member(member.id) or member

        result = await _verify_member_from_screenshot(
            member,
            image_bytes=image_bytes,
            filename=attachment.filename or "profile.png",
            content_type=attachment.content_type or "image/png",
        )
        summary = result.summary

        if not summary:
            # OCR failed entirely — couldn't read the screenshot.
            fail_count = await asyncio.to_thread(
                analytics.increment_onboarding_ocr_fail,
                guild.id, member.id,
            )
            if fail_count >= ONBOARDING_MAX_OCR_FAILS:
                logger.info(
                    "onboarding: %s hit OCR fail limit (%d), routing to manual review",
                    member.id, fail_count,
                )
                await _onboarding_route_manual_review(
                    interaction, member,
                    f"screenshot unreadable after {fail_count} attempt(s)",
                    image_bytes=image_bytes,
                )
                return
            remaining = ONBOARDING_MAX_OCR_FAILS - fail_count
            retry_text = (
                "\u26A0\uFE0F I couldn't read that screenshot.\n"
                "-# Make sure your Warframe **profile** page is fully visible "
                "(title bar with PlayerName#NNN + CLAN section).\n"
                f"-# You have **{remaining}** attempt(s) remaining before "
                "I'll route you to a staff member."
            )
            with contextlib.suppress(discord.HTTPException):
                await interaction.followup.send(retry_text, ephemeral=True)
            return

        # OCR succeeded — now validate the claimed clan.
        claimed = self._gp_claimed_slot
        claimed_name = _strip_clan_tag(claimed.clan_name or "")

        # Validate by checking whether _verify_member_from_screenshot assigned
        # the claimed clan role.  The OCR pipeline assigns clan roles
        # automatically when it detects a matching clan; if the claimed role
        # wasn't granted, the screenshot belongs to a different clan.
        clan_role_id = getattr(claimed, "role_id", 0) or 0
        if clan_role_id and member.get_role(clan_role_id) is None:
            # Role not assigned → OCR found a different (or no) clan.
            fail_count = await asyncio.to_thread(
                analytics.increment_onboarding_ocr_fail,
                guild.id, member.id,
            )
            if fail_count >= ONBOARDING_MAX_OCR_FAILS:
                await _onboarding_route_manual_review(
                    interaction, member,
                    f"clan mismatch after {fail_count} attempt(s)",
                    image_bytes=image_bytes,
                )
                return
            remaining = ONBOARDING_MAX_OCR_FAILS - fail_count
            retry_text = (
                f"\u26A0\uFE0F I couldn't confirm **{claimed_name}** from your screenshot.\n"
                "-# Your screenshot shows a different clan, or the **CLAN** "
                "section wasn't clearly visible.\n"
                f"-# You have **{remaining}** attempt(s) remaining."
            )
            with contextlib.suppress(discord.HTTPException):
                await interaction.followup.send(retry_text, ephemeral=True)
            return

        # Fallback: if the role wasn't configured, at least confirm any clan
        # was found in the OCR summary.
        if not clan_role_id:
            clan_line_found = any(
                line.lower().startswith("clan:") for line in summary
            )
            if not clan_line_found:
                fail_count = await asyncio.to_thread(
                    analytics.increment_onboarding_ocr_fail,
                    guild.id, member.id,
                )
                if fail_count >= ONBOARDING_MAX_OCR_FAILS:
                    await _onboarding_route_manual_review(
                        interaction, member,
                        f"clan not detectable after {fail_count} attempt(s)",
                        image_bytes=image_bytes,
                    )
                    return
                remaining = ONBOARDING_MAX_OCR_FAILS - fail_count
                retry_text = (
                    f"\u26A0\uFE0F I couldn't confirm **{claimed_name}** from your screenshot.\n"
                    "-# Make sure the **CLAN** section is clearly visible.\n"
                    f"-# You have **{remaining}** attempt(s) remaining."
                )
                with contextlib.suppress(discord.HTTPException):
                    await interaction.followup.send(retry_text, ephemeral=True)
                return

        # Success — mark onboarding complete and confirm to the member. The
        # four finalize steps are independent I/O (two SQLite writes, the
        # unverified-role removal, the welcome-prompt dropdown strip), so run
        # them concurrently.
        await _gather_fail_soft(
            "onboarding pass finalize",
            asyncio.to_thread(
                analytics.complete_onboarding_prompt,
                guild.id, member.id,
            ),
            _remove_unverified_role(member),
            asyncio.to_thread(
                analytics.record_verification,
                outcome="pass",
                clan=claimed_name,
                guild_id=guild.id,
                user_id=member.id,
            ),
            _remove_onboarding_dropdown(guild.id, member.id),
        )
        logger.info(
            "onboarding: %s verified via welcome prompt (clan=%s)",
            member.id, claimed_name,
        )
        # Welcome-only response: post the public "Welcome to the Golden Pagoda"
        # message to the server-entry channel (no ephemeral confirmation).
        await _post_onboarding_pass_welcome(member)
        # Maintain the member's canonical record (their full profile) in the
        # records ("profile-log") channel (fire-and-forget).
        _spawn_bg_task(_edit_or_create_member_record(
            member,
            in_game_name=result.in_game_name,
            mastery_rank=result.mastery_rank,
            image_bytes=image_bytes,
        ))


class _OnboardingNoClanModal(_GPModal):
    """Modal opened from the onboarding welcome prompt when a member clicks
    "Not listed / No". Collects a Warframe profile screenshot so the member's
    manual-review record still carries their screenshot (matching the clan
    paths), then routes the member to manual review.

    A member declaring "Not Affiliated" is NOT verified here: the screenshot is
    cached and the record is filed, but no OCR runs and no roles are assigned.
    Verification is halted until a moderator clicks the staff "Verify" button on
    the manual-review welcome, which OCRs the cached screenshot then.
    """

    def __init__(self, *, member: discord.Member) -> None:
        super().__init__(title="Submit Profile Screenshot", timeout=600)
        self._gp_member = member
        self.screenshot = discord.ui.FileUpload(
            min_values=1, max_values=1, required=True,
        )
        self.add_item(discord.ui.Label(
            text="Profile screenshot",
            description=(
                "Upload a screenshot of your Warframe profile "
                "(title bar + CLAN section visible)."
            ),
            component=self.screenshot,
        ))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        member = self._gp_member
        guild = member.guild
        with contextlib.suppress(discord.HTTPException):
            await interaction.response.defer(ephemeral=True)

        files = list(self.screenshot.values or [])
        image_bytes: bytes | None = None
        attachment = files[0] if files else None
        if attachment is not None:
            try:
                image_bytes = await attachment.read()
            except Exception:
                logger.warning(
                    "onboarding (no-clan): attachment read failed",
                    exc_info=True,
                )
                image_bytes = None

        # Prefer the cached member (no REST round-trip) so the submit stays
        # responsive.
        member = guild.get_member(member.id) or member

        # "Not Affiliated": do NOT OCR / verify here. Cache the screenshot so a
        # moderator can run verification later via the "Verify" button, and
        # route the member to manual review (the record carries the screenshot,
        # no roles assigned).
        if image_bytes is not None:
            _cache_screenshot(member.id, image_bytes)

        await _onboarding_route_manual_review(
            interaction, member, "selected 'Not Affiliated'",
            image_bytes=image_bytes,
        )


def _member_record_profile_lines(
    member: discord.Member,
    *,
    in_game_name: str | None = None,
    mastery_rank: str | None = None,
    clan_override: str | None = None,
) -> list[str]:
    """Derive the full member-profile body for the records ("profile-log")
    channel.

    The records channel is the source of truth for member profile data, so
    each record carries the complete profile as stable, parseable
    ``Key: **Value**`` lines. In-game name + exact Mastery Rank are OCR-only
    (passed in); Clan, Platform, the Mastery bucket and Syndicate are read
    live from the member's current roles. A configured clan role always wins
    the Clan line; ``clan_override`` (a free-text "not affiliated" clan name)
    is used only when the member holds no configured clan role. Categories
    with no value are omitted.
    """
    role_ids = {r.id for r in member.roles}
    lines: list[str] = []

    if in_game_name and in_game_name.strip():
        lines.append(f"In-game name: **{in_game_name.strip()}**")

    slot = next(
        (s for s in CLAN_SLOTS if s.role_id and s.role_id in role_ids),
        None,
    )
    if slot is not None and slot.clan_name:
        clan = _strip_clan_tag(slot.clan_name)
        if clan:
            lines.append(f"Clan: **{clan}**")
    elif clan_override and clan_override.strip():
        lines.append(f"Clan: **{clan_override.strip()}**")

    member_platform = next(
        (
            p for p, rid in PLATFORM_ROLE_IDS.items()
            if rid and rid in role_ids
        ),
        None,
    )
    if member_platform:
        lines.append(f"Platform: **{member_platform}**")

    mr_value: str | None = None
    if mastery_rank and mastery_rank.strip():
        mr_value = mastery_rank.strip()
    else:
        mr_names = [r.name for r in _member_mastery_roles(member)]
        if mr_names:
            mr_value = ", ".join(mr_names)
    if mr_value:
        lines.append(f"Mastery Rank: **{mr_value}**")

    if SYNDICATE_ROLE_IDS:
        syn_ids = set(SYNDICATE_ROLE_IDS)
        syn_names = [r.name for r in member.roles if r.id in syn_ids]
        if syn_names:
            lines.append(f"Syndicate: **{', '.join(syn_names)}**")

    return lines


def _build_member_record_embed(
    member: discord.Member,
    summary_lines: list[str],
    *,
    has_image: bool = False,
    has_avatar: bool = False,
) -> dict:
    """Build a /status-styled gold embed dict for the member-records channel.

    The body is the member's complete profile, carried as embed ``fields`` so
    the records channel stays the parseable source of truth: each
    ``Key: **Value**`` summary line becomes a ``{name: Key, value: **Value**}``
    field that :func:`_parse_record_embed` reads back. The member mention +
    id + join timestamp sit in the description, and the uploaded screenshot
    (when present) is referenced via ``attachment://record.png``.

    When ``has_avatar`` is set the thumbnail points at an attached
    ``attachment://avatar.png`` — the same circular, gold-ringed avatar the
    ``/profile`` card renders — instead of the raw (square) avatar URL.
    """
    joined_str = ""
    if member.joined_at:
        joined_ts = int(member.joined_at.timestamp())
        joined_str = f"  •  joined <t:{joined_ts}:F> (<t:{joined_ts}:R>)"
    name_extra = (
        f"  •  {member.display_name}"
        if member.display_name != str(member) else ""
    )
    description = f"**{member.mention}** (`{member.id}`){name_extra}{joined_str}"

    fields: list[dict] = []
    for line in summary_lines:
        m = _RECORD_LINE_RE.match(line)
        if m:
            fields.append({
                "name": f"{m.group(1).strip()}:",
                "value": f"`{m.group(2).strip()}`",
                "inline": True,
            })
        else:
            # Non Key: **Value** line (e.g. a manual-review note) — keep it
            # readable as a full-width field so nothing is silently dropped.
            fields.append({"name": "\u200b", "value": line, "inline": False})

    embed: dict = {
        "title": f"\U0001F4CB  Member Record \u2014 {member.display_name}",
        "color": ACCENT_PASS,
        "description": description,
        "fields": fields,
        "footer": {"text": "Last edited"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    avatar = getattr(member, "display_avatar", None)
    if has_avatar:
        embed["thumbnail"] = {"url": "attachment://avatar.png"}
    elif avatar is not None:
        embed["thumbnail"] = {"url": str(avatar.url)}
    if has_image:
        embed["image"] = {"url": "attachment://record.png"}
    return embed


async def _create_member_record(
    member: discord.Member,
    summary_lines: list[str],
    *,
    image_bytes: bytes | None = None,
) -> None:
    """Post a new member record to MEMBER_RECORDS_CHANNEL_ID and index it.

    Uses the member's uploaded screenshot as the record image
    (``image_bytes``); when none is available, posts text-only with the media
    gallery stripped. Fail-soft.
    """
    if not MEMBER_RECORDS_CHANNEL_ID:
        return
    avatar_bytes = await _render_record_avatar_bytes(
        _member_avatar_url(member)
    )
    embed = _build_member_record_embed(
        member, summary_lines,
        has_image=bool(image_bytes), has_avatar=bool(avatar_bytes),
    )
    if image_bytes or avatar_bytes:
        message_id = await _post_channel_embed(
            MEMBER_RECORDS_CHANNEL_ID,
            embed,
            file_bytes=image_bytes,
            file_name="record.png",
            avatar_bytes=avatar_bytes,
        )
    else:
        message_id = await _post_channel_embed(
            MEMBER_RECORDS_CHANNEL_ID, embed
        )
    logger.info(
        "records: posted member record for %s to channel %s",
        member.id, MEMBER_RECORDS_CHANNEL_ID,
    )
    # Keep the user_id -> record message_ids index fresh for fast lookups
    # (records_index). Awaited (not spawned) so the new id is in the index
    # before the per-member write lock is released — otherwise a concurrent
    # writer could still see an empty index and post a duplicate record.
    if message_id is not None:
        await asyncio.to_thread(
            records_index.add_record,
            member.id,
            message_id,
            channel_id=MEMBER_RECORDS_CHANNEL_ID,
        )


async def _edit_member_record(
    channel_id: int,
    message_id: int,
    member: discord.Member,
    summary_lines: list[str],
    *,
    image_bytes: bytes | None = None,
) -> None:
    """Edit an existing member record in place.

    Always re-uploads the record's images (screenshot + circular avatar) via a
    multipart PATCH so the embed's ``attachment://`` references resolve to
    freshly-uploaded files and nothing renders loose. When no new screenshot is
    supplied, the existing one is recovered from the record's current
    attachment and re-uploaded — a JSON-only edit can't keep ``attachment://``
    references resolvable for retained attachments, which left the screenshot +
    avatar rendering as duplicate loose images below the embed. Fail-soft.
    """
    avatar_bytes = await _render_record_avatar_bytes(
        _member_avatar_url(member)
    )
    if image_bytes is None:
        # Prefer the in-memory cache (the member's most recent screenshot) so
        # the edit re-uploads it without a CDN round-trip.
        image_bytes = _get_cached_screenshot(member.id)
    if image_bytes is None:
        # Recover the existing screenshot so we can re-upload it alongside the
        # avatar (keeping it by id won't resolve attachment://record.png).
        existing = await _fetch_record_message(channel_id, message_id)
        existing_atts = [
            a for a in (existing or {}).get("attachments", [])
            if isinstance(a, dict)
        ]
        shot = next(
            (
                a for a in existing_atts
                if a.get("filename") != "avatar.png"
                and str(a.get("content_type") or "").startswith("image/")
                and a.get("url")
            ),
            None,
        )
        if shot is not None:
            image_bytes = await _fetch_cdn_bytes(str(shot["url"]))
            if image_bytes is None:
                # The record HAS a screenshot but it couldn't be re-downloaded
                # (transient CDN failure). Rewriting the embed now would drop
                # the screenshot permanently (has_image=False, only the avatar
                # gets re-uploaded), so abort this refresh and leave the record
                # untouched — a later edit recovers the image and applies the
                # field changes then.
                logger.warning(
                    "records: skipping edit for %s \u2014 could not recover the "
                    "existing screenshot (CDN fetch failed); preserving it",
                    member.id,
                )
                return
    embed = _build_member_record_embed(
        member, summary_lines,
        has_image=bool(image_bytes), has_avatar=bool(avatar_bytes),
    )
    if image_bytes or avatar_bytes:
        await _edit_channel_embed(
            channel_id, message_id, embed,
            file_bytes=image_bytes, file_name="record.png",
            avatar_bytes=avatar_bytes,
        )
    else:
        await _edit_channel_embed(channel_id, message_id, embed)


_RECORD_WRITE_LOCKS: dict[int, asyncio.Lock] = {}


def _record_write_lock(user_id: int) -> asyncio.Lock:
    """Per-member lock serializing record writes.

    Without it, two concurrent ``_edit_or_create_member_record`` calls for the
    same member (e.g. the onboarding screenshot write and the
    ``on_member_update`` / ``_finalize_onboarding_with_record`` refresh fired
    ~simultaneously when the separate verified-role bot grants its role) can
    both read an empty ``records_index`` — the index write lags the post — and
    each create a *duplicate* record, so the screenshot only lands on one of
    them. Serializing per member makes the second writer observe the first's
    freshly-indexed record and edit it in place instead. Created lazily on the
    running loop; keyed by the globally-unique user id (matches the index key).
    """
    return get_or_create_lock(_RECORD_WRITE_LOCKS, user_id)


def _configured_clan_slot_for_name(name: str) -> "ClanSlot | None":
    """Return the configured clan slot whose (tag-stripped) name matches
    ``name`` case-insensitively, or None for a free-text clan."""
    wanted = _strip_clan_tag(name or "").casefold()
    if not wanted:
        return None
    return next(
        (
            s for s in CLAN_SLOTS
            if s.clan_name
            and _strip_clan_tag(s.clan_name).casefold() == wanted
        ),
        None,
    )


async def _edit_or_create_member_record(
    member: discord.Member,
    *,
    in_game_name: str | None = None,
    mastery_rank: str | None = None,
    clan_override: str | None = None,
    extra_lines: list[str] | None = None,
    image_bytes: bytes | None = None,
) -> None:
    """Maintain a member's single canonical record in the records
    ("profile-log") channel — the source of truth for member profile data.

    The body is the member's complete profile (in-game name + clan + platform
    + mastery + syndicate), derived from their current roles plus the OCR-only
    fields (``in_game_name`` / ``mastery_rank``), with any ``extra_lines``
    (e.g. a manual-review note) appended. Edits the existing record in place
    when one exists; posts a new one only when none does. Persists the
    structured profile snapshot to the durable ``member_profile`` store (the
    source of truth) after the record write.

    Fail-soft: exceptions are logged but never propagate (called as a
    background task from the onboarding / re-verify / role-change paths).
    """
    if not MEMBER_RECORDS_CHANNEL_ID:
        return
    try:
        # Serialize per member so a concurrent edit-or-create (e.g. the
        # onboarding screenshot write racing the role-change refresh) can't
        # post a duplicate record — the second writer waits, then observes the
        # first's freshly-indexed record and edits it in place.
        async with _record_write_lock(member.id):
            # Cache the freshly-supplied screenshot (replacing any prior one)
            # so later record edits + the staff manual-verify path can re-use it
            # without a CDN round-trip.
            if image_bytes is not None:
                _cache_screenshot(member.id, image_bytes)
            # Preserve the OCR-only fields (in-game handle + exact Mastery
            # Rank) from the existing record when the caller didn't supply
            # them — a role-change refresh must not clobber data that isn't
            # role-derivable. A stored free-text ("not affiliated") clan is
            # preserved the same way, but only when it doesn't name a
            # configured clan — a configured clan is role-derived, so
            # dropping the role must drop the Clan line too.
            if in_game_name is None or mastery_rank is None or clan_override is None:
                existing = await _member_profile_from_records(
                    member.guild.id, member.id
                )
                if existing:
                    if in_game_name is None:
                        in_game_name = existing.get("in_game_name")
                    if mastery_rank is None:
                        mastery_rank = existing.get("mastery_rank")
                    if clan_override is None:
                        stored_clan = existing.get("clan")
                        if stored_clan and _configured_clan_slot_for_name(
                            stored_clan
                        ) is None:
                            clan_override = stored_clan
            summary_lines = _member_record_profile_lines(
                member, in_game_name=in_game_name, mastery_rank=mastery_rank,
                clan_override=clan_override,
            )
            if extra_lines:
                summary_lines = summary_lines + list(extra_lines)
            ids = await asyncio.to_thread(
                records_index.get_record_message_ids, member.id
            )
            channel_id = _records_channel_id()
            if ids and channel_id:
                await _edit_member_record(
                    channel_id, ids[-1], member, summary_lines,
                    image_bytes=image_bytes,
                )
            elif image_bytes is not None or extra_lines:
                # A record is BORN only when it carries evidence: the member's
                # uploaded screenshot, or an explicit manual-review note. A
                # purely role-derived refresh (verified-role grant, mastery /
                # IGN edit, role change) that finds no existing record must NOT
                # mint a screenshot-less "profile" log — that empty record is
                # exactly the missing-screenshot log this guard prevents. Such
                # members get a real record when they actually upload a
                # screenshot (onboarding verify / /profile verify / /manage
                # screenshot update).
                await _create_member_record(
                    member, summary_lines, image_bytes=image_bytes,
                )
            else:
                logger.debug(
                    "records: no existing record for %s and no screenshot / "
                    "review note supplied — not creating a screenshot-less "
                    "record",
                    member.id,
                )
                return
            # Persist the structured profile snapshot to the durable store
            # (the source of truth). Done after the record write so the store
            # and its presentation mirror move together.
            await _store_member_profile_snapshot(
                member,
                in_game_name=in_game_name,
                mastery_rank=mastery_rank,
                summary_lines=summary_lines,
            )
    except Exception:
        logger.exception(
            "records: edit_or_create failed for %s", member.id
        )


def _member_record_jump_urls(guild_id: int, user_id: int) -> list[str]:
    """Build Discord jump URLs for a member's record messages.

    Reads the (tiny) records_index synchronously and resolves the records
    channel from the index, falling back to ``MEMBER_RECORDS_CHANNEL_ID``.
    Fail-soft (returns ``[]`` on any problem).
    """
    try:
        index = records_index.load_index()
        channel_id = index.get("channel_id") or MEMBER_RECORDS_CHANNEL_ID
        ids = index.get("users", {}).get(str(user_id), [])
    except Exception:
        logger.debug(
            "records index lookup failed for %s", user_id, exc_info=True
        )
        return []
    if not channel_id:
        return []
    return [
        f"https://discord.com/channels/{guild_id}/{channel_id}/{mid}"
        for mid in ids
    ]


# ---------- Member profile durable store (SQLite) ---------------------------

# The durable ``member_profile`` SQLite table (analytics.py) is the source of
# truth for the two profile fields that aren't recoverable from Discord roles —
# the in-game handle and the exact Mastery Rank — plus a role-derived
# platform/clan snapshot for the /manage console. The records ("profile-log")
# channel is now a pure presentation mirror (screenshot + a rendered profile
# embed); it is never parsed back for structured data on the hot path. Reads
# hit local SQLite, so no TTL cache is needed; the one remaining fall-through to
# the channel is a one-time lazy backfill for records written before this store
# existed.

# In-memory cache of each member's most recent profile screenshot bytes, keyed
# by user id. It is populated whenever a member submits a screenshot (every
# record write that carries an image), and a fresh screenshot simply replaces
# the previous one (old bytes dropped). Two readers draw from it:
#   * a record edit re-uploads the cached screenshot instead of re-fetching it
#     from the Discord CDN (`_edit_member_record`); and
#   * the staff "Verify" button on a halted "not affiliated" submission OCRs the
#     cached screenshot to assign roles (the submission itself runs no OCR).
# The entry is evicted when the member leaves the server (`on_member_remove`).
_screenshot_cache: dict[int, bytes] = {}


def _cache_screenshot(user_id: int, image_bytes: bytes | None) -> None:
    """Store the member's latest profile screenshot, replacing any prior one.

    A falsy ``image_bytes`` is ignored so a record refresh without a new
    screenshot never clears a previously cached one.
    """
    if image_bytes:
        _screenshot_cache[user_id] = image_bytes


def _get_cached_screenshot(user_id: int) -> bytes | None:
    """Return the member's most recent cached screenshot bytes, if any."""
    return _screenshot_cache.get(user_id)


def _evict_cached_screenshot(user_id: int) -> None:
    """Drop the member's cached screenshot (on leave)."""
    _screenshot_cache.pop(user_id, None)

# Record body parsing (labels, line regex, snowflake timestamps, V2 text
# walk, embed parse-back) lives in gpbot.records; re-exported above as the
# private bot.* names tests and callers already use.


def _records_channel_id() -> int:
    """Resolve the records channel id (records_index first, env fallback)."""
    try:
        cid = records_index.get_channel_id()
    except Exception:
        cid = None
    return int(cid or MEMBER_RECORDS_CHANNEL_ID or 0)


async def _fetch_record_message(channel_id: int, message_id: int) -> dict | None:
    """Fetch a single record message as raw JSON (so its V2 components are
    readable). Fail-soft: returns None on NotFound / any HTTP error."""
    from discord.http import Route

    try:
        data = await client.http.request(Route(
            "GET",
            "/channels/{channel_id}/messages/{message_id}",
            channel_id=channel_id,
            message_id=message_id,
        ))
        return data if isinstance(data, dict) else None
    except discord.NotFound:
        return None
    except discord.HTTPException:
        logger.debug(
            "records: failed to fetch message %s", message_id, exc_info=True
        )
        return None


async def _read_member_profile_from_records(
    guild_id: int, user_id: int
) -> dict | None:
    """Read a member's profile (in-game name + exact mastery, plus the
    role-derived clan/platform snapshot) from their newest record *message*.

    This is the legacy parse-back path, kept solely as a one-time lazy backfill
    for records written before the durable ``member_profile`` store existed.
    Returns a dict shaped like the durable store
    (``in_game_name`` / ``mastery_rank`` / ``platform`` / ``clan`` /
    ``last_verified_ts``) or None when no record exists / can't be read.
    """
    channel_id = _records_channel_id()
    if not channel_id:
        return None
    try:
        ids = await asyncio.to_thread(
            records_index.get_record_message_ids, user_id
        )
    except Exception:
        ids = []
    if not ids:
        return None
    message_id = ids[-1]  # newest appended last
    data = await _fetch_record_message(channel_id, message_id)
    if not data:
        return None
    # New records are rich embeds; older ones may still be Components V2 text.
    parsed = _parse_record_embed(data.get("embeds") or [])
    if not parsed:
        parsed = _parse_record_profile_text(
            _collect_v2_text(data.get("components") or [])
        )
    if not parsed:
        return None
    parsed.setdefault("last_verified_ts", _snowflake_ts(message_id))
    return parsed


async def _member_profile_from_records(
    guild_id: int, user_id: int
) -> dict | None:
    """Return a member's stored profile from the durable ``member_profile``
    SQLite store (the source of truth).

    On a store miss, falls back once to parsing the member's legacy record
    *message* and backfills the result into the store, so deployments that
    predate the store don't lose the in-game name / exact Mastery Rank. After
    that one read the channel is never parsed for this member again.
    """
    stored = await asyncio.to_thread(
        analytics.get_member_profile, guild_id, user_id
    )
    if stored:
        return stored
    legacy = await _read_member_profile_from_records(guild_id, user_id)
    if legacy:
        await asyncio.to_thread(
            analytics.upsert_member_profile,
            guild_id=guild_id,
            user_id=user_id,
            in_game_name=legacy.get("in_game_name"),
            mastery_rank=legacy.get("mastery_rank"),
            platform=legacy.get("platform"),
            clan=legacy.get("clan"),
            updated_ts=legacy.get("last_verified_ts"),
        )
    return legacy


async def _store_member_profile_snapshot(
    member: discord.Member,
    *,
    in_game_name: str | None,
    mastery_rank: str | None,
    summary_lines: list[str],
) -> None:
    """Persist a member's profile snapshot to the durable store after a record
    write.

    The OCR-only fields (``in_game_name`` / exact ``mastery_rank``) are only
    written when supplied — a role-derived refresh that passes ``None`` leaves
    the stored value untouched. ``mastery_rank`` is stored only when it is an
    exact ``MR n`` / ``LR n`` rank (a bogus ``MR 0`` never sticks). The
    role-derived ``platform`` / ``clan`` snapshot is parsed from the
    already-rendered ``summary_lines`` so it tracks the member's current roles.
    Fail-soft.
    """
    fields: dict[str, object] = {}
    if in_game_name is not None:
        fields["in_game_name"] = in_game_name.strip() or None
    if mastery_rank is not None:
        mr = mastery_rank.strip()
        if _is_exact_mastery_rank(mr):
            fields["mastery_rank"] = mr
    # Always refresh the role-derived platform/clan from the rendered lines.
    parsed = _parse_record_profile_text("\n".join(summary_lines))
    fields["platform"] = parsed.get("platform")
    fields["clan"] = parsed.get("clan")
    try:
        await asyncio.to_thread(
            analytics.upsert_member_profile,
            guild_id=member.guild.id,
            user_id=member.id,
            **fields,
        )
    except Exception:
        logger.debug(
            "records: profile store write failed for %s", member.id,
            exc_info=True,
        )


async def _handle_onboarding_interaction(
    interaction: discord.Interaction, custom_id: str
) -> None:
    """Dispatch interactions from the onboarding welcome prompt.

    ``custom_id`` format: ``onboard:<user_id>:<action>``
    Actions: ``clanselect`` (slot in the select's values) | ``none``.
    Legacy clan buttons (``clan:<slot_number>``) are still accepted.

    Ownership check: only the target member may interact.
    """
    parsed = parse_onboard_custom_id(
        custom_id,
        select_values=(interaction.data or {}).get("values"),
    )
    if parsed is None:
        return
    target_id, _action, slot_token = parsed
    user = interaction.user
    guild = interaction.guild

    # Ownership gate — reject any other user with an ephemeral.
    if user.id != target_id:
        with contextlib.suppress(Exception):
            await _interaction_callback(
                interaction, 4,
                [{"type": 17, "accent_color": ACCENT_FAIL,
                  "components": [{"type": 10, "content": (
                      "-# This prompt isn't for you, Operator."
                  )}]}],
            )
        return

    if guild is None:
        return

    member = guild.get_member(target_id)
    if member is None:
        # Member left between join and click.
        with contextlib.suppress(Exception):
            await _interaction_callback(
                interaction, 4,
                [{"type": 17, "accent_color": ACCENT_INCOMPLETE,
                  "components": [{"type": 10, "content": (
                      "-# Your session has expired. Please re-join the server."
                  )}]}],
            )
        return

    if slot_token == "none":
        try:
            await interaction.response.send_modal(
                _OnboardingNoClanModal(member=member)
            )
        except Exception:
            logger.exception(
                "onboarding: failed to send no-clan screenshot modal"
            )
        else:
            # Reset the dropdown so the member can re-fire "Not listed / No"
            # (Discord suppresses a string-select re-fire on an unchanged
            # value), mirroring the clan path.
            _spawn_bg_task(_reset_onboarding_select(guild.id, member.id))
        return

    if slot_token is not None:
        try:
            slot_no = int(slot_token)
        except (IndexError, ValueError):
            return
        slot = next((s for s in CLAN_SLOTS if s.slot == slot_no), None)
        if slot is None or not slot.clan_name:
            with contextlib.suppress(Exception):
                await _interaction_callback(
                    interaction, 4,
                    [{"type": 17, "accent_color": ACCENT_FAIL,
                      "components": [{"type": 10, "content": (
                          "-# That clan slot is no longer configured. "
                          "Please contact a staff member."
                      )}]}],
                )
            return
        try:
            await interaction.response.send_modal(
                _OnboardingVerifyModal(member=member, claimed_slot=slot)
            )
        except Exception:
            logger.exception("onboarding: failed to send verify modal")
        else:
            # Reset the dropdown so the member can re-pick the same clan and
            # retry — Discord suppresses a string-select re-fire on an
            # unchanged value, so without this a failed attempt can't be
            # retried with the same clan.
            _spawn_bg_task(_reset_onboarding_select(guild.id, member.id))


async def _onboarding_reprompt_task() -> None:
    """Background loop: re-post welcome prompts for members who haven't
    completed onboarding within ONBOARDING_REPROMPT_HOURS. Deletes the
    previous prompt message, posts a fresh one, and updates the DB.

    Also reconciles on startup: accounts for prompts whose window elapsed
    while the bot was offline, and cleans up completed / stale rows.
    """
    while True:
        try:
            await asyncio.sleep(ONBOARDING_POLL_SECONDS)
        except asyncio.CancelledError:
            return
        try:
            await _onboarding_reprompt_sweep()
        except Exception:
            logger.exception("onboarding reprompt sweep failed")


async def _onboarding_reprompt_sweep() -> None:
    """Single sweep: check all pending prompts and re-post those overdue."""
    pending = await asyncio.to_thread(analytics.list_pending_onboarding_prompts)
    if not pending:
        return
    now = time.time()
    window_secs = ONBOARDING_REPROMPT_HOURS * 3600
    for row in pending:
        guild_id = row["guild_id"]
        user_id = row["user_id"]
        channel_id = row["channel_id"]
        message_id = row["message_id"]
        posted_ts = row["posted_ts"]
        reprompt_count = row["reprompt_count"]

        guild = client.get_guild(guild_id)
        if guild is None:
            continue
        member = guild.get_member(user_id)

        decision = reprompt_decision(
            posted_ts=posted_ts,
            reprompt_count=reprompt_count,
            now=now,
            window_secs=window_secs,
            max_reprompts=ONBOARDING_MAX_REPROMPTS,
            member_present=member is not None,
        )
        if decision is RepromptDecision.WAIT:
            continue
        if decision is RepromptDecision.CLEANUP:
            # Member left; clean up.
            await asyncio.to_thread(
                analytics.delete_onboarding_prompt, guild_id, user_id
            )
            continue
        if decision is RepromptDecision.STOP:
            logger.info(
                "onboarding: %s in guild %s hit max reprompts (%d); stopping",
                user_id, guild_id, ONBOARDING_MAX_REPROMPTS,
            )
            await asyncio.to_thread(
                analytics.complete_onboarding_prompt, guild_id, user_id
            )
            continue

        # Delete old prompt message (best-effort; already-deleted is fine).
        if message_id and channel_id:
            await _delete_message(channel_id, message_id)

        # Post fresh welcome.
        components = _onboarding_welcome_components(user_id, guild)
        new_msg_id = await _post_channel_v2(
            ONBOARDING_CHANNEL_ID,
            components,
            mention_user_ids=[user_id],
        )
        if new_msg_id is None:
            logger.warning(
                "onboarding reprompt: post failed for %s in guild %s",
                user_id, guild_id,
            )
            continue
        await asyncio.to_thread(
            analytics.upsert_onboarding_prompt,
            guild_id=guild_id,
            user_id=user_id,
            channel_id=ONBOARDING_CHANNEL_ID,
            message_id=new_msg_id,
            posted_ts=int(now),
        )
        logger.info(
            "onboarding: re-prompted %s in guild %s (reprompt #%d, msg=%s)",
            user_id, guild_id, reprompt_count + 1, new_msg_id,
        )


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
        f"-# Host: `Local` (self-hosted on your device)\n"
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

    return (
        f"**Channels**\n"
        f"-# Onboarding: {fmt(ONBOARDING_CHANNEL_ID)}\n"
        f"-# Records: {fmt(MEMBER_RECORDS_CHANNEL_ID)}\n"
        f"-# Help: {fmt(HELP_CHANNEL_ID)}\n"
        f"\n**Messaging**\n"
        f"-# Reply TTL: `{REPLY_TTL_SECONDS}s`"
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
            "-# Ensure the `./data` directory is writable to enable."
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
    resolved_roles = 0
    resolved_emojis = 0
    for slot in configured:
        role = guild.get_role(slot.role_id) if slot.role_id else None
        if role is not None:
            resolved_roles += 1
        members = len(role.members) if role else 0
        glyph = slot.emoji or "\u2022"
        if slot.emoji:
            resolved_emojis += 1
        rows.append((slot.clan_name, glyph, members, role is None))

    rows.sort(key=lambda r: (-r[2], r[0].lower()))

    lines = [
        f"**Clans** ({len(rows)} configured; roles {resolved_roles}/{len(rows)}; "
        f"emojis {resolved_emojis}/{len(rows)})",
        "-# Clan role IDs and emoji literals are auto-synced from the server "
        "and written back to the bot's local `.env` on connect.",
    ]
    for name, glyph, members, missing in rows:
        suffix = " \u26a0\ufe0f missing role" if missing else ""
        lines.append(f"-# {glyph} `{name}` \u2014 `{members}` members{suffix}")
    return "\n".join(lines)


def _clan_sync_summary_line() -> str:
    configured = [s for s in CLAN_SLOTS if s.clan_name]
    if not configured:
        return "-# Clan role IDs and emoji literals are auto-synced from the server and written back to the bot's local `.env` on connect."

    resolved_roles = sum(1 for s in configured if s.role_id)
    resolved_emojis = sum(1 for s in configured if s.emoji)
    return (
        f"-# Clan sync: roles {resolved_roles}/{len(configured)}; "
        f"emojis {resolved_emojis}/{len(configured)}. "
        "Role IDs and emoji literals are auto-synced from the server and "
        "written back to the bot's local `.env` on connect."
    )


def _status_page_latency(_interaction: discord.Interaction, _snap: dict) -> str:
    """Render the live performance metrics page for /status."""
    from utils.retry import ocr_circuit_breaker
    snap = metrics_snapshot()

    sem = snap["semaphore"]
    ocr = snap["ocr_latency"]
    ana = snap["analytics_latency"]

    cb = ocr_circuit_breaker.snapshot()
    cb_state = cb["state"]
    cb_icon = {"closed": "\u2705", "open": "\u274C", "half_open": "\u26a0\ufe0f"}.get(cb_state, "\u2753")

    lines = [
        "**Heavy-job semaphore**",
        f"-# Concurrency limit: `{HEAVY_JOB_CONCURRENCY}`",
        f"-# Current / Peak: `{sem['current']}` / `{sem['peak']}`",
        f"-# Queued now: `{sem['queued']}`",
        f"-# Total acquired: `{sem['total_acquired']}`",
        f"-# Avg queue wait: `{sem['avg_queue_wait_ms']:.0f} ms`",
        "",
        "**OCR latency** (session)",
    ]
    if ocr["samples"]:
        lines += [
            f"-# Samples: `{ocr['samples']}`",
            f"-# Avg: `{ocr['avg_ms']:.0f} ms`",
            f"-# p50: `{ocr['p50_ms']:.0f} ms` \u2022 p95: `{ocr['p95_ms']:.0f} ms` \u2022 p99: `{ocr['p99_ms']:.0f} ms`",
        ]
    else:
        lines.append("-# No samples yet.")
    lines += [
        "",
        "**Analytics query latency** (session)",
    ]
    if ana["samples"]:
        lines += [
            f"-# Samples: `{ana['samples']}`",
            f"-# Avg: `{ana['avg_ms']:.0f} ms`",
            f"-# p95: `{ana['p95_ms']:.0f} ms`",
        ]
    else:
        lines.append("-# No samples yet.")
    lines += [
        "",
        "**OCR.space circuit breaker**",
        f"-# State: {cb_icon} `{cb_state}`",
        f"-# Failures: `{cb['failures']}` / `{cb['threshold']}`",
        f"-# Recovery window: `{cb['recovery_seconds']:.0f}s`",
    ]
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
    ("latency",  "Latency",   _status_page_latency),
]

# Pages that consume `analytics.summary()`. Computing the snapshot fires
# ~7 SQL queries, so we only do it when the active page needs it.
_PAGES_NEEDING_SNAPSHOT = frozenset({"ocr", "stats"})


def _pagination_nav_row(
    total: int,
    page: int,
    *,
    prev_id: str,
    noop_id: str,
    next_id: str,
    refresh_id: str,
) -> dict:
    """Shared Prev / page-indicator / Next / Refresh nav row for the
    paginated /status and /manage panels. Callers supply the four button
    custom_ids (the only thing that differs between the two panels)."""
    last = total - 1
    return {
        "type": 1,
        "components": [
            {"type": 2, "style": 2, "label": "\u25C0 Prev",
             "custom_id": prev_id, "disabled": page == 0},
            {"type": 2, "style": 2,
             "label": f"{page + 1}/{total}",
             "custom_id": noop_id, "disabled": True},
            {"type": 2, "style": 2, "label": "Next \u25B6",
             "custom_id": next_id, "disabled": page >= last},
            {"type": 2, "style": 1,
             "emoji": {"name": "\U0001F504"},
             "custom_id": refresh_id},
        ],
    }


def _status_nav_row(page: int) -> dict:
    return _pagination_nav_row(
        len(_STATUS_PAGES), page,
        prev_id=f"status:{page - 1}",
        noop_id="status:noop",
        next_id=f"status:{page + 1}",
        refresh_id=f"status:{page}",
    )


def _status_page_needs_snapshot(page: int) -> bool:
    """Whether the page at ``page`` consumes ``analytics.summary()`` \u2014 lets the
    async callers fetch the snapshot off the event loop before building."""
    page = max(0, min(page, len(_STATUS_PAGES) - 1))
    return _STATUS_PAGES[page][0] in _PAGES_NEEDING_SNAPSHOT


def _status_components(
    interaction: discord.Interaction,
    page: int,
    snap: dict | None = None,
) -> list[dict]:
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
        # Snapshot is fetched off-loop by the caller (see _send_status_page);
        # fall back to a synchronous fetch only if one wasn't supplied.
        if key in _PAGES_NEEDING_SNAPSHOT:
            snap = snap if snap is not None else analytics.summary()
        else:
            snap = {}
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
    flags = COMPONENTS_V2_FLAG
    if ephemeral:
        flags |= EPHEMERAL_FLAG
    await _discord_call_with_retry(
        lambda: _v2_interaction_callback(
            http_client=client.http,
            interaction=interaction,
            callback_type=callback_type,
            components=components,
            flags=flags,
            allowed_mentions=_ALLOWED_MENTIONS_NONE,
        ),
        label="interaction callback",
    )
    with contextlib.suppress(AttributeError):
        interaction.response._responded = True  # type: ignore[attr-defined]


async def _interaction_edit_original_v2(
    interaction: discord.Interaction, components: list[dict]
) -> None:
    """PATCH a deferred interaction's original message with refreshed V2
    components via raw HTTP.

    discord.py 2.x can't edit a message with raw Components V2 dicts, so
    after a DEFERRED_UPDATE_MESSAGE ack we hit the interaction webhook's
    ``@original`` route directly. The message keeps its IS_COMPONENTS_V2
    flag, so the edit only needs to carry the new component tree.
    """
    await _discord_call_with_retry(
        lambda: _v2_interaction_edit_original(
            http_client=client.http,
            interaction=interaction,
            components=components,
            allowed_mentions=_ALLOWED_MENTIONS_NONE,
        ),
        label="interaction edit original",
    )


async def _send_status_page(interaction: discord.Interaction, page: int) -> None:
    try:
        snap = None
        if _status_page_needs_snapshot(page):
            t0 = time.monotonic()
            snap = await asyncio.to_thread(analytics.summary)
            from utils.metrics import analytics_query_latency
            analytics_query_latency.record((time.monotonic() - t0) * 1000)
        components = _status_components(interaction, page, snap)
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


# ---------- /manage (admin backup console) ----------------------------------

# Manual admin counterpart to the autonomous on-leave data clear
# (`on_member_remove`). Styled exactly like /status: a single ephemeral
# Components V2 message that paginates through a member's stored data with
# Prev/Next/Refresh buttons, plus a confirm-gated "Clear stored data" button
# on the last page so a moderator can wipe a member's profile + titles by
# hand whenever the automatic clear isn't enough (e.g. a member who is still
# in the server, or a leave the bot missed while offline).

# (page key, title) — mirrors `_STATUS_PAGES`. Bodies are built inline in
# `_manage_components` because each one needs the gathered snapshot + member.
_MANAGE_PAGES: list[tuple[str, str]] = [
    ("overview", "Overview"),
    ("edit",     "Edit"),
    ("titles",   "Titles"),
    ("data",     "Data & Clear"),
]

# Index of the "Edit" page — the hub for the per-field editors. Kept as a
# lookup (rather than a literal) so reordering _MANAGE_PAGES can't desync the
# Back buttons that return to it.
_MANAGE_EDIT_PAGE = next(
    i for i, (k, _t) in enumerate(_MANAGE_PAGES) if k == "edit"
)

# Index of the "Data & Clear" page — used by the clear/confirm flow (and the
# Cancel button) so reordering _MANAGE_PAGES can't desync them.
_MANAGE_DATA_PAGE = next(
    i for i, (k, _t) in enumerate(_MANAGE_PAGES) if k == "data"
)

# The fields the /manage Edit page can mutate. Each opens its own sub-editor
# (a modal for the in-game name and titles, an inline select/buttons
# sub-view for the rest). Order drives the button layout on the Edit page.
_MANAGE_EDIT_FIELDS: list[tuple[str, str, str]] = [
    # (field key, button label, button emoji unicode)
    ("ign",       "In-game name", "\U0001F3F7\uFE0F"),   # 🏷️
    ("platform",  "Platform",     "\U0001F3AE"),         # 🎮
    ("mastery",   "Mastery Rank", "\U0001F3C5"),         # 🏅
    ("clan",      "Clan",         "\U0001F6E1\uFE0F"),   # 🛡️
    ("syndicate", "Syndicates",   "\U0001F516"),         # 🔖
    ("titles",    "Titles",       "\U0001F451"),         # 👑
]


def _button_emoji_from_literal(raw: str | None) -> dict | None:
    """Convert an emoji literal into a Components V2 emoji dict.

    Accepts a custom-emoji literal (``<:name:id>`` / ``<a:name:id>``) →
    ``{"id", "name", "animated"}`` or a bare unicode emoji → ``{"name": ...}``.
    Returns None for empty input so callers can omit the key.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    m = _CUSTOM_EMOJI_RE.match(raw)
    if m:
        return {
            "id": m.group(3),
            "name": m.group(2),
            "animated": m.group(1) == "a",
        }
    return {"name": raw}


async def _manage_snapshot(guild_id: int, user_id: int) -> dict:
    """Gather a member's profile + titles for the /manage panel.

    Reads the profile from the durable ``member_profile`` store (source of
    truth) and the awarded titles off the event loop (SQLite), fail-soft.
    Returns ``{"profile", "titles", "records"}``.
    """
    profile, titles, records = await asyncio.gather(
        _member_profile_from_records(guild_id, user_id),
        asyncio.to_thread(
            analytics.list_member_titles, guild_id, user_id
        ),
        asyncio.to_thread(
            _member_record_jump_urls, guild_id, user_id
        ),
    )
    return {"profile": profile, "titles": titles, "records": records}


def _manage_nav_row(member_id: int, page: int) -> dict:
    return _pagination_nav_row(
        len(_MANAGE_PAGES), page,
        prev_id=f"manage:{member_id}:p:{page - 1}",
        noop_id="manage:noop",
        next_id=f"manage:{member_id}:p:{page + 1}",
        refresh_id=f"manage:{member_id}:p:{page}",
    )


# ---------- /manage role-derived state (the Edit page reads roles) ----------

# Discord roles are the source of truth for a member's platform / clan /
# mastery bucket / syndicates; the durable store only carries the exact
# in-game name + the fine-grained Mastery Rank override. These helpers read
# the current role state synchronously (member.roles) so the Edit page can
# show "what they hold right now" and pre-select the editor controls.


def _member_current_platform(member: discord.Member) -> str | None:
    """Return the platform key (e.g. ``"PC"``) for the first configured
    platform role the member holds, or None."""
    role_ids = {r.id for r in member.roles}
    for platform, rid in PLATFORM_ROLE_IDS.items():
        if rid and rid in role_ids:
            return platform
    return None


def _member_current_clan_slot(member: discord.Member) -> "ClanSlot | None":
    """Return the configured clan slot whose role the member holds, or None."""
    role_ids = {r.id for r in member.roles}
    return next(
        (s for s in CLAN_SLOTS if s.role_id and s.role_id in role_ids),
        None,
    )


def _member_current_syndicate_ids(member: discord.Member) -> set[int]:
    """Return the configured syndicate role IDs the member currently holds."""
    syn_ids = set(SYNDICATE_ROLE_IDS)
    return {r.id for r in member.roles if r.id in syn_ids}


def _member_current_mr_role(member: discord.Member) -> "discord.Role | None":
    """Return the configured MR bucket role the member currently holds (the
    coarse Discord role), or None."""
    mr_ids = set(MR_ROLE_IDS)
    return next((r for r in member.roles if r.id in mr_ids), None)


async def _apply_platform_role(
    member: discord.Member, platform: str
) -> str:
    """Assign ``member`` the configured role for ``platform`` (removing any
    other configured platform roles) and persist the platform to the store.

    Returns ``"assigned"``, ``"no_match"`` (platform not configured), or
    ``"error"`` (role edit failed).
    """
    target_id = PLATFORM_ROLE_IDS.get(platform)
    target = member.guild.get_role(target_id) if target_id else None
    if target is None:
        return "no_match"
    plat_ids = {rid for rid in PLATFORM_ROLE_IDS.values() if rid}
    have_ids = {r.id for r in member.roles}
    to_remove = [
        r for r in member.roles if r.id in plat_ids and r.id != target.id
    ]
    ops = []
    if to_remove:
        ops.append(member.remove_roles(*to_remove, reason="Manage: platform edit"))
    if target.id not in have_ids:
        ops.append(member.add_roles(target, reason="Manage: platform edit"))
    try:
        if ops:
            await _gather_role_ops(*ops)
    except discord.HTTPException:
        logger.exception("manage: platform role swap failed")
        return "error"
    return "assigned"


async def _apply_clan_slot(member: discord.Member, slot: "ClanSlot") -> str:
    """Assign ``member`` the role for clan ``slot`` (removing any other
    configured clan roles).

    Returns ``"assigned"``, ``"no_match"`` (slot has no resolvable role), or
    ``"error"`` (role edit failed). The role change refreshes the member's
    record via ``on_member_update``.
    """
    target = member.guild.get_role(slot.role_id) if slot.role_id else None
    if target is None:
        return "no_match"
    clan_ids = {s.role_id for s in CLAN_SLOTS if s.role_id}
    have_ids = {r.id for r in member.roles}
    to_remove = [
        r for r in member.roles if r.id in clan_ids and r.id != target.id
    ]
    ops = []
    if to_remove:
        ops.append(member.remove_roles(*to_remove, reason="Manage: clan edit"))
    if target.id not in have_ids:
        ops.append(member.add_roles(target, reason="Manage: clan edit"))
    try:
        if ops:
            await _gather_role_ops(*ops)
    except discord.HTTPException:
        logger.exception("manage: clan role swap failed")
        return "error"
    return "assigned"


async def _remove_configured_clan_roles(member: discord.Member) -> str:
    """Remove every configured clan role from ``member`` ("Not Affiliated").

    Returns ``"assigned"`` (incl. no-op) or ``"error"`` (role edit failed).
    """
    clan_ids = {s.role_id for s in CLAN_SLOTS if s.role_id}
    to_remove = [r for r in member.roles if r.id in clan_ids]
    try:
        if to_remove:
            await member.remove_roles(
                *to_remove, reason="Manage: clan edit (not affiliated)"
            )
    except discord.HTTPException:
        logger.exception("manage: clan role removal failed")
        return "error"
    return "assigned"


async def _sync_syndicate_roles(
    member: discord.Member, wanted_ids: set[int]
) -> str:
    """Make ``member``'s configured syndicate roles exactly match
    ``wanted_ids`` (a subset of ``SYNDICATE_ROLE_IDS``).

    Returns ``"assigned"`` (incl. no-op) or ``"error"``.
    """
    syn_ids = set(SYNDICATE_ROLE_IDS)
    wanted = wanted_ids & syn_ids
    have = {r.id for r in member.roles if r.id in syn_ids}
    to_add = [
        role for rid in (wanted - have)
        if (role := member.guild.get_role(rid)) is not None
    ]
    to_remove = [
        role for rid in (have - wanted)
        if (role := member.guild.get_role(rid)) is not None
    ]
    ops = []
    if to_add:
        ops.append(member.add_roles(*to_add, reason="Manage: syndicate edit"))
    if to_remove:
        ops.append(member.remove_roles(
            *to_remove, reason="Manage: syndicate edit"
        ))
    try:
        if ops:
            await _gather_role_ops(*ops)
    except discord.HTTPException:
        logger.exception("manage: syndicate role sync failed")
        return "error"
    return "assigned"


def _manage_edit_page_components(
    member_id: int,
    member: "discord.Member | None",
    profile: dict | None,
    *,
    note: str | None = None,
) -> list[dict]:
    """Build the body of the /manage **Edit** page (the per-field hub).

    Shows the member's current role-derived state (platform / clan / mastery
    bucket / syndicates straight off their roles, in-game name from the store)
    next to a button per editable field. Each button opens its own sub-editor
    (a modal for the in-game name, an inline select/buttons sub-view for the
    rest). For a departed member (``member is None``) there are no roles to
    edit, so only an explanatory line is shown.
    """
    body: list[dict] = []
    if member is None:
        body.append({"type": 10, "content": (
            "**Edit**\n-# This member has left the server, so their roles "
            "can't be edited. Use the **Data & Clear** page to wipe their "
            "stored data."
        )})
        return body

    ign = (profile or {}).get("in_game_name")
    platform = _member_current_platform(member)
    clan_slot = _member_current_clan_slot(member)
    clan_name = (
        _strip_clan_tag(clan_slot.clan_name or "") if clan_slot else None
    )
    if not clan_name:
        # No configured clan role — fall back to the stored free-text
        # ("not affiliated") clan, when one is on record.
        clan_name = (profile or {}).get("clan")
    mr_role = _member_current_mr_role(member)
    mastery_override = (profile or {}).get("mastery_rank")
    if mastery_override:
        mr_display = _format_mastery_display(mastery_override) or mastery_override
    elif mr_role is not None:
        mr_display = mr_role.name
    else:
        mr_display = None
    syn_ids = _member_current_syndicate_ids(member)
    syn_names = [
        r.name for r in member.roles if r.id in syn_ids
    ]

    def _val(v: "str | None") -> str:
        return f"`{v}`" if v else "*(unset)*"

    # Current value per field key (titles is shown via its own page).
    values: dict[str, str] = {
        "ign": _val(ign),
        "platform": _val(platform),
        "mastery": _val(mr_display),
        "clan": _val(clan_name),
        "syndicate": (
            ", ".join(f"`{n}`" for n in syn_names) if syn_names else "*(none)*"
        ),
    }
    lines = [
        f"**Edit member data** ({len(values)} fields)",
        "-# Edits update the member's Discord roles **and** the stored "
        "profile together \u2014 pick a field below to change it.",
        "",
    ]
    # Drive the display rows off _MANAGE_EDIT_FIELDS so the emoji/label live
    # in exactly one place (the same source the buttons below use). The row
    # layout mirrors the /status Clans page: glyph + code label + em-dash
    # detail.
    for field, label, emoji in _MANAGE_EDIT_FIELDS:
        if field in values:
            lines.append(f"-# {emoji} `{label}` \u2014 {values[field]}")
    if note:
        lines.append("")
        lines.append(f"-# {note}")
    body.append({"type": 10, "content": "\n".join(lines)})

    # One button per editable field. ``ign`` and ``titles`` open modals; the
    # rest open inline sub-editors. Split across rows of 5.
    field_buttons: list[dict] = []
    for field, label, emoji in _MANAGE_EDIT_FIELDS:
        if field == "ign":
            cid = f"manage:{member_id}:ign"
        elif field == "titles":
            cid = f"manage:{member_id}:titles"
        else:
            cid = f"manage:{member_id}:editfield:{field}"
        field_buttons.append({
            "type": 2, "style": 2, "label": label,
            "emoji": {"name": emoji}, "custom_id": cid,
        })
    for i in range(0, len(field_buttons), 5):
        body.append({"type": 1, "components": field_buttons[i:i + 5]})
    return body


def _manage_editor_components(
    member_id: int,
    member: discord.Member,
    field: str,
    *,
    note: str | None = None,
) -> list[dict]:
    """Build the full Components V2 payload for a single-field sub-editor.

    ``field`` is one of ``"platform"``, ``"mastery"``, ``"clan"``,
    ``"syndicate"``. Each renders the appropriate control (a select per
    field — the clan editor adds a "Not Affiliated" free-text button)
    pre-reflecting the member's current state, plus a Back button to the
    Edit page. The in-game name and titles fields don't route here (each
    opens a modal instead).
    """
    back_row = {"type": 1, "components": [
        {"type": 2, "style": 2, "label": "\u25C0 Back",
         "custom_id": f"manage:{member_id}:p:{_MANAGE_EDIT_PAGE}"},
    ]}
    container: list[dict] = []
    title = "Edit"

    if field == "platform":
        title = "Platform"
        configured = [
            (p, rid) for p, rid in PLATFORM_ROLE_IDS.items() if rid
        ]
        current = _member_current_platform(member)
        if not configured:
            container.append({"type": 10, "content": (
                "**Platform**\n-# No platform roles are configured on this "
                "server."
            )})
        else:
            options = []
            for plat, _rid in configured:
                opt = {"label": plat, "value": plat, "default": plat == current}
                emoji = _button_emoji_from_literal(PLATFORM_EMOJIS.get(plat))
                if emoji:
                    opt["emoji"] = emoji
                options.append(opt)
            container.append({"type": 10, "content": (
                "**Platform**\n-# Pick the member's platform. This assigns "
                "the matching platform role (replacing any other) and stores "
                "it."
            )})
            container.append({"type": 1, "components": [{
                "type": 3, "custom_id": f"manage:{member_id}:setplatform",
                "placeholder": "Select platform",
                "min_values": 1, "max_values": 1, "options": options,
            }]})

    elif field == "mastery":
        title = "Mastery Rank"
        first, second = _mastery_select_options()
        container.append({"type": 10, "content": (
            "**Mastery Rank**\n-# Pick the member's exact rank (Legendary "
            "ranks too). This swaps their MR bucket role and stores the exact "
            "rank."
        )})
        for ph, opts, suffix in (
            ("Set Mastery Rank (1\u201325)", first, "a"),
            ("Set Mastery Rank 26\u201330 / Legendary 1\u20138", second, "b"),
        ):
            container.append({"type": 1, "components": [{
                "type": 3, "custom_id": f"manage:{member_id}:setmr:{suffix}",
                "placeholder": ph,
                "min_values": 1, "max_values": 1,
                "options": [
                    {"label": o.label, "value": o.value} for o in opts
                ],
            }]})

    elif field == "clan":
        title = "Clan"
        configured = [s for s in CLAN_SLOTS if s.clan_name and s.role_id]
        current = _member_current_clan_slot(member)
        if not configured:
            container.append({"type": 10, "content": (
                "**Clan**\n-# No clan slots are configured on this server."
            )})
        else:
            container.append({"type": 10, "content": (
                "**Clan**\n-# Pick the member's clan. This assigns the clan "
                "role (replacing any other clan role) and stores it."
            )})
            # Select options: names + emojis come straight from the live clan
            # slots (the same data /status shows), so they update on their own
            # whenever an emblem or clan is reconfigured.
            options = []
            for slot in configured:
                opt = {
                    "label": (_strip_clan_tag(slot.clan_name or "") or "?")[:100],
                    "value": str(slot.slot),
                    "default": bool(current and current.slot == slot.slot),
                }
                emoji = _button_emoji_from_literal(slot.emoji)
                if emoji:
                    opt["emoji"] = emoji
                options.append(opt)
            container.append({"type": 1, "components": [{
                "type": 3, "custom_id": f"manage:{member_id}:setclan",
                "placeholder": "Select clan",
                "min_values": 1, "max_values": 1, "options": options,
            }]})
        # "Not Affiliated": free-text clan for members outside the configured
        # slots — opens a modal to type the clan name, removes any configured
        # clan role, and stores the typed name.
        container.append({"type": 1, "components": [
            {"type": 2, "style": 2, "label": "Not Affiliated",
             "emoji": {"name": "\U0001F4DD"},
             "custom_id": f"manage:{member_id}:clanother"},
        ]})

    elif field == "syndicate":
        title = "Syndicates"
        configured = [
            r for rid in SYNDICATE_ROLE_IDS
            if (r := member.guild.get_role(rid)) is not None
        ]
        current_ids = _member_current_syndicate_ids(member)
        if not configured:
            container.append({"type": 10, "content": (
                "**Syndicates**\n-# No syndicate roles are configured on this "
                "server."
            )})
        else:
            options = []
            for r in configured:
                opt = {
                    "label": r.name[:100], "value": str(r.id),
                    "default": r.id in current_ids,
                }
                emoji = _button_emoji_from_literal(_syndicate_style(r.name)[1])
                if emoji:
                    opt["emoji"] = emoji
                options.append(opt)
            container.append({"type": 10, "content": (
                "**Syndicates**\n-# Select every syndicate the member belongs "
                "to (clear all to remove them). Roles are synced to match."
            )})
            container.append({"type": 1, "components": [{
                "type": 3, "custom_id": f"manage:{member_id}:setsyn",
                "placeholder": "Select syndicates",
                "min_values": 0, "max_values": len(options),
                "options": options,
            }]})

    if note:
        container.append({"type": 10, "content": f"-# {note}"})
    container.append(back_row)
    return [
        {"type": 10,
         "content": f"### \U0001F6E0\uFE0F  Manage \u2014 Edit {title}"},
        {"type": 17, "accent_color": ACCENT_PASS, "components": container},
    ]


def _manage_components(
    member_id: int,
    member: "discord.Member | None",
    page: int,
    snap: dict,
    *,
    confirm_clear: bool = False,
    cleared: dict | None = None,
    note: str | None = None,
) -> list[dict]:
    """Build the Components V2 payload for one /manage page.

    Tolerates ``member is None`` (the target already left the server) so the
    panel still works as a backup clear for departed members — it falls back
    to the stored in-game name / bare id for the identity line.
    """
    page = max(0, min(page, len(_MANAGE_PAGES) - 1))
    key, title = _MANAGE_PAGES[page]
    nav_row = _manage_nav_row(member_id, page)
    profile = snap.get("profile")
    titles = snap.get("titles") or []
    accent = ACCENT_PASS

    if member is not None:
        ident = f"<@{member_id}> (`{member_id}`)"
        name = member.display_name
    else:
        stored_name = (profile or {}).get("in_game_name")
        name = _strip_clan_tag(stored_name) if stored_name else f"User {member_id}"
        ident = f"*(not in server)* `{member_id}`"

    if key == "overview":
        lines = ["**Member**", f"-# {ident}", "", "**Stored profile**"]
        if profile:
            ign = profile.get("in_game_name")
            mr = profile.get("mastery_rank")
            plat = profile.get("platform")
            clan = profile.get("clan")
            lv = profile.get("last_verified_ts")
            lines.append(f"-# In-game name: {f'`{ign}`' if ign else '*(unset)*'}")
            lines.append(f"-# Mastery Rank: {f'`{mr}`' if mr else '*(unset)*'}")
            lines.append(f"-# Platform: {f'`{plat}`' if plat else '*(unset)*'}")
            lines.append(f"-# Clan: {f'`{clan}`' if clan else '*(unset)*'}")
            lines.append(
                f"-# Last verified: {f'<t:{int(lv)}:R>' if lv else '*(never)*'}"
            )
        else:
            lines.append("-# No stored profile data.")
        lines.append(f"-# Titles: `{len(titles)}`")
        records = snap.get("records") or []
        if records:
            recent = records[-3:]
            start = len(records) - len(recent) + 1
            links = " \u00b7 ".join(
                f"[record {n}]({u})"
                for n, u in enumerate(recent, start=start)
            )
            more = len(records) - len(recent)
            tail = f" (+{more} older)" if more else ""
            lines.append(
                f"-# Records: `{len(records)}` \u2014 {links}{tail}"
            )
        else:
            lines.append("-# Records: *(none)*")
        lines.append("")
        lines.append(_clan_sync_summary_line())
        container_components = [{"type": 10, "content": "\n".join(lines)}]
        if member is not None:
            # Admin shortcut: OCR a fresh profile screenshot and write the
            # member's in-game name / clan / mastery straight into the store
            # + roles (mirrors the member-facing /profile "Verify Profile
            # Data" button). Only offered while the member is still here.
            container_components.append({"type": 1, "components": [
                {"type": 2, "style": 1,
                 "label": "Update from screenshot",
                 "emoji": {"name": "\U0001F4F7"},
                 "custom_id": f"manage:{member_id}:update"},
                {"type": 2, "style": 2,
                 "label": "Start onboarding",
                 "emoji": {"name": "\U0001F44B"},
                 "custom_id": f"manage:{member_id}:onboard"},
            ]})
        container_components.append(nav_row)
    elif key == "titles":
        lines = [f"**Titles** ({len(titles)})"]
        if titles:
            for t in titles:
                t_name = t.get("title") or "?"
                reason = (t.get("reason") or "").strip()
                ts = t.get("awarded_ts")
                row = f"-# \u2022 `{t_name}`"
                if reason:
                    row += f" \u2014 {reason}"
                if ts:
                    row += f" \u2022 <t:{int(ts)}:R>"
                lines.append(row)
        else:
            lines.append("-# No titles awarded.")
        container_components = [
            {"type": 10, "content": "\n".join(lines)}, nav_row,
        ]
    elif key == "edit":
        container_components = _manage_edit_page_components(
            member_id, member, profile, note=note,
        )
        container_components.append(nav_row)
    else:  # "data"
        has_profile = "yes" if profile else "no"
        lines = [
            "**Stored data**",
            f"-# Profile (records channel): `{has_profile}`",
            f"-# Titles: `{len(titles)}`",
            "",
        ]
        action_row: dict | None = None
        if cleared is not None:
            lines.append(
                "-# \u2705 Cleared \u2014 removed titles "
                f"({cleared.get('titles', 0)}); anonymised "
                f"{cleared.get('events_anonymized', 0)} telemetry rows."
            )
            lines.append("-# Roles aren't touched \u2014 remove those manually.")
        elif confirm_clear:
            accent = ACCENT_FAIL
            lines.append(
                f"-# \u26A0\uFE0F This permanently deletes **{name}**'s titles "
                "and anonymises their telemetry. Roles are NOT touched. This "
                "can't be undone."
            )
            action_row = {"type": 1, "components": [
                {"type": 2, "style": 4, "label": "Confirm clear",
                 "custom_id": f"manage:{member_id}:clearok"},
                {"type": 2, "style": 2, "label": "Cancel",
                 "custom_id": f"manage:{member_id}:p:{_MANAGE_DATA_PAGE}"},
            ]}
        else:
            lines.append(
                "-# Clearing removes the durable store (profile + titles) and "
                "anonymises telemetry. Roles are NOT touched. This is the "
                "manual backup of the automatic on-leave clear."
            )
            if profile or titles:
                action_row = {"type": 1, "components": [
                    {"type": 2, "style": 4, "label": "Clear stored data",
                     "custom_id": f"manage:{member_id}:clear"},
                ]}
        container_components = [{"type": 10, "content": "\n".join(lines)}]
        if action_row is not None:
            container_components.append(action_row)
        container_components.append(nav_row)

    return [
        {"type": 10, "content": f"### \U0001F6E0\uFE0F  Manage \u2014 {title}"},
        {
            "type": 17,
            "accent_color": accent,
            "components": container_components,
        },
    ]


@tree.command(
    name="manage",
    description="Admin backup console: view or clear a member's stored data.",
)
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(
    member="The member whose stored data to inspect or clear.",
)
async def manage_cmd(
    interaction: discord.Interaction,
    member: discord.User,
) -> None:
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message(
            "\u274C /manage can only be used in a server.", ephemeral=True
        )
        return
    try:
        target = guild.get_member(member.id)
        snap = await _manage_snapshot(guild.id, member.id)
        components = _manage_components(member.id, target, 0, snap)
        await _interaction_callback(interaction, 4, components)
    except Exception:
        logger.exception("/manage failed")
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "\u274C Failed to open the manage panel.", ephemeral=True
            )


async def _handle_manage_interaction(
    interaction: discord.Interaction, custom_id: str
) -> None:
    """Dispatch the /manage panel's buttons (Prev/Next/Refresh + the
    confirm-gated Clear). Re-checks Manage Server on every click as defence
    in depth even though the panel is ephemeral (only the invoker sees it).
    """
    parts = custom_id.split(":")
    if len(parts) >= 2 and parts[1] == "noop":
        with contextlib.suppress(Exception):
            await _interaction_callback(interaction, 6, [])  # DEFERRED_UPDATE
        return

    user = interaction.user
    guild = interaction.guild
    if guild is None or not (
        isinstance(user, discord.Member)
        and user.guild_permissions.manage_guild
    ):
        with contextlib.suppress(Exception):
            await _interaction_callback(interaction, 6, [])
        return

    try:
        member_id = int(parts[1])
    except (IndexError, ValueError):
        return
    action = parts[2] if len(parts) > 2 else "p"
    member = guild.get_member(member_id)

    if action == "clear":
        snap = await _manage_snapshot(guild.id, member_id)
        components = _manage_components(
            member_id, member, _MANAGE_DATA_PAGE, snap, confirm_clear=True
        )
        with contextlib.suppress(Exception):
            await _interaction_callback(interaction, 7, components)
        return

    if action == "clearok":
        result = await asyncio.to_thread(
            analytics.delete_member_data,
            guild_id=guild.id,
            user_id=member_id,
        )
        logger.info(
            "manage: admin %s cleared data for %s in guild %s \u2014 "
            "titles=%d onboarding=%d events=%d",
            user.id, member_id, guild.id,
            result.get("titles", 0),
            result.get("onboarding", 0),
            result.get("events_anonymized", 0),
        )
        snap = await _manage_snapshot(guild.id, member_id)
        components = _manage_components(
            member_id, member, _MANAGE_DATA_PAGE, snap, cleared=result
        )
        with contextlib.suppress(Exception):
            await _interaction_callback(interaction, 7, components)
        return

    if action == "update":
        if member is None:
            # Raced: the member left between render and click. Nothing to
            # verify from a screenshot (no roles to assign).
            with contextlib.suppress(Exception):
                await _interaction_callback(
                    interaction, 4,
                    [{"type": 10,
                      "content": "### \U0001F6E0\uFE0F  Manage"},
                     {"type": 17, "accent_color": ACCENT_FAIL,
                      "components": [{"type": 10, "content": (
                          "-# That member is no longer in the server, so "
                          "there's nothing to update from a screenshot."
                      )}]}],
                )
            return
        try:
            await interaction.response.send_modal(
                _ManageScreenshotModal(member=member, admin_id=user.id)
            )
        except Exception:
            logger.exception("manage: send screenshot modal failed")
        return

    if action == "onboard":
        if member is None:
            # Raced: the member left between render and click. The welcome
            # prompt @-mentions the member, so there's nothing to onboard.
            with contextlib.suppress(Exception):
                await _interaction_callback(
                    interaction, 4,
                    [{"type": 10, "content": "### \U0001F44B  Onboarding"},
                     {"type": 17, "accent_color": ACCENT_FAIL,
                      "components": [{"type": 10, "content": (
                          "-# That member is no longer in the server, so the "
                          "onboarding welcome can't be posted."
                      )}]}],
                )
            return
        if ONBOARDING_CHANNEL_ID <= 0:
            with contextlib.suppress(Exception):
                await _interaction_callback(
                    interaction, 4,
                    [{"type": 10, "content": "### \U0001F44B  Onboarding"},
                     {"type": 17, "accent_color": ACCENT_FAIL,
                      "components": [{"type": 10, "content": (
                          "-# No onboarding channel is configured "
                          "(`ONBOARDING_CHANNEL_ID`)."
                      )}]}],
                )
            return
        posted = await _post_onboarding_welcome(member)
        logger.info(
            "manage: admin %s triggered onboarding for %s in guild %s (posted=%s)",
            user.id, member_id, guild.id, posted,
        )
        if posted:
            body = (
                f"-# \u2705 Posted the onboarding welcome for "
                f"**{member.display_name}** in <#{ONBOARDING_CHANNEL_ID}>."
            )
            accent_color = ACCENT_PASS
        else:
            body = (
                "-# \u274C Couldn't post the onboarding welcome \u2014 check "
                "my access to the onboarding channel and try again."
            )
            accent_color = ACCENT_FAIL
        with contextlib.suppress(Exception):
            await _interaction_callback(
                interaction, 4,
                [{"type": 10, "content": "### \U0001F44B  Onboarding"},
                 {"type": 17, "accent_color": accent_color,
                  "components": [{"type": 10, "content": body}]}],
            )
        return

    # --- Per-field editors (the Edit page) --------------------------------
    # These all need the member present (they mutate roles). For a departed
    # member just refresh the panel, which renders the "left the server" note.
    _EDIT_ACTIONS = {
        "editfield", "ign", "titles",
        "setplatform", "setmr", "setclan", "setsyn", "clanother",
    }
    if action in _EDIT_ACTIONS and member is None:
        snap = await _manage_snapshot(guild.id, member_id)
        components = _manage_components(
            member_id, None, _MANAGE_EDIT_PAGE, snap
        )
        with contextlib.suppress(Exception):
            await _interaction_callback(interaction, 7, components)
        return

    if action == "ign":
        try:
            stored = await _member_profile_from_records(guild.id, member_id)
            await interaction.response.send_modal(_ManageIGNModal(
                member=member, current=(stored or {}).get("in_game_name"),
            ))
        except Exception:
            logger.exception("manage: send IGN modal failed")
        return

    if action == "titles":
        # Open the same interactive form as /titles, member pre-selected.
        try:
            await interaction.response.send_modal(
                _TitlesModal(member=member)
            )
        except Exception:
            logger.exception("manage: send titles modal failed")
        return

    if action == "editfield":
        field = parts[3] if len(parts) > 3 else ""
        components = _manage_editor_components(member_id, member, field)
        with contextlib.suppress(Exception):
            await _interaction_callback(interaction, 7, components)
        return

    if action == "setplatform":
        values = (interaction.data or {}).get("values") or []
        note = "No platform selected."
        if values:
            status = await _apply_platform_role(member, values[0])
            note = {
                "assigned": f"\u2705 Platform set to **{values[0]}**.",
                "no_match": "That platform role isn't configured here.",
                "error": "Couldn't change the role \u2014 check my Manage "
                         "Roles permission and role position.",
            }.get(status, "Updated.")
        components = _manage_editor_components(
            member_id, member, "platform", note=note
        )
        with contextlib.suppress(Exception):
            await _interaction_callback(interaction, 7, components)
        return

    if action == "setmr":
        values = (interaction.data or {}).get("values") or []
        note = "No rank selected."
        kind, _, num = (values[0].partition(":") if values else ("", "", ""))
        if kind in ("MR", "LR") and num.isdigit() and int(num) > 0:
            value = int(num)
            # Persist the exact rank (not role-derivable) to the durable
            # store concurrently with the role swap; the channel-mirror
            # record edit happens off the critical path.
            status, _ = await asyncio.gather(
                _apply_mastery_bucket(member, kind, value),
                asyncio.to_thread(
                    analytics.upsert_member_profile,
                    guild_id=guild.id,
                    user_id=member_id,
                    mastery_rank=f"{kind} {value}",
                ),
            )
            _spawn_bg_task(_edit_or_create_member_record(
                member, mastery_rank=f"{kind} {value}",
            ))
            disp = _format_mastery_display(f"{kind} {value}")
            note = {
                "assigned": f"\u2705 Mastery Rank set to **{disp}**.",
                "no_match": f"Saved **{disp}**, but no matching MR bucket "
                            "role is configured here.",
                "error": f"Saved **{disp}**, but I couldn't change the role "
                         "\u2014 check my Manage Roles permission.",
            }.get(status, "Updated.")
        elif values:
            # Malformed select value — don't store a bogus rank.
            logger.warning("manage: ignoring malformed setmr value %r", values[0])
            note = "Couldn't read that rank \u2014 try again."
        components = _manage_editor_components(
            member_id, member, "mastery", note=note
        )
        with contextlib.suppress(Exception):
            await _interaction_callback(interaction, 7, components)
        return

    if action == "setclan":
        note = "No clan selected."
        values = (interaction.data or {}).get("values") or []
        try:
            slot_no = int(values[0]) if values else int(parts[3])
        except (IndexError, ValueError):
            slot_no = 0
        slot = next((s for s in CLAN_SLOTS if s.slot == slot_no), None)
        if slot is not None:
            status = await _apply_clan_slot(member, slot)
            label = _strip_clan_tag(slot.clan_name or "") or "?"
            note = {
                "assigned": f"\u2705 Clan set to **{label}**.",
                "no_match": "That clan slot has no resolvable role.",
                "error": "Couldn't change the role \u2014 check my Manage "
                         "Roles permission and role position.",
            }.get(status, "Updated.")
        components = _manage_editor_components(
            member_id, member, "clan", note=note
        )
        with contextlib.suppress(Exception):
            await _interaction_callback(interaction, 7, components)
        return

    if action == "clanother":
        # "Not Affiliated": open the free-text clan modal, prefilled with the
        # stored clan when it's already a free-text (non-configured) one.
        try:
            stored = await _member_profile_from_records(guild.id, member_id)
            stored_clan = (stored or {}).get("clan")
            current = (
                stored_clan
                if stored_clan
                and _configured_clan_slot_for_name(stored_clan) is None
                else None
            )
            await interaction.response.send_modal(_ManageClanNameModal(
                member=member, current=current,
            ))
        except Exception:
            logger.exception("manage: send clan name modal failed")
        return

    if action == "setsyn":
        values = (interaction.data or {}).get("values") or []
        wanted = {int(v) for v in values if str(v).isdigit()}
        status = await _sync_syndicate_roles(member, wanted)
        note = (
            f"\u2705 Syndicates updated ({len(wanted)} selected)."
            if status == "assigned"
            else "Couldn't update the roles \u2014 check my Manage Roles "
                 "permission and role position."
        )
        components = _manage_editor_components(
            member_id, member, "syndicate", note=note
        )
        with contextlib.suppress(Exception):
            await _interaction_callback(interaction, 7, components)
        return

    try:
        page = int(parts[3])
    except (IndexError, ValueError):
        page = 0
    snap = await _manage_snapshot(guild.id, member_id)
    components = _manage_components(member_id, member, page, snap)
    with contextlib.suppress(Exception):
        await _interaction_callback(interaction, 7, components)  # UPDATE_MESSAGE


# CDN GETs (avatar / emoji / record-screenshot recovery) retry transient
# failures so a momentary blip can't, e.g., drop a member-record screenshot in
# _edit_member_record. Only network errors / timeouts / 429 / 5xx are retried;
# a non-transient response (403/404/…) returns None immediately so a genuinely
# missing asset stays fast.
_CDN_RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})
_CDN_RETRY_BASE = 0.5
_CDN_RETRY_MAX_DELAY = 4.0


async def _fetch_cdn_bytes(
    url: str, *, accept: str = "image/png,image/*;q=0.8", attempts: int = 3,
) -> bytes | None:
    """GET ``url`` via the shared session; return the body or None on failure.

    Discord's CDN 403s requests without a recognisable User-Agent, so we
    always send one. Shared by the avatar, emoji, and record-screenshot
    fetchers. Transient failures (network error / timeout / 429 / 5xx) are
    retried up to ``attempts`` times with short exponential back-off (honouring
    a ``Retry-After`` header when present); a non-transient response (e.g.
    403/404) returns None immediately.
    """
    from utils.retry import exponential_backoff

    headers = {
        "User-Agent": "DiscordBot (https://github.com/aidenlong04/Golden-Pagoda-Image-Reader, 1.0)",
        "Accept": accept,
    }
    timeout = aiohttp.ClientTimeout(total=8)
    attempts = max(1, attempts)
    for attempt in range(1, attempts + 1):
        delay: float | None = None
        try:
            session = await _get_http_session()
            async with session.get(
                url, headers=headers, timeout=timeout
            ) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    return data or None
                if resp.status not in _CDN_RETRYABLE_STATUS:
                    logger.warning(
                        "cdn fetch returned HTTP %s for %s", resp.status, url
                    )
                    return None
                # Transient HTTP status — honour Retry-After when present.
                retry_after = resp.headers.get("Retry-After")
                try:
                    delay = (
                        min(float(retry_after), _CDN_RETRY_MAX_DELAY)
                        if retry_after else None
                    )
                except (TypeError, ValueError):
                    delay = None
                logger.warning(
                    "cdn fetch returned HTTP %s for %s (attempt %d/%d)",
                    resp.status, url, attempt, attempts,
                )
        except Exception:
            logger.warning(
                "cdn fetch failed for %s (attempt %d/%d)",
                url, attempt, attempts, exc_info=True,
            )
        if attempt >= attempts:
            break
        if delay is None:
            delay = exponential_backoff(
                attempt, base=_CDN_RETRY_BASE, cap=_CDN_RETRY_MAX_DELAY
            )
        await asyncio.sleep(delay)
    return None


async def _fetch_avatar_bytes(url: str) -> bytes | None:
    """Best-effort avatar fetch (avatars are <512 KiB); None on any failure."""
    return await _fetch_cdn_bytes(url, accept="image/png,image/webp,image/*;q=0.8")


# Diameter (px) of the circular record-avatar thumbnail. The /profile card
# renders at 112px; the record thumbnail can be a touch larger since Discord
# downscales it for display.
_RECORD_AVATAR_SIZE = 256


def _render_circular_avatar_png(avatar_bytes: bytes | None) -> bytes:
    """Render ``avatar_bytes`` into the /profile-style circular avatar (gold
    ring, transparent corners) and return PNG bytes. Runs in a worker thread
    via :func:`_run_heavy`."""
    img = _circular_avatar(avatar_bytes, _RECORD_AVATAR_SIZE)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


async def _render_record_avatar_bytes(avatar_url: str | None) -> bytes | None:
    """Fetch a member's avatar and render it as the /profile-style circular
    thumbnail (gold ring) for the record embed. Fail-soft -> None so a record
    still posts (with the square URL thumbnail) when the avatar can't render.
    """
    if not avatar_url:
        return None
    avatar_bytes = await _fetch_avatar_bytes(avatar_url)
    if not avatar_bytes:
        return None
    try:
        return await _run_heavy(_render_circular_avatar_png, avatar_bytes)
    except Exception:
        logger.exception("records: circular avatar render failed")
        return None


def _member_avatar_url(member: object) -> str | None:
    """Best-effort 256px PNG avatar URL for a Member/User (or a stand-in that
    only exposes ``display_avatar``). None when no avatar is resolvable."""
    avatar = getattr(member, "display_avatar", None) or getattr(
        member, "default_avatar", None
    )
    if avatar is None:
        return None
    try:
        return avatar.replace(size=256, format="png").url
    except Exception:
        url = getattr(avatar, "url", None)
        return str(url) if url else None



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
    cached = _EMOJI_BYTES_CACHE.get(eid)
    if cached is not None:
        return cached
    url = f"https://cdn.discordapp.com/emojis/{eid}.png?size=128&quality=lossless"
    data = await _fetch_cdn_bytes(url)
    # Only cache successful fetches — caching a transient CDN failure (None)
    # would permanently blank that icon for the rest of the process life.
    if data is not None:
        _EMOJI_BYTES_CACHE[eid] = data
    return data


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
    ops = []
    if to_remove:
        ops.append(member.remove_roles(
            *to_remove, reason="Mastery rank self-service edit"
        ))
    if target.id not in have_ids:
        ops.append(member.add_roles(
            target, reason="Mastery rank self-service edit"
        ))
    try:
        if ops:
            await _gather_role_ops(*ops)
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
        in_game_name: str | None = None,
    ) -> None:
        super().__init__(timeout=300)
        self.member = member
        self.owner_id = owner_id
        self.avatar_bytes = avatar_bytes
        self.display_name = display_name
        self.in_game_name = in_game_name
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
                    "Operator, you can't use this.", ephemeral=True
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

        # The role swap and the durable-store upsert are independent, and the
        # re-render below reads the *store* (not the channel mirror), so run
        # them concurrently and await both before rendering — the mirror edit
        # in the records channel moves off the critical path as a bg task.
        status, _ = await asyncio.gather(
            _apply_mastery_bucket(self.member, kind, value),
            asyncio.to_thread(
                analytics.upsert_member_profile,
                guild_id=self.member.guild.id,
                user_id=self.member.id,
                mastery_rank=f"{kind} {value}",
            ),
        )
        _spawn_bg_task(_edit_or_create_member_record(
            self.member, mastery_rank=f"{kind} {value}",
        ))
        info = await _member_profile_info_lines(self.member)
        png = await _run_heavy(
            _render_profile_card_png,
            avatar_bytes=self.avatar_bytes,
            display_name=self.display_name,
            info_lines=info,
            in_game_name=self.in_game_name,
        )
        # Drop any now-earned "Assign <category>" buttons (e.g. the Mastery
        # Rank just set), re-add the ones still outstanding, and keep the
        # screenshot button in sync — all via the shared rebuilder.
        _sync_profile_action_items(
            self,
            member=self.member,
            owner_id=self.owner_id,
            avatar_bytes=self.avatar_bytes,
            display_name=self.display_name,
            info=info,
            in_game_name=self.in_game_name,
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


class _ScreenshotVerifyModal(_GPModal):
    """Modal that lets a member upload a Warframe profile screenshot. On
    submit we OCR it and assign/store every field we can (in-game name,
    clan role, mastery rank), then re-render the /profile card in place.

    Used instead of asking the member to type their handle: one screenshot
    fills everything the verification flow would have pulled.
    """

    def __init__(
        self, *, member: discord.Member, owner_id: int,
        avatar_bytes: bytes | None, display_name: str,
        source_view: "discord.ui.View | None",
    ) -> None:
        super().__init__(title="Submit Profile Screenshot", timeout=600)
        self._gp_member = member
        self._gp_owner_id = owner_id
        self._gp_avatar_bytes = avatar_bytes
        self._gp_display_name = display_name
        self._gp_source_view = source_view
        self.screenshot = discord.ui.FileUpload(
            min_values=1, max_values=1, required=True,
        )
        self.add_item(discord.ui.Label(
            text="Profile screenshot",
            description=(
                "Upload a screenshot of your Warframe profile "
                "(title bar + CLAN visible)."
            ),
            component=self.screenshot,
        ))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        files = list(self.screenshot.values or [])
        if not files:
            with contextlib.suppress(discord.HTTPException):
                await interaction.response.defer()
            return
        attachment = files[0]
        # OCR + role HTTP can take seconds; ack as a deferred message update
        # so we can edit the source card afterward (edit_original_response).
        with contextlib.suppress(discord.HTTPException):
            await interaction.response.defer()
        try:
            image_bytes = await attachment.read()
        except Exception:
            logger.warning(
                "profile screenshot: attachment read failed", exc_info=True
            )
            with contextlib.suppress(discord.HTTPException):
                await interaction.followup.send(
                    "Couldn't read that upload — try again.",
                    ephemeral=True,
                )
            return

        member = self._gp_member
        # Use the shared screenshot-ingest pipeline: OCR + re-fetch + record.
        result, member = await _ingest_profile_screenshot(
            member,
            image_bytes=image_bytes,
            attachment=attachment,
            finalize_onboarding=False,
        )
        summary = result.summary

        # Refresh card from the (now-current) member roles and record. The
        # two store reads are independent, so run them concurrently.
        info, in_game_name = await asyncio.gather(
            _member_profile_info_lines(member),
            _member_in_game_name(member),
        )
        png = await _run_heavy(
            _render_profile_card_png,
            avatar_bytes=self._gp_avatar_bytes,
            display_name=self._gp_display_name,
            info_lines=info,
            in_game_name=in_game_name,
        )

        # Refresh the action buttons (drop the screenshot button once a
        # handle exists, re-add link buttons for whatever's still missing)
        # and keep a mastery editor's cached headline in sync.
        view = self._gp_source_view
        if view is not None:
            _sync_profile_action_items(
                view,
                member=member,
                owner_id=self._gp_owner_id,
                avatar_bytes=self._gp_avatar_bytes,
                display_name=self._gp_display_name,
                info=info,
                in_game_name=in_game_name,
            )
            if hasattr(view, "in_game_name"):
                view.in_game_name = in_game_name

        with contextlib.suppress(discord.HTTPException):
            await interaction.edit_original_response(
                attachments=[
                    discord.File(io.BytesIO(png), filename="profile.png")
                ],
                view=view,
            )

        if summary:
            body = (
                "> Operator, your personal data has been adjusted.\n"
                + "\n".join(f"* -# {line}" for line in summary)
            )
        else:
            body = (
                "I couldn't read that screenshot. Upload a clearer PNG/JPG "
                "with your title bar (PlayerName#NNN) and CLAN visible."
            )
        with contextlib.suppress(discord.HTTPException):
            await interaction.followup.send(body, ephemeral=True)


class _ScreenshotVerifyButton(discord.ui.Button):
    """Prompt button on a member's own /profile card, shown only when the
    verification/OCR flow never pulled an in-game name. Opens
    ``_ScreenshotVerifyModal`` so the member can self-verify by uploading a
    profile screenshot."""

    def __init__(
        self, *, member: discord.Member, owner_id: int,
        avatar_bytes: bytes | None, display_name: str,
    ) -> None:
        super().__init__(
            style=discord.ButtonStyle.danger,
            label="Verify Profile Data",
            emoji=(
                discord.PartialEmoji(name="verify", id=ASSIGN_ROLE_EMOJI_ID)
                if ASSIGN_ROLE_EMOJI_ID
                else None
            ),
        )
        # Non-reserved names: discord.py's Item._run_checks recurses through
        # ``_parent``; clobbering it (or ``_view``) breaks dispatch.
        self._gp_member = member
        self._gp_owner_id = owner_id
        self._gp_avatar_bytes = avatar_bytes
        self._gp_display_name = display_name

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self._gp_owner_id:
            with contextlib.suppress(discord.HTTPException):
                await interaction.response.send_message(
                    "Operator, you can't use this.", ephemeral=True
                )
            return
        modal = _ScreenshotVerifyModal(
            member=self._gp_member,
            owner_id=self._gp_owner_id,
            avatar_bytes=self._gp_avatar_bytes,
            display_name=self._gp_display_name,
            source_view=self.view,
        )
        await interaction.response.send_modal(modal)


class _ManageIGNModal(_GPModal):
    """Admin modal opened from the /manage Edit page to set a member's stored
    in-game name by hand (no OCR). Writes ``in_game_name`` to the durable
    store and refreshes the Edit page in place.
    """

    def __init__(
        self, *, member: discord.Member, current: str | None = None
    ) -> None:
        super().__init__(title="Set In-game Name", timeout=600)
        self._gp_member = member
        self.ign = discord.ui.TextInput(
            label="In-game name",
            placeholder="PlayerName#123",
            default=current or None,
            required=True,
            max_length=64,
        )
        self.add_item(self.ign)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        user = interaction.user
        if not (
            isinstance(user, discord.Member)
            and user.guild_permissions.manage_guild
        ):
            with contextlib.suppress(discord.HTTPException):
                await interaction.response.send_message(
                    "Operator, you can't use this.", ephemeral=True
                )
            return
        member = self._gp_member
        value = (self.ign.value or "").strip()
        # Refresh the Edit page in place (the modal submit is a fresh
        # interaction, so ack as a deferred update then PATCH @original).
        # Ack BEFORE the writes so the click is acknowledged instantly.
        with contextlib.suppress(discord.HTTPException):
            await interaction.response.defer()
        if value:
            # The snapshot read below hits the durable store, so only the
            # store upsert must land before the refresh; the channel-mirror
            # record edit moves off the critical path.
            await asyncio.to_thread(
                analytics.upsert_member_profile,
                guild_id=member.guild.id,
                user_id=member.id,
                in_game_name=value,
            )
            _spawn_bg_task(
                _edit_or_create_member_record(member, in_game_name=value)
            )
        with contextlib.suppress(Exception):
            snap = await _manage_snapshot(member.guild.id, member.id)
            components = _manage_components(
                member.id, member, _MANAGE_EDIT_PAGE, snap,
                note=f"\u2705 In-game name set to **{_strip_clan_tag(value)}**."
                if value else "No name entered.",
            )
            await _interaction_edit_original_v2(interaction, components)


class _ManageClanNameModal(_GPModal):
    """Admin modal opened from the /manage clan editor's "Not Affiliated"
    button. Collects a free-text clan name for a member outside the
    configured clan slots, removes any configured clan role, and stores the
    typed name in the member's record. If the typed name actually matches a
    configured clan, that clan's role is assigned instead (same as picking it
    from the select).
    """

    def __init__(
        self, *, member: discord.Member, current: str | None = None
    ) -> None:
        super().__init__(title="Set Clan (Not Affiliated)", timeout=600)
        self._gp_member = member
        self.clan = discord.ui.TextInput(
            label="Clan name",
            placeholder="The member's clan (outside this server's clans).",
            default=current or None,
            required=True,
            max_length=64,
        )
        self.add_item(self.clan)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        # Defence in depth: re-check Manage Server before mutating roles.
        user = interaction.user
        if not (
            isinstance(user, discord.Member)
            and user.guild_permissions.manage_guild
        ):
            with contextlib.suppress(discord.HTTPException):
                await interaction.response.send_message(
                    "Operator, you can't use this.", ephemeral=True
                )
            return
        member = self._gp_member
        member = member.guild.get_member(member.id) or member
        value = _strip_clan_tag((self.clan.value or "").strip())
        note = "No clan name entered."
        if value:
            slot = _configured_clan_slot_for_name(value)
            if slot is not None:
                # The typed name is one of this server's clans — assign the
                # role instead of storing a free-text duplicate.
                status = await _apply_clan_slot(member, slot)
                label = _strip_clan_tag(slot.clan_name or "") or "?"
                note = {
                    "assigned": (
                        f"✅ **{label}** is a configured clan — "
                        "assigned its role instead."
                    ),
                    "no_match": "That clan slot has no resolvable role.",
                    "error": "Couldn't change the role — check my "
                             "Manage Roles permission and role position.",
                }.get(status, "Updated.")
            else:
                status = await _remove_configured_clan_roles(member)
                await _edit_or_create_member_record(
                    member, clan_override=value
                )
                note = (
                    f"✅ Clan set to **{value}** (not affiliated — "
                    "configured clan roles removed)."
                    if status == "assigned"
                    else f"Stored clan **{value}**, but I couldn't remove "
                         "the clan role — check my Manage Roles "
                         "permission."
                )
        # Refresh the clan editor in place (the modal submit is a fresh
        # interaction, so ack as a deferred update then PATCH @original).
        with contextlib.suppress(discord.HTTPException):
            await interaction.response.defer()
        with contextlib.suppress(Exception):
            components = _manage_editor_components(
                member.id, member, "clan", note=note,
            )
            await _interaction_edit_original_v2(interaction, components)


class _ManageScreenshotModal(_GPModal):
    """Admin screenshot modal opened from the /manage panel. OCRs a member's
    Warframe profile screenshot and writes their in-game name / clan /
    mastery straight into the durable store + roles (same pipeline as the
    member-facing self-verification), then refreshes the /manage Overview
    page in place.

    Distinct from ``_ScreenshotVerifyModal`` (which re-renders a member's own
    /profile card): this one targets *another* member on an admin's behalf
    and refreshes the text-based V2 manage panel instead.
    """

    def __init__(self, *, member: discord.Member, admin_id: int) -> None:
        super().__init__(title="Update Member from Screenshot", timeout=600)
        self._gp_member = member
        self._gp_admin_id = admin_id
        self.screenshot = discord.ui.FileUpload(
            min_values=1, max_values=1, required=True,
        )
        self.add_item(discord.ui.Label(
            text="Profile screenshot",
            description=(
                "Upload the member's Warframe profile screenshot "
                "(title bar + CLAN visible)."
            ),
            component=self.screenshot,
        ))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        # Defence in depth: Discord only shows a modal to the user it was
        # sent to, but re-check Manage Server before mutating another member.
        user = interaction.user
        if not (
            isinstance(user, discord.Member)
            and user.guild_permissions.manage_guild
        ):
            with contextlib.suppress(discord.HTTPException):
                await interaction.response.send_message(
                    "Operator, you can't use this.", ephemeral=True
                )
            return

        member = self._gp_member
        files = list(self.screenshot.values or [])
        if not files:
            with contextlib.suppress(discord.HTTPException):
                await interaction.response.defer()
            return
        attachment = files[0]
        # OCR + role HTTP can take seconds; ack as a deferred message update
        # so we can refresh the /manage panel afterward (@original PATCH).
        with contextlib.suppress(discord.HTTPException):
            await interaction.response.defer()
        try:
            image_bytes = await attachment.read()
        except Exception:
            logger.warning(
                "manage screenshot: attachment read failed", exc_info=True
            )
            with contextlib.suppress(discord.HTTPException):
                await interaction.followup.send(
                    "Couldn't read that upload — try again.",
                    ephemeral=True,
                )
            return

        # Use the shared screenshot-ingest pipeline: OCR + re-fetch + record +
        # finalize. The /manage admin path sets finalize_onboarding=True since
        # an admin verifying someone completes their onboarding (different from
        # the member-initiated /profile flow where it's False).
        result, member = await _ingest_profile_screenshot(
            member,
            image_bytes=image_bytes,
            attachment=attachment,
            finalize_onboarding=True,
        )
        summary = result.summary

        # Refresh the /manage Overview page in place so the stored-profile
        # block reflects whatever the screenshot just wrote.
        with contextlib.suppress(Exception):
            snap = await _manage_snapshot(member.guild.id, member.id)
            components = _manage_components(member.id, member, 0, snap)
            await _interaction_edit_original_v2(interaction, components)

        if summary:
            # Welcome-only response: post the public verified-welcome to the
            # server-entry channel for the target member.
            await _post_onboarding_pass_welcome(member)
        else:
            with contextlib.suppress(discord.HTTPException):
                await interaction.followup.send(
                    "I couldn't read that screenshot. Upload a clearer PNG/JPG "
                    "with the title bar (PlayerName#NNN) and CLAN visible.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )


# _VerifyResult / VerifyState live in gpbot.verify (imported at top) so the
# pipeline states are unit-testable without a gateway connection.


async def _verify_member_from_screenshot(
    member: discord.Member, *, image_bytes: bytes,
    filename: str, content_type: str,
) -> _VerifyResult:
    """OCR a Warframe profile screenshot and assign every role we can for
    ``member`` (clan + mastery bucket), returning the parsed in-game name +
    exact mastery rank for the caller to write into the member's record.

    Shares the OCR + parse pipeline (``_ocr_profile_fields``) with the
    onboarding + /manage flows, but scoped to a single member's self-service:
    no reactions, no public reply, no verify/incomplete role gating. Returns
    a ``_VerifyResult`` whose ``summary`` describes what was updated; an empty
    summary means the screenshot couldn't be read (``result.state`` says why:
    ``INVALID_IMAGE`` vs ``OCR_FAILED``).
    """
    if not await asyncio.to_thread(validate_image_bytes, image_bytes):
        return _VerifyResult.failed(VerifyState.INVALID_IMAGE)

    fields = await _ocr_profile_fields(image_bytes, filename, content_type)
    if not fields.ok:
        return _VerifyResult.failed(VerifyState.OCR_FAILED)
    profile_name = fields.profile_name
    clan_name = fields.clan_name
    mastery_rank = fields.mastery_rank

    summary: list[str] = []

    # Kick the clan role add and the mastery bucket swap off concurrently —
    # they touch disjoint role IDs via independent per-role REST calls — then
    # collect their results in the original summary order.
    clan_task = None
    if clan_name:
        role = _find_clan_role(member.guild, clan_name)
        if role is not None:
            clan_task = asyncio.create_task(_add_role(
                member, role, "Profile screenshot self-verification"
            ))
    token = parse_mastery_token(mastery_rank) if mastery_rank else None
    mastery_task = None
    if token is not None:
        mastery_task = asyncio.create_task(
            _apply_mastery_bucket(member, *token)
        )

    if profile_name:
        summary.append(f"In-game name: **{_strip_clan_tag(profile_name)}**")

    if clan_name:
        clan_label = _strip_clan_tag(clan_name)
        if clan_task is not None:
            _changed, status = await clan_task
            summary.append(f"Clan: {status}")
        else:
            summary.append(
                f"Clan **{clan_label}** isn't configured on this server."
            )

    if mastery_rank:
        if mastery_task is not None:
            kind, value = token
            status = await mastery_task
            disp = _format_mastery_display(f"{kind} {value}")
            if status == "assigned":
                summary.append(f"Mastery Rank: **{disp}**")
            else:
                summary.append(
                    f"Mastery Rank **{disp}** saved (no matching rank role)."
                )
        else:
            summary.append(f"Mastery Rank: **{mastery_rank}**")

    return _VerifyResult(summary, profile_name, mastery_rank)


async def _ingest_profile_screenshot(
    member: discord.Member,
    *,
    image_bytes: bytes,
    attachment: "discord.Attachment",
    finalize_onboarding: bool = False,
) -> tuple[_VerifyResult, discord.Member]:
    """Shared core of every screenshot flow: OCR a profile screenshot, assign
    every role it can, and write the member's canonical record.

    Used by the member self-verify (``/profile``), the admin ``/manage`` panel
    and the records-channel "Add/Update Screenshot" button so all three share
    one pipeline. Returns the ``_VerifyResult`` plus the (re-fetched) member so
    callers can refresh their own UI from freshly assigned roles.

    The record write is awaited (not a background task) so a caller reading the
    record straight afterwards sees the new in-game name + exact mastery; it
    also invalidates the record-profile TTL cache. An empty ``result.summary``
    means the screenshot couldn't be read — no record is written and no roles
    are assigned. When ``finalize_onboarding`` is set, a successful read also
    completes the member's onboarding (admin-initiated paths).
    """
    result = await _verify_member_from_screenshot(
        member,
        image_bytes=image_bytes,
        filename=attachment.filename or "profile.png",
        content_type=attachment.content_type or "image/png",
    )
    # Re-fetch so callers re-render from freshly assigned roles (the cached
    # member.roles lags the role-add gateway event).
    with contextlib.suppress(discord.HTTPException):
        member = await member.guild.fetch_member(member.id)
    if result.summary:
        await _edit_or_create_member_record(
            member,
            in_game_name=result.in_game_name,
            mastery_rank=result.mastery_rank,
            image_bytes=image_bytes,
        )
        if finalize_onboarding:
            await _finalize_onboarding(member.guild.id, member.id)
    return result, member


def _sync_profile_action_items(
    view: discord.ui.View, *, member: discord.Member, owner_id: int,
    avatar_bytes: bytes | None, display_name: str,
    info: list[tuple], in_game_name: str | None,
) -> None:
    """Rebuild a /profile view's action items (link + screenshot buttons)
    from the member's current state, leaving any mastery selects in place.

    Shared by the initial /profile build, the mastery editor refresh, and
    the screenshot-verify refresh so all three stay consistent: drop the
    existing "Assign <category>" link buttons + the screenshot button, then
    re-add link buttons for whatever's still missing and the screenshot
    button while the in-game name is still unknown — but only for members who
    aren't verified yet (verified members don't get verification prompts).
    """
    for item in list(view.children):
        if isinstance(item, _ScreenshotVerifyButton):
            view.remove_item(item)
        elif (
            isinstance(item, discord.ui.Button)
            and item.style == discord.ButtonStyle.link
        ):
            view.remove_item(item)
    for btn in _assign_role_buttons(
        member.guild, _missing_assignable_categories(info)
    ):
        view.add_item(btn)
    if not in_game_name and not _member_is_verified(member):
        view.add_item(_ScreenshotVerifyButton(
            member=member,
            owner_id=owner_id,
            avatar_bytes=avatar_bytes,
            display_name=display_name,
        ))


def _can_use_command(
    member: discord.Member,
    role_ids: Iterable[int],
    *,
    open_when_empty: bool = False,
) -> bool:
    """Return True when ``member`` may use a role-gated command.

    Server managers are always allowed. Otherwise the member must hold one of
    the (non-zero) ``role_ids``. When ``open_when_empty`` is set and no gate
    role is configured, the command stays open to everyone.
    """
    ids = {rid for rid in role_ids if rid}
    if open_when_empty and not ids:
        return True
    if member.guild_permissions.manage_guild:
        return True
    return any(r.id in ids for r in member.roles)


def _can_use_profile_options(member: discord.Member) -> bool:
    """Return True when ``member`` may make a /profile reply public.

    Gates the ``ephemeral`` toggle to ``PROFILE_OPTIONS_ROLE_IDS`` (server
    managers are always allowed). Members without one of those roles always
    get an ephemeral (private) reply. The ``user`` target is open to everyone,
    and ``edit_mastery`` is self-only.
    """
    return _can_use_command(member, PROFILE_OPTIONS_ROLE_IDS)


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
    if not (
        isinstance(target, discord.Member)
        and isinstance(interaction.user, discord.Member)
    ):
        await interaction.response.send_message(
            "\u274C /profile can only be used in a server.", ephemeral=True
        )
        return

    # /profile is open to everyone and anyone may target any member. Only the
    # `ephemeral` toggle is gated to PROFILE_OPTIONS_ROLE_IDS — members without
    # one of those roles always get an ephemeral (private) reply. `edit_mastery`
    # is self-only (enforced below), so no one can change another member's MR.
    if not _can_use_profile_options(interaction.user):
        ephemeral = True

    display_name = target.display_name
    avatar_asset = target.display_avatar or target.default_avatar
    avatar_url = avatar_asset.replace(size=256, format="png").url

    # Gather role-derived data first, then render off the event loop. The
    # card carries the same reference grid as the progress card (Clan /
    # Platform / Mastery / Syndicate) without the progress bar.
    try:
        await interaction.response.defer(ephemeral=ephemeral)
        # The role-derived info read (record + titles), the avatar CDN fetch
        # and the stored in-game-name read are independent I/O, so run all
        # three concurrently rather than paying their round-trips
        # back-to-back.
        info, avatar_bytes, in_game_name = await asyncio.gather(
            _member_profile_info_lines(target),
            _fetch_avatar_bytes(avatar_url),
            _member_in_game_name(target),
        )
        png = await _run_heavy(
            _render_profile_card_png,
            avatar_bytes=avatar_bytes,
            display_name=display_name,
            info_lines=info,
            in_game_name=in_game_name,
        )
        send_kwargs: dict = dict(
            file=discord.File(io.BytesIO(png), filename="profile.png"),
            ephemeral=ephemeral,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        # The mastery editor is opt-in and only ever attached to a member's
        # own profile (it edits their roles + stored rank).
        view: discord.ui.View | None = None
        if edit_mastery and target.id == interaction.user.id:
            view = _MasteryEditorView(
                member=target,
                owner_id=interaction.user.id,
                avatar_bytes=avatar_bytes,
                display_name=display_name,
                in_game_name=in_game_name,
            )
        # Own profile only: add the self-service action items — "Assign
        # <category>" link buttons for unearned Platform / Mastery Rank /
        # Syndicate, plus a "Verify Profile Data" button to OCR-fill
        # everything when no in-game name was ever pulled.
        if target.id == interaction.user.id:
            if view is None:
                view = discord.ui.View(timeout=600)
            _sync_profile_action_items(
                view,
                member=target,
                owner_id=interaction.user.id,
                avatar_bytes=avatar_bytes,
                display_name=display_name,
                info=info,
                in_game_name=in_game_name,
            )
        # Record jump link — admins only. The records ("profile-log") channel
        # is the source-of-truth log; only server managers can jump to a
        # member's record(s) (non-managers usually can't see the channel
        # anyway). Added after the own-profile sync above (which rebuilds link
        # buttons) so it isn't stripped. Newest last.
        if interaction.user.guild_permissions.manage_guild:
            record_urls = await asyncio.to_thread(
                _member_record_jump_urls, target.guild.id, target.id
            )
            if record_urls:
                if view is None:
                    view = discord.ui.View(timeout=600)
                view.add_item(discord.ui.Button(
                    style=discord.ButtonStyle.link,
                    label="View record",
                    url=record_urls[-1],
                    emoji=discord.PartialEmoji(name="\U0001F4CB"),
                ))
        if view is not None and view.children:
            send_kwargs["view"] = view
        await interaction.followup.send(**send_kwargs)
    except Exception:
        logger.exception("/profile failed")
        # We've already deferred, so `response.is_done()` is True and a plain
        # `response.send_message` would be a no-op — use the followup webhook
        # when deferred so a render/fetch failure still reaches the user.
        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    "\u274C Failed to render profile.", ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    "\u274C Failed to render profile.", ephemeral=True
                )
        except Exception:
            logger.exception("/profile error reply failed")


# /titles — admin command to grant or remove a member's cosmetic profile
# title via a single add/remove action choice. Requires Manage Server.
async def _apply_title_change(
    guild: discord.Guild,
    member: discord.Member | discord.User,
    action: str,
    title: str,
    reason: str | None,
) -> str:
    """Grant or revoke ``title`` for ``member`` and return the confirm text.

    Shared by the /titles slash command (direct args) and ``_TitlesModal``
    (the interactive form). SQLite runs off the event loop.
    """
    if action == "add":
        reason = (reason or "").strip() or None
        await asyncio.to_thread(
            analytics.award_title,
            guild_id=guild.id,
            user_id=member.id,
            title=title,
            reason=reason,
        )
        msg = f"\u2705 Gave **{member.display_name}** the title **{title}**."
        if reason:
            msg += f"\n-# {reason}"
        return msg
    removed = await asyncio.to_thread(
        analytics.revoke_title,
        guild_id=guild.id,
        user_id=member.id,
        title=title,
    )
    if removed:
        return (
            f"\u2705 Removed the title **{title}** from "
            f"**{member.display_name}**."
        )
    return (
        f"\u2139\uFE0F **{member.display_name}** has no title matching "
        f"**{title}**."
    )


class _TitlesModal(discord.ui.Modal):
    """Interactive form mirroring the /titles args (action, member, title,
    reason). Opened when /titles is run without args, pre-filled with any
    partial args. The /manage Edit page's Titles button opens the same modal
    with the panel's member pre-selected in the member select.

    Styled to recreate Discord's native slash-command argument sheet for
    /titles exactly: the command description as a leading text display,
    lowercase field labels, and each field's ``@app_commands.describe``
    string as its Label description.
    """

    def __init__(
        self,
        *,
        member: discord.Member | None = None,
        action: str = "add",
        title_text: str | None = None,
        reason: str | None = None,
    ) -> None:
        super().__init__(title="titles", timeout=600)
        self._gp_member = member
        self.add_item(discord.ui.TextDisplay(
            "Add or remove a member's cosmetic profile title."
        ))
        self.action_select = discord.ui.Select(
            min_values=1, max_values=1,
            options=[
                discord.SelectOption(
                    label="add", value="add", default=action != "remove",
                ),
                discord.SelectOption(
                    label="remove", value="remove",
                    default=action == "remove",
                ),
            ],
        )
        self.add_item(discord.ui.Label(
            text="action",
            description="Whether to add or remove the title.",
            component=self.action_select,
        ))
        # required=True must accompany min_values=1 — Discord rejects a
        # modal select with min_values > 0 that isn't required (400), so
        # the modal would silently never open. A known member (the /manage
        # Titles button) pre-selects the target instead of hiding the field.
        self.member_select = discord.ui.UserSelect(
            min_values=1, max_values=1, required=True,
            default_values=(
                [discord.Object(id=member.id)] if member is not None else []
            ),
        )
        self.add_item(discord.ui.Label(
            text="member",
            description="The member whose title to change.",
            component=self.member_select,
        ))
        self.title_input = discord.ui.TextInput(
            default=title_text or None,
            required=True,
            max_length=100,
        )
        self.add_item(discord.ui.Label(
            text="title",
            description="The title text (case-insensitive when removing).",
            component=self.title_input,
        ))
        self.reason_input = discord.ui.TextInput(
            default=reason or None,
            required=False,
            max_length=200,
        )
        self.add_item(discord.ui.Label(
            text="reason",
            description=(
                "Optional citation shown when adding (ignored on remove)."
            ),
            component=self.reason_input,
        ))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        # Defence in depth: re-check Manage Server before mutating the store.
        user = interaction.user
        guild = interaction.guild
        if guild is None or not (
            isinstance(user, discord.Member)
            and user.guild_permissions.manage_guild
        ):
            with contextlib.suppress(discord.HTTPException):
                await interaction.response.send_message(
                    "Operator, you can't use this.", ephemeral=True
                )
            return
        member: discord.Member | discord.User | None = None
        picked = list(self.member_select.values or [])
        if picked:
            member = guild.get_member(picked[0].id) or picked[0]
        if member is None:
            member = self._gp_member
        if member is None:
            await interaction.response.send_message(
                "\u274C Pick a member.", ephemeral=True
            )
            return
        title = (self.title_input.value or "").strip()
        if not title:
            await interaction.response.send_message(
                "\u274C A title is required.", ephemeral=True
            )
            return
        values = list(self.action_select.values or [])
        action = values[0] if values else "add"
        msg = await _apply_title_change(
            guild, member, action, title, self.reason_input.value
        )
        await interaction.response.send_message(
            msg, ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


@tree.command(
    name="titles",
    description="Add or remove a member's cosmetic profile title.",
)
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(
    action="Whether to add or remove the title.",
    member="The member whose title to change.",
    title="The title text (case-insensitive when removing).",
    reason="Optional citation shown when adding (ignored on remove).",
)
@app_commands.choices(action=[
    app_commands.Choice(name="add", value="add"),
    app_commands.Choice(name="remove", value="remove"),
])
async def titles_cmd(
    interaction: discord.Interaction,
    action: app_commands.Choice[str] | None = None,
    member: discord.Member | None = None,
    title: str | None = None,
    reason: str | None = None,
) -> None:
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message(
            "\u274C /titles can only be used in a server.", ephemeral=True
        )
        return
    title = (title or "").strip()
    if action is None or member is None or not title:
        # Run without (or with partial) args: open the interactive form,
        # pre-filled with whatever was provided.
        await interaction.response.send_modal(_TitlesModal(
            member=member,
            action=action.value if action else "add",
            title_text=title or None,
            reason=reason,
        ))
        return

    msg = await _apply_title_change(guild, member, action.value, title, reason)
    await interaction.response.send_message(
        msg, ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


@tree.command(
    name="onboard",
    description="Post the onboarding welcome prompt for a member.",
)
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(
    member="The member to (re-)onboard with a welcome prompt.",
)
async def onboard_cmd(
    interaction: discord.Interaction,
    member: discord.Member,
) -> None:
    """Admin trigger for the onboarding pipeline.

    Posts the public Components V2 welcome prompt (clan buttons + screenshot
    verification) in ONBOARDING_CHANNEL_ID for ``member`` and records the
    prompt in the durable store — the same entry point used by on_member_join,
    but invoked on demand for members who joined while the bot was offline or
    who need a fresh prompt. Mirrored by the /manage Overview "Start
    onboarding" button.
    """
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message(
            "\u274C /onboard can only be used in a server.", ephemeral=True
        )
        return
    if ONBOARDING_CHANNEL_ID <= 0:
        await interaction.response.send_message(
            "\u274C No onboarding channel is configured "
            "(`ONBOARDING_CHANNEL_ID`).",
            ephemeral=True,
        )
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        posted = await _post_onboarding_welcome(member)
    except Exception:
        logger.exception("/onboard failed")
        with contextlib.suppress(Exception):
            await interaction.followup.send(
                "\u274C Couldn't post the onboarding welcome \u2014 "
                "something went wrong; check the logs.",
                ephemeral=True,
            )
        return
    logger.info(
        "onboard: admin %s triggered onboarding for %s in guild %s (posted=%s)",
        interaction.user.id, member.id, guild.id, posted,
    )
    if posted:
        msg = (
            f"\u2705 Posted the onboarding welcome for "
            f"**{member.display_name}** in <#{ONBOARDING_CHANNEL_ID}>."
        )
    else:
        msg = (
            "\u274C Couldn't post the onboarding welcome \u2014 check my "
            "access to the onboarding channel and try again."
        )
    await interaction.followup.send(
        msg, ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


async def _handle_status_interaction(
    interaction: discord.Interaction, custom_id: str
) -> None:
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
    snap = None
    if _status_page_needs_snapshot(page):
        t0 = time.monotonic()
        snap = await asyncio.to_thread(analytics.summary)
        from utils.metrics import analytics_query_latency
        analytics_query_latency.record((time.monotonic() - t0) * 1000)
    components = _status_components(interaction, page, snap)
    try:
        await _interaction_callback(interaction, 7, components)  # UPDATE_MESSAGE
    except Exception:
        logger.exception("pagination failed")


def _register_custom_id_routes() -> None:
    global _CUSTOM_ID_ROUTES_REGISTERED
    if _CUSTOM_ID_ROUTES_REGISTERED:
        return
    _CUSTOM_ID_ROUTER.register_prefix("manage:", _handle_manage_interaction)
    _CUSTOM_ID_ROUTER.register_prefix("onboard:", _handle_onboarding_interaction)
    _CUSTOM_ID_ROUTER.register_prefix("mreview:", _handle_mreview_interaction)
    _CUSTOM_ID_ROUTER.register_prefix("alias:", _handle_alias_interaction)
    _CUSTOM_ID_ROUTER.register_prefix("status:", _handle_status_interaction)
    _CUSTOM_ID_ROUTES_REGISTERED = True


_register_custom_id_routes()


@client.event
async def on_interaction(interaction: discord.Interaction) -> None:
    if interaction.type != discord.InteractionType.component:
        return
    data = interaction.data or {}
    custom_id = str(data.get("custom_id", ""))
    await _CUSTOM_ID_ROUTER.dispatch(interaction, custom_id)



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
