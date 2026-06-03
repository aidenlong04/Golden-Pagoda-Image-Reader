from __future__ import annotations

import importlib
import json

import pytest


@pytest.fixture()
def analytics_module(tmp_path, monkeypatch):
    db = tmp_path / "a.db"
    monkeypatch.setenv("ANALYTICS_DB_PATH", str(db))
    import analytics
    importlib.reload(analytics)
    return analytics


def test_record_and_summary_roundtrip(analytics_module):
    a = analytics_module
    a.record_verification(
        outcome="pass", platform="PC", clan="Argon",
        ocr_engine="ocr.space", ocr_latency_ms=320,
        user_id=1, guild_id=2,
    )
    a.record_verification(
        outcome="incomplete", platform="Xbox", clan=None,
        ocr_engine="ocr.space", ocr_latency_ms=410,
        user_id=3, guild_id=2,
    )
    a.record_verification(
        outcome="pass", platform="PC", clan="Argon",
        ocr_engine="tesseract", ocr_latency_ms=900,
        user_id=4, guild_id=2,
    )
    s = a.summary()
    assert s["available"] is True
    assert s["total"] == 3
    assert s["by_outcome"]["pass"] == 2
    assert s["by_outcome"]["incomplete"] == 1
    plats = dict(s["by_platform"])
    assert plats["PC"] == 2
    assert plats["Xbox"] == 1
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


def test_platform_scores_persisted(analytics_module):
    """Regression: record_verification used to NameError on json.dumps."""
    a = analytics_module
    scores = {"PC": 0.82, "Xbox": 0.11, "PlayStation": 0.04}
    a.record_verification(
        outcome="pass", platform="PC", clan="Argon",
        ocr_engine="ocr.space", ocr_latency_ms=200,
        user_id=10, guild_id=20,
        platform_scores=scores,
    )
    with a._connect() as conn:
        row = conn.execute(
            "SELECT platform_scores FROM events ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row is not None
    assert row[0] is not None, "platform_scores column should be populated"
    assert json.loads(row[0]) == scores
