"""One-time enrichment: OCR each migrated member record and rewrite it in
the bot's native record layout.

The channel-migration tool (``migrate_channel_to_records.py``) only copied the
screenshot + Discord metadata — it never ran OCR, so those records carry no
profile fields. This pass walks the records index, OCRs each member's record
screenshot, and re-writes the record through the bot's own
``_edit_or_create_member_record`` so it ends up byte-for-byte identical to a
natively-created record (gold ``/status``-styled embed: in-game name, clan,
platform, mastery rank, syndicate + the circular avatar thumbnail).

Two modes:
  * default (safe backfill): OCR supplies only the in-game name + exact
    Mastery Rank; Clan / Platform / Mastery-bucket / Syndicate are read from
    the member's CURRENT roles. **No roles are changed.**
  * ``--assign-roles``: run the full ``_verify_member_from_screenshot``
    pipeline, which also ASSIGNS the clan + mastery-bucket roles it reads from
    the screenshot (faithful to onboarding, but can mis-assign from a stale
    screenshot — use with care).

Runs as the bot (live token). Idempotent + resumable via a small state file.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

import discord

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import bot  # noqa: E402
import records_index  # noqa: E402


logger = logging.getLogger("records_enrich")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

_DEFAULT_STATE_PATH = Path("data/records_enrich_state.json")
_REBUILD_STATE_PATH = Path("data/records_rebuild_state.json")


def _retry_after_seconds(exc: discord.HTTPException, attempt: int) -> float:
    """Best-effort retry delay for a 429: prefer the body's ``retry_after``,
    else a bounded linear backoff."""
    try:
        body = json.loads(getattr(exc, "text", "") or "{}")
        ra = float(body.get("retry_after", 0) or 0)
        if ra > 0:
            return ra + 0.5
    except Exception:
        pass
    return min(2.0 * attempt, 10.0)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "OCR each migrated member record and rewrite it in the bot's "
            "native record layout (in-place edit of the indexed record)."
        )
    )
    parser.add_argument(
        "--index-path",
        type=Path,
        default=records_index.RECORDS_INDEX_PATH,
        help="JSON user_id -> [record message_ids] index path.",
    )
    parser.add_argument(
        "--state-path",
        type=Path,
        default=_DEFAULT_STATE_PATH,
        help="Resume state file (records completed user IDs).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process at most this many members this run.",
    )
    parser.add_argument(
        "--user-id",
        type=int,
        default=None,
        help="Only process this user ID (ignores/keeps the state file).",
    )
    parser.add_argument(
        "--assign-roles",
        action="store_true",
        help=(
            "Run the full verify pipeline, which ALSO assigns clan + "
            "mastery-bucket roles from the screenshot. Default: off "
            "(record-only enrichment, no role changes)."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="OCR + log the record that would be written, but write nothing.",
    )
    parser.add_argument(
        "--census",
        action="store_true",
        help=(
            "Audit only: report each record's image source (attachment / "
            "embed URL / none) and which members left, WITHOUT running OCR "
            "or writing. Use this to scope an OCR run before spending "
            "credits."
        ),
    )
    parser.add_argument(
        "--refresh-only",
        action="store_true",
        help=(
            "Re-render each existing record's embed in place WITHOUT OCR or "
            "re-uploading the screenshot (preserves the stored in-game name "
            "+ mastery and attachments). Use after a record-layout change "
            "(e.g. join-date format) to back-fill records cheaply."
        ),
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help=(
            "Durably repair records by DELETE + RECREATE via the bot's CREATE "
            "path (Discord only resolves embed attachment:// refs on create, "
            "not edit). Preserves the stored in-game name + mastery, recovers "
            "the screenshot, re-points the index to the fresh message id. "
            "Members who have left the guild get their record DELETED and "
            "dropped from the index. Resumable via a dedicated state file."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-process members already recorded in the state file.",
    )
    return parser.parse_args()


def _load_state(path: Path) -> set[int]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return set()
    except Exception:
        logger.warning("State file unreadable; starting fresh: %s", path)
        return set()
    done = data.get("done") if isinstance(data, dict) else None
    if not isinstance(done, list):
        return set()
    out: set[int] = set()
    for x in done:
        try:
            out.add(int(x))
        except (TypeError, ValueError):
            continue
    return out


def _save_state(path: Path, done: set[int]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"done": sorted(done)}), encoding="utf-8"
        )
    except Exception:
        logger.warning("Could not write state file %s", path, exc_info=True)


def _record_image_attachment(
    msg: discord.Message,
) -> discord.Attachment | None:
    """Best image attachment on a record: prefer one with an image
    content-type, else fall back to the first attachment (re-uploaded files
    can arrive with ``content_type=None``)."""
    img = bot._first_image_attachment(msg)
    if img is not None:
        return img
    return msg.attachments[0] if msg.attachments else None


def _record_embed_image_url(msg: discord.Message) -> str | None:
    """The first embed image URL on a record, if the screenshot was referenced
    by URL rather than re-uploaded as an attachment."""
    for embed in msg.embeds:
        if embed.image and embed.image.url:
            return embed.image.url
    return None


async def _read_record_image(
    msg: discord.Message,
) -> tuple[bytes | None, str]:
    """Return ``(image_bytes, content_type)`` for a record's screenshot from an
    attachment or, failing that, the embed image URL. ``(None, ...)`` when the
    record carries no recoverable image."""
    att = _record_image_attachment(msg)
    if att is not None:
        try:
            return await att.read(), (att.content_type or "image/png")
        except discord.HTTPException:
            logger.warning("attachment read failed", exc_info=True)
    url = _record_embed_image_url(msg)
    if url:
        data = await bot._fetch_cdn_bytes(url)
        if data:
            return data, "image/png"
    return None, "image/png"


class EnrichClient(discord.Client):
    def __init__(self, args: argparse.Namespace) -> None:
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(intents=intents)
        self._args = args

    async def on_ready(self) -> None:
        try:
            await self._run()
        finally:
            await self.close()

    async def _post_record(
        self,
        channel_id: int,
        embed: dict,
        image_bytes: bytes | None,
        avatar_bytes: bytes | None,
        *,
        tries: int = 5,
    ) -> int | None:
        """POST a fresh record (screenshot + circular avatar) so the embed's
        ``attachment://`` refs resolve. Retries 429s (the multipart path
        raises instead of auto-retrying). Returns the new message id."""
        primary, primary_name, extras, attachments = (
            bot._record_attachment_plan(image_bytes, "record.png", avatar_bytes)
        )
        payload: dict = {"embeds": [embed], "allowed_mentions": {"parse": []}}
        for attempt in range(1, tries + 1):
            try:
                if primary is not None:
                    payload["attachments"] = attachments
                    url = (
                        f"{bot._DISCORD_API_BASE}/channels/"
                        f"{channel_id}/messages"
                    )
                    data = await bot._v2_multipart_request(
                        "POST", url, payload=payload,
                        file_bytes=primary, file_name=primary_name,
                        extra_files=extras or None,
                    )
                else:
                    from discord.http import Route

                    data = await bot.client.http.request(
                        Route(
                            "POST", "/channels/{channel_id}/messages",
                            channel_id=channel_id,
                        ),
                        json=payload,
                    )
                if isinstance(data, dict) and data.get("id") is not None:
                    return int(data["id"])
                return None
            except discord.HTTPException as exc:
                if getattr(exc, "status", 0) == 429 and attempt < tries:
                    delay = _retry_after_seconds(exc, attempt)
                    logger.warning(
                        "POST 429 (attempt %d/%d) — sleeping %.1fs",
                        attempt, tries, delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                logger.warning(
                    "POST record failed (status %s)",
                    getattr(exc, "status", "?"), exc_info=True,
                )
                return None
        return None

    async def _delete_record(self, channel_id: int, message_id: int) -> bool:
        """DELETE a record message (client.http auto-retries 429). Treats an
        already-gone message as success."""
        from discord.http import Route

        try:
            await bot.client.http.request(Route(
                "DELETE",
                "/channels/{channel_id}/messages/{message_id}",
                channel_id=channel_id, message_id=message_id,
            ))
            return True
        except discord.NotFound:
            return True
        except discord.HTTPException:
            logger.warning(
                "delete message %s failed", message_id, exc_info=True
            )
            return False

    async def _rebuild_one(
        self,
        channel: discord.TextChannel | discord.Thread,
        guild: discord.Guild,
        index: dict,
        uid: int,
        mid: int,
        member: discord.Member | None,
        *,
        dry_run: bool,
    ) -> str:
        """Durably repair one record. In-guild members: delete + recreate via
        the CREATE path (preserving in-game name + mastery, recovering the
        screenshot) and re-point the index. Left members: delete the record +
        drop it from the index. Returns a short status string."""
        users = index.setdefault("users", {})
        uid_str = str(uid)
        channel_id = channel.id

        if member is None:
            if dry_run:
                logger.info("user %s: would DELETE record (left guild)", uid)
                return "left"
            await self._delete_record(channel_id, mid)
            users.pop(uid_str, None)
            logger.info("user %s: deleted record (left guild)", uid)
            return "deleted_left"

        try:
            msg = await channel.fetch_message(mid)
        except discord.HTTPException:
            logger.warning(
                "user %s: record %s fetch failed — skipped", uid, mid,
                exc_info=True,
            )
            return "fetch_fail"

        image_bytes, _content_type = await _read_record_image(msg)
        parsed = bot._parse_record_embed(
            [e.to_dict() for e in msg.embeds]
        ) or {}
        in_game_name = parsed.get("in_game_name")
        mastery_rank = parsed.get("mastery_rank")
        summary_lines = bot._member_record_profile_lines(
            member, in_game_name=in_game_name, mastery_rank=mastery_rank,
        )

        if dry_run:
            logger.info(
                "user %s (%s): would REBUILD image=%s name=%r mastery=%r "
                "-> %d line(s)",
                uid, member.display_name, bool(image_bytes),
                in_game_name, mastery_rank, len(summary_lines),
            )
            return "rebuild"

        avatar_bytes = await bot._render_record_avatar_bytes(
            bot._member_avatar_url(member)
        )
        embed = bot._build_member_record_embed(
            member, summary_lines,
            has_image=bool(image_bytes), has_avatar=bool(avatar_bytes),
        )
        new_id = await self._post_record(
            channel_id, embed, image_bytes, avatar_bytes,
        )
        if new_id is None:
            logger.warning(
                "user %s: recreate POST failed — kept old record %s",
                uid, mid,
            )
            return "post_fail"
        # Re-point the index to the fresh record FIRST (so a crash before the
        # delete leaves the good record indexed, not the broken one), then
        # delete the old broken message.
        users[uid_str] = [new_id]
        await self._delete_record(channel_id, mid)
        bot._invalidate_record_profile_cache(guild.id, uid)
        logger.info(
            "user %s (%s): rebuilt record %s -> %s (image=%s avatar=%s)",
            uid, member.display_name, mid, new_id,
            bool(image_bytes), bool(avatar_bytes),
        )
        return "rebuilt"

    async def _run(self) -> None:
        a = self._args
        # --rebuild gets its own resume state so it doesn't inherit the
        # "done" set from earlier enrich / refresh passes.
        if a.rebuild and a.state_path == _DEFAULT_STATE_PATH:
            a.state_path = _REBUILD_STATE_PATH
        # Route every bot.py helper (record I/O, OCR, role lookups) through
        # THIS connected client by rebinding the module global.
        bot.client = self

        # Resolve role NAME -> ID for clan slots / platform / MR / syndicate
        # so the role-derived record lines populate (mirrors on_ready).
        try:
            bot._sync_clan_slots_from_guilds()
            bot._sync_platform_roles_from_guilds()
            bot._sync_named_role_lists_from_guilds()
        except Exception:
            logger.warning("role-id resolution failed", exc_info=True)

        channel_id = bot._records_channel_id()
        if not channel_id:
            raise RuntimeError("records channel is not configured")
        channel = await self.fetch_channel(channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            raise RuntimeError(
                f"records channel {channel_id} is not a text channel/thread"
            )
        guild = channel.guild
        if not guild.chunked:
            try:
                await guild.chunk()
            except Exception:
                logger.warning("guild member chunk failed", exc_info=True)

        index = records_index.load_index(a.index_path)
        users = index.get("users", {})

        done = set() if (a.force or a.user_id or a.refresh_only) else (
            _load_state(a.state_path)
        )
        work: list[tuple[int, int]] = []
        for uid_str, ids in users.items():
            try:
                uid = int(uid_str)
            except (TypeError, ValueError):
                continue
            if a.user_id and uid != a.user_id:
                continue
            if uid in done or not ids:
                continue
            work.append((uid, int(ids[-1])))

        logger.info(
            "Enrich start: %d member(s) to process census=%s refresh_only=%s "
            "rebuild=%s dry_run=%s assign_roles=%s",
            len(work), a.census, a.refresh_only, a.rebuild,
            a.dry_run, a.assign_roles,
        )

        processed = ocr_ok = ocr_empty = wrote = 0
        skipped_left = skipped_noimg = 0
        census_img = census_embed = 0
        rebuilt = deleted_left = rebuild_fail = 0
        for uid, mid in work:
            if a.limit is not None and processed >= a.limit:
                break
            processed += 1

            member = guild.get_member(uid)

            if a.rebuild:
                status = await self._rebuild_one(
                    channel, guild, index, uid, mid, member,
                    dry_run=a.dry_run,
                )
                if status == "rebuilt":
                    rebuilt += 1
                    done.add(uid)
                elif status == "deleted_left":
                    deleted_left += 1
                    done.add(uid)
                elif status in ("rebuild", "left"):
                    pass  # dry-run preview
                else:
                    rebuild_fail += 1
                if not a.dry_run and status in ("rebuilt", "deleted_left"):
                    # Persist the index every iteration so an interrupted run
                    # can't orphan a freshly-created record (its id is already
                    # in the index before we move on).
                    records_index.save_index(index, a.index_path)
                    _save_state(a.state_path, done)
                await asyncio.sleep(2.0)
                continue

            if member is None:
                skipped_left += 1
                logger.info("user %s: not in guild (left?) — skipped", uid)
                continue

            if a.refresh_only:
                if a.dry_run:
                    logger.info(
                        "user %s (%s): would re-render embed (no OCR)",
                        uid, member.display_name,
                    )
                    continue
                await bot._edit_or_create_member_record(member)
                wrote += 1
                done.add(uid)
                await asyncio.sleep(1.5)
                continue

            try:
                msg = await channel.fetch_message(mid)
            except discord.HTTPException:
                logger.warning(
                    "user %s: record message %s fetch failed", uid, mid,
                    exc_info=True,
                )
                continue

            if a.census:
                atts = [
                    (x.filename, x.content_type) for x in msg.attachments
                ]
                embed_url = _record_embed_image_url(msg)
                has_att = _record_image_attachment(msg) is not None
                if has_att:
                    census_img += 1
                elif embed_url:
                    census_embed += 1
                else:
                    skipped_noimg += 1
                logger.info(
                    "user %s (%s): attachments=%s embed_image=%s",
                    uid, member.display_name, atts, bool(embed_url),
                )
                continue

            img, content_type = await _read_record_image(msg)
            if not img:
                skipped_noimg += 1
                logger.info(
                    "user %s: record %s has no recoverable image — skipped",
                    uid, mid,
                )
                continue

            if a.assign_roles:
                result = await bot._verify_member_from_screenshot(
                    member, image_bytes=img,
                    filename="record.png", content_type=content_type,
                )
                in_game_name = result.in_game_name
                mastery_rank = result.mastery_rank
                ok = bool(result.summary)
            else:
                fields = await bot._ocr_profile_fields(
                    img, "record.png", content_type,
                )
                in_game_name = fields.profile_name if fields.ok else None
                mastery_rank = fields.mastery_rank if fields.ok else None
                ok = bool(fields.ok and (in_game_name or mastery_rank))

            if ok:
                ocr_ok += 1
            else:
                ocr_empty += 1

            preview = bot._member_record_profile_lines(
                member, in_game_name=in_game_name, mastery_rank=mastery_rank,
            )
            logger.info(
                "user %s (%s): name=%r mastery=%r ok=%s -> %d line(s): %s",
                uid, member.display_name, in_game_name, mastery_rank, ok,
                len(preview), " | ".join(preview),
            )

            if a.dry_run:
                continue

            await bot._edit_or_create_member_record(
                member,
                in_game_name=in_game_name,
                mastery_rank=mastery_rank,
                image_bytes=img,
            )
            wrote += 1
            done.add(uid)
            if wrote % 10 == 0:
                _save_state(a.state_path, done)
            await asyncio.sleep(1.5)

        if not a.dry_run and not a.census and not a.refresh_only \
                and not a.user_id:
            _save_state(a.state_path, done)
        if a.rebuild and not a.dry_run:
            records_index.save_index(index, a.index_path)
            _save_state(a.state_path, done)
        if a.census:
            logger.info(
                "Census complete: processed=%d with_image=%d embed_only=%d "
                "no_image=%d left=%d",
                processed, census_img, census_embed,
                skipped_noimg, skipped_left,
            )
        elif a.rebuild:
            logger.info(
                "Rebuild complete: processed=%d rebuilt=%d deleted_left=%d "
                "failed=%d dry_run=%s",
                processed, rebuilt, deleted_left, rebuild_fail, a.dry_run,
            )
        elif a.refresh_only:
            logger.info(
                "Refresh complete: processed=%d re-rendered=%d "
                "skipped_left=%d dry_run=%s",
                processed, wrote, skipped_left, a.dry_run,
            )
        else:
            logger.info(
                "Enrich complete: processed=%d ocr_ok=%d ocr_empty=%d "
                "wrote=%d skipped_left=%d skipped_noimage=%d dry_run=%s",
                processed, ocr_ok, ocr_empty, wrote,
                skipped_left, skipped_noimg, a.dry_run,
            )


def main() -> None:
    args = _parse_args()
    if not bot.DISCORD_TOKEN:
        raise SystemExit("DISCORD_TOKEN is not configured")
    client = EnrichClient(args)
    client.run(bot.DISCORD_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
