from __future__ import annotations

import logging
import os
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import numpy as np
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
    """Find the first profile-name token in OCR text.

    Prefers matches that appear *before* the CLAN header so the parser
    can't accidentally return the clan-name discriminator (e.g. for
    clan "Golden Tenno#200" the regex would otherwise match "Tenno#200"
    as a profile handle).
    """
    if not ocr_text:
        return None

    # Split off everything from the CLAN header onward — the player's own
    # handle always appears earlier in the title bar.
    header_match = _CLAN_HEADER_RE.search(ocr_text)
    head = ocr_text[: header_match.start()] if header_match else ocr_text

    for line in head.splitlines():
        match = _PROFILE_NAME_RE.search(line)
        if match:
            return match.group(0).strip()
    match = _PROFILE_NAME_RE.search(head)
    if match:
        return match.group(0).strip()

    # Fallback: scan the full text only if the pre-header region yielded
    # nothing (handles OCR that drops the header line entirely).
    if header_match is not None:
        for line in ocr_text.splitlines():
            match = _PROFILE_NAME_RE.search(line)
            if match:
                return match.group(0).strip()
    return None


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
        # Back-compat: also accept a plain "{key}.png" file dropped in by hand
        # (older deploys / manual seeding).
        if not path.exists():
            legacy = target_dir / f"{key}.png"
            if legacy.exists():
                path = legacy
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
    hsv_arr = np.asarray(top.convert("HSV"))
    sh, sw = hsv_arr.shape[:2]
    s = hsv_arr[..., 1]
    v = hsv_arr[..., 2]
    fg_mask = ((s >= 80) & (v >= 100)) | (v >= 230)
    col_counts = fg_mask.sum(axis=0)
    threshold = max(3, sh // 5)

    runs: list[tuple[int, int]] = []
    start: int | None = None
    for x in range(sw):
        if col_counts[x] >= threshold:
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

    ys = np.where(fg_mask[:, x0 : x1 + 1].any(axis=1))[0]
    if ys.size == 0:
        return None
    y_min = int(ys.min())
    y_max = int(ys.max())

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
    rgba = np.asarray(img.convert("RGBA"))
    mask = (rgba[..., :3].max(axis=-1) >= 60) & (rgba[..., 3] >= 128)
    fg_count = int(mask.sum())
    if fg_count == 0:
        cropped = mask
    else:
        ys, xs = np.where(mask)
        y0, y1 = int(ys.min()), int(ys.max()) + 1
        x0, x1 = int(xs.min()), int(xs.max()) + 1
        cropped = mask[y0:y1, x0:x1]
    mask_img = Image.fromarray((cropped.astype(np.uint8) * 255), mode="L")
    resized_arr = np.asarray(mask_img.resize(size, Image.NEAREST))
    flat = (resized_arr >= 128).astype(np.uint8).flatten()
    return flat.tolist(), fg_count


def _iou(mask_a, mask_b) -> float:
    """Compute Intersection over Union of two binary masks."""
    a = np.asarray(mask_a, dtype=bool).ravel()
    b = np.asarray(mask_b, dtype=bool).ravel()
    if a.size != b.size:
        return 0.0
    intersection = int(np.logical_and(a, b).sum())
    union = int(np.logical_or(a, b).sum())
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
            "mask_np": np.asarray(mask, dtype=bool),
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
    cand_mask_np = np.asarray(cand_mask, dtype=bool)
    hsv_arr = np.asarray(candidate_crop.convert("HSV"))
    rgb_arr = np.asarray(candidate_crop.convert("RGB"))
    ch, cw = hsv_arr.shape[:2]

    h_chan = hsv_arr[..., 0].astype(np.int32)
    s_chan = hsv_arr[..., 1].astype(np.int32)
    v_chan = hsv_arr[..., 2].astype(np.int32)
    r_chan = rgb_arr[..., 0].astype(np.int32)
    g_chan = rgb_arr[..., 1].astype(np.int32)
    b_chan = rgb_arr[..., 2].astype(np.int32)

    sat_mask = s_chan >= 80
    white_mask = (s_chan < 50) & (v_chan >= 200)

    cand_sat_sum = int(s_chan[sat_mask].sum())
    cand_sat_pixels = int(sat_mask.sum())
    white_pixel_count = int(white_mask.sum())

    hue = (h_chan / 255.0) * 360.0
    platform_color_counts = {p: 0 for p in ALL_PLATFORMS}

    xbox_mask = (
        sat_mask
        & (hue >= 90)
        & (hue <= 160)
        & (g_chan > r_chan * 1.2)
        & (g_chan > b_chan * 1.2)
    )
    switch_mask = (
        sat_mask
        & ((hue <= 10) | (hue >= 350))
        & (r_chan > g_chan * 1.5)
        & (r_chan > b_chan * 1.4)
        & (g_chan < 40)
        & (b_chan < 50)
    )
    blueish = sat_mask & (hue >= 190) & (hue <= 230) & (b_chan >= r_chan)
    pc_mask = blueish & (v_chan >= 190) & (hue <= 215)
    ps_mask = blueish & (v_chan < 180) & (hue >= 200)

    platform_color_counts[PLATFORM_XBOX] = int(xbox_mask.sum())
    platform_color_counts[PLATFORM_SWITCH] = int(switch_mask.sum())
    platform_color_counts[PLATFORM_PC] = int(pc_mask.sum())
    platform_color_counts[PLATFORM_PLAYSTATION] = int(ps_mask.sum())

    cand_mean_sat = cand_sat_sum / cand_sat_pixels if cand_sat_pixels > 0 else 0

    # Warframe's PC (Windows) and Mobile (Apple) icons are rendered WHITE
    # on a dark title bar, so their saturated-colour pixel count is ~0.
    # Treat the candidate as a "white-on-dark glyph" when white pixels
    # dominate any colored signal — IoU shape-match then distinguishes
    # Windows-logo vs Apple-silhouette.
    white_dominant = white_pixel_count > 0 and white_pixel_count >= max(
        platform_color_counts.values()
    )

    if cand_mean_sat >= 80:
        color_weight, shape_weight = 0.70, 0.30
    elif white_dominant:
        # White glyph: lean harder on shape (IoU) since both PC and Mobile
        # tie on colour and only the silhouette tells them apart.
        color_weight, shape_weight = 0.20, 0.80
    else:
        color_weight, shape_weight = 0.30, 0.70

    total_pixels = cw * ch
    scores: dict[str, float] = {}
    for platform, feat in features.items():
        ref_mask = feat.get("mask_np", feat["mask"])
        iou = _iou(cand_mask_np, ref_mask)

        color_pixel_count = platform_color_counts.get(platform, 0)

        # Unconditionally attribute white pixels to PC and Mobile. Both
        # icons are white-on-dark; IoU above breaks the tie via shape.
        if platform in (PLATFORM_PC, PLATFORM_MOBILE):
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
    # Track the best candidate even when it fails the confidence gate so we
    # can (a) surface real scores in analytics, (b) optionally accept a
    # relaxed-gate match when no candidate clears the strict gate.
    fallback_scores: dict[str, float] = {}
    fallback_platform: str | None = None
    fallback_score_value = -1.0

    for candidate in candidates:
        platform, scores = _score_candidate(candidate, _REFERENCE_FEATURES)
        if not scores:
            continue
        sorted_scores = sorted(scores.values(), reverse=True)
        cand_top = sorted_scores[0]
        if platform and cand_top > best_score_value:
            best_platform = platform
            best_scores = scores
            best_score_value = cand_top
            winning_roi = candidate
        # Independent of gating, remember the strongest scores seen so the
        # caller has telemetry even on failure.
        if cand_top > fallback_score_value:
            fallback_score_value = cand_top
            fallback_scores = scores
            fallback_platform = max(scores.items(), key=lambda kv: kv[1])[0]

    # Relaxed-gate fallback: if the strict gate rejected everything but a
    # candidate scored confidently (top >=0.35 AND runner-up <0.30), accept
    # it. The strict 0.45 gate is tuned for crisp title-bar crops; downscaled
    # or noisy uploads often land just below it while still being
    # unambiguous (clear separation from the next-best platform).
    if best_platform is None and fallback_platform is not None:
        sorted_fb = sorted(fallback_scores.values(), reverse=True)
        runner_up = sorted_fb[1] if len(sorted_fb) > 1 else 0.0
        gap = fallback_score_value - runner_up
        if fallback_score_value >= 0.35 and runner_up < 0.30:
            best_platform = fallback_platform
            best_scores = fallback_scores
            best_score_value = fallback_score_value
            logger.info(
                "Platform accepted via relaxed gate: %s score=%.3f runner_up=%.3f",
                best_platform,
                fallback_score_value,
                runner_up,
            )
        # White-glyph gate: PC (Windows) and Mobile (Apple) icons are tiny
        # white-on-dark glyphs and routinely score in the 0.20-0.30 band on
        # downscaled uploads — well below the strict gate but still clearly
        # the leader. Accept when the leader is PC/Mobile with any margin
        # over the runner-up. Colored-platform leaders (Xbox/PS/Switch)
        # still require the strict gate because their saturated colour
        # signal is unambiguous when truly present.
        elif (
            fallback_platform in (PLATFORM_PC, PLATFORM_MOBILE)
            and fallback_score_value >= 0.20
            and gap >= 0.01
        ):
            best_platform = fallback_platform
            best_scores = fallback_scores
            best_score_value = fallback_score_value
            logger.info(
                "Platform accepted via white-glyph gate: %s score=%.3f runner_up=%.3f gap=%.3f",
                best_platform,
                fallback_score_value,
                runner_up,
                gap,
            )

    # Always expose the best observed scores so analytics/logs are useful
    # even when detection failed outright.
    if not best_scores and fallback_scores:
        best_scores = fallback_scores
    
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
