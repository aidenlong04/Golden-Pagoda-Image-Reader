"""One-off: inspect channel ``CHANNEL_ID`` and optionally create a
``profile-log`` thread that mimics the existing threads within it.

Default run is **inspect-only** (read-only): it reports the channel type, its
active + archived threads, and each thread's starter-message content + the
image attachment / emblem URL it uses (so we know what to mimic, and can reuse
whatever image the other threads link).

``--create`` then makes the ``profile-log`` thread and posts a starter message
with an image (``--image-path`` / ``--image-url``, else the emblem URL reused
from the newest existing thread). It prints the new thread ID so it can be set
as ``MEMBER_RECORDS_CHANNEL_ID``; ``--set-env`` writes that back to ``.env``.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import bot  # noqa: E402
import discord  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("create_profile_log_thread")

DEFAULT_CHANNEL_ID = 1470179691167092736
DEFAULT_THREAD_NAME = "profile-log"
# The lotus emblem the sibling *-log threads link; reused so profile-log
# matches them. Overridable with --image-url.
DEFAULT_EMBLEM_URL = "https://ik.imagekit.io/qcxbyrkgu/log-lotus-image"
# Gold embed accent the sibling log threads use (#d4af37).
_EMBED_COLOR = 0xD4AF37


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "channel_id",
        nargs="?",
        type=int,
        default=DEFAULT_CHANNEL_ID,
        help=f"Parent channel ID (default: {DEFAULT_CHANNEL_ID}).",
    )
    p.add_argument("--name", default=DEFAULT_THREAD_NAME)
    p.add_argument(
        "--create",
        action="store_true",
        help="Actually create the thread (default: inspect only).",
    )
    p.add_argument("--image-url", default=None)
    p.add_argument(
        "--content",
        default=None,
        help="Override the starter-message content.",
    )
    p.add_argument(
        "--set-env",
        action="store_true",
        help="Write MEMBER_RECORDS_CHANNEL_ID to .env after creating.",
    )
    p.add_argument(
        "--archived-limit",
        type=int,
        default=20,
        help="How many archived threads to scan when inspecting.",
    )
    return p.parse_args()


def _first_image_url(message: "discord.Message | None") -> str | None:
    if message is None:
        return None
    for att in message.attachments:
        ctype = (att.content_type or "").lower()
        if ctype.startswith("image/") or att.filename.lower().endswith(
            (".png", ".jpg", ".jpeg", ".webp", ".gif")
        ):
            return att.url
    # Fall back to an embed thumbnail/image if present.
    for emb in message.embeds:
        if emb.image and emb.image.url:
            return emb.image.url
        if emb.thumbnail and emb.thumbnail.url:
            return emb.thumbnail.url
    return None


async def _starter_message(
    thread: discord.Thread,
) -> "discord.Message | None":
    """Best-effort fetch of a thread's first (starter) message."""
    msg = thread.starter_message
    if msg is not None:
        return msg
    # For threads created from a message, the starter message id == thread id.
    try:
        return await thread.fetch_message(thread.id)
    except (discord.HTTPException, discord.NotFound):
        return None


async def _describe_thread(thread: discord.Thread, *, archived: bool) -> str:
    msg = await _starter_message(thread)
    content = (msg.content if msg else "") or ""
    snippet = content.replace("\n", " ⏎ ")
    if len(snippet) > 160:
        snippet = snippet[:160] + "…"
    image_url = _first_image_url(msg)
    flags = "archived" if archived else "active"
    att_n = len(msg.attachments) if msg else 0
    embed_repr = "none"
    if msg and msg.embeds:
        e = msg.embeds[0]
        embed_repr = (
            f"title={e.title!r} desc={(e.description or '')[:80]!r} "
            f"color={e.color} "
            f"author={getattr(e.author, 'name', None)!r} "
            f"footer={getattr(e.footer, 'text', None)!r} "
            f"image={getattr(e.image, 'url', None)} "
            f"thumb={getattr(e.thumbnail, 'url', None)}"
        )
    return (
        f"  \u2022 [{flags}] id={thread.id} name={thread.name!r} "
        f"owner={getattr(thread, 'owner_id', None)} attachments={att_n}\n"
        f"      starter: {snippet!r}\n"
        f"      embed: {embed_repr}\n"
        f"      image_url: {image_url}"
    )


async def _gather_threads(
    channel: "discord.ForumChannel | discord.TextChannel",
    archived_limit: int,
) -> tuple[list[discord.Thread], list[discord.Thread]]:
    active = list(channel.threads)
    archived: list[discord.Thread] = []
    try:
        async for t in channel.archived_threads(limit=archived_limit):
            archived.append(t)
    except (discord.HTTPException, AttributeError):
        logger.warning("could not list archived threads", exc_info=True)
    return active, archived


class _ThreadClient(discord.Client):
    def __init__(self, args: argparse.Namespace) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.message_content = True
        super().__init__(intents=intents)
        self._args = args
        self.new_thread_id: int | None = None

    async def on_ready(self) -> None:
        try:
            await self._run()
        except Exception:
            logger.exception("run failed")
        finally:
            await self.close()

    async def _run(self) -> None:
        args = self._args
        channel = self.get_channel(args.channel_id)
        if channel is None:
            channel = await self.fetch_channel(args.channel_id)
        logger.info(
            "Channel %s: name=%r type=%s",
            channel.id, getattr(channel, "name", "?"), type(channel).__name__,
        )

        if not isinstance(
            channel, (discord.ForumChannel, discord.TextChannel)
        ):
            logger.error(
                "Channel type %s is not a Forum or Text channel; cannot "
                "create threads here.", type(channel).__name__,
            )
            return

        active, archived = await _gather_threads(
            channel, args.archived_limit
        )
        logger.info(
            "Found %d active + %d archived thread(s).",
            len(active), len(archived),
        )
        emblem_url: str | None = None
        for t in active + archived:
            logger.info("%s", await _describe_thread(
                t, archived=t in archived
            ))
            if emblem_url is None:
                msg = await _starter_message(t)
                emblem_url = _first_image_url(msg)

        if not args.create:
            logger.info(
                "Inspect-only. Re-run with --create to make the %r thread.",
                args.name,
            )
            if emblem_url:
                logger.info("Reusable emblem image URL: %s", emblem_url)
            return

        # --- create path (mimic the sibling *-log forum posts) ---
        image_url = args.image_url or emblem_url or DEFAULT_EMBLEM_URL
        embed = self._build_embed(image_url)
        thread = await self._create_thread(channel, embed)
        if thread is None:
            logger.error("thread creation failed")
            return
        self.new_thread_id = thread.id
        logger.info("Created thread %r id=%s", thread.name, thread.id)
        logger.info(
            "Set MEMBER_RECORDS_CHANNEL_ID=%s to use it as the records "
            "channel.", thread.id,
        )
        if args.set_env:
            self._write_env(thread.id)

    def _build_embed(self, image_url: str | None) -> discord.Embed:
        """Build a gold embed mimicking the sibling *-log threads: no title,
        a ``This is the log for: ```...``` `` description, the lotus image,
        and a footer."""
        desc = self._args.content or (
            "This is the log for: ```member profile records \u2014 "
            "verification screenshots and profile cards```\n"
            "Records are logged here automatically by Oda Helper, each "
            "tagged with the member's user ID for fast lookup."
        )
        embed = discord.Embed(description=desc, color=_EMBED_COLOR)
        if image_url:
            embed.set_image(url=image_url)
        embed.set_footer(
            text="Please note that profile logging is run through Oda Helper"
        )
        return embed

    async def _create_thread(
        self,
        channel: "discord.ForumChannel | discord.TextChannel",
        embed: discord.Embed,
    ) -> "discord.Thread | None":
        args = self._args
        if isinstance(channel, discord.ForumChannel):
            # ForumChannel.create_thread returns a ThreadWithMessage.
            created = await channel.create_thread(
                name=args.name, embed=embed, auto_archive_duration=10080,
            )
            return created.thread
        # Text channel: create the thread, then post the starter message.
        thread = await channel.create_thread(
            name=args.name,
            type=discord.ChannelType.public_thread,
            auto_archive_duration=10080,
        )
        await thread.send(embed=embed)
        return thread

    def _write_env(self, thread_id: int) -> None:
        try:
            bot._update_env_id_list(
                "MEMBER_RECORDS_CHANNEL_ID", [thread_id]
            )
        except Exception:
            # Fall back to a direct rewrite if the helper signature differs.
            logger.warning(
                "env helper failed; set MEMBER_RECORDS_CHANNEL_ID=%s "
                "manually.", thread_id, exc_info=True,
            )
            return
        logger.info("Wrote MEMBER_RECORDS_CHANNEL_ID=%s to .env", thread_id)


def main() -> None:
    args = _parse_args()
    client = _ThreadClient(args)
    client.run(bot.DISCORD_TOKEN, log_handler=None)
    if args.create and client.new_thread_id:
        print(f"NEW_THREAD_ID={client.new_thread_id}")


if __name__ == "__main__":
    main()
