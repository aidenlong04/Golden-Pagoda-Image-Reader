"""OCR for the Golden Pagoda bot.

OCR.space (engine 3) with a local Tesseract fallback, plus the title-bar
supplement pass that recovers the PlayerName#NNN token OCR.space tends to
drop. Sync primitives only — the async orchestrator (`_ocr_profile_fields`)
stays in bot.py because it drives these through the shared `_run_heavy`
concurrency gate. Imports nothing from bot (which is `__main__` in prod);
the env-parse helper comes from config.py.
"""
from __future__ import annotations

import io
import logging
import os
import re
import time

import requests
from PIL import Image, ImageOps

from config import _int_env

try:
    import pytesseract  # optional local fallback
except ImportError:
    pytesseract = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# OCR.space API — engine 3 per requirements. Falls back to local Tesseract
# (with --oem 3) if OCR_API_KEY is not set and pytesseract is available.
OCR_API_KEY = os.getenv("OCR_API_KEY", "").strip()
OCR_API_URL = os.getenv("OCR_API_URL", "https://api.ocr.space/parse/image")
OCR_ENGINE = os.getenv("OCR_ENGINE", "3")
OCR_LANGUAGE = os.getenv("OCR_LANGUAGE", "eng")
TESSERACT_CONFIG = "--oem 3 --psm 6"
# OCR.space free tier rejects uploads >1 MB. We re-encode oversize images as
# JPEG before sending so large PNG screenshots still get verified.
OCR_MAX_UPLOAD_BYTES = _int_env("OCR_MAX_UPLOAD_BYTES", 900_000)
OCR_RECOMPRESS_QUALITY = _int_env("OCR_RECOMPRESS_QUALITY", 70)


def _shrink_for_ocr(
    image_bytes: bytes, filename: str, content_type: str
) -> tuple[bytes, str, str]:
    """Re-encode oversize images as JPEG so they fit the OCR.space 1 MB limit."""
    if len(image_bytes) <= OCR_MAX_UPLOAD_BYTES:
        return image_bytes, filename, content_type or "image/png"
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        try:
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=OCR_RECOMPRESS_QUALITY, optimize=True)
            shrunk = buf.getvalue()
        finally:
            img.close()
    except Exception:
        logger.exception("Failed to recompress image for OCR; sending original")
        return image_bytes, filename, content_type or "image/png"
    base = filename.rsplit(".", 1)[0] or "screenshot"
    logger.info("Recompressed %s for OCR: %d -> %d bytes", filename, len(image_bytes), len(shrunk))
    return shrunk, f"{base}.jpg", "image/jpeg"


def _ocr_via_api(
    image_bytes: bytes,
    filename: str,
    content_type: str,
    *,
    engine: str | None = None,
) -> tuple[str, list[tuple[str, tuple[int, int, int, int]]]]:
    image_bytes, filename, content_type = _shrink_for_ocr(
        image_bytes, filename, content_type
    )
    # OCR.space returns transient 5xx; one quick retry usually clears it.
    last_err: Exception | None = None
    for attempt in (1, 2):
        try:
            response = requests.post(
                OCR_API_URL,
                headers={"apikey": OCR_API_KEY},
                data={
                    "OCREngine": engine or OCR_ENGINE,
                    "language": OCR_LANGUAGE,
                    "scale": "true",
                    "isTable": "false",
                    "detectOrientation": "true",
                    "isOverlayRequired": "true",
                },
                files={"file": (filename, image_bytes, content_type or "image/png")},
                timeout=60,
            )
            if 500 <= response.status_code < 600 and attempt == 1:
                logger.info("OCR.space %d on attempt 1; retrying once", response.status_code)
                time.sleep(1.0)
                continue
            response.raise_for_status()
            payload = response.json()
            break
        except (requests.HTTPError, requests.ConnectionError, requests.Timeout) as e:
            last_err = e
            if attempt == 1:
                logger.info("OCR.space %s on attempt 1; retrying once", e.__class__.__name__)
                time.sleep(1.0)
                continue
            raise
    else:
        raise last_err if last_err else RuntimeError("OCR.space failed")
    if payload.get("IsErroredOnProcessing"):
        raise RuntimeError(f"OCR API error: {payload.get('ErrorMessage') or payload}")
    parsed = payload.get("ParsedResults") or []
    text = "\n".join(item.get("ParsedText", "") for item in parsed)
    words: list[tuple[str, tuple[int, int, int, int]]] = []
    for item in parsed:
        overlay = item.get("TextOverlay") or {}
        for line in overlay.get("Lines") or []:
            for word in line.get("Words") or []:
                try:
                    left = int(word.get("Left", 0))
                    top = int(word.get("Top", 0))
                    width = int(word.get("Width", 0))
                    height = int(word.get("Height", 0))
                except (TypeError, ValueError):
                    continue
                if width <= 0 or height <= 0:
                    continue
                words.append(
                    (str(word.get("WordText", "")), (left, top, left + width, top + height))
                )
    return text, words


def _preprocess_for_tesseract(image_bytes: bytes) -> Image.Image:
    """Upscale + grayscale + autocontrast. Tesseract is dramatically more
    accurate on Warframe's stylized UI font when the input is enlarged and
    contrast-normalized first.

    Caller is responsible for closing the returned Image.
    """
    img = Image.open(io.BytesIO(image_bytes))
    if img.mode != "L":
        img = ImageOps.grayscale(img)
    # Upscale only when the source is modestly sized; very large screenshots
    # are already legible and 2x would blow past Tesseract's memory budget.
    if max(img.size) < 2400:
        img = img.resize((img.width * 2, img.height * 2), Image.LANCZOS)
    img = ImageOps.autocontrast(img, cutoff=2)
    return img


def _ocr_via_tesseract(
    image_bytes: bytes,
) -> tuple[str, list[tuple[str, tuple[int, int, int, int]]]]:
    if pytesseract is None:
        raise RuntimeError("pytesseract not installed")
    try:
        prepared = _preprocess_for_tesseract(image_bytes)
    except Exception:
        logger.exception("Tesseract preprocess failed; falling back to raw image")
        prepared = Image.open(io.BytesIO(image_bytes))
    try:
        text = pytesseract.image_to_string(prepared, config=TESSERACT_CONFIG)
    finally:
        prepared.close()
    return text, []


# Engine label returned alongside (text, words) so the caller can record
# an accurate analytics tag without relying on shared mutable state
# (concurrent _ocr() invocations would otherwise race).
def _ocr(
    image_bytes: bytes, filename: str, content_type: str
) -> tuple[str, list[tuple[str, tuple[int, int, int, int]]], str]:
    if OCR_API_KEY:
        try:
            text, words = _ocr_via_api(image_bytes, filename, content_type)
            return text, words, "ocr.space"
        except Exception as api_err:
            logger.warning(
                "OCR.space engine %s failed (%s); trying engine 2",
                OCR_ENGINE,
                api_err.__class__.__name__,
            )
            # Engine 2 sometimes succeeds where engine 3 (multilang) 500s
            # on the same upload. Skip the second attempt if we were already
            # configured for engine 2.
            if OCR_ENGINE != "2":
                try:
                    text, words = _ocr_via_api(
                        image_bytes, filename, content_type, engine="2"
                    )
                    return text, words, "ocr.space:e2"
                except Exception as api_err2:
                    logger.warning(
                        "OCR.space engine 2 also failed (%s); falling back to local Tesseract",
                        api_err2.__class__.__name__,
                    )
            if pytesseract is None:
                raise
            text, words = _ocr_via_tesseract(image_bytes)
            return text, words, "tesseract"
    if pytesseract is None:
        raise RuntimeError(
            "No OCR backend available: set OCR_API_KEY or install pytesseract."
        )
    text, words = _ocr_via_tesseract(image_bytes)
    return text, words, "tesseract"


_PROFILE_TOKEN_RE = re.compile(r"#\d{2,4}")
_TITLE_NAME_RE = re.compile(r"[A-Za-z0-9_\-\.\[\]]{2,}#\d{2,4}")
# How much of the image height counts as "title bar" for the supplement pass.
_TITLE_STRIP_FRAC = 0.14


def _supplement_title_bar_ocr(
    image_bytes: bytes,
    ocr_text: str,
    ocr_words: list[tuple[str, tuple[int, int, int, int]]],
) -> tuple[str, list[tuple[str, tuple[int, int, int, int]]]]:
    """If the main OCR missed the title-bar PlayerName#NNN token, retry just
    the top strip with Tesseract. OCR.space engine 3 routinely drops the
    small, low-contrast title-bar line on Warframe profile screenshots even
    when the rest of the page reads cleanly. Tesseract on an upscaled,
    autocontrasted top-strip crop recovers it reliably.

    Returns the (possibly augmented) (text, words) tuple. No-op if pytesseract
    is unavailable or the original OCR already contains a #NNN token in the
    top portion of the image.
    """
    if pytesseract is None:
        return ocr_text, ocr_words
    img = None
    try:
        img = Image.open(io.BytesIO(image_bytes))
        w, h = img.size
        strip_h = max(40, int(h * _TITLE_STRIP_FRAC))
        # If we already have a #NNN word inside the title strip, no need to retry.
        for _text, (_x0, y0, _x1, _y1) in ocr_words:
            if y0 < strip_h and _PROFILE_TOKEN_RE.search(_text or ""):
                return ocr_text, ocr_words
        try:
            strip = img.crop((0, 0, w, strip_h)).convert("L")
            try:
                # Upscale aggressively — title-bar glyphs are ~16-22px tall on a
                # 1080p screenshot, well below Tesseract's comfort zone.
                strip = strip.resize((strip.width * 3, strip.height * 3), Image.LANCZOS)
                strip = ImageOps.autocontrast(strip, cutoff=2)
                data = pytesseract.image_to_data(
                    strip,
                    config="--oem 3 --psm 7",
                    output_type=pytesseract.Output.DICT,
                )
            finally:
                strip.close()
        except Exception:
            logger.exception("Title-bar Tesseract supplement failed")
            return ocr_text, ocr_words
    except Exception:
        return ocr_text, ocr_words
    finally:
        if img is not None:
            img.close()
    new_words: list[tuple[str, tuple[int, int, int, int]]] = []
    found_token = False
    n = len(data.get("text") or [])
    for i in range(n):
        txt = (data["text"][i] or "").strip()
        if not txt:
            continue
        try:
            left = int(data["left"][i]) // 3
            top = int(data["top"][i]) // 3
            width = int(data["width"][i]) // 3
            height = int(data["height"][i]) // 3
        except (TypeError, ValueError, KeyError):
            continue
        if width <= 0 or height <= 0:
            continue
        new_words.append((txt, (left, top, left + width, top + height)))
        if _PROFILE_TOKEN_RE.search(txt):
            found_token = True
    if not found_token:
        # Also try matching the joined line — Tesseract sometimes splits
        # "Senseiwom#241" into "Senseiwom" + "#241" tokens.
        joined = " ".join(t for t, _ in new_words)
        if _TITLE_NAME_RE.search(joined):
            found_token = True
    if not found_token:
        joined = " ".join(t for t, _ in new_words)
        logger.info(
            "Title-bar OCR supplement found no #NNN token "
            "(strip=%dpx, tokens=%d, text=%r)",
            strip_h,
            len(new_words),
            joined[:200],
        )
        return ocr_text, ocr_words
    logger.info(
        "Title-bar OCR supplement recovered %d token(s): %r",
        len(new_words),
        " ".join(t for t, _ in new_words)[:120],
    )
    # Prepend the recovered title-bar text so parse_profile_name sees it
    # before any CLAN-section content.
    supplement_line = " ".join(t for t, _ in new_words)
    augmented_text = supplement_line + "\n" + ocr_text
    return augmented_text, new_words + ocr_words
