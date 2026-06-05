"""Smoke tests for bot.py component builders.

These tests ensure that every component-builder helper can be called with
the kwargs that each production call site uses. They don't verify output
correctness (that's the unit-test layer); they exist purely to catch
signature-drift bugs at test time instead of runtime.
"""

from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

import discord


class ComponentBuilderSmokeTests(unittest.TestCase):
    """Smoke tests for component builder functions."""

    def setUp(self):
        """Mock the bot module's globals to allow import."""
        import bot as bot_module
        self.bot_module = bot_module
        # Mock the client to avoid needing a real Discord connection.
        self.original_client = getattr(bot_module, 'client', None)
        mock_client = Mock()
        mock_client.user = Mock(id=12345)
        mock_client.latency = 0.05
        mock_client.guilds = []
        bot_module.client = mock_client

    def tearDown(self):
        """Restore original values."""
        if self.original_client is not None:
            self.bot_module.client = self.original_client

    def test_pass_components_signature(self):
        """_pass_components accepts all kwargs used in production."""
        # Call site: bot.py line ~1139
        result = self.bot_module._pass_components(
            profile="Tenno#1234",
            clan="Golden Pagoda",
            clan_emoji="<:GoldenPagoda_Emblem:123>",
            mastery_rank="MR 12",
            link_buttons=[("Pick Roles", "https://discord.com/channels/1/2")],
            progress_attachment="progress.png",
            nick_suggestion="Tenno",
            user_id=9999,
        )
        self.assertIsInstance(result, list)

    def test_pass_components_structure(self):
        """Output payload conforms to Discord V2 component shape."""
        result = self.bot_module._pass_components(
            profile="Tenno#1234",
            clan="Golden Pagoda",
            clan_emoji="<:e:1>",
            mastery_rank="MR 12",
            link_buttons=[
                ("Pick Roles", "https://discord.com/channels/1/2"),
            ],
            missing_categories=["Platform"],
        )
        # Must produce at least one top-level container.
        self.assertTrue(
            any(c.get("type") == 17 for c in result),
            "expected at least one type:17 container",
        )
        # Every action row (type:1) must respect Discord's 5-button cap.
        for top in result:
            for inner in top.get("components", []) or []:
                if inner.get("type") == 1:
                    self.assertLessEqual(
                        len(inner.get("components") or []), 5,
                        "action row exceeds 5-button cap",
                    )

    def test_pass_components_with_progress_card_keeps_button(self):
        """With a progress card attached, the card image sits on top as a
        top-level media gallery and the Pick Roles link button lives in a
        V2 container below it (regression: the button used to be dropped
        when there was no nickname suggestion to host it)."""
        result = self.bot_module._pass_components(
            profile="Tenno #465",
            clan="Golden Tenno",
            link_buttons=[
                ("Pick Roles", "https://discord.com/channels/1/2"),
            ],
            progress_attachment="progress.png",
        )
        # The card image is a TOP-LEVEL media gallery (type 12), on top.
        self.assertEqual(
            result[0].get("type"), 12,
            "expected the progress card media gallery on top",
        )
        # A V2 container (type 17) follows and holds the link button.
        container = next(
            (c for c in result if c.get("type") == 17), None
        )
        self.assertIsNotNone(
            container, "expected a type:17 container for the button"
        )
        link_buttons = [
            btn
            for row in (container.get("components") or [])
            if row.get("type") == 1
            for btn in (row.get("components") or [])
            if btn.get("style") == 5 and btn.get("url")
        ]
        self.assertTrue(
            link_buttons, "Pick Roles link button missing from pass card"
        )

    def test_nick_custom_ids_within_100_chars(self):
        """_nick_custom_ids never produces a custom_id over Discord's 100-char cap."""
        # Long unicode suggestion forces URL-encoding to expand bytes.
        yes, no = self.bot_module._nick_custom_ids("A" * 200, 1234567890123456789)
        self.assertLessEqual(len(yes), 100)
        self.assertLessEqual(len(no), 100)
        # Emoji-only stress test (each char -> %XX%XX%XX%XX bytes).
        yes2, no2 = self.bot_module._nick_custom_ids(
            "\U0001F3AF" * 30, 100000000000000000
        )
        self.assertLessEqual(len(yes2), 100)
        self.assertLessEqual(len(no2), 100)

    def test_incomplete_components_signature(self):
        """_incomplete_components accepts all kwargs used in production."""
        # Call site: bot.py line ~1158
        result = self.bot_module._incomplete_components(
            reason="Missing Platform role.",
            image_url=None,
            link_buttons=[("Help", "https://discord.com/channels/1/2")],
            progress_attachment="progress.png",
            nick_suggestion="Tenno",
            user_id=9999,
        )
        self.assertIsInstance(result, list)

    def test_fail_components_signature(self):
        """_fail_components accepts all kwargs used in production."""
        # Call site: bot.py line ~1346
        result = self.bot_module._fail_components(
            headline="Not readable",
            reason="No text could be read.",
            image_url=None,
        )
        self.assertIsInstance(result, list)

    def test_nickname_prompt_top_level_signature(self):
        """_nickname_prompt_top_level accepts all kwargs used in production."""
        # Call site: bot.py line ~1472 and ~1609
        result = self.bot_module._nickname_prompt_top_level(
            suggestion="Tenno#1234",
            user_id=9999,
        )
        self.assertIsInstance(result, list)

    def test_send_v2_signature(self):
        """_send_v2 helper can be called with all production kwargs."""
        # Call site: bot.py line ~1149, ~1169
        # This is async, so we just verify the signature can be constructed.
        # We won't actually run it (that would require a real Discord client).
        mock_message = Mock(spec=discord.Message)
        mock_message.id = 1
        mock_message.channel = Mock(id=2)
        mock_message.guild = Mock(id=3)
        components = []

        # Verify the function signature accepts these kwargs without raising.
        import inspect
        sig = inspect.signature(self.bot_module._send_v2)
        # Bind the arguments to verify they're accepted.
        bound = sig.bind(
            reply_to=mock_message,
            components=components,
            mention_user=True,
            allow_role_mentions=True,
            file_bytes=b"fake",
            file_name="progress.png",
            file_content_type="image/png",
        )
        self.assertIsNotNone(bound)

    def test_interaction_callback_signature(self):
        """_interaction_callback helper accepts all production kwargs."""
        # Call site: bot.py line ~2283 onward
        mock_interaction = Mock(spec=discord.Interaction)
        mock_interaction.id = 1
        mock_interaction.token = "abc"
        components = []

        import inspect
        sig = inspect.signature(self.bot_module._interaction_callback)
        bound = sig.bind(
            interaction=mock_interaction,
            callback_type=4,
            components=components,
            ephemeral=True,
        )
        self.assertIsNotNone(bound)

    def test_status_components_signature(self):
        """_status_components accepts all production kwargs."""
        # Call site: bot.py line ~2271, ~2852
        mock_interaction = Mock(spec=discord.Interaction)
        mock_interaction.guild = None
        result = self.bot_module._status_components(
            interaction=mock_interaction,
            page=0,
        )
        self.assertIsInstance(result, list)


if __name__ == "__main__":
    unittest.main()
