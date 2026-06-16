from __future__ import annotations

import argparse
import asyncio
import io
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

import discord

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import bot  # noqa: E402
import records_index  # noqa: E402


logger = logging.getLogger("records_migration")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

_DEFAULT_STATE_PATH = Path("data/records_migration_state.json")
_DEFAULT_LAYOUT_PATH = ROOT / "scripts" / "record_layout.json"
_PLACEHOLDER_RE = re.compile(r"\{([a-z_]+)\}")
# Built-in fallback used when the layout JSON is missing or unreadable.
_BUILTIN_LAYOUT: dict[str, Any] = {
    "heading": "### \U0001F4CB Member Record",
    "empty_value": "\u2014",
    "skip_empty_lines": True,
    "lines": [
        "**User ID:** `{user_id}`",
        "**Member:** {user_mention}  \u00b7  {display_name}",
        "**Sent:** <t:{sent_unix}:F>  \u00b7  <t:{sent_unix}:R>",
        "**Source:** <#{source_channel_id}>  \u00b7  message "
        "`{source_message_id}`",
        "{original_text}",
    ],
}
_ALLOWED_TYPES = {
    discord.MessageType.default,
    discord.MessageType.reply,
}
_MESSAGE_CHANNEL_TYPES = (
    discord.TextChannel,
    discord.Thread,
    discord.DMChannel,
    discord.GroupChannel,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "One-time migration: copy a source channel into the configured "
            "member-records channel oldest-to-newest, then optionally delete "
            "the source messages after each successful repost."
        )
    )
    parser.add_argument("source_channel_id", type=int)
    parser.add_argument(
        "--destination-channel-id",
        type=int,
        default=bot.MEMBER_RECORDS_CHANNEL_ID,
        help="Override the destination records channel ID.",
    )
    parser.add_argument(
        "--state-path",
        type=Path,
        default=_DEFAULT_STATE_PATH,
        help="Resume state file path.",
    )
    parser.add_argument(
        "--layout-path",
        type=Path,
        default=_DEFAULT_LAYOUT_PATH,
        help="JSON record-layout template path (edit it to change layout).",
    )
    parser.add_argument(
        "--index-path",
        type=Path,
        default=records_index.RECORDS_INDEX_PATH,
        help=(
            "JSON user_id -> [record message_ids] index path the bot reads "
            "for fast lookups. Updated as records are posted."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional max number of source messages to migrate this run.",
    )
    parser.add_argument(
        "--user-id",
        type=int,
        default=None,
        help=(
            "Only migrate messages authored by this user ID. Scans the whole "
            "channel and ignores/does not write the resume checkpoint."
        ),
    )
    parser.add_argument(
        "--delete-source",
        action="store_true",
        help="Delete each source message after the repost succeeds.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan and log what would migrate without posting or deleting.",
    )
    parser.add_argument(
        "--test-image",
        action="store_true",
        help=(
            "Send a single generated test record (with an image) to the "
            "destination channel, then exit without scanning or deleting."
        ),
    )
    return parser.parse_args()


def _load_state(path: Path, source_id: int, dest_id: int) -> dict[str, Any]:
    if not path.exists():
        return {
            "source_channel_id": source_id,
            "destination_channel_id": dest_id,
            "last_deleted_id": None,
            "pending_delete": None,
        }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("State file unreadable; starting fresh: %s", path)
        return {
            "source_channel_id": source_id,
            "destination_channel_id": dest_id,
            "last_deleted_id": None,
            "pending_delete": None,
        }
    if (
        data.get("source_channel_id") != source_id
        or data.get("destination_channel_id") != dest_id
    ):
        logger.info(
            "State file targets a different channel pair; starting fresh"
        )
        return {
            "source_channel_id": source_id,
            "destination_channel_id": dest_id,
            "last_deleted_id": None,
            "pending_delete": None,
        }
    data.setdefault("last_deleted_id", None)
    data.setdefault("pending_delete", None)
    return data


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(state, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _display_name(author: discord.abc.User) -> str:
    name = getattr(author, "display_name", None)
    return str(name or author.name)


def _load_layout(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("lines"):
            return data
        logger.warning("Layout file has no 'lines'; using built-in layout")
    except FileNotFoundError:
        logger.warning("Layout file not found (%s); using built-in", path)
    except Exception:
        logger.warning("Layout file unreadable (%s); using built-in", path)
    return _BUILTIN_LAYOUT


def _record_context(
    message: discord.Message, source_channel_id: int
) -> dict[str, str]:
    author = message.author
    sent_ts = int(message.created_at.timestamp())
    return {
        "user_id": str(author.id),
        "user_mention": author.mention,
        "display_name": _display_name(author),
        "author_name": str(author),
        "sent_unix": str(sent_ts),
        "source_message_id": str(message.id),
        "source_channel_id": str(source_channel_id),
        "attachment_count": str(len(message.attachments)),
        "original_text": message.content or "",
    }


def _render_record_content(
    layout: dict[str, Any], ctx: dict[str, str]
) -> str:
    empty = str(layout.get("empty_value", "\u2014"))
    skip_empty = bool(layout.get("skip_empty_lines", True))
    out: list[str] = []
    heading = layout.get("heading")
    if heading:
        out.append(str(heading))
    for tmpl in layout.get("lines", []):
        tmpl = str(tmpl)
        keys = _PLACEHOLDER_RE.findall(tmpl)
        if keys:
            non_empty = [k for k in keys if str(ctx.get(k, "")).strip()]
            if skip_empty and not non_empty:
                continue

            def _repl(m: re.Match[str]) -> str:
                val = str(ctx.get(m.group(1), ""))
                return val if val.strip() else empty

            line = _PLACEHOLDER_RE.sub(_repl, tmpl)
        else:
            line = tmpl
        out.append(line)
    return _truncate_content("\n".join(out))


def _truncate_content(text: str, *, limit: int = 1900) -> str:
    if len(text) <= limit:
        return text
    trimmed = text[: limit - 64].rstrip()
    return (
        trimmed
        + "\n\n-# Truncated during migration to fit Discord's message limit."
    )


async def _message_files(message: discord.Message) -> list[discord.File]:
    files: list[discord.File] = []
    for attachment in message.attachments:
        files.append(await attachment.to_file(use_cached=True))
    return files


def _chunk_files(
    files: list[discord.File], *, chunk_size: int = 10
) -> list[list[discord.File]]:
    return [files[i:i + chunk_size] for i in range(0, len(files), chunk_size)]


async def _post_message_copy(
    destination: (
        discord.TextChannel
        | discord.Thread
        | discord.DMChannel
        | discord.GroupChannel
    ),
    message: discord.Message,
    *,
    source_channel_id: int,
    layout: dict[str, Any],
) -> int | None:
    ctx = _record_context(message, source_channel_id)
    content = _render_record_content(layout, ctx)
    files = await _message_files(message)
    allowed_mentions = discord.AllowedMentions.none()

    if not files:
        sent = await destination.send(
            content, allowed_mentions=allowed_mentions
        )
        return sent.id

    chunks = _chunk_files(files)
    first_id: int | None = None
    for index, chunk in enumerate(chunks, start=1):
        chunk_content = content if index == 1 else (
            f"### Member Record (attachments {index}/{len(chunks)})\n"
            f"**User ID:** `{ctx['user_id']}`  \u00b7  message "
            f"`{message.id}`"
        )
        sent = await destination.send(
            chunk_content,
            files=chunk,
            allowed_mentions=allowed_mentions,
        )
        if first_id is None:
            first_id = sent.id
    return first_id


async def _send_test_record(
    destination: (
        discord.TextChannel
        | discord.Thread
        | discord.DMChannel
        | discord.GroupChannel
    ),
) -> None:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (640, 320), (22, 26, 36))
    draw = ImageDraw.Draw(img)
    draw.rectangle((0, 0, 639, 6), fill=(212, 175, 55))
    draw.text((24, 40), "Golden Pagoda", fill=(212, 175, 55))
    draw.text((24, 70), "Records migration \u2014 image path test",
              fill=(235, 235, 235))
    draw.text((24, 110), "If you can see this image, attachments post OK.",
              fill=(180, 180, 180))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)

    now_ts = int(discord.utils.utcnow().timestamp())
    content = (
        "### Migrated Record \u2014 TEST\n"
        "-# Generated test record to verify the image path.\n"
        f"**Sent:** <t:{now_ts}:F>  \u2022  <t:{now_ts}:R>"
    )
    await destination.send(
        content,
        file=discord.File(buf, filename="migration_test.png"),
        allowed_mentions=discord.AllowedMentions.none(),
    )


class MigrationClient(discord.Client):
    def __init__(self, args: argparse.Namespace) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        super().__init__(intents=intents)
        self._args = args

    async def on_ready(self) -> None:
        try:
            await self._run_migration()
        finally:
            await self.close()

    async def _run_migration(self) -> None:
        source = await self.fetch_channel(self._args.source_channel_id)
        dest_id = self._args.destination_channel_id
        if dest_id <= 0:
            raise RuntimeError("MEMBER_RECORDS_CHANNEL_ID is not configured")
        destination = await self.fetch_channel(dest_id)
        if not isinstance(source, _MESSAGE_CHANNEL_TYPES):
            raise RuntimeError(
                f"Source channel {source.id} is not messageable"
            )
        if not isinstance(destination, _MESSAGE_CHANNEL_TYPES):
            raise RuntimeError(
                f"Destination channel {destination.id} is not messageable"
            )

        if self._args.test_image:
            logger.info(
                "Sending test record with image to %s", destination.id
            )
            await _send_test_record(destination)
            logger.info("Test record sent.")
            return

        layout = _load_layout(self._args.layout_path)
        logger.info("Using record layout: %s", self._args.layout_path)

        index = records_index.load_index(self._args.index_path)
        logger.info("Using records index: %s", self._args.index_path)

        user_filter = self._args.user_id

        state = _load_state(
            self._args.state_path,
            self._args.source_channel_id,
            dest_id,
        )
        pending = state.get("pending_delete") or {}
        pending_id = pending.get("message_id")
        if self._args.delete_source and pending_id and not user_filter:
            logger.info(
                "Retrying pending delete for source message %s", pending_id
            )
            try:
                source_message = await source.fetch_message(int(pending_id))
            except discord.NotFound:
                logger.info(
                    "Pending delete target already gone: %s", pending_id
                )
            else:
                await source_message.delete()
            state["last_deleted_id"] = int(pending_id)
            state["pending_delete"] = None
            _save_state(self._args.state_path, state)

        after_id = state.get("last_deleted_id")
        after = discord.Object(id=int(after_id)) if after_id else None
        # In single-user filter mode we cherry-pick one author across the whole
        # channel, so the sequential resume checkpoint doesn't apply: scan from
        # the start and never touch the shared state file.
        if user_filter:
            after = None
            after_id = None
        migrated = 0
        deleted = 0
        scanned = 0
        skipped_text = 0

        logger.info(
            (
                "Migrating from %s to %s oldest-first dry_run=%s "
                "delete_source=%s after=%s user_filter=%s"
            ),
            source.id,
            destination.id,
            self._args.dry_run,
            self._args.delete_source,
            after_id,
            user_filter,
        )

        history_kwargs: dict[str, Any] = {
            "limit": self._args.limit,
            "oldest_first": True,
        }
        if after is not None:
            history_kwargs["after"] = after

        async for message in source.history(**history_kwargs):
            scanned += 1
            if message.type not in _ALLOWED_TYPES:
                continue
            if user_filter and message.author.id != user_filter:
                continue
            if not message.content.strip() and not message.attachments:
                continue

            has_image = bot._first_image_attachment(message) is not None

            logger.info(
                "Processing message %s from %s attachments=%d "
                "has_text=%s has_image=%s",
                message.id,
                message.author,
                len(message.attachments),
                bool(message.content.strip()),
                has_image,
            )
            if self._args.dry_run:
                continue

            # Only image messages are posted to the records channel.
            # Text-only messages are still removed from the source (when
            # --delete-source is set) but are never reposted.
            if has_image:
                posted_id = await _post_message_copy(
                    destination,
                    message,
                    source_channel_id=source.id,
                    layout=layout,
                )
                migrated += 1
                if posted_id is not None:
                    records_index.index_add(
                        index,
                        message.author.id,
                        posted_id,
                        channel_id=destination.id,
                    )
                    if migrated % 25 == 0:
                        records_index.save_index(
                            index, self._args.index_path
                        )
            else:
                skipped_text += 1

            if self._args.delete_source:
                if not user_filter:
                    state["pending_delete"] = {"message_id": message.id}
                    _save_state(self._args.state_path, state)
                await message.delete()
                deleted += 1
                if not user_filter:
                    state["last_deleted_id"] = message.id
                    state["pending_delete"] = None
                    _save_state(self._args.state_path, state)
            elif not user_filter:
                state["last_deleted_id"] = message.id
                _save_state(self._args.state_path, state)

            if has_image or self._args.delete_source:
                await asyncio.sleep(1.0)

        records_index.save_index(index, self._args.index_path)

        logger.info(
            "Migration complete scanned=%d migrated=%d skipped_text=%d "
            "deleted=%d dry_run=%s",
            scanned,
            migrated,
            skipped_text,
            deleted,
            self._args.dry_run,
        )


def main() -> None:
    args = _parse_args()
    client = MigrationClient(args)
    client.run(bot.DISCORD_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
