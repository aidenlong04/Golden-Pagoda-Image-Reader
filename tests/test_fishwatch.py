"""Tests for fishwatch.py — the /watch fish-submission rules."""
from __future__ import annotations

import asyncio
import datetime
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image

import fishwatch
from fishwatch import (
    FISH,
    FISH_BY_KEY,
    PROBLEM_CODEWORD,
    PROBLEM_UNREADABLE,
    PROBLEM_WRONG_FISH,
    WatchState,
    build_watch_prompt,
    canonical_fish,
    evaluate_submission,
    find_fish,
    problem_messages,
)


# ---------------------------------------------------------------------------
# Fish reference data
# ---------------------------------------------------------------------------

def test_fish_roster_matches_spec():
    by_planet = {
        "Earth": {"Mortus Lungfish", "Mawfish", "Sharrac", "Norg"},
        "Venus": {"Scrubber", "Brickie", "Longwinder", "Tromyzon"},
        "Deimos": {"Amniophysi", "Cryptosuctus", "Duroid", "Aquapulmo"},
    }
    for planet, names in by_planet.items():
        assert {f.name for f in FISH if f.planet == planet} == names
    assert len(FISH) == 12


def test_fish_have_quality_and_rarity():
    for fish in FISH:
        assert fish.quality.endswith(("kg", "points"))
        assert fish.rarity in {"Common", "Uncommon", "Rare"}


def test_norg_weight_from_item_pull():
    assert FISH_BY_KEY["norg"].quality == "40 kg"
    assert FISH_BY_KEY["mawfish"].quality == "30 kg"
    assert FISH_BY_KEY["longwinder"].quality == "16 points"


# ---------------------------------------------------------------------------
# Fish name matching
# ---------------------------------------------------------------------------

def test_find_fish_case_insensitive():
    assert find_fish("next fish is norg") == "Norg"
    assert find_fish("MAWFISH please") == "Mawfish"


def test_find_fish_letter_spaced_title():
    # Warframe renders the caught-fish header letter-spaced.
    assert find_fish("N o r g\nLarge\n39.9 kg") == "Norg"


def test_find_fish_multi_word():
    assert find_fish("go catch a mortus lungfish now") == "Mortus Lungfish"


def test_find_fish_rejects_embedded():
    assert find_fish("snorgle") is None
    assert find_fish("") is None
    assert find_fish("no fish here") is None


def test_find_fish_returns_earliest_match():
    assert find_fish("Sharrac beats Norg") == "Sharrac"


def test_canonical_fish():
    assert canonical_fish("norg") == "Norg"
    assert canonical_fish(" TROMYZON ") == "Tromyzon"
    assert canonical_fish("boot") is None


# ---------------------------------------------------------------------------
# Submission evaluation
# ---------------------------------------------------------------------------

GOOD_TEXT = (
    "N o r g\nLarge\n39.9 kg\nThis fish inhabits the shallows\n"
    "[14:03] Donut-Prime: cerebral\nSEND MESSAGE TO SQUAD"
)


def test_evaluate_pass():
    v = evaluate_submission(GOOD_TEXT, codeword="cerebral", expected_fish="Norg")
    assert v.ok and not v.problems and v.fish == "Norg"


def test_evaluate_unreadable_empty():
    v = evaluate_submission("", codeword="cerebral", expected_fish="Norg")
    assert not v.ok and v.problems == (PROBLEM_UNREADABLE,)


def test_evaluate_unreadable_no_fish():
    v = evaluate_submission(
        "blurry nonsense text", codeword="cerebral", expected_fish="Norg"
    )
    assert not v.ok and v.problems == (PROBLEM_UNREADABLE,)


def test_evaluate_missing_codeword():
    text = "N o r g\nLarge\n39.9 kg"
    v = evaluate_submission(text, codeword="cerebral", expected_fish="Norg")
    assert not v.ok and v.problems == (PROBLEM_CODEWORD,)


def test_evaluate_wrong_codeword():
    text = "Norg\n[14:03] Donut-Prime: cerebrum"
    v = evaluate_submission(text, codeword="cerebral", expected_fish="Norg")
    assert not v.ok and PROBLEM_CODEWORD in v.problems


def test_evaluate_codeword_case_insensitive():
    text = "Norg\nchat: CEREBRAL"
    v = evaluate_submission(text, codeword="cerebral", expected_fish="Norg")
    assert v.ok


def test_evaluate_no_codeword_configured():
    v = evaluate_submission("Norg 39.9 kg", codeword="", expected_fish="Norg")
    assert v.ok


def test_evaluate_wrong_fish():
    text = "Mawfish\nMedium\nchat: cerebral"
    v = evaluate_submission(text, codeword="cerebral", expected_fish="Norg")
    assert not v.ok
    assert v.problems == (PROBLEM_WRONG_FISH,)
    assert v.fish == "Mawfish"


def test_evaluate_wrong_fish_and_missing_codeword():
    v = evaluate_submission("Mawfish", codeword="cerebral", expected_fish="Norg")
    assert set(v.problems) == {PROBLEM_CODEWORD, PROBLEM_WRONG_FISH}


def test_evaluate_no_expected_fish_accepts_any():
    v = evaluate_submission(
        "Tromyzon\nchat: cerebral", codeword="cerebral", expected_fish=None
    )
    assert v.ok and v.fish == "Tromyzon"


def test_problem_messages_cover_all_problems():
    v = evaluate_submission("Mawfish", codeword="cerebral", expected_fish="Norg")
    msgs = problem_messages(v, codeword_set=True, expected_fish="Norg")
    joined = " ".join(msgs)
    assert "codeword is required" in joined
    assert "Norg" in joined and "Mawfish" in joined

    v2 = evaluate_submission("", codeword="", expected_fish=None)
    msgs2 = problem_messages(v2, codeword_set=False, expected_fish=None)
    assert any("retry with a new" in m for m in msgs2)


# ---------------------------------------------------------------------------
# Watch state env round-trip
# ---------------------------------------------------------------------------

def test_watch_state_from_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(fishwatch.ENV_ENABLED, "1")
    monkeypatch.setenv(fishwatch.ENV_CHANNEL, "12345")
    monkeypatch.setenv(fishwatch.ENV_CODEWORD, "cerebral")
    monkeypatch.setenv(fishwatch.ENV_ADMIN_IDS, "1,2,3")
    monkeypatch.setenv(fishwatch.ENV_FISH, "norg")
    state = WatchState.from_env()
    assert state.enabled and state.channel_id == 12345
    assert state.codeword == "cerebral"
    assert state.admin_ids == {1, 2, 3}
    assert state.current_fish == "Norg"


def test_watch_state_from_env_defaults(monkeypatch: pytest.MonkeyPatch):
    for key in (
        fishwatch.ENV_ENABLED, fishwatch.ENV_CHANNEL, fishwatch.ENV_CODEWORD,
        fishwatch.ENV_ADMIN_IDS, fishwatch.ENV_FISH,
    ):
        monkeypatch.delenv(key, raising=False)
    state = WatchState.from_env()
    assert not state.enabled
    assert state.channel_id == 0
    assert state.codeword == ""
    assert state.admin_ids == set()
    assert state.current_fish is None


def test_watch_state_env_items_round_trip():
    state = WatchState(
        enabled=True, channel_id=99, codeword="w",
        admin_ids={5, 2}, current_fish="Norg",
    )
    items = dict(state.env_items())
    assert items[fishwatch.ENV_ENABLED] == "1"
    assert items[fishwatch.ENV_CHANNEL] == "99"
    assert items[fishwatch.ENV_CODEWORD] == "w"
    assert items[fishwatch.ENV_ADMIN_IDS] == "2,5"
    assert items[fishwatch.ENV_FISH] == "Norg"


# ---------------------------------------------------------------------------
# Vision prompt
# ---------------------------------------------------------------------------

def test_watch_prompt_lists_every_fish():
    prompt = build_watch_prompt()
    for fish in FISH:
        assert fish.name in prompt
    assert "green" in prompt
    assert "chat" in prompt


# ---------------------------------------------------------------------------
# bot.py watcher integration (stubbed Discord I/O)
# ---------------------------------------------------------------------------

# A 1x1 PNG so validate_image_bytes passes.
def _png_1px() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (1, 1)).save(buf, format="PNG")
    return buf.getvalue()


_PNG_1PX = _png_1px()


def _fake_message(*, author_id=7, channel_id=555, message_id=42, guild_id=1):
    return SimpleNamespace(
        id=message_id,
        author=SimpleNamespace(id=author_id, bot=False),
        channel=SimpleNamespace(id=channel_id),
        guild=SimpleNamespace(id=guild_id),
        created_at=datetime.datetime(
            2026, 7, 13, 12, 0, 0, tzinfo=datetime.timezone.utc
        ),
        add_reaction=AsyncMock(),
    )


def _fake_attachment(data: bytes):
    return SimpleNamespace(
        filename="fish.png",
        content_type="image/png",
        read=AsyncMock(return_value=data),
    )


def test_process_watch_submission_fail_then_pass(monkeypatch):
    import bot

    posted: list[tuple[int, list]] = []
    deleted: list[int] = []

    async def fake_post(channel_id, components, **kw):
        posted.append((channel_id, components))
        return 9000 + len(posted)

    async def fake_delete(channel_id, message_id):
        deleted.append(message_id)

    ocr_text = {"value": "blurry nonsense"}

    async def fake_run_heavy(func, *args, **kwargs):
        return ocr_text["value"], [], "test"

    monkeypatch.setattr(bot, "_post_channel_v2", fake_post)
    monkeypatch.setattr(bot, "_delete_message", fake_delete)
    monkeypatch.setattr(bot, "_run_heavy", fake_run_heavy)
    monkeypatch.setattr(bot, "_watch_error_replies", {})
    monkeypatch.setattr(bot._WATCH_STATE, "enabled", True)
    recorded: list[dict] = []
    monkeypatch.setattr(
        bot.analytics, "record_fish_catch",
        lambda **kw: recorded.append(kw),
    )

    msg = _fake_message()
    att = _fake_attachment(_PNG_1PX)

    # 1) Unreadable submission -> ❌ react + error reply tracked.
    asyncio.run(bot._process_watch_submission(
        msg, att, codeword="cerebral", expected_fish="Norg",
    ))
    assert len(posted) == 1
    assert bot._watch_error_replies[7] == [9001]
    msg.add_reaction.assert_awaited_with("\u274C")
    body = posted[0][1][0]["components"][0]["content"]
    assert "retry" in body

    # 2) Wrong fish + codeword present.
    ocr_text["value"] = "Mawfish\nLarge\nchat: cerebral"
    msg2 = _fake_message(message_id=43)
    asyncio.run(bot._process_watch_submission(
        msg2, att, codeword="cerebral", expected_fish="Norg",
    ))
    # The previous error reply was deleted, a new one tracked.
    assert deleted == [9001]
    assert bot._watch_error_replies[7] == [9002]
    body2 = posted[1][1][0]["components"][0]["content"]
    assert "Mawfish" in body2 and "Norg" in body2

    # 3) Corrected image passes: old error reply deleted, ✅ react, none tracked.
    ocr_text["value"] = "N o r g\nLarge\n39.9 kg\n[14:03] Donut-Prime: cerebral"
    msg3 = _fake_message(message_id=44)
    asyncio.run(bot._process_watch_submission(
        msg3, att, codeword="cerebral", expected_fish="Norg",
    ))
    assert deleted == [9001, 9002]
    assert 7 not in bot._watch_error_replies
    msg3.add_reaction.assert_awaited_with("\u2705")
    assert len(posted) == 2  # no new error reply
    # The measured weight was recorded for the leaderboard.
    assert len(recorded) == 1
    assert recorded[0]["fish"] == "Norg"
    assert recorded[0]["weight"] == pytest.approx(39.9)
    assert recorded[0]["unit"] == "kg"
    assert recorded[0]["message_id"] == 44
    # Ties break on the message's post time, not OCR completion time.
    assert recorded[0]["caught_ts"] == int(msg3.created_at.timestamp())


def test_watch_components_and_state(monkeypatch):
    import bot

    monkeypatch.setattr(
        bot, "_WATCH_STATE",
        fishwatch.WatchState(
            enabled=True, channel_id=555, codeword="cerebral",
            admin_ids={1}, current_fish="Norg",
        ),
    )
    comps = bot._watch_components()
    assert "Fish Watch" in comps[0]["content"]
    rows = comps[1]["components"]
    ids = [b["custom_id"] for b in rows[1]["components"]]
    assert ids == [
        "watch:start", "watch:stop", "watch:codeword", "watch:admins",
    ]
    nav_ids = [b["custom_id"] for b in rows[2]["components"]]
    assert nav_ids == [
        "watch:page:-1", "watch:noop", "watch:page:1", "watch:page:0",
    ]
    body = comps[1]["components"][0]["content"]
    assert "cerebral" in body and "Norg" in body and "<#555>" in body


# ---------------------------------------------------------------------------
# Weight extraction + Records leaderboard page
# ---------------------------------------------------------------------------

def test_extract_weight_kg():
    text = "N o r g\nLarge\n24.35 kg\nchat: cerebral"
    assert fishwatch.extract_weight(text, "Norg") == pytest.approx(24.35)


def test_extract_weight_points_for_servofish():
    text = "Longwinder\nMedium\n14 points\nchat: cerebral"
    assert fishwatch.extract_weight(text, "Longwinder") == pytest.approx(14)
    # A kg figure does not satisfy a points-measured species.
    assert fishwatch.extract_weight("Longwinder 14 kg", "Longwinder") is None


def test_extract_weight_rejects_junk():
    assert fishwatch.extract_weight("", "Norg") is None
    assert fishwatch.extract_weight("Norg Large", "Norg") is None
    assert fishwatch.extract_weight("Norg 0 kg", "Norg") is None
    assert fishwatch.extract_weight("Norg 99999 kg", "Norg") is None


def test_fish_unit():
    assert fishwatch.fish_unit("Norg") == "kg"
    assert fishwatch.fish_unit("Scrubber") == "points"


def test_evaluate_submission_carries_weight():
    v = evaluate_submission(
        "Norg\nLarge\n39.9 kg\nchat: cerebral",
        codeword="cerebral", expected_fish="Norg",
    )
    assert v.ok and v.weight == pytest.approx(39.9) and v.unit == "kg"
    v2 = evaluate_submission(
        "Norg\nLarge\nchat: cerebral",
        codeword="cerebral", expected_fish="Norg",
    )
    assert v2.ok and v2.weight is None and v2.unit is None


def test_watch_records_page_empty(monkeypatch):
    import bot

    comps = bot._watch_components(1, guild_id=1, top={})
    assert "Records" in comps[0]["content"]
    body = comps[1]["components"][0]["content"]
    assert "No catches recorded yet" in body
    nav_ids = [b["custom_id"] for b in comps[1]["components"][-1]["components"]]
    assert nav_ids == [
        "watch:page:0", "watch:noop", "watch:page:2", "watch:page:1",
    ]


def test_watch_records_page_lists_top_catches():
    import bot

    top = {
        "norg": [
            {"fish_name": "Norg", "weight": 39.9, "unit": "kg",
             "user_id": 7, "channel_id": 555, "message_id": 44,
             "caught_ts": 1},
            {"fish_name": "Norg", "weight": 30.0, "unit": "kg",
             "user_id": None, "channel_id": 555, "message_id": 45,
             "caught_ts": 2},
        ],
        "longwinder": [
            {"fish_name": "Longwinder", "weight": 14.0, "unit": "points",
             "user_id": 8, "channel_id": 555, "message_id": 46,
             "caught_ts": 3},
        ],
    }
    comps = bot._watch_components(1, guild_id=99, top=top)
    body = comps[1]["components"][0]["content"]
    assert "Norg" in body and "39.9 kg" in body and "30 kg" in body
    assert "<@7>" in body and "former member" in body
    assert "14 points" in body
    assert "https://discord.com/channels/99/555/44" in body
    # Fish with no recorded catches are omitted.
    assert "Mawfish" not in body
