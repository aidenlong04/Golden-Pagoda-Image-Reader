"""Smoke tests for bot.py component builders.

These tests ensure that every component-builder helper can be called with
the kwargs that each production call site uses. They don't verify output
correctness (that's the unit-test layer); they exist purely to catch
signature-drift bugs at test time instead of runtime.
"""

from __future__ import annotations

import unittest
from unittest.mock import Mock

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

    def test_pass_components_with_progress_card_image_only(self):
        """With a progress card attached and no nickname suggestion to host,
        the reply is just the card image as a top-level media gallery
        (type 12) — no empty container or stray action row trails it."""
        result = self.bot_module._pass_components(
            profile="Tenno #465",
            clan="Golden Tenno",
            progress_attachment="progress.png",
        )
        # The card image is a TOP-LEVEL media gallery (type 12), on top.
        self.assertEqual(
            result[0].get("type"), 12,
            "expected the progress card media gallery on top",
        )
        # Nothing else: no container, no action row.
        self.assertEqual(
            len(result), 1,
            "expected only the media gallery when there's no nick prompt",
        )

    def test_pass_components_card_nick_buttons_in_one_container(self):
        """With a progress card AND a nick suggestion, the call-sign
        buttons live in ONE gold container below the image (no second
        container, no stray top-level action row)."""
        result = self.bot_module._pass_components(
            profile="GoldenTenno#200",
            clan="Golden Tenno",
            progress_attachment="progress.png",
            nick_suggestion="GoldenTenno",
            user_id=4242,
            current_nick="oldnick",
        )
        # Image on top, then exactly one container.
        self.assertEqual(result[0].get("type"), 12)
        containers = [c for c in result if c.get("type") == 17]
        self.assertEqual(
            len(containers), 1, "expected exactly one type:17 container"
        )
        # No action row floats at the top level — every button is nested.
        self.assertFalse(
            any(c.get("type") == 1 for c in result),
            "action row should live inside the container, not top-level",
        )
        rows = [
            ch for ch in (containers[0].get("components") or [])
            if ch.get("type") == 1
        ]
        self.assertTrue(rows, "expected an action row inside the container")
        buttons = rows[0].get("components") or []
        has_nick = any(
            str(b.get("custom_id", "")).startswith("nick:") for b in buttons
        )
        self.assertTrue(has_nick, "call-sign nick button missing")
        self.assertLessEqual(len(buttons), 5, "row exceeds 5-button cap")

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
        result = self.bot_module._incomplete_components(
            reason="Missing Platform role.",
            image_url=None,
            link_buttons=[("Help", "https://discord.com/channels/1/2")],
            progress_attachment="progress.png",
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
        # The headline must actually surface to the user (it was previously
        # accepted but silently dropped from the rendered message).
        self.assertIn("Not readable", str(result))


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

    def test_render_profile_card_png_returns_png(self):
        """_render_profile_card_png returns PNG bytes across the syndicate
        layouts (none / one / two / three+ factions) and the legacy
        em-dash + clan-only + empty inputs without raising."""
        red = (176, 38, 42)
        blue = (58, 150, 221)
        green = (124, 185, 73)
        base = [
            ("Clan", "Golden Tenno", None, (212, 168, 87)),
            ("Platform", "PlayStation", None),
            ("Mastery Rank", "28", None),
        ]
        cases = [
            base + [("Syndicate", [])],
            base + [("Syndicate", [("Red Veil", red, None)])],
            base + [("Syndicate", [
                ("Red Veil", red, None), ("Cephalon Suda", blue, None),
            ])],
            base + [("Syndicate", [
                ("Red Veil", red, None), ("Cephalon Suda", blue, None),
                ("New Loka", green, None),
            ])],
            # Legacy / degenerate shapes must still render.
            [("Clan", "\u2014", None), ("Syndicate", "\u2014", None)],
            [("Clan", "\u2014", None)],
            [],
        ]
        for info in cases:
            png = self.bot_module._render_profile_card_png(
                avatar_bytes=None,
                display_name="Tenno",
                info_lines=info,
            )
            self.assertIsInstance(png, bytes)
            self.assertTrue(
                png.startswith(b"\x89PNG\r\n\x1a\n"),
                "expected PNG magic bytes",
            )


class MasteryEditorHelperTests(unittest.TestCase):
    """Pure-logic tests for the /profile mastery-rank editor helpers."""

    def setUp(self):
        import bot as bot_module
        self.bot_module = bot_module

    def test_format_mastery_display(self):
        b = self.bot_module
        self.assertEqual(b._format_mastery_display("MR 28"), "28")
        self.assertEqual(b._format_mastery_display("LR 3"), "Legendary 3")
        self.assertEqual(b._format_mastery_display("Unranked"), "Unranked")
        self.assertEqual(b._format_mastery_display(None), "")
        self.assertEqual(b._format_mastery_display(""), "")

    def test_parse_mr_bucket_range(self):
        b = self.bot_module
        self.assertEqual(b._parse_mr_bucket_range("MR 1-10"), ("MR", 1, 10))
        self.assertEqual(b._parse_mr_bucket_range("MR 22-29"), ("MR", 22, 29))
        self.assertEqual(b._parse_mr_bucket_range("MR 30"), ("MR", 30, 30))
        self.assertEqual(b._parse_mr_bucket_range("LR 1-7"), ("LR", 1, 7))
        self.assertEqual(
            b._parse_mr_bucket_range("Legendary 1-8"), ("LR", 1, 8)
        )
        self.assertIsNone(b._parse_mr_bucket_range("no digits"))

    def test_mastery_select_options_within_discord_cap(self):
        first, second = self.bot_module._mastery_select_options()
        # Discord caps a select menu at 25 options.
        self.assertLessEqual(len(first), 25)
        self.assertLessEqual(len(second), 25)
        # 30 ranks + 8 legendary tiers across both menus.
        self.assertEqual(len(first) + len(second), 38)
        values = [o.value for o in first] + [o.value for o in second]
        self.assertIn("MR:1", values)
        self.assertIn("MR:30", values)
        self.assertIn("LR:1", values)
        self.assertIn("LR:8", values)
        # No duplicate values across the two menus.
        self.assertEqual(len(values), len(set(values)))

    def test_select_does_not_clobber_reserved_parent(self):
        """Regression: the mastery selects must keep their editor back-ref
        OFF discord.py's reserved ``Item._parent`` (used by
        ``Item._run_checks`` during interaction dispatch). Clobbering it
        with the View crashes every dropdown pick with
        ``AttributeError: '_MasteryEditorView' object has no attribute
        '_run_checks'``.
        """
        b = self.bot_module
        view = b._MasteryEditorView(
            member=Mock(), owner_id=123, avatar_bytes=None,
            display_name="Tenno",
        )
        selects = [c for c in view.children
                   if isinstance(c, b._MasterySelect)]
        self.assertEqual(len(selects), 2)
        for sel in selects:
            # Reserved attribute must stay at the discord.py default so
            # Item._run_checks doesn't recurse into the View.
            self.assertIsNone(sel._parent)
            # The editor back-reference the callback relies on is present.
            self.assertIs(sel._editor, view)

    def test_screenshot_verify_button_and_modal_construct(self):
        """The /profile screenshot-submission button + modal build cleanly,
        keep their back-refs off discord.py's reserved ``Item._parent``, and
        the modal carries a single file-upload component."""
        import discord
        b = self.bot_module
        btn = b._ScreenshotVerifyButton(
            member=Mock(), owner_id=123, avatar_bytes=None,
            display_name="Tenno",
        )
        # Reserved attribute untouched so dispatch doesn't recurse.
        self.assertIsNone(btn._parent)
        self.assertEqual(btn.label, "Verify Profile Data")
        self.assertEqual(btn.style, discord.ButtonStyle.danger)

        modal = b._ScreenshotVerifyModal(
            member=Mock(), owner_id=123, avatar_bytes=None,
            display_name="Tenno", source_view=None,
        )
        # The modal exposes a single FileUpload for the screenshot.
        self.assertIsInstance(modal.screenshot, discord.ui.FileUpload)
        self.assertEqual(modal.screenshot.max_values, 1)

    def test_mr_bucket_role_for_maps_rank_to_bucket(self):
        b = self.bot_module

        class _Role:
            def __init__(self, rid, name):
                self.id = rid
                self.name = name

        roles = {
            10: _Role(10, "MR 1-10"),
            15: _Role(15, "MR 11-15"),
            29: _Role(29, "MR 22-29"),
            30: _Role(30, "MR 30"),
            40: _Role(40, "LR 1-7"),
        }

        class _Guild:
            def get_role(self, rid):
                return roles.get(rid)

        original = b.MR_ROLE_IDS
        b.MR_ROLE_IDS = list(roles.keys())
        try:
            guild = _Guild()
            self.assertEqual(
                b._mr_bucket_role_for(guild, "MR", 28).name, "MR 22-29"
            )
            self.assertEqual(
                b._mr_bucket_role_for(guild, "MR", 30).name, "MR 30"
            )
            self.assertEqual(
                b._mr_bucket_role_for(guild, "LR", 3).name, "LR 1-7"
            )
            # LR 8 has no bucket (top role only covers LR 1-7).
            self.assertIsNone(b._mr_bucket_role_for(guild, "LR", 8))
        finally:
            b.MR_ROLE_IDS = original


if __name__ == "__main__":
    unittest.main()
