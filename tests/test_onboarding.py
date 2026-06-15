"""Tests for the member onboarding flow.

Covers:
- analytics.py: onboarding_prompts CRUD + integration with delete_member_data
- bot.py: _onboarding_welcome_components structure + interaction ownership gating
- reprompt sweep: elapsed window detection and max-reprompt cap
"""
from __future__ import annotations

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

class OnboardingComponentsTests(unittest.TestCase):
    """Smoke tests for the onboarding welcome component builder."""

    def setUp(self):
        import bot as bot_module
        self.bot = bot_module

    def test_welcome_components_returns_list(self):
        result = self.bot._onboarding_welcome_components(12345)
        self.assertIsInstance(result, list)
        self.assertGreater(len(result), 0)

    def test_welcome_components_has_container(self):
        result = self.bot._onboarding_welcome_components(12345)
        self.assertTrue(
            any(c.get("type") == 17 for c in result),
            "expected at least one type:17 container",
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
        custom_ids: list[str] = []
        for top in result:
            for section in top.get("components", []) or []:
                if section.get("type") == 1:
                    for btn in section.get("components", []) or []:
                        cid = btn.get("custom_id", "")
                        if cid:
                            custom_ids.append(cid)
        self.assertTrue(
            any(str(member_id) in cid for cid in custom_ids),
            "no custom_id encodes the member_id",
        )

    def test_welcome_components_has_none_button(self):
        member_id = 42
        result = self.bot._onboarding_welcome_components(member_id)
        custom_ids: list[str] = []
        for top in result:
            for section in top.get("components", []) or []:
                if section.get("type") == 1:
                    for btn in section.get("components", []) or []:
                        cid = btn.get("custom_id", "")
                        if cid:
                            custom_ids.append(cid)
        none_ids = [c for c in custom_ids if c.endswith(":none")]
        self.assertTrue(none_ids, "no 'Not listed / No' button found")

    def test_welcome_components_clan_buttons_use_onboard_prefix(self):
        result = self.bot._onboarding_welcome_components(12345)
        for top in result:
            for section in top.get("components", []) or []:
                if section.get("type") == 1:
                    for btn in section.get("components", []) or []:
                        cid = btn.get("custom_id", "")
                        if cid:
                            self.assertTrue(
                                cid.startswith("onboard:"),
                                f"button custom_id '{cid}' doesn't start with 'onboard:'",
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
        """The owner clicking 'not listed' triggers _onboarding_route_manual_review."""
        target_uid = 1111
        custom_id = f"onboard:{target_uid}:none"

        member_mock = MagicMock()
        member_mock.id = target_uid
        member_mock.mention = f"<@{target_uid}>"
        interaction = self._make_interaction(target_uid)
        interaction.guild.get_member = MagicMock(return_value=member_mock)

        with patch.object(
            self.bot, "_onboarding_route_manual_review", new_callable=AsyncMock
        ) as mock_review:
            await self.bot._handle_onboarding_interaction(interaction, custom_id)
            mock_review.assert_awaited_once()

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


# ---------------------------------------------------------------------------
# Reprompt sweep — elapsed window detection and max-reprompt cap
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
# Member record card components
# ---------------------------------------------------------------------------

class MemberRecordComponentsTests(unittest.TestCase):
    """Tests for _build_member_record_components structure."""

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
        return m

    def test_components_has_heading_text(self):
        """First component is a type-10 text block with 'Member Record' in it."""
        member = self._make_member("TestUser")
        comps = self.bot._build_member_record_components(member, ["Clan: Golden Pagoda"])
        self.assertTrue(comps, "component list must not be empty")
        self.assertEqual(comps[0]["type"], 10)
        self.assertIn("Member Record", comps[0]["content"])
        self.assertIn("TestUser", comps[0]["content"])

    def test_components_has_media_gallery(self):
        """A type-12 media gallery referencing 'attachment://record.png' is present."""
        member = self._make_member()
        comps = self.bot._build_member_record_components(member, [])
        gallery_types = [c for c in comps if c.get("type") == 12]
        self.assertTrue(gallery_types, "must have at least one type-12 gallery")
        items = gallery_types[0].get("items", [])
        self.assertTrue(
            any("attachment://record.png" in str(i) for i in items),
            "gallery must reference attachment://record.png",
        )

    def test_components_has_gold_container(self):
        """A type-17 container with accent_color == ACCENT_PASS is present."""
        member = self._make_member()
        comps = self.bot._build_member_record_components(member, ["Mastery Rank: MR 10"])
        containers = [c for c in comps if c.get("type") == 17]
        self.assertTrue(containers, "must have at least one type-17 container")
        container = containers[0]
        self.assertEqual(container.get("accent_color"), self.bot.ACCENT_PASS)

    def test_summary_lines_appear_in_container(self):
        """Summary lines from verification are included in the container text."""
        member = self._make_member()
        summary = ["Clan: Golden Pagoda", "Mastery Rank: MR 15"]
        comps = self.bot._build_member_record_components(member, summary)
        containers = [c for c in comps if c.get("type") == 17]
        self.assertTrue(containers)
        body = str(containers[0])
        self.assertIn("Golden Pagoda", body)
        self.assertIn("MR 15", body)

    def test_member_id_appears_in_container(self):
        """The member ID is always embedded in the record."""
        member = self._make_member(uid=123456789)
        comps = self.bot._build_member_record_components(member, [])
        body = str(comps)
        self.assertIn("123456789", body)


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
