"""One-off: send a sample of every Components V2 reply the bot can produce
to a target channel, so the styling can be reviewed at a glance.

Usage: python -m scripts.preview_responses
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import discord  # noqa: E402
from discord.http import Route  # noqa: E402

import bot  # noqa: E402

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("preview_responses")

PREVIEW_CHANNEL_ID = 1378199771428163765


SAMPLES: list[tuple[str, list[dict]]] = [
    (
        "PASS",
        bot._pass_components(
            profile="MonguPrime002#661",
            platform="PlayStation",
            clan="Grand Warhorde",
            role_lines=[
                "Platform: assigned **PlayStation**",
                "Clan: already has **Grand Warhorde**",
            ],
        ),
    ),
    ("FAIL — Not an image", bot._fail_components("Not an image", "Upload a PNG/JPG screenshot of your Warframe profile.")),
    ("FAIL — Invalid image", bot._fail_components("Invalid image", "Image could not be opened. Re-upload a valid PNG/JPG.")),
    ("FAIL — Not readable", bot._fail_components("Not readable", "No text could be read. Upload a clearer screenshot.")),
    ("FAIL — Profile not found", bot._fail_components("Profile not found", "Make sure your title bar (PlayerName#NNN) and platform icon are visible at the top.", image_url=bot.ICON_EXAMPLE_URL)),
    ("INCOMPLETE", bot._incomplete_components("No role for clan **Grand Warhorde**.")),
]


async def _main() -> None:
    @bot.client.event
    async def on_ready() -> None:
        try:
            logger.info("Logged in as %s", bot.client.user)
            route = Route(
                "POST",
                "/channels/{channel_id}/messages",
                channel_id=PREVIEW_CHANNEL_ID,
            )
            for label, components in SAMPLES:
                logger.info("Sending: %s", label)
                payload = {
                    "flags": bot.COMPONENTS_V2_FLAG,
                    "components": components,
                    "allowed_mentions": {"parse": []},
                }
                try:
                    await bot.client.http.request(route, json=payload)
                except discord.HTTPException:
                    logger.exception("Failed to send %s", label)
                await asyncio.sleep(0.5)
        finally:
            await bot.client.close()

    await bot.client.start(bot.DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(_main())
