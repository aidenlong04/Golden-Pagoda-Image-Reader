"""One-off / temp: migrate every image out of the server-entry channel into
the member-records channel, then clean up the entry channel.

For the source channel (SOURCE_CHANNEL_ID) it walks the full history, oldest
first, and for every message:

  * If it carries one or more image attachments, the images are re-uploaded
    (i.e. "moved") into the destination channel (DEST_CHANNEL_ID) with a short
    caption noting the original poster + timestamp, and the original message is
    then deleted from the entry channel.
  * Otherwise, if it is a *human* user's text-only message (no image
    attachments, author is not a bot), the message is deleted.

Bot messages with no images (e.g. the onboarding welcome prompts) are left
untouched.

Safety: pass ``--dry-run`` to preview every action without sending or deleting
anything. Without it the script performs the migration + deletions for real.

Usage:
    python -m scripts.migrate_entry_images            # execute
    python -m scripts.migrate_entry_images --dry-run  # preview only
"""
from __future__ import annotations

import asyncio
import io
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import discord  # noqa: E402
import bot  # noqa: E402  — import triggers env loading + client/intents setup

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("migrate_entry_images")

SOURCE_CHANNEL_ID = 1361846841934610568  # server-entry channel
DEST_CHANNEL_ID = 1516206387464507594    # member-records channel

# Be gentle with the API between actions.
_ACTION_DELAY_SECONDS = 0.75

_NO_MENTIONS = discord.AllowedMentions.none()


def _image_attachments(message: discord.Message) -> list[discord.Attachment]:
    """Return every image attachment on a message (by content-type, with a
    filename-extension fallback for attachments Discord didn't sniff)."""
    out: list[discord.Attachment] = []
    for att in message.attachments:
        ctype = (att.content_type or "").lower()
        if ctype.startswith("image/"):
            out.append(att)
            continue
        name = (att.filename or "").lower()
        if name.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff")):
            out.append(att)
    return out


async def _migrate_one(
    message: discord.Message,
    dest: discord.abc.Messageable,
    images: list[discord.Attachment],
    *,
    dry_run: bool,
) -> bool:
    """Re-upload a message's images into ``dest`` then delete the original.

    Returns True if the original was deleted (or would be, in dry-run)."""
    author = message.author
    when = message.created_at.strftime("%Y-%m-%d %H:%M UTC") if message.created_at else "?"
    caption = (
        f"\U0001F5BC\uFE0F Migrated from <#{SOURCE_CHANNEL_ID}> \u2014 "
        f"originally posted by {author} (`{author.id}`) on {when}"
    )

    if dry_run:
        logger.info(
            "[dry-run] would move %d image(s) from msg %s (%s) -> dest, then delete original",
            len(images), message.id, author,
        )
        return True

    files: list[discord.File] = []
    for att in images:
        try:
            data = await att.read()
        except discord.HTTPException:
            logger.exception("failed to download attachment %s on msg %s", att.id, message.id)
            return False
        files.append(discord.File(io.BytesIO(data), filename=att.filename or "image.png"))

    try:
        await dest.send(content=caption, files=files, allowed_mentions=_NO_MENTIONS)
    except discord.HTTPException:
        logger.exception("failed to re-upload images from msg %s; leaving original intact", message.id)
        return False

    try:
        await message.delete()
    except discord.HTTPException:
        logger.exception("re-uploaded msg %s but failed to delete the original", message.id)
        return False

    logger.info("moved %d image(s) from msg %s (%s) and deleted original", len(images), message.id, author)
    return True


async def _run(dry_run: bool) -> None:
    source = bot.client.get_channel(SOURCE_CHANNEL_ID)
    if source is None:
        source = await bot.client.fetch_channel(SOURCE_CHANNEL_ID)
    dest = bot.client.get_channel(DEST_CHANNEL_ID)
    if dest is None:
        dest = await bot.client.fetch_channel(DEST_CHANNEL_ID)

    if not isinstance(source, (discord.TextChannel, discord.Thread)):
        logger.error("source channel %s is not a text channel/thread", SOURCE_CHANNEL_ID)
        return
    if not isinstance(dest, (discord.TextChannel, discord.Thread)):
        logger.error("dest channel %s is not a text channel/thread", DEST_CHANNEL_ID)
        return

    logger.info(
        "%sscanning #%s -> #%s",
        "[dry-run] " if dry_run else "", source.name, dest.name,
    )

    moved = deleted_text = scanned = skipped = 0
    async for message in source.history(limit=None, oldest_first=True):
        scanned += 1
        images = _image_attachments(message)
        if images:
            if await _migrate_one(message, dest, images, dry_run=dry_run):
                moved += 1
            await asyncio.sleep(_ACTION_DELAY_SECONDS)
            continue

        # No images on this message.
        if message.author.bot:
            skipped += 1  # leave bot prompts / records alone
            continue

        # Human user's text-only message -> delete.
        if dry_run:
            logger.info("[dry-run] would delete text-only msg %s (%s)", message.id, message.author)
            deleted_text += 1
            continue
        try:
            await message.delete()
            deleted_text += 1
            logger.info("deleted text-only msg %s (%s)", message.id, message.author)
        except discord.HTTPException:
            logger.exception("failed to delete text-only msg %s", message.id)
        await asyncio.sleep(_ACTION_DELAY_SECONDS)

    logger.info(
        "%sdone: scanned=%d moved=%d text_deleted=%d bot_skipped=%d",
        "[dry-run] " if dry_run else "", scanned, moved, deleted_text, skipped,
    )


async def _main(dry_run: bool) -> None:
    @bot.client.event
    async def on_ready() -> None:  # overrides bot.on_ready for this run
        try:
            logger.info("Logged in as %s; %d guild(s)", bot.client.user, len(bot.client.guilds))
            await _run(dry_run)
        finally:
            await bot.client.close()

    await bot.client.start(bot.DISCORD_TOKEN)


if __name__ == "__main__":
    _dry = "--dry-run" in sys.argv[1:]
    asyncio.run(_main(_dry))
