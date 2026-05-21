from __future__ import annotations

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

try:  # optional — only needed for the lazy icon downloader
    import requests  # type: ignore
except ImportError:  # pragma: no cover
    requests = None  # type: ignore[assignment]


# --- Profile name -----------------------------------------------------------

# Matches Warframe profile titles like "[DE]KickBot#072" or "PlayerName#123".
_PROFILE_NAME_RE = re.compile(r"(?:\[[A-Za-z0-9]+\])?[A-Za-z0-9_\-\.]{2,}#\d{2,4}")


def parse_profile_name(ocr_text: str) -> str | None:
    """Find the first profile-name token in OCR text."""
    if not ocr_text:
        return None
    for line in ocr_text.splitlines():
        match = _PROFILE_NAME_RE.search(line)
        if match:
            return match.group(0).strip()
    match = _PROFILE_NAME_RE.search(ocr_text)
    return match.group(0).strip() if match else None


# --- Clan name --------------------------------------------------------------

_CLAN_HEADER_RE = re.compile(r"\bCLAN\b", re.IGNORECASE)
_NO_CLAN_TOKENS = ("UNAFFILIATED", "NO CLAN")


_MASTERY_HEADER_RE = re.compile(r"MASTERY\s*RANK", re.IGNORECASE)


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
        for candidate in lines[index + 1 : index + 5]:
            if not candidate:
                continue
            upper = candidate.upper()
            if "UNRANKED" in upper:
                return "Unranked"
            m = re.search(r"\b(\d{1,3})\b", candidate)
            if m:
                return f"MR {int(m.group(1))}"
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


# --- Platform detection -----------------------------------------------------

PLATFORM_PC = "PC"
PLATFORM_XBOX = "Xbox"
PLATFORM_PLAYSTATION = "PlayStation"
PLATFORM_SWITCH = "Switch"
PLATFORM_MOBILE = "Mobile"
ALL_PLATFORMS = (
    PLATFORM_PC,
    PLATFORM_XBOX,
    PLATFORM_PLAYSTATION,
    PLATFORM_SWITCH,
    PLATFORM_MOBILE,
)

# Reference icons from the Warframe wiki MMF symbol set (xWhite variants).
# Cross-Play icon is intentionally excluded.
PLATFORM_ICON_URLS: dict[str, str] = {
    PLATFORM_PC: "https://wiki.warframe.com/w/Special:FilePath/IconWindows(xWhite).png",
    PLATFORM_XBOX: "https://wiki.warframe.com/w/Special:FilePath/IconXbox(xWhite).png",
    PLATFORM_PLAYSTATION: "https://wiki.warframe.com/w/Special:FilePath/IconPlaystation(xWhite).png",
    PLATFORM_SWITCH: "https://wiki.warframe.com/w/Special:FilePath/IconSwitch(xWhite).png",
    PLATFORM_MOBILE: "https://wiki.warframe.com/w/Special:FilePath/IconApple(xWhite).png",
}

_DEFAULT_ICON_DIR = Path(os.getenv("PLATFORM_ICON_DIR", "icons"))
_REFERENCE_CACHE: dict[str, Image.Image] | None = None


def load_default_references(
    icon_dir: Path | None = None,
) -> dict[str, Image.Image]:
    """Load (and cache) the default reference icons, downloading any missing."""
    global _REFERENCE_CACHE
    if _REFERENCE_CACHE is not None and icon_dir is None:
        return _REFERENCE_CACHE

    target_dir = icon_dir or _DEFAULT_ICON_DIR
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        if icon_dir is None:
            _REFERENCE_CACHE = {}
        return {}

    icons: dict[str, Image.Image] = {}
    for key, url in PLATFORM_ICON_URLS.items():
        path = target_dir / f"{key}.png"
        if not path.exists():
            if requests is None:
                continue
            try:
                resp = requests.get(url, timeout=15, allow_redirects=True)
                resp.raise_for_status()
                path.write_bytes(resp.content)
            except Exception:
                continue
        try:
            icons[key] = Image.open(path).convert("RGBA")
        except Exception:
            continue

    if icon_dir is None:
        _REFERENCE_CACHE = icons
    return icons


def _icon_bbox(image: Image.Image) -> tuple[int, int, int, int] | None:
    """Locate the platform icon (a compact bright blob) in the title bar."""
    width, height = image.size
    strip_h = max(8, int(height * 0.12))
    top = image.convert("RGB").crop((0, 0, width, strip_h))
    hsv = top.convert("HSV")
    hsv_px = hsv.load()
    sw, sh = top.size

    def is_fg(x: int, y: int) -> bool:
        _, s, v = hsv_px[x, y]
        return (s >= 80 and v >= 100) or v >= 230

    col_counts = [sum(1 for y in range(sh) if is_fg(x, y)) for x in range(sw)]
    threshold = max(3, sh // 5)

    runs: list[tuple[int, int]] = []
    start: int | None = None
    for x, c in enumerate(col_counts):
        if c >= threshold:
            if start is None:
                start = x
        elif start is not None:
            runs.append((start, x - 1))
            start = None
    if start is not None:
        runs.append((start, sw - 1))
    if not runs:
        return None

    square = [r for r in runs if (r[1] - r[0] + 1) <= sh * 1.5]
    runs = square or runs
    runs_right_half = [r for r in runs if r[0] >= sw // 3]
    if runs_right_half:
        runs = runs_right_half
    x0, x1 = runs[-1]

    y_min, y_max = sh, -1
    for x in range(x0, x1 + 1):
        for y in range(sh):
            if is_fg(x, y):
                if y < y_min:
                    y_min = y
                if y > y_max:
                    y_max = y
    if y_max < 0:
        return None

    pad = 2
    return (
        max(0, x0 - pad),
        max(0, y_min - pad),
        min(width, x1 + 1 + pad),
        min(strip_h, y_max + 1 + pad),
    )


def _silhouette(image: Image.Image, size: tuple[int, int]) -> list[int]:
    """Return a binary silhouette (0/1 per pixel) of the icon at the given size."""
    img = image.convert("RGBA").resize(size, Image.LANCZOS)
    px = img.load()
    w, h = size
    out: list[int] = []
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            v = max(r, g, b)
            out.append(1 if a >= 128 and v >= 60 else 0)
    return out


def _silhouette_score(a: list[int], b: list[int]) -> float:
    if not a or len(a) != len(b):
        return 1.0
    diff = sum(1 for x, y in zip(a, b, strict=True) if x != y)
    return diff / len(a)


def detect_platform_from_image(
    image: Image.Image,
    references: dict[str, Image.Image] | None = None,
) -> str | None:
    """Detect platform by matching the title-bar icon against reference icons.

    By default the wiki MMF icons are auto-loaded (and cached) and used for
    silhouette template-matching. Pass ``references={}`` to force the
    hue-based fallback only.
    """
    if image is None:
        return None

    if references is None:
        references = load_default_references()

    bbox = _icon_bbox(image)
    if bbox is None:
        return _color_fallback(image)

    candidate = image.convert("RGBA").crop(bbox)
    cw, ch = candidate.size
    if cw < 6 or ch < 6:
        return _color_fallback(image)

    if references:
        size = (32, 32)
        cand_sil = _silhouette(candidate, size)
        scores: dict[str, float] = {}
        for key, ref in references.items():
            scores[key] = _silhouette_score(cand_sil, _silhouette(ref, size))
        
        sorted_platforms = sorted(scores.items(), key=lambda x: x[1])
        if not sorted_platforms:
            return _color_fallback(image)
        
        best_key, best_score = sorted_platforms[0]
        
        per_platform_thresholds = {
            PLATFORM_PLAYSTATION: 0.20,
            PLATFORM_SWITCH: 0.22,
            PLATFORM_PC: 0.25,
            PLATFORM_XBOX: 0.25,
            PLATFORM_MOBILE: 0.25,
        }
        threshold = per_platform_thresholds.get(best_key, 0.25)
        
        if best_score <= threshold:
            if len(sorted_platforms) > 1:
                second_score = sorted_platforms[1][1]
                if second_score - best_score < 0.08:
                    return _color_fallback(image)
            return best_key

    return _color_fallback(image)


def detect_platform_near_anchor(
    image: Image.Image,
    anchor_bbox: tuple[int, int, int, int],
) -> str | None:
    """Detect platform by classifying icon pixels adjacent to a known anchor.

    ``anchor_bbox`` is the (left, top, right, bottom) of the OCR'd profile-name
    word. The Warframe profile shows the platform icon immediately to the left
    of the player handle; some layouts place it on the right. We probe both
    sides and pick whichever yields more saturated brand-coloured pixels.
    """
    if image is None:
        return None
    rgb = image.convert("RGB")
    iw, ih = rgb.size
    if iw == 0 or ih == 0:
        return None

    left, top, right, bottom = anchor_bbox
    h = max(8, bottom - top)
    pad_y = max(2, h // 4)
    y0 = max(0, top - pad_y)
    y1 = min(ih, bottom + pad_y)
    box_w = max(h, 24)  # icon is roughly square at line height

    candidates: list[tuple[int, int, int, int]] = []
    if left - 4 > 0:
        candidates.append((max(0, left - box_w - 8), y0, max(0, left - 2), y1))
    if right + 4 < iw:
        candidates.append((min(iw, right + 2), y0, min(iw, right + box_w + 8), y1))

    best_platform: str | None = None
    best_score = 0
    for cx0, cy0, cx1, cy1 in candidates:
        if cx1 - cx0 < 6 or cy1 - cy0 < 6:
            continue
        crop = rgb.crop((cx0, cy0, cx1, cy1))
        platform, score = _vote_platform_color(crop)
        if platform is not None and score > best_score:
            best_platform = platform
            best_score = score

    if best_platform is not None and best_score >= 8:
        return best_platform
    return None


def _vote_platform_color(crop: Image.Image) -> tuple[str | None, int]:
    hsv = crop.convert("HSV")
    hsv_px = hsv.load()
    rgb_px = crop.load()
    cw, ch = crop.size
    counts = {p: 0 for p in ALL_PLATFORMS}
    for y in range(ch):
        for x in range(cw):
            h, s, v = hsv_px[x, y]
            if s < 90 or v < 100:
                continue
            r, g, b = rgb_px[x, y]
            platform = _classify_platform_color(h, s, v, r, g, b)
            if platform is not None:
                counts[platform] += 1
    if not counts:
        return None, 0
    best = max(counts, key=counts.get)
    return (best, counts[best]) if counts[best] > 0 else (None, 0)


def _color_fallback(image: Image.Image) -> str | None:
    """Hue-based platform classification used when no references are provided."""
    rgb = image.convert("RGB")
    width, height = rgb.size
    if width == 0 or height == 0:
        return None

    top_strip = rgb.crop((0, 0, width, max(1, int(height * 0.10))))
    hsv = top_strip.convert("HSV")
    hsv_px = hsv.load()
    rgb_px = top_strip.load()
    sw, sh = top_strip.size

    counts = {p: 0 for p in ALL_PLATFORMS}
    for y in range(sh):
        for x in range(sw):
            h, s, v = hsv_px[x, y]
            if s < 100 or v < 100:
                continue
            r, g, b = rgb_px[x, y]
            platform = _classify_platform_color(h, s, v, r, g, b)
            if platform is not None:
                counts[platform] += 1

    best = max(counts, key=counts.get)
    return best if counts[best] >= 50 else None


def _classify_platform_color(
    h: int, s: int, v: int, r: int, g: int, b: int
) -> str | None:
    """Classify a saturated pixel into a Warframe platform brand colour."""
    hue = (h / 255.0) * 360.0

    if 90 <= hue <= 160 and g > r * 1.2 and g > b * 1.2:
        return PLATFORM_XBOX
    if (hue <= 10 or hue >= 350) and r > g * 1.5 and r > b * 1.4 and g < 40 and b < 50:
        return PLATFORM_SWITCH
    if 190 <= hue <= 230 and b >= r:
        if v >= 190 and hue <= 215:
            return PLATFORM_PC
        if v < 180 and 200 <= hue <= 230:
            return PLATFORM_PLAYSTATION
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
    needle = re.sub(r"#\d+\s*$", "", clan_name).strip().lower()
    if not needle:
        return None
    for slot in slots:
        if not slot.clan_name:
            continue
        candidate = re.sub(r"#\d+\s*$", "", slot.clan_name).strip().lower()
        if candidate == needle:
            return slot
    return None
