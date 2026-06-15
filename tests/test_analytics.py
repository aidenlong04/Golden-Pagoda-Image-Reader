from __future__ import annotations

import importlib
import sqlite3

import pytest


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


def test_award_and_list_titles_roundtrip(analytics_module):
    a = analytics_module
    a.award_title(
        guild_id=2, user_id=5, title="boot licker",
        reason="Submitted 10 boots during fishing derby",
        event_name="Fishing Derby", awarded_ts=1000,
    )
    a.award_title(
        guild_id=2, user_id=5, title="Sharpshooter",
        reason="Won the shooting gallery", awarded_ts=2000,
    )
    titles = a.list_member_titles(2, 5)
    assert [t["title"] for t in titles] == ["Sharpshooter", "boot licker"]
    boot = next(t for t in titles if t["title"] == "boot licker")
    assert boot["reason"] == "Submitted 10 boots during fishing derby"
    assert boot["event_name"] == "Fishing Derby"
    assert boot["awarded_ts"] == 1000


def test_award_title_idempotent_refreshes(analytics_module):
    a = analytics_module
    a.award_title(
        guild_id=2, user_id=5, title="Boot Licker",
        reason="old reason", awarded_ts=1000,
    )
    # Re-awarding the same title (case-insensitive) must not duplicate;
    # it refreshes the reason + timestamp instead.
    a.award_title(
        guild_id=2, user_id=5, title="boot licker",
        reason="new reason", awarded_ts=3000,
    )
    titles = a.list_member_titles(2, 5)
    assert len(titles) == 1
    assert titles[0]["title"] == "boot licker"
    assert titles[0]["reason"] == "new reason"
    assert titles[0]["awarded_ts"] == 3000


def test_award_title_preserves_reason_when_omitted(analytics_module):
    a = analytics_module
    a.award_title(
        guild_id=2, user_id=5, title="boot licker",
        reason="keep me", event_name="Derby", awarded_ts=1000,
    )
    # A re-award with no reason/event keeps the previous values (COALESCE).
    a.award_title(guild_id=2, user_id=5, title="boot licker", awarded_ts=2000)
    t = a.list_member_titles(2, 5)[0]
    assert t["reason"] == "keep me"
    assert t["event_name"] == "Derby"
    assert t["awarded_ts"] == 2000


def test_award_title_blank_is_ignored(analytics_module):
    a = analytics_module
    a.award_title(guild_id=2, user_id=5, title="   ")
    assert a.list_member_titles(2, 5) == []


def test_revoke_title(analytics_module):
    a = analytics_module
    a.award_title(guild_id=2, user_id=5, title="boot licker")
    a.award_title(guild_id=2, user_id=5, title="Sharpshooter")
    # Case-insensitive match; returns True when a row is deleted.
    assert a.revoke_title(guild_id=2, user_id=5, title="BOOT LICKER") is True
    remaining = [t["title"] for t in a.list_member_titles(2, 5)]
    assert remaining == ["Sharpshooter"]
    # Revoking something that isn't there returns False.
    assert a.revoke_title(guild_id=2, user_id=5, title="nope") is False


def test_titles_isolated_by_member_and_guild(analytics_module):
    a = analytics_module
    a.award_title(guild_id=2, user_id=5, title="boot licker")
    a.award_title(guild_id=2, user_id=6, title="Sharpshooter")
    a.award_title(guild_id=3, user_id=5, title="Champion")
    assert [t["title"] for t in a.list_member_titles(2, 5)] == ["boot licker"]
    assert [t["title"] for t in a.list_member_titles(2, 6)] == ["Sharpshooter"]
    assert [t["title"] for t in a.list_member_titles(3, 5)] == ["Champion"]


def test_titles_missing_returns_empty(analytics_module):
    assert analytics_module.list_member_titles(2, 999) == []


def test_titles_fail_soft_when_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("ANALYTICS_DB_PATH", "/proc/forbidden/x.db")
    import analytics
    importlib.reload(analytics)
    # None of these should raise even though the store is disabled.
    analytics.award_title(guild_id=2, user_id=5, title="boot licker")
    assert analytics.list_member_titles(2, 5) == []
    assert analytics.revoke_title(guild_id=2, user_id=5, title="boot licker") is False


def test_delete_member_data_clears_profile_and_titles(analytics_module):
    a = analytics_module
    a.upsert_member_profile(
        guild_id=2, user_id=5, mastery_rank="MR 12",
        in_game_name="Tenno", platform="PC", clan="Golden Pagoda",
    )
    a.award_title(guild_id=2, user_id=5, title="boot licker")
    a.award_title(guild_id=2, user_id=5, title="Sharpshooter")
    a.record_verification(outcome="pass", user_id=5, guild_id=2)
    a.record_verification(outcome="fail", user_id=5, guild_id=2)

    result = a.delete_member_data(guild_id=2, user_id=5)
    assert result["profiles"] == 1
    assert result["titles"] == 2
    assert result["events_anonymized"] == 2

    # Profile + titles are gone; aggregate event count is preserved.
    assert a.get_member_profile(2, 5) is None
    assert a.list_member_titles(2, 5) == []
    assert a.summary()["total"] == 2


def test_delete_member_data_anonymizes_events_only(analytics_module):
    a = analytics_module
    a.record_verification(outcome="pass", clan="Argon", user_id=5, guild_id=2)
    a.delete_member_data(guild_id=2, user_id=5)
    # The row survives for stats, but the user_id is NULLed out.
    with a._connect() as conn:
        rows = conn.execute(
            "SELECT user_id, outcome FROM events WHERE guild_id=2"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["user_id"] is None
    assert rows[0]["outcome"] == "pass"


def test_events_user_guild_index_exists(analytics_module):
    a = analytics_module
    # Touch the store so the schema (and its indexes) is initialised.
    a.record_verification(outcome="pass", user_id=5, guild_id=2)
    with a._connect() as conn:
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
    # Backs the delete_member_data anonymise scan (guild_id, user_id).
    assert "idx_events_user_guild" in names


def test_delete_member_data_scoped_to_user_and_guild(analytics_module):
    a = analytics_module
    a.upsert_member_profile(guild_id=2, user_id=5, mastery_rank="MR 12")
    a.upsert_member_profile(guild_id=2, user_id=6, mastery_rank="MR 30")
    a.upsert_member_profile(guild_id=3, user_id=5, mastery_rank="LR 1")
    a.award_title(guild_id=2, user_id=6, title="keep me")

    a.delete_member_data(guild_id=2, user_id=5)

    # Only (guild 2, user 5) is cleared; the other rows are untouched.
    assert a.get_member_profile(2, 5) is None
    assert a.get_member_profile(2, 6)["mastery_rank"] == "MR 30"
    assert a.get_member_profile(3, 5)["mastery_rank"] == "LR 1"
    assert [t["title"] for t in a.list_member_titles(2, 6)] == ["keep me"]


def test_delete_member_data_missing_is_zero(analytics_module):
    result = analytics_module.delete_member_data(guild_id=2, user_id=999)
    assert result == {"profiles": 0, "titles": 0, "events_anonymized": 0, "onboarding": 0}


def test_delete_member_data_fail_soft_when_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("ANALYTICS_DB_PATH", "/proc/forbidden/x.db")
    import analytics
    importlib.reload(analytics)
    # Must not raise; returns the zeroed audit dict.
    result = analytics.delete_member_data(guild_id=2, user_id=5)
    assert result == {"profiles": 0, "titles": 0, "events_anonymized": 0, "onboarding": 0}


def test_delete_member_data_is_atomic_on_failure(analytics_module, monkeypatch):
    a = analytics_module
    a.upsert_member_profile(guild_id=2, user_id=5, mastery_rank="MR 12")
    a.award_title(guild_id=2, user_id=5, title="keep me")
    a.record_verification(outcome="pass", user_id=5, guild_id=2)

    real_conn = a._conn
    real_connect = a._connect

    class _FailingConn:
        # Proxy that forwards everything to the real connection but blows up
        # on the final statement of the purge to simulate a mid-purge crash.
        def execute(self, sql, *args):
            if sql.startswith("UPDATE events"):
                raise sqlite3.OperationalError("simulated mid-purge crash")
            return real_conn.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(real_conn, name)

    import contextlib

    @contextlib.contextmanager
    def _fake_connect():
        yield _FailingConn()

    monkeypatch.setattr(a, "_connect", _fake_connect)
    result = a.delete_member_data(guild_id=2, user_id=5)
    monkeypatch.undo()
    # `delete_member_data` caught the sqlite error and dropped the cached
    # connection; re-validate over a fresh real connection.

    # The whole purge must roll back: nothing reported, nothing deleted.
    assert result == {"profiles": 0, "titles": 0, "events_anonymized": 0, "onboarding": 0}
    assert a.get_member_profile(2, 5)["mastery_rank"] == "MR 12"
    assert [t["title"] for t in a.list_member_titles(2, 5)] == ["keep me"]
    with a._connect() as conn:
        row = conn.execute(
            "SELECT user_id FROM events WHERE guild_id=2"
        ).fetchone()
    assert row["user_id"] == 5  # telemetry NOT anonymised — rolled back


