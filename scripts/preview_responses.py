"""One-off: send a sample of every Components V2 reply the bot can produce
to a target channel, so the styling can be reviewed at a glance.

Usage:
    DISCORD_TOKEN=... python -m scripts.preview_responses

Targets channel https://discord.com/channels/1361846841905381629/1378199771428163765.
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
SAMPLE_USER_ID = 100000000000000000  # placeholder; nick buttons are inert here


def _build_samples() -> list[tuple[str, list[dict]]]:
    sample_clan = (
        bot.CLAN_SLOTS[0].clan_name
        if bot.CLAN_SLOTS and bot.CLAN_SLOTS[0].clan_name
        else "Golden Tenno"
    )
    sample_emoji = (
        (bot.CLAN_SLOTS[0].emoji if bot.CLAN_SLOTS else None)
        or bot.CLAN_EMOJI
    )
    # Skip guild-dependent lookups (channel jump URLs need a real guild,
    # which the preview script doesn't have at SAMPLES-build time).
    pass_buttons: list[tuple[str, str]] = []
    if bot.PASS_INFO_CHANNEL_ID:
        pass_buttons.append((
            "Pick Roles",
            bot._channel_url(bot.GUILD_ID, bot.PASS_INFO_CHANNEL_ID),
        ))

    return [
        (
            "PASS — mastery + missing categories",
            bot._pass_components(
                "GoldenTenno#200",
                sample_clan,
                clan_emoji=sample_emoji,
                mastery_rank="MR 28",
                link_buttons=pass_buttons,
                missing_categories=["Platform", "Syndicate"],
            ),
        ),
        (
            "PASS — mastery only, fully verified",
            bot._pass_components(
                "MonguPrime002#661",
                sample_clan,
                clan_emoji=sample_emoji,
                mastery_rank="MR 30",
                link_buttons=pass_buttons,
            ),
        ),
        (
            "FAIL — Not an image",
            bot._fail_components(
                "Not an image",
                "Upload a PNG/JPG screenshot of your Warframe profile.",
            ),
        ),
        (
            "FAIL — Invalid image",
            bot._fail_components(
                "Invalid image",
                "Image could not be opened. Re-upload a valid PNG/JPG.",
            ),
        ),
        (
            "FAIL — Not readable",
            bot._fail_components(
                "Not readable",
                "No text could be read. Upload a clearer screenshot.",
            ),
        ),
        (
            "FAIL — Profile not found",
            bot._fail_components(
                "Profile not found",
                "Make sure your title bar (PlayerName#NNN) and platform "
                "icon are visible at the top.",
            ),
        ),
        (
            "INCOMPLETE — unknown clan",
            bot._incomplete_components(
                f"No role for clan **{sample_clan}**.",
            ),
        ),
        (
            "NICKNAME PROMPT (standalone)",
            bot._nickname_prompt_components(
                "GoldenTenno",
                SAMPLE_USER_ID,
                current_nick="OldNick",
            ),
        ),
    ]


async def _main() -> None:
    sent_event = asyncio.Event()

    @bot.client.event
    async def on_ready() -> None:
        try:
            logger.info("Logged in as %s", bot.client.user)
            samples = _build_samples()
            route = Route(
                "POST",
                "/channels/{channel_id}/messages",
                channel_id=PREVIEW_CHANNEL_ID,
            )
            for label, components in samples:
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
            sent_event.set()
            await bot.client.close()

    await bot.client.start(bot.DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(_main())
