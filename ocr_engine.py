"""OCR for the Golden Pagoda bot.

OCR.space (engine 3) with a local Tesseract fallback, plus the title-bar
supplement pass that recovers the PlayerName#NNN token OCR.space tends to
drop. Sync primitives only — the async orchestrator (`_ocr_profile_fields`)
stays in bot.py because it drives these through the shared `_run_heavy`
concurrency gate. Imports nothing from bot (which is `__main__` in prod);
the env-parse helpers come from config.py.

Performance enhancements (all tunable via env vars):
- Exponential back-off with jitter on OCR.space API retries
  (OCR_RETRY_MAX_ATTEMPTS, OCR_RETRY_BASE_DELAY, OCR_RETRY_MAX_DELAY).
- Circuit breaker that opens after OCR_CIRCUIT_BREAKER_THRESHOLD consecutive
  failures and stays open for OCR_CIRCUIT_BREAKER_RECOVERY seconds.
- In-memory LRU cache of OCR results keyed by SHA-256 of the image bytes
  (OCR_CACHE_SIZE entries, default 32).  A cache hit skips both the HTTP
  round-trip *and* the Tesseract fall-through entirely.
"""
from __future__ import annotations

import base64
import hashlib
import io
import logging
import os
import re
import threading
import time
from collections import OrderedDict

import requests
from PIL import Image, ImageOps

from config import _int_env
from utils.retry import (
    CircuitOpenError,
    OCR_RETRY_BASE_DELAY,
    OCR_RETRY_MAX_ATTEMPTS,
    OCR_RETRY_MAX_DELAY,
    exponential_backoff,
    ocr_circuit_breaker,
)

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

# Local Ollama vision backend. When OLLAMA_OCR_MODEL is set, the bot reads the
# screenshot text with a local Ollama vision model (e.g. llama3.2-vision,
# llava) instead of OCR.space — fully offline, no API key. Falls through to
# OCR.space / Tesseract if Ollama is unreachable or returns nothing.
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434").rstrip("/")
OLLAMA_OCR_MODEL = os.getenv("OLLAMA_OCR_MODEL", "").strip()
OLLAMA_TIMEOUT = _int_env("OLLAMA_TIMEOUT", 120)
_OLLAMA_OCR_PROMPT = (
    "You are an OCR engine. Transcribe ALL text visible in this Warframe "
    "profile screenshot exactly as it appears, line by line. Preserve the "
    "title-bar player handle in the form PlayerName#NNN, the MASTERY RANK "
    "number, and the CLAN name. Output only the raw transcribed text — no "
    "commentary, no markdown, no explanations."
)
# OCR.space free tier rejects uploads >1 MB. We re-encode oversize images as
# JPEG before sending so large PNG screenshots still get verified.
OCR_MAX_UPLOAD_BYTES = _int_env("OCR_MAX_UPLOAD_BYTES", 900_000)
OCR_RECOMPRESS_QUALITY = _int_env("OCR_RECOMPRESS_QUALITY", 70)

# ---------------------------------------------------------------------------
# In-memory OCR result cache (keyed by SHA-256 of the original image bytes)
# ---------------------------------------------------------------------------
# Avoids redundant OCR round-trips when the same screenshot is submitted
# multiple times (re-uploads, catch-up retries).  LRU eviction via
# OrderedDict.  Cache is never persisted — it resets on container restart.
OCR_CACHE_SIZE = _int_env("OCR_CACHE_SIZE", 32)
_ocr_cache: "OrderedDict[str, tuple[str, list[tuple[str, tuple[int, int, int, int]]], str]]" = OrderedDict()
_ocr_cache_lock = threading.Lock()


def _cache_key(image_bytes: bytes) -> str:
    return hashlib.sha256(image_bytes).hexdigest()


def _cache_get(key: str) -> "tuple[str, list[tuple[str, tuple[int, int, int, int]]], str] | None":
    with _ocr_cache_lock:
        if key not in _ocr_cache:
            return None
        # Move to end (most-recently used).
        _ocr_cache.move_to_end(key)
        return _ocr_cache[key]


def _cache_put(
    key: str,
    value: "tuple[str, list[tuple[str, tuple[int, int, int, int]]], str]",
) -> None:
    with _ocr_cache_lock:
        _ocr_cache[key] = value
        _ocr_cache.move_to_end(key)
        while len(_ocr_cache) > OCR_CACHE_SIZE:
            _ocr_cache.popitem(last=False)


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
    """Call the OCR.space API with exponential back-off retries and circuit
    breaker protection.

    Raises :exc:`~utils.retry.CircuitOpenError` immediately when the circuit
    is open (too many recent failures) so the caller can fall through to the
    Tesseract backend without consuming a full 60-second timeout.
    """
    if not ocr_circuit_breaker.allow_request():
        raise CircuitOpenError(
            f"OCR.space circuit is open — skipping API call "
            f"({ocr_circuit_breaker.snapshot()})"
        )

    image_bytes, filename, content_type = _shrink_for_ocr(
        image_bytes, filename, content_type
    )

    payload: dict | None = None
    last_err: Exception | None = None

    for attempt in range(1, OCR_RETRY_MAX_ATTEMPTS + 1):
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
            if 500 <= response.status_code < 600:
                exc = requests.HTTPError(
                    f"OCR.space returned {response.status_code}", response=response
                )
                last_err = exc
                if attempt < OCR_RETRY_MAX_ATTEMPTS:
                    delay = exponential_backoff(
                        attempt,
                        base=OCR_RETRY_BASE_DELAY,
                        cap=OCR_RETRY_MAX_DELAY,
                    )
                    logger.info(
                        "OCR.space %d on attempt %d/%d; retrying in %.1fs",
                        response.status_code,
                        attempt,
                        OCR_RETRY_MAX_ATTEMPTS,
                        delay,
                    )
                    time.sleep(delay)
                    continue
                ocr_circuit_breaker.record_failure()
                raise exc
            response.raise_for_status()
            payload = response.json()
            break
        except (requests.HTTPError, requests.ConnectionError, requests.Timeout) as exc:
            last_err = exc
            if attempt < OCR_RETRY_MAX_ATTEMPTS:
                delay = exponential_backoff(
                    attempt,
                    base=OCR_RETRY_BASE_DELAY,
                    cap=OCR_RETRY_MAX_DELAY,
                )
                logger.info(
                    "OCR.space %s on attempt %d/%d; retrying in %.1fs",
                    exc.__class__.__name__,
                    attempt,
                    OCR_RETRY_MAX_ATTEMPTS,
                    delay,
                )
                time.sleep(delay)
                continue
            ocr_circuit_breaker.record_failure()
            raise

    if payload is None:
        ocr_circuit_breaker.record_failure()
        raise last_err if last_err else RuntimeError("OCR.space failed")

    if payload.get("IsErroredOnProcessing"):
        ocr_circuit_breaker.record_failure()
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
    ocr_circuit_breaker.record_success()
    return text, words


def _ocr_via_ollama(
    image_bytes: bytes,
) -> tuple[str, list[tuple[str, tuple[int, int, int, int]]]]:
    """Transcribe a screenshot with a local Ollama vision model.

    Returns ``(text, [])`` — Ollama yields plain text with no per-word bounding
    boxes, so the words list is always empty (the title-bar Tesseract
    supplement still runs afterward to recover the PlayerName#NNN box when a
    local Tesseract is available).
    """
    if not OLLAMA_OCR_MODEL:
        raise RuntimeError("OLLAMA_OCR_MODEL not configured")
    b64 = base64.b64encode(image_bytes).decode("ascii")
    response = requests.post(
        f"{OLLAMA_URL}/api/generate",
        json={
            "model": OLLAMA_OCR_MODEL,
            "prompt": _OLLAMA_OCR_PROMPT,
            "images": [b64],
            "stream": False,
            "options": {"temperature": 0},
        },
        timeout=OLLAMA_TIMEOUT,
    )
    response.raise_for_status()
    data = response.json()
    text = (data.get("response") or "").strip()
    return text, []


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
    # Fast path: return a previously cached result for the same image bytes.
    key = _cache_key(image_bytes)
    cached = _cache_get(key)
    if cached is not None:
        logger.debug("OCR cache hit for %s (%d bytes)", filename, len(image_bytes))
        return cached

    result = _ocr_uncached(image_bytes, filename, content_type)
    # Fold the title-bar supplement into the cached result: OCR.space routinely
    # drops the small PlayerName#NNN line, and recovering it via a Tesseract
    # top-strip crop is pure CPU. Caching the augmented result means re-uploads
    # / catch-up retries skip that strip pass too (not just the backend call).
    text, words, engine = result
    text, words = _supplement_title_bar_ocr(image_bytes, text, words)
    result = (text, words, engine)
    _cache_put(key, result)
    return result


def _ocr_uncached(
    image_bytes: bytes, filename: str, content_type: str
) -> tuple[str, list[tuple[str, tuple[int, int, int, int]]], str]:
    # Preferred backend: a local Ollama vision model (offline, no API key).
    if OLLAMA_OCR_MODEL:
        try:
            text, words = _ocr_via_ollama(image_bytes)
            if text:
                return text, words, "ollama"
            logger.warning(
                "Ollama OCR (%s) returned empty text; falling back",
                OLLAMA_OCR_MODEL,
            )
        except Exception as ollama_err:
            logger.warning(
                "Ollama OCR (%s) failed (%s); falling back",
                OLLAMA_OCR_MODEL,
                ollama_err.__class__.__name__,
            )
        # Fall through: prefer OCR.space if configured, else Tesseract.
        if OCR_API_KEY:
            try:
                text, words = _ocr_via_api(image_bytes, filename, content_type)
                return text, words, "ocr.space"
            except Exception:
                logger.warning("OCR.space fallback after Ollama also failed")
        if pytesseract is not None:
            text, words = _ocr_via_tesseract(image_bytes)
            return text, words, "tesseract"
        raise RuntimeError(
            "Ollama OCR failed and no OCR.space key / Tesseract fallback "
            "is available."
        )
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
