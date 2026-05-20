"""One-off: log into Discord, reverse-resolve role IDs already present in
.env (CLAN_ROLE_*_ID and PLATFORM_ROLE_*_ID), fill in the matching NAMEs,
write the .env back, then exit.

Usage: python -m scripts.sync_role_names
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot  # noqa: E402  — imports trigger env loading

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sync_role_names")


async def _main() -> None:
    ready = asyncio.Event()

    @bot.client.event
    async def on_ready() -> None:  # overrides bot.on_ready for this run
        try:
            logger.info("Logged in as %s; %d guild(s)", bot.client.user, len(bot.client.guilds))
            bot._sync_clan_slots_from_guilds()
            bot._sync_platform_roles_from_guilds()
            # Force a writeback even when nothing changed, so blank NAMEs
            # get filled from any IDs already present in .env.
            bot._update_env_clan_slots(bot.CLAN_SLOTS)
            bot._update_env_platform_ids(bot.PLATFORM_ROLE_IDS)
            logger.info("Clan slots: %s", [(s.slot, s.clan_name, s.role_id) for s in bot.CLAN_SLOTS])
            logger.info("Platform roles: %s", bot.PLATFORM_ROLE_IDS)
        finally:
            ready.set()
            await bot.client.close()

    await bot.client.start(bot.DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(_main())
