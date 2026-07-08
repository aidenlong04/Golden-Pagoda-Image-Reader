"""Tests for gpbot.verify — verification pipeline primitives."""
from __future__ import annotations

import io

from PIL import Image

from gpbot.verify import (
    VerifyResult,
    VerifyState,
    parse_mastery_token,
    validate_image_bytes,
)


def _png_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), (10, 20, 30)).save(buf, format="PNG")
    return buf.getvalue()


class TestValidateImageBytes:
    def test_valid_png(self):
        assert validate_image_bytes(_png_bytes())

    def test_garbage_rejected(self):
        assert not validate_image_bytes(b"not an image")
        assert not validate_image_bytes(b"")


class TestParseMasteryToken:
    def test_mr(self):
        assert parse_mastery_token("MR 22") == ("MR", 22)

    def test_lr_case_insensitive(self):
        assert parse_mastery_token("lr3") == ("LR", 3)

    def test_leading_space(self):
        assert parse_mastery_token("  MR 7 extra") == ("MR", 7)

    def test_rejects_junk(self):
        assert parse_mastery_token("Rank 5") is None
        assert parse_mastery_token("") is None
        assert parse_mastery_token(None) is None


class TestVerifyResult:
    def test_backward_compatible_positional_construction(self):
        r = VerifyResult(["line"], "Player#1", "MR 9")
        assert r.summary == ["line"]
        assert r.in_game_name == "Player#1"
        assert r.mastery_rank == "MR 9"
        assert r.state is VerifyState.VERIFIED
        assert r.ok

    def test_failed_factory(self):
        r = VerifyResult.failed(VerifyState.OCR_FAILED)
        assert r.summary == []
        assert r.in_game_name is None
        assert r.mastery_rank is None
        assert r.state is VerifyState.OCR_FAILED
        assert not r.ok

    def test_verified_with_empty_summary_not_ok(self):
        assert not VerifyResult([], None, None).ok

    def test_clan_name_defaults_none(self):
        r = VerifyResult(["line"], "Player#1", "MR 9")
        assert r.clan_name is None

    def test_clan_name_carried(self):
        r = VerifyResult(["line"], "Player#1", "MR 9", clan_name="My Clan")
        assert r.clan_name == "My Clan"
        assert r.ok

    def test_failed_factory_has_no_clan_name(self):
        assert VerifyResult.failed(VerifyState.OCR_FAILED).clan_name is None
