"""Pure member-record parsing helpers.

Everything here is side-effect free: parsing a record message's body
(Components V2 text or rich embeds) back into a profile dict, plus the
snowflake-timestamp helper used to date legacy records. The record I/O
(fetch/create/edit against the records channel) stays in bot.py, which owns
the Discord client; these helpers are imported and re-exported there.
"""
from __future__ import annotations

import re

# Maps a record body label to the profile dict key it populates. Mirrors the
# lines emitted by ``bot._member_record_profile_lines``.
RECORD_PROFILE_LABELS: dict[str, str] = {
    "in-game name": "in_game_name",
    "mastery rank": "mastery_rank",
    "clan": "clan",
    "platform": "platform",
    "syndicate": "syndicate",
}

# A record body line: "Key: **Value**", optionally prefixed by the "-# "
# small-text marker the container uses.
RECORD_LINE_RE = re.compile(
    r"^\s*(?:-#\s*)?([A-Za-z][\w \-]*?):\s*\*\*(.+?)\*\*\s*$",
    re.MULTILINE,
)

# Discord epoch (2015-01-01) in ms, for deriving a timestamp from a snowflake.
DISCORD_EPOCH_MS = 1420070400000

_EXACT_MASTERY_RE = re.compile(r"^(MR|LR)\s*(\d+)$", re.IGNORECASE)


def snowflake_ts(snowflake: int) -> int:
    """Return the unix-seconds creation time encoded in a Discord snowflake."""
    return int((((int(snowflake) >> 22) + DISCORD_EPOCH_MS) / 1000))


def collect_v2_text(components: object) -> str:
    """Walk a Components V2 tree and join every type-10 ``content`` string."""
    parts: list[str] = []

    def _walk(node: object) -> None:
        if isinstance(node, dict):
            if node.get("type") == 10 and isinstance(node.get("content"), str):
                parts.append(node["content"])
            _walk(node.get("components"))
            _walk(node.get("items"))
        elif isinstance(node, list):
            for child in node:
                _walk(child)

    _walk(components)
    return "\n".join(parts)


def is_exact_mastery_rank(value: str | None) -> bool:
    """True when ``value`` is an exact ``MR n`` / ``LR n`` rank with a real
    (>= 1) number.

    The durable store keeps the exact OCR rank and prefers it over the coarse
    role bucket on every refresh, so a bogus ``MR 0`` (a stray UI/credit digit
    the OCR misread) would otherwise stick forever and never update to the
    member's real rank. Rejecting rank 0 here drops that bad value on read so
    the role bucket wins instead. Rank roles run MR 1-30 / Legendary 1-8 and
    the mastery editor already requires ``> 0``, so 0 is never a real rank.
    """
    m = _EXACT_MASTERY_RE.match(value or "")
    return bool(m) and int(m.group(2)) >= 1


def parse_record_profile_text(text: str | None) -> dict:
    """Parse a record body's ``Key: **Value**`` lines into a profile dict.

    Recognises the labels emitted by ``bot._member_record_profile_lines``.
    The Mastery Rank value is kept only when it's an exact ``MR n`` / ``LR n``
    rank (the OCR-exact override); a coarse role-bucket name is dropped so the
    returned shape matches the durable-store semantics (where ``mastery_rank``
    is always the exact rank or absent).
    """
    out: dict = {}
    for m in RECORD_LINE_RE.finditer(text or ""):
        key = RECORD_PROFILE_LABELS.get(m.group(1).strip().lower())
        if not key or key in out:
            continue
        value = m.group(2).strip()
        if key == "mastery_rank" and not is_exact_mastery_rank(value):
            continue
        out[key] = value
    return out


def parse_record_embed(embeds: object) -> dict:
    """Parse a member record's profile fields out of its rich ``embeds``.

    Mirrors :func:`parse_record_profile_text` but reads the structured
    ``embeds[].fields`` (``{name, value}``) written by
    ``bot._build_member_record_embed``. Field names map through
    ``RECORD_PROFILE_LABELS``; values may be wrapped in ``**bold**`` or
    `` `code` ``. Mastery Rank is kept only when it's an exact ``MR n`` /
    ``LR n`` rank.
    """
    out: dict = {}
    if not isinstance(embeds, list):
        return out
    for embed in embeds:
        if not isinstance(embed, dict):
            continue
        for field in embed.get("fields") or []:
            if not isinstance(field, dict):
                continue
            name = str(field.get("name", "")).strip().rstrip(":").strip().lower()
            key = RECORD_PROFILE_LABELS.get(name)
            if not key or key in out:
                continue
            value = str(field.get("value", "")).strip()
            if value.startswith("`") and value.endswith("`") and len(value) > 2:
                value = value[1:-1].strip()
            elif value.startswith("**") and value.endswith("**") and len(value) > 4:
                value = value[2:-2].strip()
            if not value:
                continue
            if key == "mastery_rank" and not is_exact_mastery_rank(value):
                continue
            out[key] = value
    return out
