from __future__ import annotations

import importlib

import pytest


@pytest.fixture()
def analytics_module(tmp_path, monkeypatch):
    db = tmp_path / "a.db"
    monkeypatch.setenv("ANALYTICS_DB_PATH", str(db))
    import analytics
    # Close any stale connection cached from a previous test fixture
    # before reload, otherwise the old sqlite handle leaks until GC.
    old_conn = getattr(analytics, "_conn", None)
    if old_conn is not None:
        try:
            old_conn.close()
        except Exception:
            pass
    importlib.reload(analytics)
    return analytics


def test_record_and_summary_roundtrip(analytics_module):
    a = analytics_module
    a.record_verification(
        outcome="pass", clan="Argon",
        ocr_engine="ocr.space", ocr_latency_ms=320,
        user_id=1, guild_id=2,
    )
    a.record_verification(
        outcome="incomplete", clan=None,
        ocr_engine="ocr.space", ocr_latency_ms=410,
        user_id=3, guild_id=2,
    )
    a.record_verification(
        outcome="pass", clan="Argon",
        ocr_engine="tesseract", ocr_latency_ms=900,
        user_id=4, guild_id=2,
    )
    s = a.summary()
    assert s["available"] is True
    assert s["total"] == 3
    assert s["by_outcome"]["pass"] == 2
    assert s["by_outcome"]["incomplete"] == 1
    assert s["windows"]["24h"] == 3
    assert s["ocr"]["samples"] == 3
    assert s["ocr"]["avg_ms"] == (320 + 410 + 900) // 3
    engines = dict(s["ocr"]["engines"])
    assert engines["ocr.space"] == 2
    assert engines["tesseract"] == 1


def test_summary_when_empty(analytics_module):
    s = analytics_module.summary()
    assert s["available"] is True
    assert s["total"] == 0
    assert s["by_outcome"] == {}
    assert s["windows"]["24h"] == 0


def test_disabled_when_path_unwritable(tmp_path, monkeypatch):
    monkeypatch.setenv("ANALYTICS_DB_PATH", "/proc/forbidden/x.db")
    import analytics
    importlib.reload(analytics)
    # Should not raise.
    analytics.record_verification(outcome="pass")
    s = analytics.summary()
    assert s["available"] is False
    assert s["total"] == 0


def test_member_profile_roundtrip(analytics_module):
    a = analytics_module
    a.upsert_member_profile(
        guild_id=2, user_id=5,
        mastery_rank="MR 12", in_game_name="Tenno",
        platform="PC", clan="Golden Pagoda", last_verified_ts=1000,
    )
    p = a.get_member_profile(2, 5)
    assert p is not None
    assert p["mastery_rank"] == "MR 12"
    assert p["in_game_name"] == "Tenno"
    assert p["platform"] == "PC"
    assert p["clan"] == "Golden Pagoda"
    assert p["last_verified_ts"] == 1000
    assert p["updated_ts"] >= 0


def test_member_profile_partial_update_preserves_fields(analytics_module):
    a = analytics_module
    a.upsert_member_profile(
        guild_id=2, user_id=5,
        mastery_rank="MR 12", in_game_name="Tenno",
        platform="PC", clan="Golden Pagoda", last_verified_ts=1000,
    )
    # A mastery-only edit (e.g. the /profile dropdown) must not wipe the
    # rest of the snapshot.
    a.upsert_member_profile(guild_id=2, user_id=5, mastery_rank="LR 3")
    p = a.get_member_profile(2, 5)
    assert p["mastery_rank"] == "LR 3"
    assert p["in_game_name"] == "Tenno"
    assert p["platform"] == "PC"
    assert p["clan"] == "Golden Pagoda"
    assert p["last_verified_ts"] == 1000


def test_member_profile_missing_returns_none(analytics_module):
    assert analytics_module.get_member_profile(2, 999) is None


def test_member_profile_isolated_by_guild(analytics_module):
    a = analytics_module
    a.upsert_member_profile(guild_id=2, user_id=5, mastery_rank="MR 12")
    a.upsert_member_profile(guild_id=3, user_id=5, mastery_rank="MR 30")
    assert a.get_member_profile(2, 5)["mastery_rank"] == "MR 12"
    assert a.get_member_profile(3, 5)["mastery_rank"] == "MR 30"
