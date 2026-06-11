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

    def test_render_profile_card_png_with_titles(self):
        """Title chips render across empty / few / over-cap lists (the
        overflow folds into a "+N" chip) and a very long title is
        ellipsized rather than overflowing — all without raising."""
        base = [
            ("Clan", "Golden Tenno", None, (212, 168, 87)),
            ("Platform", "PlayStation", None),
            ("Mastery Rank", "28", None),
        ]
        title_cases = [
            [],
            ["boot licker"],
            ["boot licker", "Sharpshooter", "Fisher King"],
            # More than the visible cap exercises the "+N" overflow chip.
            [f"Title {i}" for i in range(10)],
            # A very long title must be ellipsized, not overflow the card.
            ["A ridiculously long achievement title " * 3],
        ]
        for titles in title_cases:
            info = base + [("Titles", titles)]
            png = self.bot_module._render_profile_card_png(
                avatar_bytes=None,
                display_name="Tenno",
                info_lines=info,
                in_game_name="Tenno#1234",
            )
            self.assertIsInstance(png, bytes)
            self.assertTrue(
                png.startswith(b"\x89PNG\r\n\x1a\n"),
                "expected PNG magic bytes",
            )

    def test_render_progress_card_png_returns_png(self):
        """_render_progress_card_png returns PNG bytes across the empty /
        partial / complete bar states and with/without info rows. (The
        verify pass card path; previously only the profile card had a
        render test.)"""
        info = [
            ("Clan", "Golden Tenno", None),
            ("Mastery Rank", "28", None),
            ("Profile", "Tenno#1234", None),
            ("Platform", "PC", None),
        ]
        cases = [
            dict(count=0, target=4, info_lines=None),
            dict(count=2, target=4, info_lines=info),
            dict(count=4, target=4, info_lines=info),
            dict(count=0, target=0, info_lines=None),  # no categories configured
        ]
        for kw in cases:
            png = self.bot_module._render_progress_card_png(
                avatar_bytes=None,
                display_name="Tenno",
                **kw,
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

    def test_mastery_label_value(self):
        b = self.bot_module
        # Legendary ranks relabel and carry just the number/range.
        self.assertEqual(
            b._mastery_label_value("LR 3"), ("Legendary Rank", "3")
        )
        self.assertEqual(
            b._mastery_label_value("LR 1-7"), ("Legendary Rank", "1-7")
        )
        self.assertEqual(
            b._mastery_label_value("Legendary 3"), ("Legendary Rank", "3")
        )
        # Non-legendary ranks keep the Mastery Rank label.
        self.assertEqual(b._mastery_label_value("MR 28"), ("Mastery Rank", "28"))
        self.assertEqual(b._mastery_label_value("28"), ("Mastery Rank", "28"))
        self.assertEqual(b._mastery_label_value(None), ("Mastery Rank", ""))
        self.assertEqual(
            b._mastery_label_value("\u2014"), ("Mastery Rank", "\u2014")
        )

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


class MemberProfileInfoLinesTests(unittest.TestCase):
    """Mastery Rank row precedence in _member_profile_info_lines."""

    def setUp(self):
        import bot as bot_module
        self.b = bot_module

    def _mastery_row(self, *, role_names, mr_role_ids, stored):
        """Build a fake member holding ``role_names`` and run the gatherer
        with analytics + emoji fetch patched, returning the Mastery Rank
        ``(label, value)`` the card would render."""
        import asyncio
        b = self.b

        class _Role:
            def __init__(self, rid, name):
                self.id = rid
                self.name = name
                self.color = Mock(value=0)

        roles = [_Role(1000 + i, n) for i, n in enumerate(role_names)]

        class _Guild:
            id = 7

            def get_role(self, rid):
                return next((r for r in roles if r.id == rid), None)

        member = Mock()
        member.roles = roles
        member.id = 5
        member.guild = _Guild()

        async def _fake_fetch(_literal):
            return None

        orig_mr = b.MR_ROLE_IDS
        orig_clan = b.CLAN_SLOTS
        orig_plat = b.PLATFORM_ROLE_IDS
        orig_syn = b.SYNDICATE_ROLE_IDS
        orig_fetch = b._fetch_emoji_bytes
        orig_get = b.analytics.get_member_profile
        orig_titles = b.analytics.list_member_titles
        # Map the configured MR ids onto the fake roles by name order.
        b.MR_ROLE_IDS = [
            1000 + role_names.index(n) for n in role_names
            if n in mr_role_ids
        ]
        b.CLAN_SLOTS = []
        b.PLATFORM_ROLE_IDS = {}
        b.SYNDICATE_ROLE_IDS = []
        b._fetch_emoji_bytes = _fake_fetch
        b.analytics.get_member_profile = lambda *a, **k: (
            {"mastery_rank": stored} if stored else None
        )
        b.analytics.list_member_titles = lambda *a, **k: []
        try:
            rows = asyncio.run(b._member_profile_info_lines(member))
        finally:
            b.MR_ROLE_IDS = orig_mr
            b.CLAN_SLOTS = orig_clan
            b.PLATFORM_ROLE_IDS = orig_plat
            b.SYNDICATE_ROLE_IDS = orig_syn
            b._fetch_emoji_bytes = orig_fetch
            b.analytics.get_member_profile = orig_get
            b.analytics.list_member_titles = orig_titles

        mr = next((r for r in rows if r[0] == "Mastery Rank"), None)
        self.assertIsNotNone(mr, "Mastery Rank row missing")
        return self.b._mastery_label_value(mr[1])

    def test_legendary_role_overrides_lower_stored_rank(self):
        # Member holds the "Legendary 1-7" bucket but stored rank is "MR 1"
        # (stale OCR). The legendary role should win -> "Legendary Rank".
        label, value = self._mastery_row(
            role_names=["MR 1-10", "Legendary 1-7"],
            mr_role_ids=["MR 1-10", "Legendary 1-7"],
            stored="MR 1",
        )
        self.assertEqual(label, "Legendary Rank")
        self.assertEqual(value, "1-7")

    def test_legendary_stored_rank_kept_over_role(self):
        # An exact stored LR rank stays precise rather than collapsing to
        # the coarse bucket range.
        label, value = self._mastery_row(
            role_names=["Legendary 1-7"],
            mr_role_ids=["Legendary 1-7"],
            stored="LR 3",
        )
        self.assertEqual(label, "Legendary Rank")
        self.assertEqual(value, "3")

    def test_non_legendary_uses_stored_rank(self):
        # No legendary role -> the exact stored MR wins as before.
        label, value = self._mastery_row(
            role_names=["MR 22-29"],
            mr_role_ids=["MR 22-29"],
            stored="MR 28",
        )
        self.assertEqual(label, "Mastery Rank")
        self.assertEqual(value, "28")


class ManagePanelTests(unittest.TestCase):
    """Tests for the /manage admin backup console (styled after /status)."""

    def setUp(self):
        import bot as bot_module
        self.b = bot_module

    @staticmethod
    def _container(components):
        # Header text (type 10) + container (type 17).
        return next(c for c in components if c["type"] == 17)

    @staticmethod
    def _buttons(container):
        out = []
        for comp in container["components"]:
            if comp.get("type") == 1:
                out.extend(comp["components"])
        return out

    def test_overview_page_lists_stored_profile(self):
        snap = {
            "profile": {
                "in_game_name": "Tenno", "mastery_rank": "MR 28",
                "platform": "PC", "clan": "Golden Pagoda",
                "last_verified_ts": 1000,
            },
            "titles": [{"title": "boot licker", "awarded_ts": 1}],
        }
        member = Mock(display_name="Tenno")
        comps = self.b._manage_components(5, member, 0, snap)
        # Header is a top-level text component.
        self.assertEqual(comps[0]["type"], 10)
        self.assertIn("Manage", comps[0]["content"])
        body = self._container(comps)["components"][0]["content"]
        self.assertIn("Tenno", body)
        self.assertIn("MR 28", body)
        self.assertIn("Titles: `1`", body)

    def test_data_page_shows_clear_button(self):
        snap = {"profile": {"in_game_name": "X"}, "titles": []}
        comps = self.b._manage_components(5, Mock(display_name="X"), 2, snap)
        buttons = self._buttons(self._container(comps))
        clear = next(
            (b for b in buttons if b.get("custom_id") == "manage:5:clear"),
            None,
        )
        self.assertIsNotNone(clear, "Clear button missing")
        self.assertEqual(clear["style"], 4)  # danger

    def test_data_page_no_clear_button_when_empty(self):
        snap = {"profile": None, "titles": []}
        comps = self.b._manage_components(5, Mock(display_name="X"), 2, snap)
        buttons = self._buttons(self._container(comps))
        self.assertFalse(
            any(b.get("custom_id", "").endswith(":clear") for b in buttons)
        )

    def test_confirm_clear_uses_fail_accent_and_confirm_buttons(self):
        snap = {"profile": {"in_game_name": "X"}, "titles": []}
        comps = self.b._manage_components(
            5, Mock(display_name="X"), 2, snap, confirm_clear=True
        )
        container = self._container(comps)
        self.assertEqual(container["accent_color"], self.b.ACCENT_FAIL)
        ids = {b.get("custom_id") for b in self._buttons(container)}
        self.assertIn("manage:5:clearok", ids)
        self.assertIn("manage:5:p:2", ids)  # Cancel -> back to data page

    def test_cleared_state_reports_counts_and_drops_clear(self):
        snap = {"profile": None, "titles": []}
        cleared = {"profiles": 1, "titles": 2, "events_anonymized": 3}
        comps = self.b._manage_components(
            5, Mock(display_name="X"), 2, snap, cleared=cleared
        )
        container = self._container(comps)
        body = container["components"][0]["content"]
        self.assertIn("Cleared", body)
        ids = {b.get("custom_id") for b in self._buttons(container)}
        self.assertNotIn("manage:5:clearok", ids)
        self.assertNotIn("manage:5:clear", ids)

    def test_departed_member_falls_back_to_stored_name(self):
        snap = {"profile": {"in_game_name": "GhostTenno"}, "titles": []}
        comps = self.b._manage_components(5, None, 0, snap)
        body = self._container(comps)["components"][0]["content"]
        self.assertIn("not in server", body)

    def test_nav_row_prev_disabled_on_first_page(self):
        row = self.b._manage_nav_row(5, 0)
        prev = row["components"][0]
        self.assertTrue(prev["disabled"])
        nxt = row["components"][2]
        self.assertEqual(nxt["custom_id"], "manage:5:p:1")

    def test_overview_page_offers_screenshot_update_for_present_member(self):
        snap = {"profile": {"in_game_name": "Tenno"}, "titles": []}
        comps = self.b._manage_components(5, Mock(display_name="Tenno"), 0, snap)
        ids = {b.get("custom_id") for b in self._buttons(self._container(comps))}
        self.assertIn("manage:5:update", ids)

    def test_overview_page_hides_screenshot_update_for_departed_member(self):
        snap = {"profile": {"in_game_name": "GhostTenno"}, "titles": []}
        comps = self.b._manage_components(5, None, 0, snap)
        ids = {b.get("custom_id") for b in self._buttons(self._container(comps))}
        self.assertNotIn("manage:5:update", ids)

    def test_manage_screenshot_modal_constructs(self):
        """The /manage admin screenshot modal builds cleanly and carries a
        single file-upload component for the target member's screenshot."""
        import discord
        modal = self.b._ManageScreenshotModal(member=Mock(), admin_id=123)
        self.assertIsInstance(modal.screenshot, discord.ui.FileUpload)
        self.assertEqual(modal.screenshot.max_values, 1)
        self.assertEqual(modal._gp_admin_id, 123)


class OnLeaveClearTests(unittest.TestCase):
    """Tests for the autonomous on-leave data clear."""

    def setUp(self):
        import bot as bot_module
        self.b = bot_module

    def test_clear_helper_calls_delete_scoped(self):
        import asyncio
        captured = {}

        def _fake_delete(*, guild_id, user_id):
            captured["guild_id"] = guild_id
            captured["user_id"] = user_id
            return {"profiles": 1, "titles": 0, "events_anonymized": 2}

        orig = self.b.analytics.delete_member_data
        self.b.analytics.delete_member_data = _fake_delete
        try:
            asyncio.run(
                self.b._clear_member_data_on_leave(2, 5, "Tenno")
            )
        finally:
            self.b.analytics.delete_member_data = orig
        self.assertEqual(captured, {"guild_id": 2, "user_id": 5})

    def test_clear_helper_is_exception_safe(self):
        import asyncio

        def _boom(*, guild_id, user_id):
            raise RuntimeError("db exploded")

        orig = self.b.analytics.delete_member_data
        self.b.analytics.delete_member_data = _boom
        try:
            # Must not propagate — a gateway event can't crash the bot.
            asyncio.run(
                self.b._clear_member_data_on_leave(2, 5, "Tenno")
            )
        finally:
            self.b.analytics.delete_member_data = orig

    def test_on_member_remove_spawns_clear(self):
        import asyncio
        captured = []

        def _fake_spawn(coro):
            captured.append(coro)
            coro.close()  # avoid "coroutine never awaited" warning

        orig = self.b._spawn_bg_task
        self.b._spawn_bg_task = _fake_spawn
        try:
            member = Mock()
            member.id = 5
            member.guild = Mock(id=2)
            member.display_name = "Tenno"
            asyncio.run(self.b.on_member_remove(member))
        finally:
            self.b._spawn_bg_task = orig
        self.assertEqual(len(captured), 1)


class CardTextHelperTests(unittest.TestCase):
    """Tests for the shared card-text helpers."""

    def setUp(self):
        import bot as bot_module
        self.b = bot_module
        self.font = self.b._load_font(16)
        from PIL import Image, ImageDraw
        self.draw = ImageDraw.Draw(Image.new("RGB", (10, 10)))

    def test_ellipsize_keeps_short_text(self):
        # Text that already fits is returned unchanged (no ellipsis).
        out = self.b._ellipsize(self.draw, "Hi", self.font, 10_000)
        self.assertEqual(out, "Hi")

    def test_ellipsize_truncates_with_ellipsis(self):
        long = "WidescreenTennoNameThatIsWayTooLong" * 4
        out = self.b._ellipsize(self.draw, long, self.font, 80)
        self.assertTrue(out.endswith("\u2026"))
        self.assertLess(len(out), len(long))
        self.assertLessEqual(self.draw.textlength(out, font=self.font), 80)

    def test_ellipsize_handles_empty(self):
        self.assertEqual(self.b._ellipsize(self.draw, "", self.font, 50), "")


class RadialGradientTests(unittest.TestCase):
    """Tests for the smooth numpy radial-glow backdrop helper."""

    def setUp(self):
        import bot as bot_module
        self.b = bot_module

    def test_returns_rgba_of_requested_size(self):
        img = self.b._radial_gradient(
            200, 120, center=(0, 0), radius=150,
            color=(212, 168, 87), inner_alpha=40,
        )
        self.assertEqual(img.mode, "RGBA")
        self.assertEqual(img.size, (200, 120))

    def test_alpha_peaks_at_center_and_fades_out(self):
        # Brightest at the glow centre, fully transparent past the radius.
        img = self.b._radial_gradient(
            120, 120, center=(60, 60), radius=40,
            color=(212, 168, 87), inner_alpha=200,
        )
        px = img.load()
        self.assertGreater(px[60, 60][3], px[5, 5][3])
        self.assertEqual(px[5, 5][3], 0)  # corner lies beyond the radius

    def test_inner_alpha_caps_opacity(self):
        # The painted glow never exceeds the requested inner alpha.
        img = self.b._radial_gradient(
            80, 80, center=(40, 40), radius=60,
            color=(93, 208, 243), inner_alpha=50,
        )
        self.assertLessEqual(img.split()[3].getextrema()[1], 50)

    def test_is_deterministic(self):
        a = self.b._radial_gradient(
            96, 64, center=(10, 10), radius=80,
            color=(212, 168, 87), inner_alpha=30,
        )
        b = self.b._radial_gradient(
            96, 64, center=(10, 10), radius=80,
            color=(212, 168, 87), inner_alpha=30,
        )
        self.assertEqual(a.tobytes(), b.tobytes())


class VignetteTests(unittest.TestCase):
    """Tests for the framing vignette helper."""

    def setUp(self):
        import bot as bot_module
        self.b = bot_module

    def test_returns_rgba_of_requested_size(self):
        img = self.b._vignette(200, 120, strength=80)
        self.assertEqual(img.mode, "RGBA")
        self.assertEqual(img.size, (200, 120))

    def test_center_transparent_corners_dark(self):
        img = self.b._vignette(120, 120, strength=90)
        px = img.load()
        self.assertEqual(px[60, 60][3], 0)        # centre untouched
        self.assertGreater(px[0, 0][3], 0)        # corner darkened
        self.assertLessEqual(px[0, 0][3], 90)     # capped at strength
        self.assertEqual(px[0, 0][:3], (0, 0, 0))  # darkens with black ink


class CardBackdropTests(unittest.TestCase):
    """Tests for the shared, smooth card backdrop (no scatter/lotus)."""

    def setUp(self):
        import bot as bot_module
        self.b = bot_module

    def test_returns_opaque_rgba_of_requested_size(self):
        img = self.b._card_backdrop(300, 160)
        self.assertEqual(img.mode, "RGBA")
        self.assertEqual(img.size, (300, 160))
        # Fully opaque — the rounded-corner mask is applied by the caller.
        self.assertEqual(img.split()[3].getextrema(), (255, 255))

    def test_is_deterministic(self):
        a = self.b._card_backdrop(220, 120)
        b = self.b._card_backdrop(220, 120)
        self.assertEqual(a.tobytes(), b.tobytes())

    def test_returns_distinct_copies(self):
        # Each call must hand back a fresh object so a caller compositing
        # onto the backdrop can never corrupt the memoised instance.
        a = self.b._card_backdrop(180, 100)
        b = self.b._card_backdrop(180, 100)
        self.assertIsNot(a, b)
        a.paste((255, 0, 0, 255), (0, 0, a.width, a.height))
        # Mutating one copy leaves the next call's backdrop untouched.
        c = self.b._card_backdrop(180, 100)
        self.assertEqual(b.tobytes(), c.tobytes())

    def test_build_is_memoised(self):
        # Repeated same-size requests hit the LRU instead of rebuilding.
        self.b._card_backdrop_cached.cache_clear()
        self.b._card_backdrop(200, 110)
        self.b._card_backdrop(200, 110)
        self.b._card_backdrop(200, 110)
        info = self.b._card_backdrop_cached.cache_info()
        self.assertGreaterEqual(info.hits, 2)
        self.assertEqual(info.misses, 1)


class PagodaSilhouetteTests(unittest.TestCase):
    """Tests for the faint pagoda watermark layered into the backdrop."""

    def setUp(self):
        import bot as bot_module
        self.b = bot_module

    def test_returns_rgba_of_requested_size(self):
        img = self.b._pagoda_silhouette(
            120, 160, color=(200, 156, 102), alpha=15,
        )
        self.assertEqual(img.mode, "RGBA")
        self.assertEqual(img.size, (120, 160))

    def test_paints_some_pixels_but_stays_faint(self):
        # The silhouette must actually draw (non-empty alpha) yet never
        # exceed the requested faint cap.
        img = self.b._pagoda_silhouette(
            160, 220, color=(200, 156, 102), alpha=15,
        )
        lo, hi = img.split()[3].getextrema()
        self.assertGreater(hi, 0, "expected some silhouette pixels")
        self.assertLessEqual(hi, 15, "watermark must stay faint")

    def test_is_deterministic(self):
        a = self.b._pagoda_silhouette(
            96, 130, color=(200, 156, 102), alpha=12,
        )
        b = self.b._pagoda_silhouette(
            96, 130, color=(200, 156, 102), alpha=12,
        )
        self.assertEqual(a.tobytes(), b.tobytes())


class HeavyJobGateTests(unittest.TestCase):
    """The render/OCR gate must bound concurrency to protect the 512MB box."""

    def setUp(self):
        import bot as bot_module
        self.b = bot_module

    def test_run_heavy_bounds_concurrency_to_two(self):
        import asyncio

        async def _scenario():
            live = 0
            peak = 0
            lock = asyncio.Lock()

            def _job():
                # Pure-sync work runs in a worker thread via to_thread.
                return None

            async def _tracked():
                nonlocal live, peak
                async with self.b._HEAVY_JOB_SEMAPHORE:
                    async with lock:
                        live += 1
                        peak = max(peak, live)
                    await asyncio.sleep(0.02)
                    async with lock:
                        live -= 1

            # Fan out more jobs than the semaphore allows at once.
            await asyncio.gather(*[_tracked() for _ in range(6)])
            return peak

        peak = asyncio.run(_scenario())
        self.assertLessEqual(peak, 2)

    def test_run_heavy_returns_callable_result(self):
        import asyncio
        out = asyncio.run(self.b._run_heavy(lambda a, b: a + b, 2, 3))
        self.assertEqual(out, 5)


# ---------------------------------------------------------------------------
# Phase 0 characterization tests (regression oracle).
#
# These lock the CURRENT exact output shapes of the V2 component builders,
# the status/manage nav rows, the custom_id routing contract that
# on_interaction dispatches on, the .env rewrite skeleton, and the /profile
# access gates -- BEFORE any refactor touches them. They are intentionally
# strict so a behaviour-changing refactor fails loudly.
# ---------------------------------------------------------------------------


class IncompleteAndNickStructureTests(unittest.TestCase):
    """Exact V2 shape of the incomplete / nickname / call-sign builders."""

    def setUp(self):
        import bot as bot_module
        self.b = bot_module

    def test_incomplete_components_minimal_is_single_container(self):
        # No progress card, no image, no links -> just the gold-accent
        # incomplete container at the top level.
        result = self.b._incomplete_components(reason="Missing Platform role.")
        self.assertEqual(len(result), 1)
        container = result[0]
        self.assertEqual(container["type"], 17)
        self.assertEqual(container["accent_color"], self.b.ACCENT_INCOMPLETE)
        heading = container["components"][0]
        self.assertEqual(heading["type"], 10)
        self.assertIn("Please select the missing roles", heading["content"])
        self.assertIn("Missing Platform role.", heading["content"])

    def test_incomplete_components_with_card_puts_image_on_top(self):
        result = self.b._incomplete_components(
            reason="Missing Platform role.",
            progress_attachment="progress.png",
            link_buttons=[("Help", "https://discord.com/channels/1/2")],
        )
        # Top-level media gallery first, container last.
        self.assertEqual(result[0]["type"], 12)
        self.assertEqual(
            result[0]["items"][0]["media"]["url"],
            "attachment://progress.png",
        )
        container = result[-1]
        self.assertEqual(container["type"], 17)
        self.assertEqual(container["accent_color"], self.b.ACCENT_INCOMPLETE)
        # The Help link button lives inside the container as a style-5 button.
        link_buttons = [
            btn
            for child in container["components"]
            if child.get("type") == 1
            for btn in child.get("components", [])
            if btn.get("style") == 5
        ]
        self.assertTrue(link_buttons, "expected a link button in the container")
        self.assertEqual(
            link_buttons[0]["url"], "https://discord.com/channels/1/2"
        )

    def test_nickname_prompt_components_structure(self):
        result = self.b._nickname_prompt_components(
            "GoldenTenno", 4242, current_nick="oldnick",
        )
        self.assertEqual(len(result), 1)
        container = result[0]
        self.assertEqual(container["type"], 17)
        self.assertEqual(container["accent_color"], self.b._NICK_PROMPT_ACCENT)
        rows = [c for c in container["components"] if c.get("type") == 1]
        self.assertEqual(len(rows), 1, "expected exactly one action row")
        buttons = rows[0]["components"]
        self.assertLessEqual(len(buttons), 5)
        self.assertTrue(
            all(str(b["custom_id"]).startswith("nick:") for b in buttons)
        )

    def test_callsign_buttons_empty_without_suggestion(self):
        caption, buttons = self.b._callsign_buttons(None, None, "")
        self.assertEqual(caption, [])
        self.assertEqual(buttons, [])

    def test_callsign_buttons_yes_no_custom_id_prefixes(self):
        caption, buttons = self.b._callsign_buttons("GoldenTenno", 4242, "old")
        self.assertTrue(caption, "expected a caption when a name is offered")
        self.assertEqual(len(buttons), 2)
        ids = [b["custom_id"] for b in buttons]
        # Order is (server-nick "no", in-game "yes").
        self.assertTrue(ids[0].startswith("nick:n:4242:"))
        self.assertTrue(ids[1].startswith("nick:y:4242:"))


class StatusNavContractTests(unittest.TestCase):
    """The /status nav row + outer container contract (guards on_interaction
    routing + the shared-pagination refactor)."""

    def setUp(self):
        import bot as bot_module
        self.b = bot_module
        self._orig_client = getattr(bot_module, "client", None)
        mock_client = Mock()
        mock_client.user = Mock(id=12345)
        mock_client.latency = 0.05
        mock_client.guilds = []
        bot_module.client = mock_client

    def tearDown(self):
        if self._orig_client is not None:
            self.b.client = self._orig_client

    def test_nav_row_all_custom_ids_use_status_prefix(self):
        for page in range(len(self.b._STATUS_PAGES)):
            row = self.b._status_nav_row(page)
            self.assertEqual(row["type"], 1)
            buttons = row["components"]
            self.assertEqual(len(buttons), 4)
            for btn in buttons:
                self.assertTrue(
                    str(btn["custom_id"]).startswith("status:"),
                    f"nav button custom_id must route to status: {btn}",
                )
            # Prev disabled on the first page, Next disabled on the last.
            self.assertEqual(buttons[0]["disabled"], page == 0)
            self.assertEqual(
                buttons[2]["disabled"], page >= len(self.b._STATUS_PAGES) - 1
            )
            self.assertEqual(
                buttons[1]["label"], f"{page + 1}/{len(self.b._STATUS_PAGES)}"
            )

    def test_status_components_outer_shape(self):
        mock_interaction = Mock(spec=discord.Interaction)
        mock_interaction.guild = None
        for page in (0, 1):  # bot (builder) + roles (inline, no builder)
            result = self.b._status_components(mock_interaction, page)
            self.assertEqual(len(result), 2)
            self.assertEqual(result[0]["type"], 10)
            self.assertTrue(result[0]["content"].startswith("### "))
            container = result[1]
            self.assertEqual(container["type"], 17)
            self.assertEqual(container["accent_color"], self.b.ACCENT_PASS)
            # The nav row is the LAST child of the container.
            self.assertEqual(container["components"][-1]["type"], 1)

    def test_status_page_clamp_is_stable(self):
        # Out-of-range pages clamp to the first / last page; the snapshot
        # predicate (same clamp logic) is the cheapest deterministic probe.
        self.assertEqual(
            self.b._status_page_needs_snapshot(-5),
            self.b._status_page_needs_snapshot(0),
        )
        self.assertEqual(
            self.b._status_page_needs_snapshot(9999),
            self.b._status_page_needs_snapshot(len(self.b._STATUS_PAGES) - 1),
        )


class ManageComponentsCharacterizationTests(unittest.TestCase):
    """Exact V2 shape of every /manage page + its action buttons."""

    def setUp(self):
        import bot as bot_module
        self.b = bot_module
        self.member = Mock()
        self.member.display_name = "TestUser"
        self.snap = {
            "profile": {
                "in_game_name": "GoldenTenno",
                "mastery_rank": "MR 28",
                "platform": "PC",
                "clan": "Golden Tenno",
                "last_verified_ts": 1700000000,
            },
            "titles": [
                {"title": "boot licker", "reason": "",
                 "awarded_ts": 1700000000},
            ],
        }

    def _ids(self, result):
        return [
            btn.get("custom_id")
            for child in result[1]["components"]
            if child.get("type") == 1
            for btn in child.get("components", [])
        ]

    def test_outer_shape_and_header(self):
        result = self.b._manage_components(42, self.member, 0, self.snap)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["type"], 10)
        self.assertTrue(result[0]["content"].startswith("### "))
        self.assertEqual(result[1]["type"], 17)
        self.assertEqual(result[1]["accent_color"], self.b.ACCENT_PASS)

    def test_overview_has_update_button_when_member_present(self):
        result = self.b._manage_components(42, self.member, 0, self.snap)
        self.assertIn("manage:42:update", self._ids(result))

    def test_overview_omits_update_button_when_member_absent(self):
        result = self.b._manage_components(42, None, 0, self.snap)
        self.assertNotIn("manage:42:update", self._ids(result))

    def test_data_page_default_offers_clear(self):
        result = self.b._manage_components(42, self.member, 2, self.snap)
        self.assertIn("manage:42:clear", self._ids(result))

    def test_data_page_confirm_is_fail_accent_with_confirm_cancel(self):
        result = self.b._manage_components(
            42, self.member, 2, self.snap, confirm_clear=True,
        )
        self.assertEqual(result[1]["accent_color"], self.b.ACCENT_FAIL)
        ids = self._ids(result)
        self.assertIn("manage:42:clearok", ids)
        self.assertIn("manage:42:p:2", ids)  # Cancel returns to the data page.

    def test_data_page_cleared_shows_no_action_buttons(self):
        cleared = {"profiles": 1, "titles": 1, "events_anonymized": 3}
        result = self.b._manage_components(
            42, self.member, 2, self.snap, cleared=cleared,
        )
        ids = self._ids(result)
        self.assertNotIn("manage:42:clear", ids)
        self.assertNotIn("manage:42:clearok", ids)

    def test_data_page_empty_store_has_no_clear(self):
        empty = {"profile": None, "titles": []}
        result = self.b._manage_components(42, self.member, 2, empty)
        self.assertNotIn("manage:42:clear", self._ids(result))

    def test_all_nav_custom_ids_use_manage_prefix(self):
        for page in range(len(self.b._MANAGE_PAGES)):
            row = self.b._manage_nav_row(7, page)
            self.assertEqual(row["type"], 1)
            for btn in row["components"]:
                self.assertTrue(
                    str(btn["custom_id"]).startswith("manage:"),
                    f"nav button must route to manage: {btn}",
                )

    def test_page_index_clamps(self):
        low = self.b._manage_components(42, self.member, -3, self.snap)
        high = self.b._manage_components(42, self.member, 99, self.snap)
        self.assertIn("Overview", low[0]["content"])
        self.assertIn("Data", high[0]["content"])


class EnvRewriteRoundtripTests(unittest.TestCase):
    """The shared .env read->replace->append skeleton (Phase 4 moves this to
    envstore.py; the roundtrip locks its behaviour first)."""

    def setUp(self):
        import bot as bot_module
        self.b = bot_module
        import tempfile
        import pathlib
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.env_path = pathlib.Path(self._dir.name) / "config.env"
        self._orig = self.b.ENV_FILE_PATH
        self.b.ENV_FILE_PATH = self.env_path

    def tearDown(self):
        self.b.ENV_FILE_PATH = self._orig

    def test_returns_false_when_file_missing(self):
        self.assertFalse(
            self.b._rewrite_env_file(lambda line: None, lambda: [])
        )

    def test_replace_existing_line(self):
        self.env_path.write_text("A=1\nB=2\n")
        ok = self.b._rewrite_env_file(
            lambda line: "A=9" if line == "A=1" else None,
            lambda: [],
        )
        self.assertTrue(ok)
        self.assertEqual(self.env_path.read_text(), "A=9\nB=2\n")

    def test_append_missing_line_after_blank_separator(self):
        self.env_path.write_text("A=1\n")
        ok = self.b._rewrite_env_file(lambda line: None, lambda: ["C=3"])
        self.assertTrue(ok)
        self.assertEqual(self.env_path.read_text(), "A=1\n\nC=3\n")

    def test_atomic_write_leaves_no_tmp_file(self):
        self.env_path.write_text("A=1\n")
        self.b._rewrite_env_file(lambda line: None, lambda: ["C=3"])
        tmp = self.env_path.with_suffix(self.env_path.suffix + ".tmp")
        self.assertFalse(tmp.exists(), "atomic write must remove the .tmp file")

    def test_update_env_id_list_replaces_then_appends(self):
        self.env_path.write_text("FOO_IDS=9\nBAR=1\n")
        self.assertTrue(self.b._update_env_id_list("FOO_IDS", [1, 2, 3]))
        self.assertEqual(
            self.env_path.read_text(), "FOO_IDS=1,2,3\nBAR=1\n"
        )
        # Missing key path appends after a blank separator.
        self.env_path.write_text("BAR=1\n")
        self.assertTrue(self.b._update_env_id_list("NEW_IDS", [4, 5]))
        self.assertEqual(self.env_path.read_text(), "BAR=1\n\nNEW_IDS=4,5\n")


class ProfileAccessGateTests(unittest.TestCase):
    """Truth table for the two /profile gates (Phase 2 merges them into one
    _can_use_command helper; this locks the current semantics)."""

    def setUp(self):
        import bot as bot_module
        self.b = bot_module

    def _member(self, *, manage_guild, role_ids):
        m = Mock()
        m.guild_permissions = Mock(manage_guild=manage_guild)
        m.roles = [Mock(id=r) for r in role_ids]
        return m

    def test_profile_open_when_no_access_role_configured(self):
        with patch.object(self.b, "PROFILE_ACCESS_ROLE_ID", 0):
            self.assertTrue(
                self.b._can_use_profile(
                    self._member(manage_guild=False, role_ids=[])
                )
            )

    def test_profile_requires_access_role_or_manager(self):
        with patch.object(self.b, "PROFILE_ACCESS_ROLE_ID", 555):
            self.assertTrue(  # manager always allowed
                self.b._can_use_profile(
                    self._member(manage_guild=True, role_ids=[])
                )
            )
            self.assertTrue(  # has the access role
                self.b._can_use_profile(
                    self._member(manage_guild=False, role_ids=[555])
                )
            )
            self.assertFalse(  # neither
                self.b._can_use_profile(
                    self._member(manage_guild=False, role_ids=[1, 2])
                )
            )

    def test_profile_options_require_options_role_or_manager(self):
        with patch.object(self.b, "PROFILE_OPTIONS_ROLE_IDS", [777]):
            self.assertTrue(
                self.b._can_use_profile_options(
                    self._member(manage_guild=True, role_ids=[])
                )
            )
            self.assertTrue(
                self.b._can_use_profile_options(
                    self._member(manage_guild=False, role_ids=[777])
                )
            )
            self.assertFalse(
                self.b._can_use_profile_options(
                    self._member(manage_guild=False, role_ids=[1])
                )
            )


class BuildPassInfoLinesTests(unittest.TestCase):
    """The pass-card info-row builder must keep the documented row-major
    order (Clan | Mastery / Profile | Platform, then Missing Data) and omit
    fields the member hasn't earned. _fetch_emoji_bytes is patched out so
    the test never touches the network."""

    def setUp(self):
        import bot as bot_module
        self.b = bot_module

    def _run(self, **kw):
        import asyncio

        async def _fake_fetch(literal):
            return None

        defaults = dict(
            clan_name=None, clan_emoji=None, member_platform=None,
            mastery_rank=None, profile_name=None, pass_missing=[],
        )
        defaults.update(kw)
        with patch.object(self.b, "_fetch_emoji_bytes", _fake_fetch):
            return asyncio.run(self.b._build_pass_info_lines(**defaults))

    def test_full_row_order(self):
        rows = self._run(
            clan_name="Golden Tenno", clan_emoji="<:c:1>",
            member_platform="PC", mastery_rank="MR 28",
            profile_name="Tenno#1234", pass_missing=["Syndicate"],
        )
        self.assertEqual(
            [r[0] for r in rows],
            ["Clan", "Mastery Rank", "Profile", "Platform", "Missing Data"],
        )

    def test_absent_fields_are_omitted(self):
        rows = self._run(clan_name="Golden Tenno")
        self.assertEqual([r[0] for r in rows], ["Clan"])

    def test_empty_inputs_give_no_rows(self):
        self.assertEqual(self._run(), [])

    def test_tenno_fallback_name_kept_verbatim(self):
        # The synthetic "Tenno #NNN" fallback keeps its suffix verbatim;
        # real handles are clan-tag stripped.
        rows = self._run(profile_name="Tenno #465")
        profile = [r for r in rows if r[0] == "Profile"][0]
        self.assertEqual(profile[1], "Tenno #465")


if __name__ == "__main__":
    unittest.main()

