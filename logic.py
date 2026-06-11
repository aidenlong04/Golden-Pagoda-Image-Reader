from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_CLAN_TAG_SUFFIX_RE = re.compile(r"#\d+\s*$")


# --- Profile name -----------------------------------------------------------

# Matches Warframe profile titles like "[DE]KickBot#072" or "PlayerName#123".
_PROFILE_NAME_RE = re.compile(r"(?:\[[A-Za-z0-9]+\])?[A-Za-z0-9_\-\.]{2,}#\d{2,4}")


def parse_profile_name(ocr_text: str) -> str | None:
    """Find the first profile-name token in OCR text.

    Prefers matches that appear *before* the CLAN header so the parser
    can't accidentally return the clan-name discriminator (e.g. for
    clan "Golden Tenno#200" the regex would otherwise match "Tenno#200"
    as a profile handle).
    """
    if not ocr_text:
        return None

    header_match = _CLAN_HEADER_RE.search(ocr_text)
    head = ocr_text[: header_match.start()] if header_match else ocr_text

    for line in head.splitlines():
        match = _PROFILE_NAME_RE.search(line)
        if match:
            return match.group(0).strip()
    match = _PROFILE_NAME_RE.search(head)
    if match:
        return match.group(0).strip()

    return None


# --- Clan name --------------------------------------------------------------

_CLAN_HEADER_RE = re.compile(r"\bCLAN\b", re.IGNORECASE)
_NO_CLAN_TOKENS = ("UNAFFILIATED", "NO CLAN")


_MASTERY_HEADER_RE = re.compile(r"MASTERY\s*RANK", re.IGNORECASE)
# Digit groups with thousands separators (e.g. "2,037,372" / "3 837 977") and
# long pure runs ("65128") are XP/credit totals that can leak below the header;
# the MR badge itself is always a small 1-3 digit standalone integer.
_THOUSANDS_RE = re.compile(r"\d[\d,\s\.]*\d{3}\b")
_LONG_DIGITS_RE = re.compile(r"\b\d{4,}\b")
_SMALL_INT_RE = re.compile(r"\b(\d{1,3})\b")


def parse_mastery_rank(ocr_text: str) -> str | None:
    """Return the mastery rank as a display string (e.g. 'MR 12' or 'Unranked').

    Looks below a 'MASTERY RANK' header for the first non-empty line. Accepts
    either a numeric rank or the literal 'UNRANKED'. Returns None if absent.
    """
    if not ocr_text:
        return None
    lines = [line.strip() for line in ocr_text.splitlines()]
    for index, line in enumerate(lines):
        if not _MASTERY_HEADER_RE.search(line):
            continue
        # Widen the search window: on some captures (esp. console UI with
        # an offset HUD), the credit count from the top bar can leak onto
        # the line right after the header, pushing the actual badge
        # number a few lines further down.
        for candidate in lines[index + 1 : index + 10]:
            if not candidate:
                continue
            upper = candidate.upper()
            if "UNRANKED" in upper:
                return "Unranked"
            # Skip lines that look like XP / credit totals; the MR badge
            # itself is always a small 1-3 digit standalone integer.
            if _THOUSANDS_RE.search(candidate):
                continue
            if _LONG_DIGITS_RE.search(candidate):
                continue
            m = _SMALL_INT_RE.search(candidate)
            if m:
                value = int(m.group(1))
                # Warframe MR currently caps well under 100; treat
                # anything above 50 as a stray match and keep scanning.
                if value > 50:
                    continue
                return f"MR {value}"
    return None


def parse_clan_name(ocr_text: str) -> str | None:
    """Return the clan name found below the CLAN header, or None if unaffiliated."""
    if not ocr_text:
        return None

    lines = [line.strip() for line in ocr_text.splitlines()]
    for index, line in enumerate(lines):
        if not _CLAN_HEADER_RE.search(line):
            continue
        for candidate in lines[index + 1 : index + 6]:
            if not candidate:
                continue
            upper = candidate.upper()
            if any(token in upper for token in _NO_CLAN_TOKENS):
                return None
            if re.fullmatch(r"[A-Z ]{2,}", upper) and len(upper) <= 4:
                continue
            return candidate
    return None


# --- Clan slot configuration ------------------------------------------------


@dataclass
class ClanSlot:
    slot: int  # 1..N
    clan_name: str | None
    role_id: int | None
    emoji: str | None = None


def find_clan_slot(slots: Iterable["ClanSlot"], clan_name: str) -> "ClanSlot | None":
    """Return the slot whose configured clan name matches the given clan name.

    Match is case-insensitive and ignores any trailing ``#NNN`` clan tag on
    either side, so ``Grand Warhorde#245`` matches a slot named
    ``Grand Warhorde``.
    """
    if not clan_name:
        return None
    needle = _CLAN_TAG_SUFFIX_RE.sub("", clan_name).strip().lower()
    if not needle:
        return None
    for slot in slots:
        if not slot.clan_name:
            continue
        candidate = _CLAN_TAG_SUFFIX_RE.sub("", slot.clan_name).strip().lower()
        if candidate == needle:
            return slot
    return None


def _format_mastery_display(value: str | None) -> str:
    """Normalize a stored/OCR'd mastery rank to a card-ready value.

    The label on the card is already "Mastery Rank", so the value drops
    the redundant prefix: ``"MR 28" -> "28"``. Legendary ranks expand:
    ``"LR 3" -> "Legendary 3"``. Anything else (e.g. "Unranked") passes
    through unchanged. Shared by the verify card, the /profile gatherer,
    and the mastery editor so the formatting lives in one place.
    """
    if not value:
        return ""
    upper = value.upper()
    if upper.startswith("MR "):
        return value[3:].strip()
    if upper.startswith("LR "):
        return f"Legendary {value[3:].strip()}"
    return value


def _mastery_label_value(value: str | None) -> tuple[str, str]:
    """Return the ``(label, display_value)`` for a member's mastery field.

    Legendary ranks surface under a "Legendary Rank" label carrying just
    the number/range (``"LR 3" -> ("Legendary Rank", "3")``,
    ``"LR 1-7" -> ("Legendary Rank", "1-7")``); every other rank keeps the
    "Mastery Rank" label (``"MR 28" -> ("Mastery Rank", "28")``). Accepts a
    raw stored rank, a coarse bucket role name, or an already-formatted
    display value, so every card labels the field consistently.
    """
    disp = _format_mastery_display(value)
    if disp.startswith("Legendary"):
        return "Legendary Rank", disp[len("Legendary"):].strip() or disp
    return "Mastery Rank", disp
