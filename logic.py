from __future__ import annotations

import logging
import os
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

try:  # optional — only needed for the lazy icon downloader
    import requests  # type: ignore
except ImportError:  # pragma: no cover
    requests = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


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

# Reference icons from the Warframe wiki MMF symbol set (brand-colored variants).
# Cross-Play icon is intentionally excluded.
PLATFORM_ICON_URLS: dict[str, str] = {
    PLATFORM_PC: "https://wiki.warframe.com/w/Special:FilePath/IconWindows.png",
    PLATFORM_XBOX: "https://wiki.warframe.com/w/Special:FilePath/IconXbox.png",
    PLATFORM_PLAYSTATION: "https://wiki.warframe.com/w/Special:FilePath/IconPlaystation.png",
    PLATFORM_SWITCH: "https://wiki.warframe.com/w/Special:FilePath/IconSwitch.png",
    PLATFORM_MOBILE: "https://wiki.warframe.com/w/Special:FilePath/IconApple.png",
}

_DEFAULT_ICON_DIR = Path(os.getenv("PLATFORM_ICON_DIR", "icons"))
_REFERENCE_CACHE: dict[str, Image.Image] | None = None
_REFERENCE_FEATURES: dict[str, dict] | None = None
_PLATFORM_DEBUG_DIR = os.getenv("PLATFORM_DEBUG_DIR")


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
        path = target_dir / f"{key}.colored.png"
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


def _binarize_and_crop(
    img: Image.Image, size: tuple[int, int] = (32, 32)
) -> tuple[list[int], int]:
    """Binarize an image, tight-crop to the bounding box, resize, and return mask + count."""
    rgba = img.convert("RGBA")
    px = rgba.load()
    w, h = rgba.size
    mask_img = Image.new("1", (w, h), 0)
    mask_px = mask_img.load()
    fg_count = 0
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            if max(r, g, b) >= 60 and a >= 128:
                mask_px[x, y] = 1
                fg_count += 1
    bbox = mask_img.getbbox()
    if bbox is None:
        resized = mask_img.resize(size, Image.NEAREST)
    else:
        cropped = mask_img.crop(bbox)
        resized = cropped.resize(size, Image.NEAREST)
    return list(resized.getdata()), fg_count


def _iou(mask_a: list[int], mask_b: list[int]) -> float:
    """Compute Intersection over Union of two binary masks."""
    if len(mask_a) != len(mask_b):
        return 0.0
    intersection = sum(1 for a, b in zip(mask_a, mask_b, strict=True) if a and b)
    union = sum(1 for a, b in zip(mask_a, mask_b, strict=True) if a or b)
    return intersection / union if union > 0 else 0.0


def _extract_reference_features(
    references: dict[str, Image.Image]
) -> dict[str, dict]:
    """Extract binarized mask for each reference icon."""
    features: dict[str, dict] = {}
    for key, ref in references.items():
        mask, fg_count = _binarize_and_crop(ref, size=(32, 32))
        features[key] = {
            "mask": mask,
            "fg_count": fg_count,
        }
    return features


def _score_candidate(
    candidate_crop: Image.Image, features: dict[str, dict]
) -> tuple[str | None, dict[str, float]]:
    """Score a candidate icon ROI against all reference features."""
    cw, ch = candidate_crop.size
    if cw * ch < 100:
        return None, {p: 0.0 for p in features}
    cand_mask, cand_fg = _binarize_and_crop(candidate_crop, size=(32, 32))
    if cand_fg < 50:
        return None, {p: 0.0 for p in features}
    hsv = candidate_crop.convert("HSV")
    rgb = candidate_crop.convert("RGB")
    hsv_px = hsv.load()
    rgb_px = rgb.load()
    cw, ch = hsv.size
    
    cand_sat_sum = 0
    cand_sat_pixels = 0
    platform_color_counts = {p: 0 for p in ALL_PLATFORMS}
    white_pixel_count = 0
    
    for y in range(ch):
        for x in range(cw):
            h_val, s, v = hsv_px[x, y]
            if s >= 80:
                cand_sat_sum += s
                cand_sat_pixels += 1
                r, g, b = rgb_px[x, y]
                platform = _classify_platform_color(h_val, s, v, r, g, b)
                if platform:
                    platform_color_counts[platform] += 1
            elif s < 50 and v >= 200:
                white_pixel_count += 1
    
    cand_mean_sat = cand_sat_sum / cand_sat_pixels if cand_sat_pixels > 0 else 0
    
    if cand_mean_sat >= 80:
        color_weight, shape_weight = 0.70, 0.30
    else:
        color_weight, shape_weight = 0.30, 0.70
    
    total_pixels = cw * ch
    scores: dict[str, float] = {}
    for platform, feat in features.items():
        iou = _iou(cand_mask, feat["mask"])
        
        color_pixel_count = platform_color_counts.get(platform, 0)
        
        if white_pixel_count > color_pixel_count and platform in (PLATFORM_PC, PLATFORM_MOBILE):
            color_pixel_count += white_pixel_count
        
        color_score = color_pixel_count / total_pixels if total_pixels > 0 else 0.0
        
        fused = color_weight * color_score + shape_weight * iou
        scores[platform] = fused
    
    sorted_platforms = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    if not sorted_platforms:
        return None, scores
    
    best_platform, best_score = sorted_platforms[0]
    
    if best_score < 0.45:
        return None, scores
    
    if len(sorted_platforms) > 1:
        runner_up_score = sorted_platforms[1][1]
        if best_score - runner_up_score < 0.15:
            return None, scores
    
    return best_platform, scores


def _candidate_rois(
    image: Image.Image, anchor_bbox: tuple[int, int, int, int] | None = None
) -> list[Image.Image]:
    """Extract candidate icon ROIs from title bar or near anchor."""
    candidates: list[Image.Image] = []
    
    bbox = _icon_bbox(image)
    if bbox is not None:
        candidates.append(image.convert("RGBA").crop(bbox))
    
    if anchor_bbox is not None:
        rgb = image.convert("RGB")
        iw, ih = rgb.size
        left, top, right, bottom = anchor_bbox
        h = max(8, bottom - top)
        pad_y = max(2, h // 4)
        y0 = max(0, top - pad_y)
        y1 = min(ih, bottom + pad_y)
        box_w = max(h, 24)
        
        anchor_candidates: list[tuple[int, int, int, int]] = []
        if left - 4 > 0:
            anchor_candidates.append((max(0, left - box_w - 8), y0, max(0, left - 2), y1))
        if right + 4 < iw:
            anchor_candidates.append((min(iw, right + 2), y0, min(iw, right + box_w + 8), y1))
        
        for cx0, cy0, cx1, cy1 in anchor_candidates:
            if cx1 - cx0 >= 6 and cy1 - cy0 >= 6:
                candidates.append(rgb.crop((cx0, cy0, cx1, cy1)))
    
    return candidates


def detect_platform(
    image: Image.Image, anchor_bbox: tuple[int, int, int, int] | None = None
) -> tuple[str | None, dict[str, float]]:
    """Unified platform detection returning (platform, scores_dict)."""
    if image is None:
        return None, {}
    
    global _REFERENCE_FEATURES
    references = load_default_references()
    if not references:
        return None, {}
    
    if _REFERENCE_FEATURES is None:
        _REFERENCE_FEATURES = _extract_reference_features(references)
    
    candidates = _candidate_rois(image, anchor_bbox)
    
    best_platform: str | None = None
    best_scores: dict[str, float] = {}
    best_score_value = -1.0
    winning_roi: Image.Image | None = None
    
    for candidate in candidates:
        platform, scores = _score_candidate(candidate, _REFERENCE_FEATURES)
        max_score = max(scores.values()) if scores else 0.0
        if platform and max_score > best_score_value:
            best_platform = platform
            best_scores = scores
            best_score_value = max_score
            winning_roi = candidate
    
    if _PLATFORM_DEBUG_DIR and winning_roi:
        try:
            debug_path = Path(_PLATFORM_DEBUG_DIR)
            debug_path.mkdir(parents=True, exist_ok=True)
            ts = int(time.time() * 1000)
            filename = f"{ts}_{best_platform or 'none'}_{best_score_value:.3f}.png"
            winning_roi.save(debug_path / filename)
        except Exception:
            logger.exception("Failed to save debug icon ROI")
    
    return best_platform, best_scores


def detect_platform_from_image(
    image: Image.Image,
    references: dict[str, Image.Image] | None = None,
) -> str | None:
    """Legacy shim: detect platform by matching title-bar icon against references.

    Now delegates to detect_platform(). The references parameter is ignored.
    """
    platform, _ = detect_platform(image, anchor_bbox=None)
    return platform


def detect_platform_near_anchor(
    image: Image.Image,
    anchor_bbox: tuple[int, int, int, int],
) -> str | None:
    """Legacy shim: detect platform near a known OCR anchor.

    Now delegates to detect_platform().
    """
    platform, _ = detect_platform(image, anchor_bbox)
    return platform


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
