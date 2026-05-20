from __future__ import annotations

import io
import logging
import os

import discord
import pytesseract
from PIL import Image

from logic import match_role_name, parse_role_rules

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
TARGET_CHANNEL_ID = int(os.getenv("TARGET_CHANNEL_ID", "0"))
ROLE_RULES = parse_role_rules(os.getenv("ROLE_RULES", ""))

if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN is required")
if TARGET_CHANNEL_ID <= 0:
    raise RuntimeError("TARGET_CHANNEL_ID must be set to a valid channel ID")
if not ROLE_RULES:
    raise RuntimeError("ROLE_RULES must include at least one keyword:Role Name rule")


intents = discord.Intents.default()
intents.guilds = True
intents.messages = True
intents.message_content = True
intents.members = True

client = discord.Client(intents=intents)


async def _extract_text_from_image_attachment(attachment: discord.Attachment) -> str:
    image_bytes = await attachment.read()
    image = Image.open(io.BytesIO(image_bytes))
    return pytesseract.image_to_string(image)


def _first_image_attachment(message: discord.Message) -> discord.Attachment | None:
    for attachment in message.attachments:
        if attachment.content_type and attachment.content_type.startswith("image/"):
            return attachment
    return None


@client.event
async def on_ready() -> None:
    logger.info("Logged in as %s", client.user)


@client.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot or message.channel.id != TARGET_CHANNEL_ID:
        return

    attachment = _first_image_attachment(message)
    if attachment is None:
        return

    try:
        ocr_text = (await _extract_text_from_image_attachment(attachment)).strip()
    except Exception:
        logger.exception("OCR failed for uploaded image")
        await message.reply(
            "I couldn't read that screenshot. Please upload a clearer image and try again."
        )
        return

    if not ocr_text:
        await message.reply(
            "I couldn't read that screenshot. Please upload a clearer image and try again."
        )
        return

    role_name = match_role_name(ocr_text, ROLE_RULES)
    if role_name is None:
        await message.reply(
            "I read the screenshot, but it did not match any role rule."
        )
        return

    if message.guild is None:
        await message.reply("I can only assign roles in a server channel.")
        return

    role = discord.utils.find(
        lambda candidate: candidate.name.lower() == role_name.lower(),
        message.guild.roles,
    )
    if role is None:
        await message.reply(
            f"I matched '{role_name}', but that role does not exist in this server."
        )
        return

    member = message.author
    if role in member.roles:
        await message.reply(f"You already have the '{role.name}' role.")
        return

    try:
        await member.add_roles(role, reason="Screenshot verification")
    except discord.Forbidden:
        await message.reply("I don't have permission to assign that role.")
        return

    await message.reply(f"Screenshot verified. Assigned role: **{role.name}**")


if __name__ == "__main__":
    client.run(DISCORD_TOKEN)
