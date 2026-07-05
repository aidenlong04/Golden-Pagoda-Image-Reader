"""Tests for gpbot.onboarding — pure onboarding decision helpers."""
from __future__ import annotations

from gpbot.onboarding import (
    RepromptDecision,
    parse_onboard_custom_id,
    reprompt_decision,
)


class TestParseOnboardCustomID:
    def test_clanselect_with_values(self):
        parsed = parse_onboard_custom_id(
            "onboard:123:clanselect", select_values=["4"]
        )
        assert parsed == (123, "clanselect", "4")

    def test_clanselect_without_values(self):
        parsed = parse_onboard_custom_id("onboard:123:clanselect")
        assert parsed == (123, "clanselect", None)

    def test_legacy_clan_button(self):
        parsed = parse_onboard_custom_id("onboard:42:clan:2")
        assert parsed == (42, "clan", "2")

    def test_none_action(self):
        parsed = parse_onboard_custom_id("onboard:42:none")
        assert parsed == (42, "none", "none")

    def test_invalid_user_id(self):
        assert parse_onboard_custom_id("onboard:abc:none") is None
        assert parse_onboard_custom_id("onboard") is None

    def test_missing_action(self):
        parsed = parse_onboard_custom_id("onboard:7")
        assert parsed == (7, "", None)


class TestRepromptDecision:
    def _decide(self, **kw):
        base = dict(
            posted_ts=0.0,
            reprompt_count=0,
            now=100_000.0,
            window_secs=3600.0,
            max_reprompts=3,
            member_present=True,
        )
        base.update(kw)
        return reprompt_decision(**base)

    def test_wait_inside_window(self):
        assert self._decide(now=100.0) is RepromptDecision.WAIT

    def test_cleanup_when_member_left(self):
        assert self._decide(member_present=False) is RepromptDecision.CLEANUP

    def test_stop_at_max_reprompts(self):
        assert self._decide(reprompt_count=3) is RepromptDecision.STOP

    def test_repost_when_overdue(self):
        assert self._decide() is RepromptDecision.REPOST

    def test_member_left_inside_window_waits(self):
        # Cleanup only happens once the window has elapsed (matches the
        # pre-refactor sweep behavior).
        assert self._decide(
            now=100.0, member_present=False
        ) is RepromptDecision.WAIT
