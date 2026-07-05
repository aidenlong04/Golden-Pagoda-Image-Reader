"""Verification pipeline primitives.

The screenshot-verify flow shared by onboarding, ``/profile`` and ``/manage``
decomposes into explicit, individually testable states:

``INVALID_IMAGE``  the upload isn't a decodable image — nothing is assigned.
``OCR_FAILED``     the OCR chain couldn't read the screenshot — nothing is
                   assigned.
``VERIFIED``       the screenshot parsed; roles were assigned and the summary
                   lines describe every field that was updated.

The role-assignment I/O itself stays in bot.py (it owns the Discord client);
this module holds the pure decision helpers plus the result envelope.
"""
from __future__ import annotations

import enum
import io
import logging
import re
from typing import NamedTuple

from PIL import Image

logger = logging.getLogger(__name__)


class VerifyState(enum.Enum):
    """Explicit outcome states of the screenshot-verification pipeline."""

    INVALID_IMAGE = "invalid_image"
    OCR_FAILED = "ocr_failed"
    VERIFIED = "verified"


class VerifyResult(NamedTuple):
    """Outcome of the screenshot verification pipeline.

    ``summary`` holds the human-readable per-field lines (empty when the
    screenshot couldn't be read). ``in_game_name`` and ``mastery_rank`` carry
    the OCR-only fields — the handle + exact rank that aren't recoverable from
    Discord roles — so callers can thread them into the durable member store.
    """

    summary: list[str]
    in_game_name: str | None
    mastery_rank: str | None
    state: VerifyState = VerifyState.VERIFIED

    @property
    def ok(self) -> bool:
        return self.state is VerifyState.VERIFIED and bool(self.summary)

    @classmethod
    def failed(cls, state: VerifyState) -> "VerifyResult":
        return cls([], None, None, state)


def validate_image_bytes(image_bytes: bytes) -> bool:
    """True when ``image_bytes`` decodes as an image PIL can verify.

    This is the cheap pre-OCR gate: rejecting a corrupt upload here skips the
    whole OCR chain (and its worker-thread slot) entirely.
    """
    try:
        probe = Image.open(io.BytesIO(image_bytes))
        try:
            probe.verify()
        finally:
            probe.close()
    except Exception:
        logger.warning("verify: invalid image upload", exc_info=True)
        return False
    return True


_MASTERY_TOKEN_RE = re.compile(r"\s*(MR|LR)\s*(\d+)", re.IGNORECASE)


def parse_mastery_token(raw: str | None) -> tuple[str, int] | None:
    """Parse an OCR'd mastery string into ``(kind, value)``.

    ``kind`` is ``"MR"`` or ``"LR"``. Returns None when ``raw`` doesn't start
    with a recognisable rank token.
    """
    m = _MASTERY_TOKEN_RE.match(raw or "")
    if not m:
        return None
    return m.group(1).upper(), int(m.group(2))
