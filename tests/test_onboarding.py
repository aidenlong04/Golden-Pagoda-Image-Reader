"""Tests for the member onboarding flow.

Covers:
- analytics.py: onboarding_prompts CRUD + integration with delete_member_data
- bot.py: _onboarding_welcome_components structure + interaction ownership gating
- reprompt sweep: elapsed window detection and max-reprompt cap
"""
from __future__ import annotations

import asyncio
import importlib
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# analytics_module fixture is defined in conftest.py (shared with test_analytics.py)


# ---------------------------------------------------------------------------
# analytics.py — onboarding_prompts CRUD
# ---------------------------------------------------------------------------

def test_upsert_and_get_onboarding_prompt(analytics_module):
    a = analytics_module
    now = int(time.time())
    a.upsert_onboarding_prompt(
        guild_id=1, user_id=100, channel_id=999, message_id=42, posted_ts=now
    )
    row = a.get_onboarding_prompt(1, 100)
    assert row is not None
    assert row["guild_id"] == 1
    assert row["user_id"] == 100
    assert row["channel_id"] == 999
    assert row["message_id"] == 42
    assert row["posted_ts"] == now
    assert row["completed"] == 0
    assert row["reprompt_count"] == 0
    assert row["ocr_fail_count"] == 0


def test_get_onboarding_prompt_missing_returns_none(analytics_module):
    assert analytics_module.get_onboarding_prompt(1, 999) is None


def test_upsert_onboarding_prompt_increments_reprompt_count(analytics_module):
    a = analytics_module
    now = int(time.time())
    a.upsert_onboarding_prompt(
        guild_id=1, user_id=100, channel_id=999, message_id=1, posted_ts=now
    )
    a.upsert_onboarding_prompt(
        guild_id=1, user_id=100, channel_id=999, message_id=2, posted_ts=now + 100
    )
    row = a.get_onboarding_prompt(1, 100)
    assert row["reprompt_count"] == 1
    assert row["message_id"] == 2
    assert row["posted_ts"] == now + 100


def test_upsert_onboarding_prompt_resets_ocr_fail_count(analytics_module):
    a = analytics_module
    now = int(time.time())
    a.upsert_onboarding_prompt(
        guild_id=1, user_id=100, channel_id=999, message_id=1, posted_ts=now
    )
    a.increment_onboarding_ocr_fail(guild_id=1, user_id=100)
    a.increment_onboarding_ocr_fail(guild_id=1, user_id=100)
    row = a.get_onboarding_prompt(1, 100)
    assert row["ocr_fail_count"] == 2
    # Re-prompt resets fail count
    a.upsert_onboarding_prompt(
        guild_id=1, user_id=100, channel_id=999, message_id=2, posted_ts=now + 100
    )
    row = a.get_onboarding_prompt(1, 100)
    assert row["ocr_fail_count"] == 0


def test_complete_onboarding_prompt(analytics_module):
    a = analytics_module
    a.upsert_onboarding_prompt(
        guild_id=1, user_id=100, channel_id=999, message_id=1, posted_ts=1000
    )
    a.complete_onboarding_prompt(guild_id=1, user_id=100)
    row = a.get_onboarding_prompt(1, 100)
    assert row["completed"] == 1


def test_delete_onboarding_prompt(analytics_module):
    a = analytics_module
    a.upsert_onboarding_prompt(
        guild_id=1, user_id=100, channel_id=999, message_id=1, posted_ts=1000
    )
    a.delete_onboarding_prompt(guild_id=1, user_id=100)
    assert a.get_onboarding_prompt(1, 100) is None


def test_list_pending_onboarding_prompts(analytics_module):
    a = analytics_module
    now = int(time.time())
    a.upsert_onboarding_prompt(
        guild_id=1, user_id=100, channel_id=999, message_id=1, posted_ts=now
    )
    a.upsert_onboarding_prompt(
        guild_id=1, user_id=200, channel_id=999, message_id=2, posted_ts=now
    )
    a.upsert_onboarding_prompt(
        guild_id=1, user_id=300, channel_id=999, message_id=3, posted_ts=now
    )
    a.complete_onboarding_prompt(guild_id=1, user_id=300)

    pending = a.list_pending_onboarding_prompts()
    user_ids = {row["user_id"] for row in pending}
    assert 100 in user_ids
    assert 200 in user_ids
    assert 300 not in user_ids


def test_list_pending_returns_empty_when_all_complete(analytics_module):
    a = analytics_module
    a.upsert_onboarding_prompt(
        guild_id=1, user_id=100, channel_id=999, message_id=1, posted_ts=1000
    )
    a.complete_onboarding_prompt(guild_id=1, user_id=100)
    assert a.list_pending_onboarding_prompts() == []


def test_increment_onboarding_ocr_fail(analytics_module):
    a = analytics_module
    a.upsert_onboarding_prompt(
        guild_id=1, user_id=100, channel_id=999, message_id=1, posted_ts=1000
    )
    new_count = a.increment_onboarding_ocr_fail(guild_id=1, user_id=100)
    assert new_count == 1
    new_count = a.increment_onboarding_ocr_fail(guild_id=1, user_id=100)
    assert new_count == 2
    row = a.get_onboarding_prompt(1, 100)
    assert row["ocr_fail_count"] == 2


def test_increment_ocr_fail_returns_zero_when_missing(analytics_module):
    # If there's no row (or store is disabled), returns 0 safely.
    result = analytics_module.increment_onboarding_ocr_fail(guild_id=1, user_id=999)
    assert result == 0


def test_onboarding_prompts_isolated_by_guild(analytics_module):
    a = analytics_module
    a.upsert_onboarding_prompt(
        guild_id=1, user_id=100, channel_id=1, message_id=1, posted_ts=1000
    )
    a.upsert_onboarding_prompt(
        guild_id=2, user_id=100, channel_id=2, message_id=2, posted_ts=2000
    )
    assert a.get_onboarding_prompt(1, 100)["channel_id"] == 1
    assert a.get_onboarding_prompt(2, 100)["channel_id"] == 2


# ---------------------------------------------------------------------------
# delete_member_data also clears onboarding_prompts
# ---------------------------------------------------------------------------

def test_delete_member_data_clears_onboarding_prompt(analytics_module):
    a = analytics_module
    a.upsert_onboarding_prompt(
        guild_id=1, user_id=100, channel_id=999, message_id=1, posted_ts=1000
    )
    result = a.delete_member_data(guild_id=1, user_id=100)
    assert result.get("onboarding", 0) == 1
    assert a.get_onboarding_prompt(1, 100) is None


def test_delete_member_data_onboarding_count_zero_when_missing(analytics_module):
    result = analytics_module.delete_member_data(guild_id=1, user_id=999)
    # Either key absent or zero — both are acceptable.
    assert result.get("onboarding", 0) == 0


def test_delete_member_data_does_not_affect_other_members_onboarding(analytics_module):
    a = analytics_module
    a.upsert_onboarding_prompt(
        guild_id=1, user_id=100, channel_id=999, message_id=1, posted_ts=1000
    )
    a.upsert_onboarding_prompt(
        guild_id=1, user_id=200, channel_id=999, message_id=2, posted_ts=2000
    )
    a.delete_member_data(guild_id=1, user_id=100)
    assert a.get_onboarding_prompt(1, 100) is None
    assert a.get_onboarding_prompt(1, 200) is not None


def test_onboarding_fail_soft_when_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("ANALYTICS_DB_PATH", "/proc/forbidden/x.db")
    import analytics
    importlib.reload(analytics)
    # None of these should raise.
    analytics.upsert_onboarding_prompt(
        guild_id=1, user_id=100, channel_id=999, message_id=1, posted_ts=1000
    )
    assert analytics.get_onboarding_prompt(1, 100) is None
    assert analytics.list_pending_onboarding_prompts() == []
    analytics.complete_onboarding_prompt(guild_id=1, user_id=100)
    analytics.delete_onboarding_prompt(guild_id=1, user_id=100)
    assert analytics.increment_onboarding_ocr_fail(guild_id=1, user_id=100) == 0


# ---------------------------------------------------------------------------
# bot.py — _onboarding_welcome_components structure
# ---------------------------------------------------------------------------

def _walk_components(components):
    """Yield every component dict in a (possibly nested) V2 component tree."""
    for comp in components or []:
        if not isinstance(comp, dict):
            continue
        yield comp
        yield from _walk_components(comp.get("components"))


def _collect_custom_ids(components):
    return [
        comp["custom_id"]
        for comp in _walk_components(components)
        if comp.get("custom_id")
    ]


def _collect_option_values(components):
    values: list[str] = []
    for comp in _walk_components(components):
        for opt in comp.get("options", []) or []:
            values.append(opt.get("value", ""))
    return values


class OnboardingComponentsTests(unittest.TestCase):
    """Smoke tests for the onboarding welcome component builder."""

    def setUp(self):
        import bot as bot_module
        self.bot = bot_module

    def test_welcome_components_returns_list(self):
        result = self.bot._onboarding_welcome_components(12345)
        self.assertIsInstance(result, list)
        self.assertGreater(len(result), 0)

    def test_welcome_components_is_flat_text_plus_select(self):
        result = self.bot._onboarding_welcome_components(12345)
        # Per the onboarding JSON: a top-level text component (type 10) and a
        # top-level action row (type 1) holding a string select (type 3); no
        # type:17 container wrapper.
        top_types = [c.get("type") for c in result]
        self.assertIn(10, top_types, "expected a top-level type:10 text component")
        self.assertIn(1, top_types, "expected a top-level type:1 action row")
        self.assertNotIn(17, top_types, "welcome should not be wrapped in a type:17 container")
        select_row = next(c for c in result if c.get("type") == 1)
        self.assertTrue(
            any(comp.get("type") == 3 for comp in select_row.get("components", [])),
            "expected a type:3 string select in the action row",
        )

    def test_welcome_components_action_rows_respect_5_button_cap(self):
        result = self.bot._onboarding_welcome_components(12345)
        for top in result:
            for inner in top.get("components", []) or []:
                if inner.get("type") == 1:
                    btn_count = len(inner.get("components") or [])
                    self.assertLessEqual(
                        btn_count, 5,
                        f"action row has {btn_count} buttons — exceeds Discord's 5-button cap",
                    )

    def test_welcome_components_custom_ids_encode_member_id(self):
        member_id = 99887766
        result = self.bot._onboarding_welcome_components(member_id)
        custom_ids = _collect_custom_ids(result)
        self.assertTrue(
            any(str(member_id) in cid for cid in custom_ids),
            "no custom_id encodes the member_id",
        )

    def test_welcome_components_has_none_option(self):
        member_id = 42
        result = self.bot._onboarding_welcome_components(member_id)
        option_values = _collect_option_values(result)
        self.assertIn(
            "none", option_values, "no 'Not Affiliated' option found"
        )

    def test_welcome_components_clan_options_use_onboard_prefix(self):
        result = self.bot._onboarding_welcome_components(12345)
        custom_ids = _collect_custom_ids(result)
        self.assertTrue(custom_ids, "expected at least one custom_id in the welcome")
        for cid in custom_ids:
            self.assertTrue(
                cid.startswith("onboard:"),
                f"custom_id '{cid}' doesn't start with 'onboard:'",
            )


class OnboardingPassWelcomeTests(unittest.TestCase):
    """The public verified-welcome card (pass + manual-review variants)."""

    def setUp(self):
        import bot as bot_module
        self.bot = bot_module

    def _buttons(self, components):
        out = []
        for top in components:
            for inner in top.get("components", []) or []:
                if inner.get("type") == 1:
                    out.extend(inner.get("components") or [])
        return out

    def test_pass_welcome_has_no_verify_button(self):
        comps = self.bot._onboarding_pass_welcome_components(12345)
        labels = [b.get("label") for b in self._buttons(comps)]
        self.assertNotIn("Verify", labels)

    def test_manual_review_verify_button_label_and_emoji(self):
        member_id = 778899
        comps = self.bot._onboarding_pass_welcome_components(
            member_id, manual_review=True
        )
        verify = next(
            (b for b in self._buttons(comps)
             if b.get("custom_id") == f"mreview:{member_id}:approve"),
            None,
        )
        self.assertIsNotNone(
            verify, "manual-review card missing the approve button"
        )
        self.assertEqual(verify["label"], "Verify")
        self.assertEqual(verify["emoji"]["name"], "Processing")
        self.assertEqual(verify["emoji"]["id"], "1459403163432910972")
        self.assertTrue(verify["emoji"]["animated"])

    def _selects(self, components):
        out = []
        for top in components:
            for inner in top.get("components", []) or []:
                if inner.get("type") == 1:
                    out.extend(
                        c for c in inner.get("components") or []
                        if c.get("type") == 3
                    )
        return out

    def test_pass_welcome_has_alias_select_not_self_roles_button(self):
        member_id = 12345
        comps = self.bot._onboarding_pass_welcome_components(member_id)
        labels = [b.get("label") for b in self._buttons(comps)]
        self.assertNotIn("Self Roles", labels)
        selects = self._selects(comps)
        self.assertEqual(len(selects), 1)
        select = selects[0]
        self.assertEqual(select["custom_id"], f"alias:{member_id}:std")
        opts = {o["value"]: o for o in select["options"]}
        self.assertEqual(set(opts), {"ign", "nick"})
        self.assertEqual(opts["ign"]["label"], "IGN")
        self.assertEqual(
            opts["ign"]["description"], "Continue as your Tenno Alias"
        )
        self.assertEqual(opts["nick"]["label"], "Server name")
        self.assertEqual(opts["nick"]["description"], "Pick a new Alias")

    def test_manual_review_welcome_alias_select_variant(self):
        member_id = 778899
        comps = self.bot._onboarding_pass_welcome_components(
            member_id, manual_review=True
        )
        selects = self._selects(comps)
        self.assertEqual(len(selects), 1)
        self.assertEqual(
            selects[0]["custom_id"], f"alias:{member_id}:mr"
        )


# ---------------------------------------------------------------------------
# /onboard admin command — triggers the onboarding pipeline on demand
# ---------------------------------------------------------------------------

class OnboardCommandTests(unittest.TestCase):
    """The admin /onboard slash command + its /manage Overview mirror."""

    def setUp(self):
        import bot as bot_module
        self.bot = bot_module

    def test_onboard_command_registered(self):
        names = {c.name for c in self.bot.tree.get_commands()}
        self.assertIn("onboard", names)

    def test_onboard_command_requires_manage_guild(self):
        cmd = next(
            c for c in self.bot.tree.get_commands() if c.name == "onboard"
        )
        perms = cmd.default_permissions
        self.assertIsNotNone(perms)
        self.assertTrue(perms.manage_guild)


# ---------------------------------------------------------------------------
# Interaction ownership gating — unit test via mock interaction
# ---------------------------------------------------------------------------

class OnboardingOwnershipGatingTests(unittest.IsolatedAsyncioTestCase):
    """Verify that _handle_onboarding_interaction rejects non-owner clicks."""

    async def asyncSetUp(self):
        import bot as bot_module
        self.bot = bot_module

    def _make_interaction(self, clicker_id: int, *, message_id: int = 1) -> Mock:
        interaction = MagicMock()
        interaction.user = MagicMock()
        interaction.user.id = clicker_id
        interaction.guild = MagicMock()
        interaction.guild.id = 7
        interaction.guild.get_member = MagicMock(return_value=None)
        interaction.message = MagicMock()
        interaction.message.id = message_id
        interaction.channel_id = 999
        interaction.response = AsyncMock()
        interaction.followup = AsyncMock()
        return interaction

    async def test_wrong_user_gets_ephemeral_rejection(self):
        """A click from a different user must be rejected ephemerally."""
        target_uid = 1111
        clicker_uid = 2222
        custom_id = f"onboard:{target_uid}:none"

        interaction = self._make_interaction(clicker_uid)

        # Patch _interaction_callback to capture calls rather than making HTTP requests.
        with patch.object(self.bot, "_interaction_callback", new_callable=AsyncMock) as mock_cb:
            await self.bot._handle_onboarding_interaction(interaction, custom_id)
            # Must have called _interaction_callback (the rejection path).
            mock_cb.assert_awaited()

    async def test_owner_none_button_calls_manual_review(self):
        """The owner clicking 'not listed' must open the screenshot modal."""
        import bot as bot_module

        target_uid = 1111
        custom_id = f"onboard:{target_uid}:none"

        member_mock = MagicMock()
        member_mock.id = target_uid
        member_mock.mention = f"<@{target_uid}>"
        interaction = self._make_interaction(target_uid)
        interaction.guild.get_member = MagicMock(return_value=member_mock)

        send_modal_mock = AsyncMock()
        interaction.response.send_modal = send_modal_mock

        with patch.object(
            bot_module, "_reset_onboarding_select", new_callable=AsyncMock
        ):
            await bot_module._handle_onboarding_interaction(interaction, custom_id)

        send_modal_mock.assert_awaited_once()
        args = send_modal_mock.call_args[0]
        self.assertIsInstance(args[0], bot_module._OnboardingNoClanModal)

    async def test_owner_clan_button_opens_modal(self):
        """The owner clicking a clan button must open the screenshot modal."""
        import bot as bot_module
        from logic import ClanSlot

        target_uid = 1111
        slot_no = 1
        custom_id = f"onboard:{target_uid}:clan:{slot_no}"

        member_mock = MagicMock()
        member_mock.id = target_uid
        interaction = self._make_interaction(target_uid)
        interaction.guild.get_member = MagicMock(return_value=member_mock)

        fake_slot = ClanSlot(
            slot=slot_no, clan_name="Golden Pagoda", role_id=100, emoji="<:gp:1>"
        )
        original_slots = bot_module.CLAN_SLOTS
        bot_module.CLAN_SLOTS = [fake_slot]

        send_modal_mock = AsyncMock()
        interaction.response.send_modal = send_modal_mock

        try:
            await bot_module._handle_onboarding_interaction(interaction, custom_id)
        finally:
            bot_module.CLAN_SLOTS = original_slots

        send_modal_mock.assert_awaited_once()
        args = send_modal_mock.call_args[0]
        self.assertIsInstance(args[0], bot_module._OnboardingVerifyModal)

    async def test_owner_clan_dropdown_opens_modal(self):
        """Selecting a clan from the dropdown must open the screenshot modal."""
        import bot as bot_module
        from logic import ClanSlot

        target_uid = 1111
        slot_no = 1
        custom_id = f"onboard:{target_uid}:clanselect"

        member_mock = MagicMock()
        member_mock.id = target_uid
        interaction = self._make_interaction(target_uid)
        interaction.guild.get_member = MagicMock(return_value=member_mock)
        interaction.data = {"values": [str(slot_no)]}

        fake_slot = ClanSlot(
            slot=slot_no, clan_name="Golden Pagoda", role_id=100, emoji="<:gp:1>"
        )
        original_slots = bot_module.CLAN_SLOTS
        bot_module.CLAN_SLOTS = [fake_slot]

        send_modal_mock = AsyncMock()
        interaction.response.send_modal = send_modal_mock

        try:
            await bot_module._handle_onboarding_interaction(interaction, custom_id)
        finally:
            bot_module.CLAN_SLOTS = original_slots

        send_modal_mock.assert_awaited_once()
        args = send_modal_mock.call_args[0]
        self.assertIsInstance(args[0], bot_module._OnboardingVerifyModal)

    async def test_owner_dropdown_none_calls_manual_review(self):
        """Selecting 'Not listed / No' from the dropdown opens the screenshot modal."""
        import bot as bot_module

        target_uid = 1111
        custom_id = f"onboard:{target_uid}:clanselect"

        member_mock = MagicMock()
        member_mock.id = target_uid
        member_mock.mention = f"<@{target_uid}>"
        interaction = self._make_interaction(target_uid)
        interaction.guild.get_member = MagicMock(return_value=member_mock)
        interaction.data = {"values": ["none"]}

        send_modal_mock = AsyncMock()
        interaction.response.send_modal = send_modal_mock

        with patch.object(
            bot_module, "_reset_onboarding_select", new_callable=AsyncMock
        ):
            await bot_module._handle_onboarding_interaction(interaction, custom_id)

        send_modal_mock.assert_awaited_once()
        args = send_modal_mock.call_args[0]
        self.assertIsInstance(args[0], bot_module._OnboardingNoClanModal)
# ---------------------------------------------------------------------------

class RepromptSweepTests(unittest.IsolatedAsyncioTestCase):
    """Tests for _onboarding_reprompt_sweep logic."""

    async def asyncSetUp(self):
        import bot as bot_module
        self.bot = bot_module

    async def test_no_reprompt_within_window(self):
        """Prompts posted recently (well within the window) must not be re-posted."""
        now = time.time()
        fresh_row = {
            "guild_id": 1,
            "user_id": 100,
            "channel_id": 999,
            "message_id": 1,
            "posted_ts": int(now),  # just posted
            "reprompt_count": 0,
        }

        with (
            patch.object(
                self.bot.analytics,
                "list_pending_onboarding_prompts",
                return_value=[fresh_row],
            ),
            patch.object(self.bot, "_post_onboarding_welcome", new_callable=AsyncMock) as mock_post,
            patch.object(self.bot, "client") as mock_client,
        ):
            guild_mock = MagicMock()
            member_mock = MagicMock()
            member_mock.id = 100
            guild_mock.get_member = MagicMock(return_value=member_mock)
            mock_client.get_guild = MagicMock(return_value=guild_mock)
            # Force a very small reprompt window so the "fresh" row is still inside it.
            original = self.bot.ONBOARDING_REPROMPT_HOURS
            self.bot.ONBOARDING_REPROMPT_HOURS = 5.0
            try:
                await self.bot._onboarding_reprompt_sweep()
            finally:
                self.bot.ONBOARDING_REPROMPT_HOURS = original

            mock_post.assert_not_awaited()

    async def test_reprompt_fires_after_window_elapsed(self):
        """A prompt whose window has elapsed must trigger a re-post and delete."""
        now = time.time()
        old_ts = int(now - 6 * 3600)  # 6 hours ago (window = 5h)
        old_row = {
            "guild_id": 1,
            "user_id": 200,
            "channel_id": 999,
            "message_id": 77,
            "posted_ts": old_ts,
            "reprompt_count": 0,
        }

        deleted_messages: list[int] = []

        async def fake_delete_msg(channel_id: int, message_id: int) -> None:
            deleted_messages.append(message_id)

        with (
            patch.object(
                self.bot.analytics,
                "list_pending_onboarding_prompts",
                return_value=[old_row],
            ),
            patch.object(
                self.bot.analytics,
                "complete_onboarding_prompt",
            ),
            patch.object(
                self.bot.analytics,
                "upsert_onboarding_prompt",
            ),
            patch.object(
                self.bot,
                "_delete_message",
                new=AsyncMock(side_effect=fake_delete_msg),
            ),
            patch.object(
                self.bot,
                "_post_channel_v2",
                new=AsyncMock(return_value=555),
            ) as mock_post,
            patch.object(self.bot, "client") as mock_client,
        ):
            guild_mock = MagicMock()
            member_mock = MagicMock()
            member_mock.id = 200
            guild_mock.get_member = MagicMock(return_value=member_mock)
            mock_client.get_guild = MagicMock(return_value=guild_mock)

            original = self.bot.ONBOARDING_REPROMPT_HOURS
            self.bot.ONBOARDING_REPROMPT_HOURS = 5.0
            try:
                await self.bot._onboarding_reprompt_sweep()
            finally:
                self.bot.ONBOARDING_REPROMPT_HOURS = original

            self.assertIn(77, deleted_messages, "old message was not deleted")
            mock_post.assert_awaited_once()

    async def test_max_reprompts_stops_further_posting(self):
        """A prompt that has hit ONBOARDING_MAX_REPROMPTS must not be re-posted."""
        now = time.time()
        old_ts = int(now - 6 * 3600)
        capped_row = {
            "guild_id": 1,
            "user_id": 300,
            "channel_id": 999,
            "message_id": 88,
            "posted_ts": old_ts,
            "reprompt_count": 3,  # at or above default cap of 3
        }

        with (
            patch.object(
                self.bot.analytics,
                "list_pending_onboarding_prompts",
                return_value=[capped_row],
            ),
            patch.object(
                self.bot.analytics,
                "complete_onboarding_prompt",
            ) as mock_complete,
            patch.object(self.bot, "_post_onboarding_welcome", new_callable=AsyncMock) as mock_post,
            patch.object(self.bot, "client") as mock_client,
        ):
            guild_mock = MagicMock()
            member_mock = MagicMock()
            member_mock.id = 300
            guild_mock.get_member = MagicMock(return_value=member_mock)
            mock_client.get_guild = MagicMock(return_value=guild_mock)

            original_max = self.bot.ONBOARDING_MAX_REPROMPTS
            original_hrs = self.bot.ONBOARDING_REPROMPT_HOURS
            self.bot.ONBOARDING_MAX_REPROMPTS = 3
            self.bot.ONBOARDING_REPROMPT_HOURS = 5.0
            try:
                await self.bot._onboarding_reprompt_sweep()
            finally:
                self.bot.ONBOARDING_MAX_REPROMPTS = original_max
                self.bot.ONBOARDING_REPROMPT_HOURS = original_hrs

            mock_post.assert_not_awaited()
            mock_complete.assert_called_once()

    async def test_member_left_cleans_up_prompt(self):
        """If the member left the guild, the onboarding row is deleted (not re-prompted)."""
        now = time.time()
        old_ts = int(now - 6 * 3600)
        row = {
            "guild_id": 1,
            "user_id": 400,
            "channel_id": 999,
            "message_id": 55,
            "posted_ts": old_ts,
            "reprompt_count": 0,
        }

        with (
            patch.object(
                self.bot.analytics,
                "list_pending_onboarding_prompts",
                return_value=[row],
            ),
            patch.object(
                self.bot.analytics,
                "delete_onboarding_prompt",
            ) as mock_delete,
            patch.object(self.bot, "_post_onboarding_welcome", new_callable=AsyncMock) as mock_post,
            patch.object(self.bot, "client") as mock_client,
        ):
            guild_mock = MagicMock()
            guild_mock.get_member = MagicMock(return_value=None)  # member left
            mock_client.get_guild = MagicMock(return_value=guild_mock)

            original = self.bot.ONBOARDING_REPROMPT_HOURS
            self.bot.ONBOARDING_REPROMPT_HOURS = 5.0
            try:
                await self.bot._onboarding_reprompt_sweep()
            finally:
                self.bot.ONBOARDING_REPROMPT_HOURS = original

            mock_post.assert_not_awaited()
            mock_delete.assert_called_once_with(1, 400)


# ---------------------------------------------------------------------------
# Concurrent record-write serialization (duplicate-log regression guard)
# ---------------------------------------------------------------------------

class RecordWriteLockTests(unittest.IsolatedAsyncioTestCase):
    """Concurrent record writes for one member must not duplicate the record.

    Regression guard for the duplicate "Member Record" logs: the onboarding
    screenshot write and the role-grant refresh can fire for the same member
    within the same second. Without per-member serialization both observe an
    empty records_index (the index write lags the post) and each create a
    record. ``_edit_or_create_member_record`` holds a per-member lock so the
    second writer observes the first's freshly-indexed record and edits it in
    place. This test fails (two creates) if the lock is removed.
    """

    async def asyncSetUp(self):
        import bot as bot_module
        self.bot = bot_module
        # Drop any lock bound to a previous test's (now-closed) event loop.
        self.bot._RECORD_WRITE_LOCKS.clear()

    async def test_concurrent_edit_or_create_makes_single_record(self):
        bot = self.bot
        member = MagicMock()
        member.id = 4242
        member.guild = MagicMock()
        member.guild.id = 7

        index: dict[int, list[int]] = {}
        next_id = {"v": 1000}

        async def fake_create(m, summary_lines, *, image_bytes=None):
            # Simulate the post latency that precedes the index write — the
            # exact window that produced duplicate records before the lock.
            await asyncio.sleep(0.01)
            next_id["v"] += 1
            index.setdefault(m.id, []).append(next_id["v"])

        edits: list[int] = []

        async def fake_edit(
            channel_id, message_id, m, summary_lines, *, image_bytes=None
        ):
            edits.append(message_id)

        def fake_get_ids(user_id):
            return list(index.get(user_id, []))

        with (
            patch.object(bot, "MEMBER_RECORDS_CHANNEL_ID", 555),
            patch.object(bot, "_create_member_record", side_effect=fake_create),
            patch.object(bot, "_edit_member_record", side_effect=fake_edit),
            patch.object(bot, "_records_channel_id", return_value=555),
            patch.object(
                bot, "_member_record_profile_lines",
                return_value=["In-game name: **Viroella#826**"],
            ),
            patch.object(
                bot, "_member_profile_from_records",
                new=AsyncMock(return_value=None),
            ),
            patch.object(
                bot.records_index, "get_record_message_ids",
                side_effect=fake_get_ids,
            ),
        ):
            await asyncio.gather(
                bot._edit_or_create_member_record(
                    member, in_game_name="Viroella#826",
                    mastery_rank="MR 25", image_bytes=b"shot",
                ),
                bot._edit_or_create_member_record(member),
            )

        # Exactly one record created; the second writer edited it in place.
        self.assertEqual(len(index.get(member.id, [])), 1)
        self.assertEqual(len(edits), 1)


# ---------------------------------------------------------------------------
# Record creation policy (missing-screenshot regression guard)
# ---------------------------------------------------------------------------

class RecordCreationPolicyTests(unittest.IsolatedAsyncioTestCase):
    """A member record must be BORN only with evidence: the member's uploaded
    screenshot, or an explicit manual-review note. A purely role-derived
    refresh (verified-role grant, mastery / IGN edit, role change) that finds
    no existing record must NOT mint a brand-new, screenshot-less "Member
    Record" log. Regression guard for empty profile logs appearing with no
    screenshot. ``_edit_or_create_member_record`` still edits an existing
    record in any case (the role-derived refresh path).
    """

    async def asyncSetUp(self):
        import bot as bot_module
        self.bot = bot_module
        self.bot._RECORD_WRITE_LOCKS.clear()

    def _make_member(self):
        member = MagicMock()
        member.id = 909090
        member.guild = MagicMock()
        member.guild.id = 11
        return member

    async def _run(self, *, existing_ids, **call_kwargs):
        bot = self.bot
        member = self._make_member()
        create = AsyncMock()
        edit = AsyncMock()
        with (
            patch.object(bot, "MEMBER_RECORDS_CHANNEL_ID", 555),
            patch.object(bot, "_create_member_record", new=create),
            patch.object(bot, "_edit_member_record", new=edit),
            patch.object(bot, "_records_channel_id", return_value=555),
            patch.object(
                bot, "_member_record_profile_lines",
                return_value=["Clan: **Golden Tenno**"],
            ),
            patch.object(
                bot, "_member_profile_from_records",
                new=AsyncMock(return_value=None),
            ),
            patch.object(
                bot.records_index, "get_record_message_ids",
                side_effect=lambda uid: list(existing_ids),
            ),
        ):
            await bot._edit_or_create_member_record(member, **call_kwargs)
        return create, edit

    async def test_role_derived_refresh_does_not_create(self):
        # No screenshot, no review note, no existing record -> nothing posted.
        create, edit = await self._run(existing_ids=[])
        create.assert_not_called()
        edit.assert_not_called()

    async def test_screenshot_creates_record(self):
        create, edit = await self._run(existing_ids=[], image_bytes=b"shot")
        create.assert_awaited_once()
        edit.assert_not_called()

    async def test_review_note_creates_record(self):
        # Manual-review "pending" records are intentionally screenshot-less.
        create, edit = await self._run(
            existing_ids=[], extra_lines=["Manual review pending — x"],
        )
        create.assert_awaited_once()
        edit.assert_not_called()

    async def test_role_derived_refresh_edits_existing_record(self):
        create, edit = await self._run(existing_ids=[12345])
        edit.assert_awaited_once()
        create.assert_not_called()


# ---------------------------------------------------------------------------
# Member record card components
# ---------------------------------------------------------------------------

class MemberRecordComponentsTests(unittest.TestCase):
    """Tests for _build_member_record_embed structure (records are embeds)."""

    def setUp(self):
        import bot as bot_module
        self.bot = bot_module

    def _make_member(self, display_name: str = "Tenno", uid: int = 9999) -> MagicMock:
        m = MagicMock()
        m.id = uid
        m.display_name = display_name
        m.mention = f"<@{uid}>"
        m.__str__ = MagicMock(return_value=f"Tenno#{uid}")
        m.joined_at = None
        m.display_avatar = MagicMock()
        m.display_avatar.url = "https://cdn.example/avatar.png"
        return m

    def test_embed_has_title(self):
        """The embed title carries 'Member Record' and the display name."""
        member = self._make_member("TestUser")
        embed = self.bot._build_member_record_embed(
            member, ["Clan: **Golden Pagoda**"]
        )
        self.assertIn("Member Record", embed["title"])
        self.assertIn("TestUser", embed["title"])

    def test_embed_image_when_screenshot_present(self):
        """With an image the embed references 'attachment://record.png'."""
        member = self._make_member()
        embed = self.bot._build_member_record_embed(
            member, [], has_image=True
        )
        self.assertEqual(
            embed.get("image", {}).get("url"), "attachment://record.png"
        )

    def test_embed_no_image_when_text_only(self):
        """Without a screenshot the embed carries no image reference."""
        member = self._make_member()
        embed = self.bot._build_member_record_embed(
            member, [], has_image=False
        )
        self.assertNotIn("image", embed)

    def test_embed_thumbnail_is_url_without_avatar_attachment(self):
        """Without an attached avatar the thumbnail is the raw avatar URL."""
        member = self._make_member()
        embed = self.bot._build_member_record_embed(
            member, [], has_avatar=False
        )
        self.assertEqual(
            embed.get("thumbnail", {}).get("url"),
            "https://cdn.example/avatar.png",
        )

    def test_embed_thumbnail_is_circular_avatar_attachment(self):
        """With has_avatar the thumbnail references attachment://avatar.png
        (the /profile-style circular avatar) instead of the square URL."""
        member = self._make_member()
        embed = self.bot._build_member_record_embed(
            member, [], has_avatar=True
        )
        self.assertEqual(
            embed.get("thumbnail", {}).get("url"), "attachment://avatar.png"
        )

    def test_embed_is_gold(self):
        """The embed color is ACCENT_PASS (the /status gold)."""
        member = self._make_member()
        embed = self.bot._build_member_record_embed(
            member, ["Mastery Rank: **MR 10**"]
        )
        self.assertEqual(embed.get("color"), self.bot.ACCENT_PASS)

    def test_summary_lines_become_fields(self):
        """Key: **Value** summary lines become embed fields (name carries a
        trailing colon; value is wrapped in backticks)."""
        member = self._make_member()
        summary = ["Clan: **Golden Pagoda**", "Mastery Rank: **MR 15**"]
        embed = self.bot._build_member_record_embed(member, summary)
        names = {f["name"] for f in embed["fields"]}
        body = str(embed["fields"])
        self.assertIn("Clan:", names)
        self.assertIn("Mastery Rank:", names)
        self.assertIn("`Golden Pagoda`", body)
        self.assertIn("`MR 15`", body)

    def test_member_id_appears_in_embed(self):
        """The member ID is always embedded in the record description."""
        member = self._make_member(uid=123456789)
        embed = self.bot._build_member_record_embed(member, [])
        self.assertIn("123456789", str(embed))

    def test_embed_fields_round_trip_through_parser(self):
        """The fields _build_member_record_embed writes parse back via
        _parse_record_embed to the source-of-truth profile dict."""
        member = self._make_member()
        summary = [
            "In-game name: **TennoOne**",
            "Clan: **Golden Pagoda**",
            "Platform: **PC**",
            "Mastery Rank: **MR 27**",
            "Syndicate: **Red Veil**",
        ]
        embed = self.bot._build_member_record_embed(member, summary)
        parsed = self.bot._parse_record_embed([embed])
        self.assertEqual(parsed.get("in_game_name"), "TennoOne")
        self.assertEqual(parsed.get("clan"), "Golden Pagoda")
        self.assertEqual(parsed.get("platform"), "PC")
        self.assertEqual(parsed.get("mastery_rank"), "MR 27")
        self.assertEqual(parsed.get("syndicate"), "Red Veil")

    def test_parser_drops_coarse_mastery_bucket(self):
        """A non-exact mastery bucket name is dropped (matches old store)."""
        member = self._make_member()
        embed = self.bot._build_member_record_embed(
            member, ["Mastery Rank: **MR 10-15**"]
        )
        parsed = self.bot._parse_record_embed([embed])
        self.assertNotIn("mastery_rank", parsed)

    def test_parser_drops_zero_mastery_rank(self):
        """A bogus 'MR 0' is rejected on read so it can't stick in the record
        (it would otherwise be preferred over the real rank role forever)."""
        member = self._make_member()
        embed = self.bot._build_member_record_embed(
            member, ["Mastery Rank: **MR 0**"]
        )
        parsed = self.bot._parse_record_embed([embed])
        self.assertNotIn("mastery_rank", parsed)

    def test_is_exact_mastery_rank(self):
        b = self.bot
        self.assertTrue(b._is_exact_mastery_rank("MR 27"))
        self.assertTrue(b._is_exact_mastery_rank("LR 3"))
        self.assertFalse(b._is_exact_mastery_rank("MR 0"))
        self.assertFalse(b._is_exact_mastery_rank("LR 0"))
        self.assertFalse(b._is_exact_mastery_rank("MR 10-15"))
        self.assertFalse(b._is_exact_mastery_rank("Unranked"))
        self.assertFalse(b._is_exact_mastery_rank(""))

    def test_footer_is_last_edited_with_timestamp(self):
        """The record footer reads 'Last edited' and carries an ISO timestamp
        (rendered next to the footer text as the last-edited time)."""
        member = self._make_member()
        embed = self.bot._build_member_record_embed(member, [])
        self.assertEqual(embed.get("footer", {}).get("text"), "Last edited")
        self.assertIsInstance(embed.get("timestamp"), str)
        # Parseable ISO 8601 (the format Discord expects for embed.timestamp).
        from datetime import datetime
        datetime.fromisoformat(embed["timestamp"])


class RecordAttachmentPlanTests(unittest.TestCase):
    """Tests for _record_attachment_plan attachment ordering."""

    def setUp(self):
        import bot as bot_module
        self.bot = bot_module

    def test_no_files_returns_none_primary(self):
        primary, name, extra, atts = self.bot._record_attachment_plan(
            None, "record.png", None
        )
        self.assertIsNone(primary)
        self.assertEqual(extra, [])
        self.assertEqual(atts, [])

    def test_screenshot_only_is_id_zero(self):
        primary, name, extra, atts = self.bot._record_attachment_plan(
            b"img", "record.png", None
        )
        self.assertEqual(primary, b"img")
        self.assertEqual(name, "record.png")
        self.assertEqual(extra, [])
        self.assertEqual(atts, [{"id": 0, "filename": "record.png"}])

    def test_avatar_only_is_id_zero(self):
        primary, name, extra, atts = self.bot._record_attachment_plan(
            None, "record.png", b"av"
        )
        self.assertEqual(primary, b"av")
        self.assertEqual(name, "avatar.png")
        self.assertEqual(extra, [])
        self.assertEqual(atts, [{"id": 0, "filename": "avatar.png"}])

    def test_both_screenshot_then_avatar(self):
        primary, name, extra, atts = self.bot._record_attachment_plan(
            b"img", "record.png", b"av"
        )
        self.assertEqual(primary, b"img")
        self.assertEqual(name, "record.png")
        self.assertEqual(extra, [(b"av", "avatar.png")])
        self.assertEqual(atts, [
            {"id": 0, "filename": "record.png"},
            {"id": 1, "filename": "avatar.png"},
        ])


# ---------------------------------------------------------------------------
# Channel constant defaults
# ---------------------------------------------------------------------------

class ChannelConstantTests(unittest.TestCase):
    """Tests for ONBOARDING_CHANNEL_ID / MEMBER_RECORDS_CHANNEL_ID defaults."""

    def test_onboarding_channel_defaults_to_target_channel_when_not_set(self):
        """ONBOARDING_CHANNEL_ID falls back to TARGET_CHANNEL_ID when unset."""
        import bot as bot_module
        # When TARGET_CHANNEL_ID is set (done by test env) and ONBOARDING_CHANNEL_ID
        # is not separately configured, both must be > 0.
        self.assertGreater(bot_module.ONBOARDING_CHANNEL_ID, 0)

    def test_member_records_channel_is_int(self):
        """MEMBER_RECORDS_CHANNEL_ID is an integer (0 when unset)."""
        import bot as bot_module
        self.assertIsInstance(bot_module.MEMBER_RECORDS_CHANNEL_ID, int)


# ---------------------------------------------------------------------------
# Profile screenshot cache + "Not Affiliated" halt-until-verify flow
# ---------------------------------------------------------------------------

class ScreenshotCacheTests(unittest.TestCase):
    """The per-user in-memory screenshot byte cache helpers."""

    def setUp(self):
        import bot as bot_module
        self.bot = bot_module
        self.bot._screenshot_cache.clear()

    def tearDown(self):
        self.bot._screenshot_cache.clear()

    def test_set_get_and_replace(self):
        self.bot._cache_screenshot(42, b"first")
        self.assertEqual(self.bot._get_cached_screenshot(42), b"first")
        # A new screenshot replaces (drops) the old one.
        self.bot._cache_screenshot(42, b"second")
        self.assertEqual(self.bot._get_cached_screenshot(42), b"second")

    def test_falsy_bytes_ignored(self):
        self.bot._cache_screenshot(42, b"keep")
        self.bot._cache_screenshot(42, None)
        self.bot._cache_screenshot(42, b"")
        self.assertEqual(self.bot._get_cached_screenshot(42), b"keep")

    def test_evict(self):
        self.bot._cache_screenshot(42, b"x")
        self.bot._evict_cached_screenshot(42)
        self.assertIsNone(self.bot._get_cached_screenshot(42))
        # Evicting a missing entry is a no-op.
        self.bot._evict_cached_screenshot(999)

    def test_lru_cap_evicts_least_recently_used(self):
        cap = self.bot._SCREENSHOT_CACHE_MAX
        for uid in range(cap):
            self.bot._cache_screenshot(uid, b"x")
        # Touch user 0 so it becomes most-recently-used.
        self.assertEqual(self.bot._get_cached_screenshot(0), b"x")
        # One over the cap evicts the least-recently-used entry (user 1).
        self.bot._cache_screenshot(cap, b"new")
        self.assertEqual(len(self.bot._screenshot_cache), cap)
        self.assertIsNone(self.bot._get_cached_screenshot(1))
        self.assertEqual(self.bot._get_cached_screenshot(0), b"x")
        self.assertEqual(self.bot._get_cached_screenshot(cap), b"new")


class EditMemberRecordCacheTests(unittest.IsolatedAsyncioTestCase):
    """_edit_member_record prefers the cached screenshot over a CDN re-fetch."""

    async def asyncSetUp(self):
        import bot as bot_module
        self.bot = bot_module
        self.bot._screenshot_cache.clear()

    def tearDown(self):
        self.bot._screenshot_cache.clear()

    async def test_edit_draws_image_from_cache(self):
        member = MagicMock()
        member.id = 555
        self.bot._cache_screenshot(555, b"cached-shot")

        with (
            patch.object(self.bot, "_render_record_avatar_bytes",
                         new_callable=AsyncMock, return_value=b"av"),
            patch.object(self.bot, "_fetch_record_message",
                         new_callable=AsyncMock) as mock_fetch,
            patch.object(self.bot, "_build_member_record_embed",
                         return_value={"embed": 1}),
            patch.object(self.bot, "_edit_channel_embed",
                         new_callable=AsyncMock) as mock_edit,
        ):
            await self.bot._edit_member_record(
                10, 20, member, ["line"], image_bytes=None
            )

        # The CDN recovery path must NOT run when the cache has the bytes.
        mock_fetch.assert_not_awaited()
        mock_edit.assert_awaited_once()
        self.assertEqual(mock_edit.call_args.kwargs["file_bytes"], b"cached-shot")


class NoClanModalHaltTests(unittest.IsolatedAsyncioTestCase):
    """The 'Not Affiliated' modal caches the screenshot but runs no OCR."""

    async def asyncSetUp(self):
        import bot as bot_module
        self.bot = bot_module
        self.bot._screenshot_cache.clear()

    def tearDown(self):
        self.bot._screenshot_cache.clear()

    async def test_submit_caches_and_does_not_verify(self):
        member = MagicMock()
        member.id = 321
        member.guild = MagicMock()
        member.guild.get_member = MagicMock(return_value=member)

        modal = self.bot._OnboardingNoClanModal(member=member)
        attachment = MagicMock()
        attachment.read = AsyncMock(return_value=b"shot-bytes")
        attachment.filename = "p.png"
        attachment.content_type = "image/png"
        modal.screenshot = MagicMock()
        modal.screenshot.values = [attachment]

        interaction = MagicMock()
        interaction.response = AsyncMock()

        with (
            patch.object(self.bot, "_verify_member_from_screenshot",
                         new_callable=AsyncMock) as mock_verify,
            patch.object(self.bot, "_onboarding_route_manual_review",
                         new_callable=AsyncMock) as mock_route,
        ):
            await modal.on_submit(interaction)

        # No OCR/verification at submit time — verification is halted.
        mock_verify.assert_not_awaited()
        # Screenshot is cached for the later moderator-triggered verify.
        self.assertEqual(self.bot._get_cached_screenshot(321), b"shot-bytes")
        # Routed to manual review with the screenshot, no OCR fields.
        mock_route.assert_awaited_once()
        self.assertEqual(
            mock_route.call_args.kwargs["image_bytes"], b"shot-bytes"
        )
        self.assertNotIn("in_game_name", mock_route.call_args.kwargs)
        self.assertNotIn("mastery_rank", mock_route.call_args.kwargs)


class MreviewCachedVerifyTests(unittest.IsolatedAsyncioTestCase):
    """The staff Verify button OCRs the cached screenshot then grants roles."""

    async def asyncSetUp(self):
        import bot as bot_module
        self.bot = bot_module
        self.bot._screenshot_cache.clear()

    def tearDown(self):
        self.bot._screenshot_cache.clear()

    def _make_interaction(self, target_id: int) -> MagicMock:
        interaction = MagicMock()
        import discord
        interaction.user = MagicMock(spec=discord.Member)
        interaction.user.id = 99
        interaction.user.guild_permissions = MagicMock()
        interaction.user.guild_permissions.manage_guild = True
        member = MagicMock()
        member.id = target_id
        member.roles = []
        member.display_name = "Tester"
        guild = MagicMock()
        guild.get_member = MagicMock(return_value=member)
        guild.get_role = MagicMock(return_value=None)
        guild.fetch_member = AsyncMock(return_value=member)
        interaction.guild = guild
        interaction.message = MagicMock()
        interaction.message.id = 1
        interaction.message.channel = MagicMock()
        interaction.message.channel.id = 2
        interaction.followup = AsyncMock()
        return interaction

    async def test_verify_runs_ocr_from_cache(self):
        target_id = 4242
        self.bot._cache_screenshot(target_id, b"cached")
        interaction = self._make_interaction(target_id)

        verify_result = self.bot._VerifyResult(
            summary=["Clan: assigned"], in_game_name="Op", mastery_rank="MR 10"
        )
        with (
            patch.object(self.bot, "_interaction_callback",
                         new_callable=AsyncMock),
            patch.object(self.bot, "_verify_member_from_screenshot",
                         new_callable=AsyncMock,
                         return_value=verify_result) as mock_verify,
            patch.object(self.bot, "_edit_or_create_member_record",
                         new_callable=AsyncMock) as mock_record,
            patch.object(self.bot, "_spawn_bg_task"),
            patch.object(self.bot, "_delete_message", new=MagicMock()),
        ):
            await self.bot._handle_mreview_interaction(
                interaction, f"mreview:{target_id}:approve"
            )

        mock_verify.assert_awaited_once()
        self.assertEqual(
            mock_verify.call_args.kwargs["image_bytes"], b"cached"
        )
        # A readable result writes the record with the OCR fields.
        mock_record.assert_awaited_once()
        self.assertEqual(
            mock_record.call_args.kwargs["in_game_name"], "Op"
        )

    async def test_verify_without_cache_skips_ocr(self):
        target_id = 4243
        interaction = self._make_interaction(target_id)

        with (
            patch.object(self.bot, "_interaction_callback",
                         new_callable=AsyncMock),
            patch.object(self.bot, "_verify_member_from_screenshot",
                         new_callable=AsyncMock) as mock_verify,
            patch.object(self.bot, "_edit_or_create_member_record",
                         new_callable=AsyncMock),
            patch.object(self.bot, "_spawn_bg_task"),
            patch.object(self.bot, "_delete_message", new=MagicMock()),
        ):
            await self.bot._handle_mreview_interaction(
                interaction, f"mreview:{target_id}:approve"
            )

        mock_verify.assert_not_awaited()


# ---------------------------------------------------------------------------
# Member profile durable store (SQLite source of truth) + lazy backfill
# ---------------------------------------------------------------------------

class MemberProfileStoreTests(unittest.IsolatedAsyncioTestCase):
    """_member_profile_from_records now reads the durable member_profile store
    first, falling back once to parsing the legacy record message and
    backfilling the store."""

    def setUp(self):
        import bot as bot_module
        self.bot = bot_module

    async def test_reads_store_without_touching_channel(self):
        b = self.bot
        with (
            patch.object(
                b.analytics, "get_member_profile",
                return_value={"in_game_name": "Viro#1", "mastery_rank": "MR 25"},
            ),
            patch.object(
                b, "_read_member_profile_from_records",
                new=AsyncMock(return_value=None),
            ) as legacy,
        ):
            out = await b._member_profile_from_records(7, 11)
        self.assertEqual(out["in_game_name"], "Viro#1")
        # The legacy channel parse must not run when the store has the row.
        legacy.assert_not_awaited()

    async def test_lazy_backfill_on_store_miss(self):
        b = self.bot
        legacy_profile = {
            "in_game_name": "Old#9", "mastery_rank": "LR 2",
            "platform": "PC", "clan": "Golden", "last_verified_ts": 123,
        }
        with (
            patch.object(b.analytics, "get_member_profile", return_value=None),
            patch.object(
                b, "_read_member_profile_from_records",
                new=AsyncMock(return_value=legacy_profile),
            ),
            patch.object(b.analytics, "upsert_member_profile") as upsert,
        ):
            out = await b._member_profile_from_records(7, 11)
        self.assertEqual(out["in_game_name"], "Old#9")
        # The parsed legacy profile is persisted to the store.
        upsert.assert_called_once()
        kwargs = upsert.call_args.kwargs
        self.assertEqual(kwargs["in_game_name"], "Old#9")
        self.assertEqual(kwargs["mastery_rank"], "LR 2")
        self.assertEqual(kwargs["platform"], "PC")
        self.assertEqual(kwargs["clan"], "Golden")

    async def test_store_snapshot_preserves_ocr_fields_on_role_refresh(self):
        # A role-derived refresh (no in_game_name / mastery_rank) must omit
        # those fields from the upsert so the store preserves them, while still
        # refreshing the role-derived platform/clan from the rendered lines.
        b = self.bot
        member = MagicMock()
        member.id = 42
        member.guild = MagicMock()
        member.guild.id = 7
        with patch.object(b.analytics, "upsert_member_profile") as upsert:
            await b._store_member_profile_snapshot(
                member,
                in_game_name=None,
                mastery_rank=None,
                summary_lines=["Platform: **Xbox**", "Clan: **Golden**"],
            )
        kwargs = upsert.call_args.kwargs
        self.assertNotIn("in_game_name", kwargs)
        self.assertNotIn("mastery_rank", kwargs)
        self.assertEqual(kwargs["platform"], "Xbox")
        self.assertEqual(kwargs["clan"], "Golden")

    async def test_store_snapshot_rejects_bogus_mastery(self):
        b = self.bot
        member = MagicMock()
        member.id = 42
        member.guild = MagicMock()
        member.guild.id = 7
        with patch.object(b.analytics, "upsert_member_profile") as upsert:
            await b._store_member_profile_snapshot(
                member,
                in_game_name="Viro#1",
                mastery_rank="MR 0",
                summary_lines=["In-game name: **Viro#1**"],
            )
        kwargs = upsert.call_args.kwargs
        self.assertEqual(kwargs["in_game_name"], "Viro#1")
        # 'MR 0' is not an exact rank -> not written (would clobber the real
        # rank role otherwise).
        self.assertNotIn("mastery_rank", kwargs)


# ---------------------------------------------------------------------------
# Alias select — nickname flow on the verified-welcome card
# ---------------------------------------------------------------------------

class AliasSelectTests(unittest.IsolatedAsyncioTestCase):
    """The alias select handler: IGN nickname set + Server-name modal."""

    async def asyncSetUp(self):
        import bot as bot_module
        self.bot = bot_module

    def _make_interaction(self, clicker_id: int, values: list[str]) -> Mock:
        interaction = MagicMock()
        interaction.user = MagicMock()
        interaction.user.id = clicker_id
        interaction.guild = MagicMock()
        interaction.guild.id = 7
        interaction.guild.get_member = MagicMock(return_value=None)
        interaction.data = {"values": values}
        interaction.message = MagicMock()
        interaction.message.id = 1
        interaction.message.channel.id = 999
        interaction.response = AsyncMock()
        interaction.followup = AsyncMock()
        return interaction

    async def test_wrong_user_gets_ephemeral_rejection(self):
        interaction = self._make_interaction(2222, ["ign"])
        with patch.object(
            self.bot, "_interaction_callback", new_callable=AsyncMock
        ) as mock_cb:
            await self.bot._handle_alias_interaction(
                interaction, "alias:1111:std"
            )
        mock_cb.assert_awaited()

    async def test_ign_option_sets_nickname_from_record(self):
        b = self.bot
        target_uid = 1111
        member = MagicMock()
        member.id = target_uid
        member.edit = AsyncMock()
        interaction = self._make_interaction(target_uid, ["ign"])
        interaction.guild.get_member = MagicMock(return_value=member)

        with patch.object(
            b, "_member_in_game_name", new_callable=AsyncMock,
            return_value="Viro#123",
        ), patch.object(
            b, "_interaction_callback", new_callable=AsyncMock
        ) as mock_cb, patch.object(
            b, "_reset_alias_select", new_callable=AsyncMock
        ), patch.object(
            b, "_edit_or_create_member_record", new_callable=AsyncMock
        ) as mock_record:
            await b._handle_alias_interaction(
                interaction, f"alias:{target_uid}:std"
            )

        member.edit.assert_awaited_once()
        self.assertEqual(member.edit.call_args.kwargs["nick"], "Viro#123")
        mock_cb.assert_awaited()
        # Nickname flow never touches the member record (IGN in logging
        # is not replaced).
        mock_record.assert_not_awaited()

    async def test_ign_option_without_record_reports_failure(self):
        b = self.bot
        target_uid = 1111
        member = MagicMock()
        member.id = target_uid
        member.edit = AsyncMock()
        interaction = self._make_interaction(target_uid, ["ign"])
        interaction.guild.get_member = MagicMock(return_value=member)

        with patch.object(
            b, "_member_in_game_name", new_callable=AsyncMock,
            return_value=None,
        ), patch.object(
            b, "_interaction_callback", new_callable=AsyncMock
        ) as mock_cb, patch.object(
            b, "_reset_alias_select", new_callable=AsyncMock
        ):
            await b._handle_alias_interaction(
                interaction, f"alias:{target_uid}:std"
            )

        member.edit.assert_not_awaited()
        mock_cb.assert_awaited()

    async def test_nick_option_opens_modal(self):
        b = self.bot
        target_uid = 1111
        member = MagicMock()
        member.id = target_uid
        member.nick = None
        interaction = self._make_interaction(target_uid, ["nick"])
        interaction.guild.get_member = MagicMock(return_value=member)
        send_modal_mock = AsyncMock()
        interaction.response.send_modal = send_modal_mock

        with patch.object(
            b, "_reset_alias_select", new_callable=AsyncMock
        ):
            await b._handle_alias_interaction(
                interaction, f"alias:{target_uid}:std"
            )

        send_modal_mock.assert_awaited_once()
        args = send_modal_mock.call_args[0]
        self.assertIsInstance(args[0], b._AliasNicknameModal)

    async def test_nickname_modal_sets_nick_without_record_write(self):
        b = self.bot
        member = MagicMock()
        member.id = 1111
        member.nick = None
        member.edit = AsyncMock()
        modal = b._AliasNicknameModal(member=member)
        modal.nickname._value = "Fresh Alias"
        interaction = MagicMock()
        interaction.response = AsyncMock()

        with patch.object(
            b, "_edit_or_create_member_record", new_callable=AsyncMock
        ) as mock_record:
            await modal.on_submit(interaction)

        member.edit.assert_awaited_once()
        self.assertEqual(member.edit.call_args.kwargs["nick"], "Fresh Alias")
        mock_record.assert_not_awaited()

    async def test_set_member_nickname_truncates_to_32(self):
        member = MagicMock()
        member.edit = AsyncMock()
        long_name = "x" * 50
        ok, _msg = await self.bot._set_member_nickname(
            member, long_name, reason="test"
        )
        self.assertTrue(ok)
        self.assertEqual(
            member.edit.call_args.kwargs["nick"], "x" * 32
        )


class JoinDebounceTests(unittest.IsolatedAsyncioTestCase):
    """Concurrent joins must stagger their welcome posts, not fire together.

    Regression guard for the debounce race: before the slot was reserved
    prior to sleeping, every concurrent ``on_member_join`` read the same
    ``_JOIN_LAST_POST`` timestamp, slept the same duration, and posted at
    once — defeating the raid debounce.
    """

    async def asyncSetUp(self):
        import bot as bot_module
        self.bot = bot_module
        self.bot._JOIN_LAST_POST.clear()

    async def asyncTearDown(self):
        self.bot._JOIN_LAST_POST.clear()

    def _member(self, uid: int) -> MagicMock:
        member = MagicMock()
        member.id = uid
        member.guild = MagicMock()
        member.guild.id = 7
        return member

    async def test_concurrent_joins_reserve_staggered_slots(self):
        bot = self.bot
        sleeps: list[float] = []

        def fake_spawn(coro, **kwargs):
            coro.close()

        async def fake_post(member):
            return None

        async def fake_sleep(delay):
            sleeps.append(delay)

        with patch.object(bot, "_spawn_bg_task", side_effect=fake_spawn), \
                patch.object(bot, "_post_onboarding_welcome", fake_post), \
                patch.object(bot.asyncio, "sleep", fake_sleep):
            await asyncio.gather(
                bot.on_member_join(self._member(1)),
                bot.on_member_join(self._member(2)),
                bot.on_member_join(self._member(3)),
            )

        # Each join after the first must have reserved a distinct, later slot.
        self.assertEqual(len(sleeps), 2)
        self.assertNotEqual(sleeps[0], sleeps[1])
        self.assertAlmostEqual(
            abs(sleeps[1] - sleeps[0]), bot._JOIN_DEBOUNCE_SECONDS, delta=0.5
        )
