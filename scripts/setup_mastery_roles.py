"""One-off: create the per-rank Mastery **vanity** roles in the Discord
server (MR 1..MR 30 then Legendary 1..Legendary 8), optionally delete the old
coarse *bucket* roles they replace, and write the resolved names + IDs back to
``.env`` (``MR_ROLE_NAMES`` / ``MR_ROLE_IDS``).

A Legendary rank is just the post-MR-30 prestige tier of mastery, so the
Legendary roles live in the same ``MR_*`` config the bot already assigns from
the OCR'd rank — ``_mr_bucket_role_for`` matches a single-value name like
``"MR 28"`` / ``"Legendary 3"`` exactly.

Vanity = no permissions, not hoisted, not mentionable, no colour.

**Dry-run by default** — it logs in (read-only) and prints the plan. Pass
``--apply`` to actually create roles + write ``.env``; add ``--delete-old`` to
also remove the replaced bucket roles.

Usage (from the repo root, with DISCORD_TOKEN in .env):
  python -m scripts.setup_mastery_roles                  # dry-run (plan only)
  python -m scripts.setup_mastery_roles --apply          # create + write .env
  python -m scripts.setup_mastery_roles --apply --delete-old
  python -m scripts.setup_mastery_roles --guild <id>     # pick guild explicitly
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import discord  # noqa: E402
import bot  # noqa: E402  — importing triggers env loading (needs DISCORD_TOKEN)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("setup_mastery_roles")

# The 38 per-rank role names, in display order (MR 1..30, then Legendary 1..8).
MR_NAMES = [f"MR {n}" for n in range(1, 31)]
LR_NAMES = [f"Legendary {n}" for n in range(1, 9)]
NEW_NAMES = MR_NAMES + LR_NAMES
DISCORD_ROLE_CAP = 250  # hard per-guild role limit


def _norm(name: str) -> str:
    """Normalise a role name for case/space-insensitive comparison."""
    return " ".join((name or "").split()).casefold()


def _pick_guild(guild_id: int | None) -> "discord.Guild | None":
    guilds = list(bot.client.guilds)
    if not guilds:
        logger.error("Bot is not in any guild.")
        return None
    if guild_id is not None:
        match = next((g for g in guilds if g.id == guild_id), None)
        if match is None:
            logger.error("Bot is not in guild %s.", guild_id)
        return match
    if len(guilds) > 1:
        logger.error(
            "Bot is in %d guilds; pass --guild <id>. Guilds: %s",
            len(guilds), [(g.id, g.name) for g in guilds],
        )
        return None
    return guilds[0]


def _plan(guild: "discord.Guild") -> tuple[list[str], dict, list["discord.Role"]]:
    """Return (names_to_create, existing_by_name, roles_to_delete).

    Old roles to replace = any role whose name parses as an MR/LR rank bucket
    (``_parse_mr_bucket_range`` — needs a kind keyword *and* a number, so a
    plain role like "VoiceMaster" never matches) but isn't one of the 38 new
    per-rank names. This catches whatever the server actually named them
    (e.g. "Mastery Rank 1 - 10", "Legendary 1-7") regardless of the stale
    ``MR_ROLE_NAMES`` config.
    """
    existing = {_norm(r.name): r for r in guild.roles}
    new_norm = {_norm(n) for n in NEW_NAMES}

    to_create = [n for n in NEW_NAMES if _norm(n) not in existing]
    to_delete = [
        r for r in guild.roles
        if _norm(r.name) not in new_norm
        and bot._parse_mr_bucket_range(r.name) is not None
    ]
    return to_create, existing, to_delete


def _delete_label(role: "discord.Role", bot_top: int) -> str:
    """Format a delete-candidate line with member count + hierarchy warning."""
    warn = "  ⚠ ABOVE bot — can't delete" if role.position >= bot_top else ""
    return f"{role.name} ({len(role.members)} members){warn}"


async def _do(apply: bool, delete_old: bool, guild_id: int | None) -> None:
    guild = _pick_guild(guild_id)
    if guild is None:
        return
    logger.info("Target guild: %s (%s)", guild.name, guild.id)

    to_create, existing, to_delete = _plan(guild)
    reused = [n for n in NEW_NAMES if _norm(n) in existing]
    bot_top = guild.me.top_role.position

    logger.info("Plan for %d per-rank roles:", len(NEW_NAMES))
    logger.info("  create (%d): %s", len(to_create), to_create or "—")
    logger.info("  reuse existing (%d): %s", len(reused), reused or "—")
    delete_lines = [_delete_label(r, bot_top) for r in to_delete]
    if delete_old:
        logger.info("  delete old mastery roles (%d):", len(to_delete))
        for line in delete_lines:
            logger.info("    - %s", line)
        if not delete_lines:
            logger.info("    (none)")
    else:
        logger.info(
            "  old mastery roles left in place (%d) — pass --delete-old to "
            "remove:", len(to_delete),
        )
        for line in delete_lines:
            logger.info("    - %s", line)

    projected = len(guild.roles) + len(to_create)
    if projected > DISCORD_ROLE_CAP:
        logger.error(
            "Aborting: guild would have %d roles (cap %d). Free up roles first.",
            projected, DISCORD_ROLE_CAP,
        )
        return

    if not apply:
        logger.info(
            "DRY-RUN — no changes made. Re-run with --apply%s to execute.",
            " --delete-old" if delete_old else "",
        )
        return

    # --- Create (idempotent: reuse a same-named role if it already exists) ---
    resolved: dict[str, discord.Role] = {}
    for name in NEW_NAMES:
        role = existing.get(_norm(name))
        if role is None:
            role = await guild.create_role(
                name=name,
                permissions=discord.Permissions.none(),
                colour=discord.Colour.default(),
                hoist=False,
                mentionable=False,
                reason="Golden Pagoda per-rank mastery vanity roles",
            )
            logger.info("created %s (%s)", role.name, role.id)
        resolved[name] = role

    # --- Delete old mastery/legendary roles (only with --delete-old) ---
    if delete_old:
        for r in to_delete:
            if r.position >= guild.me.top_role.position:
                logger.warning(
                    "skip delete %s (%s) — it's above my top role; move it "
                    "below me or delete it manually", r.name, r.id,
                )
                continue
            try:
                await r.delete(reason="Replaced by per-rank mastery roles")
                logger.info("deleted old mastery role %s (%s)", r.name, r.id)
            except discord.HTTPException:
                logger.exception(
                    "could not delete %s (%s) — check role hierarchy/perms",
                    r.name, r.id,
                )

    # --- Persist to .env (names + resolved IDs, in display order) ---
    ordered_ids = [resolved[name].id for name in NEW_NAMES]
    bot._update_env_value("MR_ROLE_NAMES", ",".join(NEW_NAMES))
    bot._update_env_id_list("MR_ROLE_IDS", ordered_ids)
    logger.info(
        "wrote MR_ROLE_NAMES (%d) + MR_ROLE_IDS (%d) to %s",
        len(NEW_NAMES), len(ordered_ids), bot.ENV_FILE_PATH,
    )
    logger.info("Done. Restart the bot to pick up the new .env.")


async def _run(apply: bool, delete_old: bool, guild_id: int | None) -> None:
    @bot.client.event
    async def on_ready() -> None:  # overrides bot.on_ready for this run
        try:
            await _do(apply, delete_old, guild_id)
        finally:
            await bot.client.close()

    await bot.client.start(bot.DISCORD_TOKEN)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true",
        help="actually create roles + write .env (default: dry-run)",
    )
    parser.add_argument(
        "--delete-old", action="store_true",
        help="also delete the old mastery/legendary bucket roles being replaced",
    )
    parser.add_argument(
        "--guild", type=int, default=None,
        help="guild id to operate on (required if the bot is in >1 guild)",
    )
    args = parser.parse_args()
    asyncio.run(_run(args.apply, args.delete_old, args.guild))


if __name__ == "__main__":
    main()
