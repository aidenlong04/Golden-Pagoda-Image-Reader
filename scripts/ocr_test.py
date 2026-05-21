"""One-off: fetch a specific Discord message and OCR its first image attachment."""
from __future__ import annotations

import asyncio
import io
import os
import sys

import discord
import requests
from PIL import Image

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from logic import detect_platform_from_image, detect_platform, parse_clan_name, parse_profile_name

MESSAGE_ID = int(sys.argv[1]) if len(sys.argv) > 1 else 1506665856632094902
SHOW_PLATFORM_SCORES = "--platform-scores" in sys.argv
CHANNEL_ID = int(os.getenv("TARGET_CHANNEL_ID", "0"))
TOKEN = os.getenv("DISCORD_TOKEN", "")

OCR_API_KEY = os.getenv("OCR_API_KEY", "").strip()
OCR_API_URL = os.getenv("OCR_API_URL", "https://api.ocr.space/parse/image")
OCR_ENGINE = os.getenv("OCR_ENGINE", "3")
OCR_LANGUAGE = os.getenv("OCR_LANGUAGE", "eng")


def ocr_via_api(image_bytes: bytes, filename: str, content_type: str) -> str:
    r = requests.post(
        OCR_API_URL,
        headers={"apikey": OCR_API_KEY},
        data={
            "OCREngine": OCR_ENGINE,
            "language": OCR_LANGUAGE,
            "scale": "true",
            "isTable": "false",
            "detectOrientation": "true",
        },
        files={"file": (filename, image_bytes, content_type or "image/png")},
        timeout=60,
    )
    r.raise_for_status()
    payload = r.json()
    if payload.get("IsErroredOnProcessing"):
        raise RuntimeError(f"OCR API error: {payload.get('ErrorMessage') or payload}")
    parsed = payload.get("ParsedResults") or []
    return "\n".join(item.get("ParsedText", "") for item in parsed)


async def main() -> None:
    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready() -> None:
        try:
            channel = client.get_channel(CHANNEL_ID) or await client.fetch_channel(CHANNEL_ID)
            print(f"channel: {channel} ({CHANNEL_ID})")
            msg = await channel.fetch_message(MESSAGE_ID)
            print(f"message author: {msg.author} ({msg.author.id})")
            print(f"attachments: {len(msg.attachments)}")
            attachment = next(
                (a for a in msg.attachments if a.content_type and a.content_type.startswith("image/")),
                None,
            )
            if attachment is None:
                print("no image attachment")
                return
            print(f"attachment: {attachment.filename} ({attachment.content_type}, {attachment.size}b)")

            data = await attachment.read()
            text = ocr_via_api(data, attachment.filename, attachment.content_type or "image/png")
            print("\n----- OCR TEXT -----")
            print(text)
            print("--------------------\n")

            print(f"profile_name: {parse_profile_name(text)!r}")
            print(f"clan_name:    {parse_clan_name(text)!r}")
            try:
                img = Image.open(io.BytesIO(data))
                if SHOW_PLATFORM_SCORES:
                    platform, scores = detect_platform(img)
                    print(f"platform:     {platform!r}")
                    print(f"scores:       {scores}")
                else:
                    print(f"platform:     {detect_platform_from_image(img)!r}")
            except Exception as e:
                print(f"platform detection failed: {e}")
        finally:
            await client.close()

    await client.start(TOKEN)


if __name__ == "__main__":
    if not TOKEN or CHANNEL_ID <= 0:
        raise SystemExit("DISCORD_TOKEN and TARGET_CHANNEL_ID must be set in .env")
    asyncio.run(main())
