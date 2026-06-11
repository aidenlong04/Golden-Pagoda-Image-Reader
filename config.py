"""Environment-variable parsing helpers for the Golden Pagoda bot.

Pure ``os.getenv`` readers shared by bot.py and the leaf modules (e.g.
ocr_engine). They live in their own module so a leaf can import them without
importing bot.py — in production the bot runs as ``python -u bot.py`` (module
name ``__main__``), so a ``from bot import`` in a leaf would re-import the
whole bot as a second module (duplicate client/handlers). config.py imports
nothing from the rest of the app, so it is always safe to import.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def _int_env(name: str, default: int = 0) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        # base=0 accepts decimal, 0x hex, 0o octal, 0b binary literals.
        return int(raw, 0) if raw.lower().startswith(("0x", "0o", "0b")) else int(raw)
    except ValueError:
        logger.warning("Env var %s=%r is not a valid integer; using %d", name, raw, default)
        return default


def _float_env(name: str, default: float = 0.0) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("Env var %s=%r is not a valid float; using %s", name, raw, default)
        return default


def _csv(name: str, default: str = "") -> list[str]:
    return [
        x.strip() for x in (os.getenv(name) or default).split(",") if x.strip()
    ]


def _csv_ids(name: str) -> list[int]:
    return [int(x) for x in _csv(name) if x.isdigit()]
