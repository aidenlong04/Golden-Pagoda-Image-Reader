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
    # Foreground = saturated colored OR bright white-on-dark glyph pixels.
    # The bright-V cutoff is loose (>=190) so anti-aliased edges of
    # white-rendered Warframe icons (Xbox/PC/Mobile) still count.
    fg_mask = ((s >= 80) & (v >= 100)) | (v >= 190)
    col_counts = fg_mask.sum(axis=0)
    # Column threshold has to accept thin glyphs (the Xbox X has only a
    # handful of bright pixels per column) while still rejecting noise.
    # sh//5 was calibrated for solid colored rings and rejected white X
    # glyphs outright -> _icon_bbox returned None -> (unknown).
    threshold = max(3, sh // 12)

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

    # Merge runs whose horizontal gap is small (< sh//3). Thin white-on-dark
    # glyphs like the Xbox X produce multiple short column-runs separated by
    # the dark interior between arms; without merging we'd pick a single
    # arm fragment and lose the icon's true extent.
    merge_gap = max(2, sh // 3)
    merged: list[tuple[int, int]] = []
    for r in runs:
        if merged and r[0] - merged[-1][1] <= merge_gap:
            merged[-1] = (merged[-1][0], r[1])
        else:
            merged.append(r)
    runs = merged

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

    # Pad generously so anti-aliased glyph edges (which fall below the
    # bright-V cutoff) are still included in the cropped ROI.
    pad = max(2, sh // 12)
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


# --- Perceptual hashes & normalized cross-correlation ----------------------
#
# Pictogram-recognition primitives. All numpy + Pillow, no extra deps.
#
# dhash (difference hash): compare adjacent column brightness; produces a
#   (size, size) bool grid. Robust to scaling/blur/AA. Hamming distance is
#   the per-bit disagreement count.
# phash (perceptual hash): 2D DCT-II via numpy.fft on a downsampled grayscale
#   image, take the low-frequency 8x8 block (minus DC), threshold at median.
#   Stronger than dhash against subtle distortions; same Hamming-distance
#   compare.
# ncc (normalized cross-correlation): zero-mean unit-norm dot product
#   between two equal-sized grayscale arrays; 1.0 = identical, 0.0 = no
#   correlation. Cheap when both are pre-resized to 32x32.
#
# Each returns a similarity in 0..1 (1 = identical). The ensemble in
# _score_candidate fuses dhash + phash + ncc + iou with shape-only weights.

_DHASH_SIZE = 16  # 256 bits — discriminative enough for 5 classes
_PHASH_LOW = 8    # keep the 8x8 low-frequency block (64 bits, ex-DC)
_PHASH_DOWN = 32  # downsample size before DCT
_TEMPLATE_SIZE = 32  # NCC template size


def _grayscale_array(img: Image.Image, size: int) -> np.ndarray:
    g = img.convert("L").resize((size, size), Image.LANCZOS)
    return np.asarray(g, dtype=np.float32)


def _tight_crop_to_fg(img: Image.Image) -> Image.Image:
    """Crop to the same foreground bbox _binarize_and_crop uses.

    Keeps hash/NCC inputs invariant to slack padding around the icon
    (the title-bar bbox finder pads generously and can extend to the
    canvas edge). Falls back to the original image if no foreground.
    """
    rgba = np.asarray(img.convert("RGBA"))
    mask = (rgba[..., :3].max(axis=-1) >= 60) & (rgba[..., 3] >= 128)
    if not mask.any():
        return img
    ys, xs = np.where(mask)
    y0, y1 = int(ys.min()), int(ys.max()) + 1
    x0, x1 = int(xs.min()), int(xs.max()) + 1
    return img.crop((x0, y0, x1, y1))


def _dhash_bits(img: Image.Image) -> np.ndarray:
    cropped = _tight_crop_to_fg(img)
    g = cropped.convert("L").resize((_DHASH_SIZE + 1, _DHASH_SIZE), Image.LANCZOS)
    arr = np.asarray(g, dtype=np.int16)
    return arr[:, 1:] > arr[:, :-1]


def _dct1d(a: np.ndarray) -> np.ndarray:
    """DCT-II along the last axis using numpy.fft.rfft (Makhoul's method)."""
    N = a.shape[-1]
    # Build the 2N-length even-symmetric extension.
    v = np.empty(a.shape[:-1] + (2 * N,), dtype=np.float64)
    v[..., :N] = a
    v[..., N:] = a[..., ::-1]
    V = np.fft.rfft(v, axis=-1)
    k = np.arange(N)
    phase = np.exp(-1j * np.pi * k / (2 * N))
    return (V[..., :N] * phase).real


def _dct2(a: np.ndarray) -> np.ndarray:
    return _dct1d(_dct1d(a).T).T


def _phash_bits(img: Image.Image) -> np.ndarray:
    cropped = _tight_crop_to_fg(img)
    g = cropped.convert("L").resize((_PHASH_DOWN, _PHASH_DOWN), Image.LANCZOS)
    arr = np.asarray(g, dtype=np.float64)
    dct = _dct2(arr)
    low = dct[:_PHASH_LOW, :_PHASH_LOW].copy()
    low_flat = low.flatten()
    med = float(np.median(low_flat[1:]))
    return low > med


def _hash_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Random-baseline-corrected Hamming similarity.

    Two unrelated bit-hashes agree on ~50% of bits by chance, so a naive
    ``1 - hamming/N`` similarity bottoms out near 0.5 and compresses the
    discriminative gap between classes. Rescale to ``max(0, 2*s - 1)`` so
    chance-level agreement maps to 0.0 and identical hashes still map to 1.0.
    """
    if a.shape != b.shape:
        return 0.0
    total = a.size
    hamming = int(np.count_nonzero(a != b))
    raw = 1.0 - (hamming / total)
    return max(0.0, 2.0 * raw - 1.0)


def _ncc_template(img: Image.Image) -> np.ndarray:
    """Zero-mean unit-norm grayscale template (size _TEMPLATE_SIZE)."""
    cropped = _tight_crop_to_fg(img)
    arr = _grayscale_array(cropped, _TEMPLATE_SIZE)
    arr -= arr.mean()
    norm = float(np.sqrt((arr * arr).sum()))
    return arr / norm if norm > 0 else arr


def _ncc_similarity(a: np.ndarray, b: np.ndarray) -> float:
    if a.shape != b.shape:
        return 0.0
    # Both are zero-mean unit-norm; correlation is just the dot product in
    # the range [-1, 1]. Clamp negatives (anticorrelation isn't useful here)
    # and treat as a 0..1 similarity.
    val = float((a * b).sum())
    return max(0.0, min(1.0, val))


def _extract_reference_features(
    references: dict[str, Image.Image]
) -> dict[str, dict]:
    """Precompute mask + dhash + phash + NCC template for each reference."""
    features: dict[str, dict] = {}
    for key, ref in references.items():
        mask, fg_count = _binarize_and_crop(ref, size=(32, 32))
        features[key] = {
            "mask": mask,
            "mask_np": np.asarray(mask, dtype=bool),
            "fg_count": fg_count,
            "dhash": _dhash_bits(ref),
            "phash": _phash_bits(ref),
            "ncc": _ncc_template(ref),
        }
    return features


def _score_candidate(
    candidate_crop: Image.Image, features: dict[str, dict]
) -> tuple[str | None, dict[str, float]]:
    """Score a candidate icon ROI against reference shapes using IoU.

    Pure shape match (no colour analysis). Warframe's profile UI renders
    every platform icon as white-on-dark, so colour signal is unreliable;
    the silhouette alone separates all five platforms cleanly.
    """
    cw, ch = candidate_crop.size
    if cw * ch < 100:
        return None, {p: 0.0 for p in features}
    cand_mask, cand_fg = _binarize_and_crop(candidate_crop, size=(32, 32))
    if cand_fg < 50:
        return None, {p: 0.0 for p in features}
    cand_mask_np = np.asarray(cand_mask, dtype=bool)
    cand_dhash = _dhash_bits(candidate_crop)
    cand_phash = _phash_bits(candidate_crop)
    cand_ncc = _ncc_template(candidate_crop)

    # Ensemble weights. IoU on the binarized silhouette stays the strongest
    # signal (the Warframe title-bar icons are simple white-on-dark shapes
    # whose foreground masks are very class-discriminative). Phash + dhash +
    # NCC add robustness against compression artefacts, mild rescaling, and
    # AA-edge variation that can erode IoU on real screenshots. Weights sum
    # to 1.0 so the existing 0.45 / 0.15 strict and 0.30 / 0.10 relaxed gates
    # keep their calibration.
    W_PHASH, W_DHASH, W_NCC, W_IOU = 0.15, 0.15, 0.20, 0.50

    scores: dict[str, float] = {}
    components: dict[str, dict[str, float]] = {}
    for platform, feat in features.items():
        ref_mask = feat.get("mask_np", feat["mask"])
        iou = _iou(cand_mask_np, ref_mask)
        dhash_sim = _hash_similarity(cand_dhash, feat["dhash"])
        phash_sim = _hash_similarity(cand_phash, feat["phash"])
        ncc = _ncc_similarity(cand_ncc, feat["ncc"])
        components[platform] = {
            "iou": iou,
            "dhash": dhash_sim,
            "phash": phash_sim,
            "ncc": ncc,
        }
        scores[platform] = (
            W_PHASH * phash_sim
            + W_DHASH * dhash_sim
            + W_NCC * ncc
            + W_IOU * iou
        )

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

    # Relaxed-gate fallback: if the strict gate (best>=0.45, gap>=0.15)
    # rejected everything, accept a downscaled/noisy match where the
    # leader is still comfortably ahead (best>=0.30, gap>=0.10). The
    # margin requirement is what actually disambiguates one platform
    # from another — the absolute floor is just to reject obviously
    # off-target ROIs.
    if best_platform is None and fallback_platform is not None:
        sorted_fb = sorted(fallback_scores.values(), reverse=True)
        runner_up = sorted_fb[1] if len(sorted_fb) > 1 else 0.0
        gap = fallback_score_value - runner_up
        if fallback_score_value >= 0.30 and gap >= 0.10:
            best_platform = fallback_platform
            best_scores = fallback_scores
            best_score_value = fallback_score_value
            logger.info(
                "Platform accepted via relaxed gate: %s score=%.3f runner_up=%.3f gap=%.3f",
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
