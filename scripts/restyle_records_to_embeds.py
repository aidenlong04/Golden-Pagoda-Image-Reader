"""Re-style existing member records in the profile-log channel as embeds.

The live bot used to post member records as Components V2 messages; records are
now gold rich **embeds** (see ``bot._build_member_record_embed`` +
``scripts/record_layout.json``). This one-time tool converts records that are
still in the old V2 (or plain-text) shape: it reads each record's profile data
+ screenshot, **OCRs the screenshot** to recover the in-game name / clan /
mastery that metadata-only records lack, renders the member's **circular
(gold-ring) avatar** (the same output model as the ``/profile`` card), re-posts
everything as the new embed, then deletes the old message — **keeping all
data**.

Safety:
- **Dry-run by default.** Pass ``--apply`` to actually write/delete.
- **Post-new-then-delete-old ordering.** A record's replacement embed is sent
  (and its message id confirmed) *before* the old message is deleted, so a
  crash mid-run can never lose a record. A record whose screenshot can't be
  re-downloaded is skipped (the old message is kept) so an image is never lost.
- Already-converted embed records are skipped (idempotent — safe to re-run).
- The records index (``records_index``) is updated in place: each converted
  record's old message id is swapped for the new one.

Run it on the machine where the bot runs (so its records index stays in sync):

    python scripts/restyle_records_to_embeds.py            # dry-run (scan only)
    python scripts/restyle_records_to_embeds.py --apply    # convert for real
    python scripts/restyle_records_to_embeds.py --apply --user-id 123  # one user
"""

from __future__ import annotations

import argparse
import asyncio
import io
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


logger = logging.getLogger("records_restyle")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# Heading/title marker shared by the old V2 record and the new embed record.
_RECORD_MARKER = "Member Record"
# The record body carries the canonical user id one of a few ways depending on
# which writer produced it: "**<@123>** (`123`)" (live embed), "**User ID:**
# `123`" (plain backfill), or a bare "<@123>" mention. Match any of them.
_USER_ID_RE = re.compile(r"\(`(\d+)`\)|\*\*User ID:\*\*\s*`(\d+)`|<@!?(\d+)>")
# "### \U0001F4CB Member Record \u2014 Display" — recover the display name.
_HEADING_NAME_RE = re.compile(r"Member Record\s*\u2014\s*(.+)")
_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif")

# Profile rows, in record order, mapped to the dict keys _parse_record_*
# produce — used to rebuild summary lines for members who have left the guild.
_PROFILE_ORDER = [
    ("In-game name", "in_game_name"),
    ("Clan", "clan"),
    ("Platform", "platform"),
    ("Mastery Rank", "mastery_rank"),
    ("Syndicate", "syndicate"),
]


class _ShimAsset:
    """Tiny ``discord.Asset`` stand-in exposing just ``.url`` + ``.replace``
    so ``bot._member_avatar_url`` resolves an avatar for a member who left."""

    def __init__(self, url: str) -> None:
        self.url = url

    def replace(self, **_kwargs: Any) -> "_ShimAsset":
        return self


class _MemberShim:
    """Minimal stand-in for ``discord.Member`` so a record can be rebuilt even
    when the member has since left the guild (we still have their id + name,
    and best-effort their avatar)."""

    def __init__(
        self, user_id: int, display_name: str, *, avatar_url: str | None = None
    ) -> None:
        self.id = user_id
        self.display_name = display_name or str(user_id)
        self.mention = f"<@{user_id}>"
        self.joined_at = None
        self.display_avatar = _ShimAsset(avatar_url) if avatar_url else None

    def __str__(self) -> str:
        return self.display_name


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Convert existing Components-V2 member records in the profile-log "
            "channel to the new gold embed style (delete + re-post, keep data)."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually re-post + delete. Without this it's a dry-run scan.",
    )
    parser.add_argument(
        "--inspect",
        action="store_true",
        help="Dump each bot-authored message's shape and exit (read-only).",
    )
    parser.add_argument(
        "--no-ocr",
        action="store_true",
        help="Skip OCR; rebuild profile fields from roles + the old record "
             "only (faster, but won't recover data the records lack).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-process records that are ALREADY embeds (re-OCR + re-render "
             "the circular avatar + re-post). Without this they're skipped.",
    )
    parser.add_argument(
        "--channel-id",
        type=int,
        default=0,
        help="Records channel/thread id (default: bot.MEMBER_RECORDS_CHANNEL_ID).",
    )
    parser.add_argument(
        "--user-id",
        type=int,
        default=0,
        help="Only convert records for this user id.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=2000,
        help="Maximum number of channel messages to scan (default 2000).",
    )
    parser.add_argument(
        "--index-path",
        type=Path,
        default=records_index.RECORDS_INDEX_PATH,
        help="Records index JSON path to keep in sync.",
    )
    return parser.parse_args()


def _first_image_attachment(data: dict[str, Any]) -> dict[str, Any] | None:
    for att in data.get("attachments") or []:
        if not isinstance(att, dict):
            continue
        ct = str(att.get("content_type") or "")
        fn = str(att.get("filename") or "")
        if ct.startswith("image/") or fn.lower().endswith(_IMAGE_EXTS):
            return att
    return None


def _is_embed_record(data: dict[str, Any]) -> bool:
    for embed in data.get("embeds") or []:
        if isinstance(embed, dict) and _RECORD_MARKER in str(embed.get("title", "")):
            return True
    return False


def _extract_record(
    data: dict[str, Any]
) -> tuple[int, str, dict] | None:
    """Return ``(user_id, display_name, old_profile)`` for a recognisable
    member record, or None.

    Handles both record shapes the bot has used: Components-V2 text (live
    records) and a plain markdown ``content`` body (the earlier migration
    backfill). ``old_profile`` carries any profile fields parseable from the
    old record (e.g. the OCR-only in-game name / exact mastery on V2 records);
    it's empty for the metadata-only migration records.
    """
    text = bot._collect_v2_text(data.get("components") or [])
    source = text if _RECORD_MARKER in text else ""
    if not source:
        content = str(data.get("content") or "")
        if _RECORD_MARKER in content:
            source = content
    if not source:
        return None

    m = _USER_ID_RE.search(source)
    if not m:
        return None
    user_id = int(next(g for g in m.groups() if g))

    display_name = ""
    name_m = _HEADING_NAME_RE.search(source)
    if name_m:
        display_name = name_m.group(1).strip()
    if not display_name:
        mem_m = re.search(
            r"\*\*Member:\*\*\s*<@\d+>\s*[\u00b7\-]\s*(.+)", source
        )
        if mem_m:
            display_name = mem_m.group(1).strip()

    old_profile = bot._parse_record_profile_text(source)
    return user_id, display_name or str(user_id), old_profile


def _derive_summary_lines(
    member: Any, old_profile: dict, ocr: Any | None = None
) -> list[str]:
    """Build the record's profile summary lines, merging three sources.

    Priority per field: role-derived data (for a live member) wins, then OCR
    read off the screenshot fills any gaps (in-game name / clan / mastery —
    the fields OCR can recover), then whatever the old record still held is the
    final fallback. For a member who has left we only have OCR + the old
    record. The result is emitted in the canonical ``_PROFILE_ORDER``.
    """
    if isinstance(member, _MemberShim):
        data: dict[str, str] = {}
    else:
        role_lines = bot._member_record_profile_lines(
            member,
            in_game_name=(
                (ocr.profile_name if ocr else None)
                or old_profile.get("in_game_name")
            ),
            mastery_rank=(
                (ocr.mastery_rank if ocr else None)
                or old_profile.get("mastery_rank")
            ),
        )
        data = {}
        for line in role_lines:
            m = bot._RECORD_LINE_RE.match(line)
            if not m:
                continue
            key = bot._RECORD_PROFILE_LABELS.get(m.group(1).strip().lower())
            if key:
                data[key] = m.group(2).strip()

    if ocr is not None:
        if ocr.profile_name and not data.get("in_game_name"):
            data["in_game_name"] = ocr.profile_name
        if ocr.clan_name and not data.get("clan"):
            data["clan"] = ocr.clan_name
        if ocr.mastery_rank and not data.get("mastery_rank"):
            data["mastery_rank"] = ocr.mastery_rank

    for _label, key in _PROFILE_ORDER:
        if not data.get(key) and old_profile.get(key):
            data[key] = old_profile[key]

    return [
        f"{label}: **{data[key]}**"
        for label, key in _PROFILE_ORDER
        if data.get(key)
    ]


async def _scan(client: discord.Client, channel_id: int, limit: int) -> list[dict]:
    """Fetch up to ``limit`` raw message dicts from the channel, oldest first."""
    collected: list[dict] = []
    before: str | None = None
    while len(collected) < limit:
        batch_size = min(100, limit - len(collected))
        batch = await client.http.logs_from(channel_id, batch_size, before=before)
        if not batch:
            break
        collected.extend(batch)
        before = batch[-1]["id"]
        if len(batch) < batch_size:
            break
    collected.reverse()
    return collected


class RestyleClient(discord.Client):
    def __init__(self, args: argparse.Namespace) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        super().__init__(intents=intents)
        self._args = args

    async def on_ready(self) -> None:
        try:
            await self._run()
        finally:
            await self.close()

    async def _resolve_member(
        self, guild: discord.Guild | None, user_id: int, display_name: str
    ) -> Any:
        if guild is not None:
            try:
                return await guild.fetch_member(user_id)
            except (discord.NotFound, discord.HTTPException):
                pass
        # Left the guild — fetch the User for at least the avatar + name so the
        # rebuilt record still shows a circular profile picture.
        avatar_url: str | None = None
        name = display_name
        try:
            user = await self.fetch_user(user_id)
            avatar_url = user.display_avatar.replace(
                size=256, format="png"
            ).url
            name = user.display_name or display_name
        except (discord.NotFound, discord.HTTPException):
            pass
        return _MemberShim(user_id, name, avatar_url=avatar_url)

    async def _render_avatar(self, member: Any) -> bytes | None:
        """Fetch + render the member's circular (gold-ring) avatar — the same
        output model the /profile card uses. Fail-soft -> None."""
        avatar_url = bot._member_avatar_url(member)
        if not avatar_url:
            return None
        try:
            avatar_bytes = await self.http.get_from_cdn(avatar_url)
        except discord.HTTPException:
            return None
        if not avatar_bytes:
            return None
        try:
            return await asyncio.to_thread(
                bot._render_circular_avatar_png, avatar_bytes
            )
        except Exception:
            logger.exception("avatar render failed for user %s", member.id)
            return None

    async def _run(self) -> None:
        channel_id = self._args.channel_id or bot.MEMBER_RECORDS_CHANNEL_ID
        if not channel_id:
            raise RuntimeError(
                "No records channel: set MEMBER_RECORDS_CHANNEL_ID or --channel-id"
            )
        channel = await self.fetch_channel(channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            raise RuntimeError(f"Channel {channel_id} is not a text channel/thread")
        guild = getattr(channel, "guild", None)
        me_id = self.user.id if self.user else 0

        index = records_index.load_index(self._args.index_path)

        messages = await _scan(self, channel_id, self._args.limit)
        logger.info(
            "Scanned %d messages in channel %s (apply=%s)",
            len(messages), channel_id, self._args.apply,
        )

        if self._args.inspect:
            for data in messages:
                author = data.get("author") or {}
                author_id = int(author.get("id") or 0)
                mine = "BOT" if author_id == me_id else "other"
                embeds = data.get("embeds") or []
                embed_titles = [str(e.get("title", "")) for e in embeds]
                v2_text = bot._collect_v2_text(data.get("components") or [])
                snippet = v2_text.replace("\n", " | ")[:160]
                atts = len(data.get("attachments") or [])
                logger.info(
                    "msg=%s by=%s(%s) embeds=%d titles=%s atts=%d content=%r "
                    "v2=%r",
                    data.get("id"), author.get("username"), mine,
                    len(embeds), embed_titles, atts,
                    (data.get("content") or "")[:80], snippet,
                )
            return

        scanned = converted = already = skipped = failed = 0
        for data in messages:
            author_id = int((data.get("author") or {}).get("id") or 0)
            if author_id != me_id:
                continue
            scanned += 1
            if _is_embed_record(data):
                already += 1
                continue
            parsed = _extract_record(data)
            if parsed is None:
                continue
            user_id, display_name, old_profile = parsed
            if self._args.user_id and user_id != self._args.user_id:
                continue
            old_id = int(data["id"])
            img_att = _first_image_attachment(data)
            member = await self._resolve_member(guild, user_id, display_name)

            # Re-download the screenshot up front: OCR reads it to recover the
            # in-game name / clan / mastery the metadata-only records lack, and
            # the converted embed re-uploads it (so the image is preserved).
            image_bytes: bytes | None = None
            filename = "record.png"
            content_type = "image/png"
            if img_att is not None and img_att.get("url"):
                try:
                    image_bytes = await self.http.get_from_cdn(img_att["url"])
                    filename = img_att.get("filename") or filename
                    content_type = img_att.get("content_type") or content_type
                except discord.HTTPException:
                    image_bytes = None

            ocr = None
            if image_bytes and not self._args.no_ocr:
                try:
                    ocr = await bot._ocr_profile_fields(
                        image_bytes, filename, content_type
                    )
                except Exception:
                    logger.exception("OCR failed for msg %s", old_id)
                    ocr = None

            summary_lines = _derive_summary_lines(member, old_profile, ocr)
            left = isinstance(member, _MemberShim)
            ocr_note = ""
            if ocr is not None:
                ocr_note = (
                    f" ocr=(name={ocr.profile_name!r} clan={ocr.clan_name!r} "
                    f"mr={ocr.mastery_rank!r})"
                )

            logger.info(
                "Record msg=%s user=%s left=%s fields=%d image=%s%s%s",
                old_id, user_id, left, len(summary_lines),
                image_bytes is not None, ocr_note,
                "" if self._args.apply else "  (dry-run)",
            )
            if not self._args.apply:
                converted += 1
                continue

            # Don't drop a screenshot we couldn't re-download — keep the old
            # record instead of posting a replacement that loses the image.
            if img_att is not None and image_bytes is None:
                logger.warning(
                    "msg %s: couldn't re-download screenshot; keeping old "
                    "record", old_id,
                )
                skipped += 1
                continue

            try:
                ok = await self._convert_one(
                    channel, old_id, user_id, member, summary_lines,
                    image_bytes, index,
                )
            except Exception:
                logger.exception("Failed converting msg %s", old_id)
                ok = False
            if ok:
                converted += 1
            else:
                skipped += 1
                failed += 1

        if self._args.apply:
            records_index.save_index(index, self._args.index_path)

        logger.info(
            "Done. authored=%d converted=%d already_embed=%d skipped=%d failed=%d",
            scanned, converted, already, skipped, failed,
        )
        if not self._args.apply:
            logger.info("Dry-run only — re-run with --apply to convert for real.")

    async def _convert_one(
        self,
        channel: discord.TextChannel | discord.Thread,
        old_id: int,
        user_id: int,
        member: Any,
        summary_lines: list[str],
        image_bytes: bytes | None,
        index: dict,
    ) -> bool:
        # Render the /profile-style circular avatar (gold ring) for the embed
        # thumbnail. Fail-soft: a None avatar just falls back to the URL.
        avatar_bytes = await self._render_avatar(member)

        embed_dict = bot._build_member_record_embed(
            member, summary_lines,
            has_image=image_bytes is not None,
            has_avatar=avatar_bytes is not None,
        )
        embed = discord.Embed.from_dict(embed_dict)
        allowed = discord.AllowedMentions.none()

        files: list[discord.File] = []
        if image_bytes is not None:
            files.append(
                discord.File(io.BytesIO(image_bytes), filename="record.png")
            )
        if avatar_bytes is not None:
            files.append(
                discord.File(io.BytesIO(avatar_bytes), filename="avatar.png")
            )

        # Post the replacement BEFORE deleting the original.
        if files:
            new_msg = await channel.send(
                embed=embed, files=files, allowed_mentions=allowed,
            )
        else:
            new_msg = await channel.send(embed=embed, allowed_mentions=allowed)

        new_id = new_msg.id
        # Swap the index entry, then delete the old message.
        users = index.setdefault("users", {})
        lst = users.setdefault(str(user_id), [])
        if old_id in lst:
            lst.remove(old_id)
        if new_id not in lst:
            lst.append(new_id)
        index["channel_id"] = channel.id

        try:
            await self.http.delete_message(channel.id, old_id)
        except discord.NotFound:
            pass
        except discord.HTTPException:
            logger.warning(
                "msg %s: re-posted as %s but failed to delete the old message; "
                "delete it manually", old_id, new_id,
            )
        logger.info("Converted msg %s -> %s (user %s)", old_id, new_id, user_id)
        return True


def main() -> None:
    args = _parse_args()
    client = RestyleClient(args)
    client.run(bot.DISCORD_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
