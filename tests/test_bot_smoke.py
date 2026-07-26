"""Smoke tests for bot.py component builders.

These tests ensure that every component-builder helper can be called with
the kwargs that each production call site uses. They don't verify output
correctness (that's the unit-test layer); they exist purely to catch
signature-drift bugs at test time instead of runtime.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

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
        # Per-rank role names parse as a single-value range (lo == hi).
        self.assertEqual(b._parse_mr_bucket_range("MR 28"), ("MR", 28, 28))
        self.assertEqual(b._parse_mr_bucket_range("MR 1"), ("MR", 1, 1))
        self.assertEqual(
            b._parse_mr_bucket_range("Legendary 3"), ("LR", 3, 3)
        )
        self.assertIsNone(b._parse_mr_bucket_range("no digits"))

    def test_mr_bucket_role_for_matches_individual_ranks(self):
        """Per-rank role names (MR 28 / Legendary 3) resolve exactly."""
        b = self.bot_module

        class _Role:
            def __init__(self, rid, name):
                self.id = rid
                self.name = name

        roles = {n: _Role(n, f"MR {n}") for n in range(1, 31)}
        roles.update({100 + n: _Role(100 + n, f"Legendary {n}") for n in range(1, 9)})

        class _Guild:
            def get_role(self, rid):
                return roles.get(rid)

        original = b.MR_ROLE_IDS
        b.MR_ROLE_IDS = list(roles.keys())
        try:
            guild = _Guild()
            self.assertEqual(b._mr_bucket_role_for(guild, "MR", 28).name, "MR 28")
            self.assertEqual(b._mr_bucket_role_for(guild, "MR", 1).name, "MR 1")
            self.assertEqual(b._mr_bucket_role_for(guild, "MR", 30).name, "MR 30")
            self.assertEqual(
                b._mr_bucket_role_for(guild, "LR", 3).name, "Legendary 3"
            )
            self.assertEqual(
                b._mr_bucket_role_for(guild, "LR", 8).name, "Legendary 8"
            )
            # A rank with no configured role returns None (safe no-op upstream).
            self.assertIsNone(b._mr_bucket_role_for(guild, "MR", 31))
        finally:
            b.MR_ROLE_IDS = original

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

    def test_member_is_verified(self):
        """``_member_is_verified`` is True only when the member holds one of
        the configured verified-role IDs."""
        b = self.bot_module

        class _Role:
            def __init__(self, rid):
                self.id = rid

        class _Member:
            def __init__(self, role_ids):
                self.roles = [_Role(r) for r in role_ids]

        verified_id = b._VERIFIED_ROLE_IDS[0]
        self.assertTrue(b._member_is_verified(_Member([verified_id, 999])))
        self.assertFalse(b._member_is_verified(_Member([999, 1000])))
        self.assertFalse(b._member_is_verified(_Member([])))

    def test_sync_profile_action_items_hides_verify_for_verified(self):
        """The /profile "Verify Profile Data" button is suppressed for members
        who already hold the verified role — verification prompts only make
        sense while a member is still onboarding. Unverified members without an
        in-game name still get it."""
        import discord
        b = self.bot_module

        class _Role:
            def __init__(self, rid):
                self.id = rid

        class _Member:
            def __init__(self, role_ids):
                self.roles = [_Role(r) for r in role_ids]
                self.guild = Mock()

        verified_id = b._VERIFIED_ROLE_IDS[0]

        def has_verify(view):
            return any(
                isinstance(c, b._ScreenshotVerifyButton)
                for c in view.children
            )

        def build(role_ids, in_game_name):
            view = discord.ui.View()
            b._sync_profile_action_items(
                view, member=_Member(role_ids), owner_id=1,
                avatar_bytes=None, display_name="Tenno", info=[],
                in_game_name=in_game_name,
            )
            return view

        # Verified member, no in-game name -> NO verify button.
        self.assertFalse(has_verify(build([verified_id], None)))
        # Unverified member, no in-game name -> verify button present.
        self.assertTrue(has_verify(build([999], None)))
        # Any member WITH an in-game name -> no verify button.
        self.assertFalse(has_verify(build([999], "Tenno#123")))

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
        orig_get = b._member_profile_from_records
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

        async def _fake_profile(*a, **k):
            return {"mastery_rank": stored} if stored else None

        b._member_profile_from_records = _fake_profile
        b.analytics.list_member_titles = lambda *a, **k: []
        try:
            rows = asyncio.run(b._member_profile_info_lines(member))
        finally:
            b.MR_ROLE_IDS = orig_mr
            b.CLAN_SLOTS = orig_clan
            b.PLATFORM_ROLE_IDS = orig_plat
            b.SYNDICATE_ROLE_IDS = orig_syn
            b._fetch_emoji_bytes = orig_fetch
            b._member_profile_from_records = orig_get
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

    def test_pattern_fallback_when_mr_role_ids_empty(self):
        # MR_ROLE_IDS unresolved (empty, e.g. the server's role names don't
        # match MR_ROLE_NAMES) but the member holds a role named like a
        # mastery bucket -> the name-pattern fallback still surfaces it so
        # the card isn't missing the Mastery Rank row.
        label, value = self._mastery_row(
            role_names=["MR 16-21"],
            mr_role_ids=[],
            stored=None,
        )
        self.assertEqual(label, "Mastery Rank")
        self.assertEqual(value, "16-21")

    def test_pattern_fallback_legendary_when_ids_empty(self):
        # Same fallback recognises a Legendary bucket by name.
        label, value = self._mastery_row(
            role_names=["Legendary 1-7"],
            mr_role_ids=[],
            stored=None,
        )
        self.assertEqual(label, "Legendary Rank")
        self.assertEqual(value, "1-7")


class ProfileClanOverrideTests(unittest.TestCase):
    """A stored free-text ("not affiliated") clan surfaces as the Clan row
    on /profile when the member holds no configured clan role."""

    def setUp(self):
        import bot as bot_module
        self.b = bot_module

    def _clan_row(self, *, member_role_ids, clan_slots, stored_clan):
        import asyncio
        b = self.b

        class _Role:
            def __init__(self, rid, name):
                self.id = rid
                self.name = name
                self.color = Mock(value=0)

        roles = [_Role(rid, f"Role {rid}") for rid in member_role_ids]

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

        async def _fake_profile(*a, **k):
            return {"clan": stored_clan} if stored_clan else None

        orig = (
            b.MR_ROLE_IDS, b.CLAN_SLOTS, b.PLATFORM_ROLE_IDS,
            b.SYNDICATE_ROLE_IDS, b._fetch_emoji_bytes,
            b._member_profile_from_records, b.analytics.list_member_titles,
        )
        b.MR_ROLE_IDS = []
        b.CLAN_SLOTS = clan_slots
        b.PLATFORM_ROLE_IDS = {}
        b.SYNDICATE_ROLE_IDS = []
        b._fetch_emoji_bytes = _fake_fetch
        b._member_profile_from_records = _fake_profile
        b.analytics.list_member_titles = lambda *a, **k: []
        try:
            rows = asyncio.run(b._member_profile_info_lines(member))
        finally:
            (
                b.MR_ROLE_IDS, b.CLAN_SLOTS, b.PLATFORM_ROLE_IDS,
                b.SYNDICATE_ROLE_IDS, b._fetch_emoji_bytes,
                b._member_profile_from_records,
                b.analytics.list_member_titles,
            ) = orig
        return next((r for r in rows if r[0] == "Clan"), None)

    def _slot(self, no, name, role_id):
        from logic import ClanSlot
        return ClanSlot(slot=no, clan_name=name, role_id=role_id)

    def test_stored_free_text_clan_shown_without_clan_role(self):
        row = self._clan_row(
            member_role_ids=[],
            clan_slots=[self._slot(1, "Golden Pagoda", 1001)],
            stored_clan="Some Other Clan",
        )
        self.assertIsNotNone(row)
        self.assertEqual(row[1], "Some Other Clan")

    def test_configured_clan_role_wins_over_stored_override(self):
        row = self._clan_row(
            member_role_ids=[1001],
            clan_slots=[self._slot(1, "Golden Pagoda", 1001)],
            stored_clan="Some Other Clan",
        )
        self.assertIsNotNone(row)
        self.assertEqual(row[1], "Golden Pagoda")

    def test_stored_configured_name_without_role_is_dropped(self):
        # A configured clan name in the store is role-derived: dropping the
        # role must drop the Clan value too (em-dash), never resurrect it.
        row = self._clan_row(
            member_role_ids=[],
            clan_slots=[self._slot(1, "Golden Pagoda", 1001)],
            stored_clan="Golden Pagoda",
        )
        self.assertIsNotNone(row)
        self.assertEqual(row[1], "\u2014")

    def test_override_shown_even_without_configured_slots(self):
        row = self._clan_row(
            member_role_ids=[],
            clan_slots=[],
            stored_clan="Some Other Clan",
        )
        self.assertIsNotNone(row)
        self.assertEqual(row[1], "Some Other Clan")

    def test_no_row_when_nothing_configured_and_no_override(self):
        row = self._clan_row(
            member_role_ids=[], clan_slots=[], stored_clan=None,
        )
        self.assertIsNone(row)


class VerifyClanOverrideTests(unittest.TestCase):
    """_verify_clan_override: OCR'd non-configured clans become free-text
    clan overrides; configured clans stay role-derived."""

    def setUp(self):
        import bot as bot_module
        self.b = bot_module
        from logic import ClanSlot
        self._orig_slots = self.b.CLAN_SLOTS
        self.b.CLAN_SLOTS = [
            ClanSlot(slot=1, clan_name="Golden Pagoda", role_id=1001),
        ]

    def tearDown(self):
        self.b.CLAN_SLOTS = self._orig_slots

    def test_non_configured_clan_becomes_override(self):
        r = self.b._VerifyResult(
            ["Clan **Some Other Clan** isn't configured on this server."],
            "Player#1", "MR 9", clan_name="Some Other Clan",
        )
        self.assertEqual(self.b._verify_clan_override(r), "Some Other Clan")

    def test_configured_clan_returns_none(self):
        r = self.b._VerifyResult(
            ["Clan: ok"], "Player#1", "MR 9", clan_name="Golden Pagoda",
        )
        self.assertIsNone(self.b._verify_clan_override(r))

    def test_no_clan_returns_none(self):
        r = self.b._VerifyResult(["line"], "Player#1", "MR 9")
        self.assertIsNone(self.b._verify_clan_override(r))


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
        comps = self.b._manage_components(
            5, Mock(display_name="X"), self.b._MANAGE_DATA_PAGE, snap
        )
        buttons = self._buttons(self._container(comps))
        clear = next(
            (b for b in buttons if b.get("custom_id") == "manage:5:clear"),
            None,
        )
        self.assertIsNotNone(clear, "Clear button missing")
        self.assertEqual(clear["style"], 4)  # danger

    def test_data_page_no_clear_button_when_empty(self):
        snap = {"profile": None, "titles": []}
        comps = self.b._manage_components(
            5, Mock(display_name="X"), self.b._MANAGE_DATA_PAGE, snap
        )
        buttons = self._buttons(self._container(comps))
        self.assertFalse(
            any(b.get("custom_id", "").endswith(":clear") for b in buttons)
        )

    def test_confirm_clear_uses_fail_accent_and_confirm_buttons(self):
        snap = {"profile": {"in_game_name": "X"}, "titles": []}
        comps = self.b._manage_components(
            5, Mock(display_name="X"), self.b._MANAGE_DATA_PAGE, snap,
            confirm_clear=True,
        )
        container = self._container(comps)
        self.assertEqual(container["accent_color"], self.b.ACCENT_FAIL)
        ids = {b.get("custom_id") for b in self._buttons(container)}
        self.assertIn("manage:5:clearok", ids)
        # Cancel -> back to data page
        self.assertIn(f"manage:5:p:{self.b._MANAGE_DATA_PAGE}", ids)

    def test_cleared_state_reports_counts_and_drops_clear(self):
        snap = {"profile": None, "titles": []}
        cleared = {"titles": 2, "events_anonymized": 3, "onboarding": 0}
        comps = self.b._manage_components(
            5, Mock(display_name="X"), self.b._MANAGE_DATA_PAGE, snap,
            cleared=cleared,
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

    def test_overview_page_offers_onboarding_for_present_member(self):
        snap = {"profile": {"in_game_name": "Tenno"}, "titles": []}
        comps = self.b._manage_components(5, Mock(display_name="Tenno"), 0, snap)
        ids = {b.get("custom_id") for b in self._buttons(self._container(comps))}
        self.assertIn("manage:5:onboard", ids)

    def test_overview_page_hides_onboarding_for_departed_member(self):
        snap = {"profile": {"in_game_name": "GhostTenno"}, "titles": []}
        comps = self.b._manage_components(5, None, 0, snap)
        ids = {b.get("custom_id") for b in self._buttons(self._container(comps))}
        self.assertNotIn("manage:5:onboard", ids)

    def test_manage_screenshot_modal_constructs(self):
        """The /manage admin screenshot modal builds cleanly and carries a
        single file-upload component for the target member's screenshot."""
        import discord
        modal = self.b._ManageScreenshotModal(member=Mock(), admin_id=123)
        self.assertIsInstance(modal.screenshot, discord.ui.FileUpload)
        self.assertEqual(modal.screenshot.max_values, 1)
        self.assertEqual(modal._gp_admin_id, 123)

    def test_titles_modal_includes_member_select_without_member(self):
        """/titles run bare opens the full form: description text, action,
        member, title, reason."""
        import discord
        modal = self.b._TitlesModal()
        self.assertIsInstance(modal.member_select, discord.ui.UserSelect)
        self.assertEqual(len(modal.children), 5)

    def test_titles_modal_member_select_is_required(self):
        """min_values=1 with required=False is a contradiction Discord
        rejects with a 400 — the modal would silently never open."""
        modal = self.b._TitlesModal()
        self.assertTrue(modal.member_select.required)

    def test_titles_modal_prefills_member_select_with_member(self):
        """The /manage Titles button opens the same full native sheet with
        the panel's member pre-selected in the member select."""
        import discord
        modal = self.b._TitlesModal(member=discord.Object(id=7))
        self.assertIsInstance(modal.member_select, discord.ui.UserSelect)
        self.assertEqual(len(modal.children), 5)
        self.assertEqual([v.id for v in modal.member_select.default_values],
                         [7])

    def test_titles_modal_prefills_partial_args(self):
        modal = self.b._TitlesModal(
            action="remove", title_text="Champ", reason="won the event"
        )
        self.assertEqual(modal.title_input.default, "Champ")
        self.assertEqual(modal.reason_input.default, "won the event")
        defaults = {o.value: o.default for o in modal.action_select.options}
        self.assertTrue(defaults["remove"])
        self.assertFalse(defaults["add"])


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
            return {"titles": 0, "events_anonymized": 2, "onboarding": 0}

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
            self.b._watch_error_replies[5] = [9001]
            asyncio.run(self.b.on_member_remove(member))
        finally:
            self.b._spawn_bg_task = orig
        self.assertEqual(len(captured), 1)
        # Tracked fish-watch error replies are evicted (memory leak fix).
        self.assertNotIn(5, self.b._watch_error_replies)


class WatchPendingDeleteTests(unittest.TestCase):
    """Tests for the rejected-screenshot delayed-delete machinery."""

    def setUp(self):
        import bot as bot_module
        self.b = bot_module

    def test_reject_delete_window_is_five_minutes(self):
        self.assertEqual(self.b._WATCH_REJECT_DELETE_SECONDS, 300)

    def test_cancel_watch_pending_deletes_cancels_and_clears(self):
        task = Mock()
        self.b._watch_pending_deletes[123] = task
        try:
            self.b._cancel_watch_pending_deletes()
        finally:
            self.b._watch_pending_deletes.pop(123, None)
        task.cancel.assert_called_once()
        self.assertEqual(self.b._watch_pending_deletes, {})

    def test_delete_watch_submission_later_deletes_and_untracks(self):
        import asyncio
        calls = []

        async def fake_delete(cid, mid):
            calls.append((cid, mid))

        orig_delete = self.b._delete_message
        orig_delay = self.b._WATCH_REJECT_DELETE_SECONDS
        self.b._delete_message = fake_delete
        self.b._WATCH_REJECT_DELETE_SECONDS = 0
        try:
            self.b._watch_pending_deletes[42] = Mock()
            asyncio.run(self.b._delete_watch_submission_later(7, 42))
        finally:
            self.b._delete_message = orig_delete
            self.b._WATCH_REJECT_DELETE_SECONDS = orig_delay
            self.b._watch_pending_deletes.pop(42, None)
        self.assertEqual(calls, [(7, 42)])
        self.assertNotIn(42, self.b._watch_pending_deletes)


class WatchDeletedSubmissionTests(unittest.TestCase):
    """Deleting a rejected submission also deletes its error reply."""

    def setUp(self):
        import bot as bot_module
        self.b = bot_module

    def tearDown(self):
        self.b._watch_submission_replies.clear()
        self.b._watch_error_replies.clear()
        self.b._watch_pending_deletes.clear()

    def test_raw_delete_removes_tracked_error_reply(self):
        import asyncio
        calls = []

        async def fake_delete(cid, mid):
            calls.append((cid, mid))

        orig_delete = self.b._delete_message
        self.b._delete_message = fake_delete
        try:
            self.b._watch_submission_replies[42] = (5, 9001)
            self.b._watch_error_replies[5] = [9001]
            pending = Mock()
            self.b._watch_pending_deletes[42] = pending
            payload = Mock(message_id=42, channel_id=7)
            asyncio.run(self.b.on_raw_message_delete(payload))
        finally:
            self.b._delete_message = orig_delete
        self.assertEqual(calls, [(7, 9001)])
        self.assertNotIn(42, self.b._watch_submission_replies)
        self.assertNotIn(5, self.b._watch_error_replies)
        self.assertNotIn(42, self.b._watch_pending_deletes)
        pending.cancel.assert_called_once()

    def test_raw_delete_ignores_untracked_message(self):
        import asyncio
        calls = []

        async def fake_delete(cid, mid):
            calls.append((cid, mid))

        orig_delete = self.b._delete_message
        self.b._delete_message = fake_delete
        try:
            payload = Mock(message_id=999, channel_id=7)
            asyncio.run(self.b.on_raw_message_delete(payload))
        finally:
            self.b._delete_message = orig_delete
        self.assertEqual(calls, [])

    def test_untrack_watch_error_reply_prunes_both_maps(self):
        self.b._watch_error_replies[5] = [9001, 9002]
        self.b._watch_submission_replies[42] = (5, 9001)
        self.b._watch_submission_replies[43] = (5, 9002)
        self.b._untrack_watch_error_reply(5, 9001)
        self.assertEqual(self.b._watch_error_replies[5], [9002])
        self.assertNotIn(42, self.b._watch_submission_replies)
        self.assertIn(43, self.b._watch_submission_replies)
        self.b._untrack_watch_error_reply(5, 9002)
        self.assertNotIn(5, self.b._watch_error_replies)
        self.assertEqual(self.b._watch_submission_replies, {})


class WatchChannelMatchTests(unittest.TestCase):
    """Thread/forum-aware detection of the watched channel."""

    def setUp(self):
        import bot as bot_module
        self.b = bot_module
        self.state = self.b._WATCH_STATE
        self.orig_channel_id = self.state.channel_id
        self.state.channel_id = 7

    def tearDown(self):
        self.state.channel_id = self.orig_channel_id

    def test_matches_watched_channel_itself(self):
        channel = SimpleNamespace(id=7, parent_id=None)
        self.assertTrue(self.b._watch_channel_matches(channel))

    def test_matches_thread_under_watched_channel(self):
        thread = SimpleNamespace(id=99, parent_id=7)
        self.assertTrue(self.b._watch_channel_matches(thread))

    def test_matches_forum_post_under_watched_forum(self):
        post = SimpleNamespace(id=123, parent_id=7)
        self.assertTrue(self.b._watch_channel_matches(post))

    def test_rejects_unrelated_channel(self):
        channel = SimpleNamespace(id=8, parent_id=None)
        self.assertFalse(self.b._watch_channel_matches(channel))

    def test_rejects_thread_under_other_channel(self):
        thread = SimpleNamespace(id=99, parent_id=8)
        self.assertFalse(self.b._watch_channel_matches(thread))

    def test_rejects_everything_when_no_channel_set(self):
        self.state.channel_id = 0
        channel = SimpleNamespace(id=0, parent_id=None)
        self.assertFalse(self.b._watch_channel_matches(channel))


class WatchAdminCodewordTests(unittest.TestCase):
    """An admin naming a known codeword sets it, like naming a fish."""

    def setUp(self):
        import bot as bot_module
        self.b = bot_module
        self.state = self.b._WATCH_STATE
        self.orig = (
            self.state.enabled, self.state.channel_id,
            self.state.codeword, set(self.state.admin_ids),
            self.state.current_fish,
        )
        self.state.enabled = True
        self.state.channel_id = 7
        self.state.codeword = ""
        self.state.admin_ids = {5}
        self.state.current_fish = None

    def tearDown(self):
        (self.state.enabled, self.state.channel_id, self.state.codeword,
         admin_ids, self.state.current_fish) = self.orig
        self.state.admin_ids = admin_ids

    def _run_message(self, content, channel=None, author_id=5):
        import asyncio
        message = Mock()
        message.author = Mock(bot=False, id=author_id)
        message.guild = Mock()
        message.channel = channel or SimpleNamespace(id=7, parent_id=None)
        message.attachments = []
        message.content = content
        message.add_reaction = AsyncMock()
        orig_persist = self.b._persist_watch_state
        orig_post = self.b._post_channel_v2
        self.b._persist_watch_state = lambda: None
        self.post_v2 = AsyncMock(return_value=None)
        self.b._post_channel_v2 = self.post_v2
        try:
            asyncio.run(self.b.on_message(message))
        finally:
            self.b._persist_watch_state = orig_persist
            self.b._post_channel_v2 = orig_post
        return message

    def test_admin_message_sets_codeword(self):
        message = self._run_message("codeword is dywatta citrus onion")
        self.assertEqual(self.state.codeword, "DYWATTA Citrus Onion")
        message.add_reaction.assert_awaited_once()

    def test_no_confirmation_message_posted(self):
        # The 🎣 reaction is the only acknowledgment — nothing is sent.
        self._run_message("codeword is dywatta citrus onion")
        self.post_v2.assert_not_awaited()

    def test_no_confirmation_for_fish_and_codeword_together(self):
        message = self._run_message("Norg — capybara pinocchio skibbibidy")
        self.assertEqual(self.state.current_fish, "Norg")
        self.assertEqual(self.state.codeword, "Capybara Pinocchio Skibbibidy")
        message.add_reaction.assert_awaited_once()
        self.post_v2.assert_not_awaited()

    def test_admin_message_in_thread_of_watched_channel_sets_codeword(self):
        thread = SimpleNamespace(id=99, parent_id=7)
        message = self._run_message(
            "codeword is dywatta citrus onion", channel=thread
        )
        self.assertEqual(self.state.codeword, "DYWATTA Citrus Onion")
        message.add_reaction.assert_awaited_once()

    def test_admin_message_sets_fish_and_codeword_together(self):
        self._run_message("Norg — capybara pinocchio skibbibidy")
        self.assertEqual(self.state.current_fish, "Norg")
        self.assertEqual(
            self.state.codeword, "Capybara Pinocchio Skibbibidy"
        )

    def test_admin_message_sets_codeword_while_watch_stopped(self):
        # Admin declarations are configuration — they apply even when the
        # watch is stopped (e.g. announcing the codeword before Start).
        self.state.enabled = False
        message = self._run_message("codeword is dywatta citrus onion")
        self.assertEqual(self.state.codeword, "DYWATTA Citrus Onion")
        message.add_reaction.assert_awaited_once()

    def test_admin_message_sets_fish_while_watch_stopped(self):
        self.state.enabled = False
        self._run_message("Norg")
        self.assertEqual(self.state.current_fish, "Norg")

    def test_non_admin_message_while_stopped_changes_nothing(self):
        self.state.enabled = False
        message = self._run_message("dywatta citrus onion", author_id=6)
        self.assertEqual(self.state.codeword, "")
        message.add_reaction.assert_not_awaited()

    def test_admin_message_without_known_phrases_changes_nothing(self):
        message = self._run_message("hello team")
        self.assertEqual(self.state.codeword, "")
        self.assertIsNone(self.state.current_fish)
        message.add_reaction.assert_not_awaited()
        self.post_v2.assert_not_awaited()


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

    def test_scenic_backdrop_is_opaque_and_distinct(self):
        # The /profile scenic variant (moon disc + mountains + pagoda +
        # reflection) stays a fully-opaque RGBA of the requested size and
        # differs from the plain verification backdrop.
        plain = self.b._card_backdrop(320, 200)
        scenic = self.b._card_backdrop(320, 200, scenic=True)
        self.assertEqual(scenic.mode, "RGBA")
        self.assertEqual(scenic.size, (320, 200))
        self.assertEqual(scenic.split()[3].getextrema(), (255, 255))
        self.assertNotEqual(
            plain.tobytes(), scenic.tobytes(),
            "scenic backdrop should differ from the plain one",
        )

    def test_scenic_backdrop_is_deterministic(self):
        a = self.b._card_backdrop(300, 180, scenic=True)
        b = self.b._card_backdrop(300, 180, scenic=True)
        self.assertEqual(a.tobytes(), b.tobytes())


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
        self.member.roles = []
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
        result = self.b._manage_components(
            42, self.member, self.b._MANAGE_DATA_PAGE, self.snap
        )
        self.assertIn("manage:42:clear", self._ids(result))

    def test_data_page_confirm_is_fail_accent_with_confirm_cancel(self):
        result = self.b._manage_components(
            42, self.member, self.b._MANAGE_DATA_PAGE, self.snap,
            confirm_clear=True,
        )
        self.assertEqual(result[1]["accent_color"], self.b.ACCENT_FAIL)
        ids = self._ids(result)
        self.assertIn("manage:42:clearok", ids)
        # Cancel returns to the data page.
        self.assertIn(f"manage:42:p:{self.b._MANAGE_DATA_PAGE}", ids)

    def test_data_page_cleared_shows_no_action_buttons(self):
        cleared = {"titles": 1, "events_anonymized": 3, "onboarding": 0}
        result = self.b._manage_components(
            42, self.member, self.b._MANAGE_DATA_PAGE, self.snap,
            cleared=cleared,
        )
        ids = self._ids(result)
        self.assertNotIn("manage:42:clear", ids)
        self.assertNotIn("manage:42:clearok", ids)

    def test_data_page_empty_store_has_no_clear(self):
        empty = {"profile": None, "titles": []}
        result = self.b._manage_components(
            42, self.member, self.b._MANAGE_DATA_PAGE, empty
        )
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
        self.assertIn(self.b._MANAGE_PAGES[0][1], low[0]["content"])
        self.assertIn(self.b._MANAGE_PAGES[-1][1], high[0]["content"])


class ManageEditPanelTests(unittest.TestCase):
    """The /manage Edit page + per-field sub-editors (roles + store sync)."""

    def setUp(self):
        import bot as bot_module
        self.b = bot_module
        self._saved = {
            "PLATFORM_ROLE_IDS": dict(self.b.PLATFORM_ROLE_IDS),
            "SYNDICATE_ROLE_IDS": list(self.b.SYNDICATE_ROLE_IDS),
            "MR_ROLE_IDS": list(self.b.MR_ROLE_IDS),
            "CLAN_SLOTS": list(self.b.CLAN_SLOTS),
        }
        self.b.PLATFORM_ROLE_IDS.clear()
        self.b.PLATFORM_ROLE_IDS.update({"PC": 111, "Xbox": 222})
        self.b.SYNDICATE_ROLE_IDS[:] = [501, 502]
        self.b.MR_ROLE_IDS[:] = [601]
        self.b.CLAN_SLOTS[:] = [
            self.b.ClanSlot(slot=1, clan_name="Golden Pagoda",
                            role_id=701, emoji="<:gp:123>"),
            self.b.ClanSlot(slot=2, clan_name="Silver Lotus",
                            role_id=702, emoji="\U0001F337"),
        ]
        self.member = self._make_member([111, 501, 601, 701])

    def tearDown(self):
        self.b.PLATFORM_ROLE_IDS.clear()
        self.b.PLATFORM_ROLE_IDS.update(self._saved["PLATFORM_ROLE_IDS"])
        self.b.SYNDICATE_ROLE_IDS[:] = self._saved["SYNDICATE_ROLE_IDS"]
        self.b.MR_ROLE_IDS[:] = self._saved["MR_ROLE_IDS"]
        self.b.CLAN_SLOTS[:] = self._saved["CLAN_SLOTS"]

    def _make_member(self, role_ids):
        names = {
            111: "PC", 222: "Xbox", 501: "Red Veil", 502: "New Loka",
            601: "MR 21-30", 701: "Golden Pagoda", 702: "Silver Lotus",
        }

        def role(rid):
            r = Mock()
            r.id = rid
            r.name = names.get(rid, str(rid))
            c = Mock()
            c.value = 0
            c.to_rgb = lambda: (1, 2, 3)
            r.color = c
            return r

        roles = {rid: role(rid) for rid in names}
        guild = Mock()
        guild.get_role = lambda rid: roles.get(rid)
        member = Mock()
        member.id = 42
        member.display_name = "Tenno"
        member.guild = guild
        member.roles = [roles[rid] for rid in role_ids]
        return member

    @staticmethod
    def _selects(payload):
        out = []
        for child in payload[1]["components"]:
            if child.get("type") == 1:
                out.extend(
                    s for s in child["components"] if s.get("type") == 3
                )
        return out

    @staticmethod
    def _buttons(payload):
        out = []
        for child in payload[1]["components"]:
            if child.get("type") == 1:
                out.extend(
                    b for b in child["components"] if b.get("type") == 2
                )
        return out

    def test_button_emoji_from_literal(self):
        self.assertEqual(
            self.b._button_emoji_from_literal("<:gp:123>"),
            {"id": "123", "name": "gp", "animated": False},
        )
        self.assertEqual(
            self.b._button_emoji_from_literal("<a:gp:123>"),
            {"id": "123", "name": "gp", "animated": True},
        )
        self.assertEqual(
            self.b._button_emoji_from_literal("\U0001F337"),
            {"name": "\U0001F337"},
        )
        self.assertIsNone(self.b._button_emoji_from_literal(""))
        self.assertIsNone(self.b._button_emoji_from_literal(None))

    def test_edit_page_lists_fields_and_buttons(self):
        snap = {"profile": {"in_game_name": "Tenno#1",
                            "mastery_rank": "MR 28"}, "titles": []}
        comps = self.b._manage_components(
            42, self.member, self.b._MANAGE_EDIT_PAGE, snap
        )
        body = comps[1]["components"][0]["content"]
        self.assertIn("Tenno#1", body)
        self.assertIn("PC", body)
        self.assertIn("Golden Pagoda", body)
        self.assertIn("Red Veil", body)
        ids = {b.get("custom_id") for b in self._buttons(comps)}
        self.assertIn("manage:42:ign", ids)
        self.assertIn("manage:42:editfield:platform", ids)
        self.assertIn("manage:42:editfield:mastery", ids)
        self.assertIn("manage:42:editfield:clan", ids)
        self.assertIn("manage:42:editfield:syndicate", ids)
        self.assertIn("manage:42:titles", ids)

    def test_edit_page_for_departed_member_has_no_field_buttons(self):
        comps = self.b._manage_components(
            42, None, self.b._MANAGE_EDIT_PAGE,
            {"profile": None, "titles": []},
        )
        ids = {b.get("custom_id") for b in self._buttons(comps)}
        self.assertNotIn("manage:42:ign", ids)
        self.assertNotIn("manage:42:editfield:platform", ids)

    def test_platform_editor_preselects_current(self):
        payload = self.b._manage_editor_components(42, self.member, "platform")
        sel = self._selects(payload)[0]
        self.assertEqual(sel["custom_id"], "manage:42:setplatform")
        defaults = [o["value"] for o in sel["options"] if o.get("default")]
        self.assertEqual(defaults, ["PC"])

    def test_mastery_editor_has_two_unique_selects(self):
        payload = self.b._manage_editor_components(42, self.member, "mastery")
        sels = self._selects(payload)
        self.assertEqual(len(sels), 2)
        self.assertNotEqual(sels[0]["custom_id"], sels[1]["custom_id"])
        total = sum(len(s["options"]) for s in sels)
        self.assertEqual(total, 38)  # MR 1-30 + Legendary 1-8

    def test_clan_editor_uses_select_with_emojis(self):
        payload = self.b._manage_editor_components(42, self.member, "clan")
        sel = next(
            s for s in self._selects(payload)
            if s["custom_id"] == "manage:42:setclan"
        )
        values = [o["value"] for o in sel["options"]]
        self.assertEqual(values, ["1", "2"])
        gp = next(o for o in sel["options"] if o["value"] == "1")
        self.assertEqual(gp["emoji"], {"id": "123", "name": "gp",
                                       "animated": False})
        # Current clan (Golden Pagoda, slot 1) is preselected.
        defaults = [o["value"] for o in sel["options"] if o.get("default")]
        self.assertEqual(defaults, ["1"])
        # A "Not Affiliated" button sits under the select.
        ids = {b.get("custom_id") for b in self._buttons(payload)}
        self.assertIn("manage:42:clanother", ids)

    def test_clan_editor_offers_not_affiliated_without_slots(self):
        self.b.CLAN_SLOTS[:] = []
        payload = self.b._manage_editor_components(42, self.member, "clan")
        self.assertEqual(self._selects(payload), [])
        ids = {b.get("custom_id") for b in self._buttons(payload)}
        self.assertIn("manage:42:clanother", ids)

    def test_configured_clan_slot_for_name(self):
        slot = self.b._configured_clan_slot_for_name("golden pagoda")
        self.assertIsNotNone(slot)
        self.assertEqual(slot.slot, 1)
        self.assertIsNone(self.b._configured_clan_slot_for_name("Free Clan"))
        self.assertIsNone(self.b._configured_clan_slot_for_name(""))

    def test_record_lines_clan_role_beats_override(self):
        # No configured clan role: the free-text override is written.
        member = self._make_member([111])
        lines = self.b._member_record_profile_lines(
            member, clan_override="Free Clan"
        )
        self.assertIn("Clan: **Free Clan**", lines)
        # A configured clan role always wins the Clan line.
        member = self._make_member([111, 701])
        lines = self.b._member_record_profile_lines(
            member, clan_override="Free Clan"
        )
        self.assertIn("Clan: **Golden Pagoda**", lines)
        self.assertNotIn("Clan: **Free Clan**", lines)

    def test_syndicate_editor_multiselect_preselects_held(self):
        payload = self.b._manage_editor_components(42, self.member, "syndicate")
        sel = self._selects(payload)[0]
        self.assertEqual(sel["custom_id"], "manage:42:setsyn")
        self.assertEqual(sel["min_values"], 0)
        self.assertEqual(sel["max_values"], 2)
        defaults = {o["value"] for o in sel["options"] if o.get("default")}
        self.assertEqual(defaults, {"501"})

    def test_editor_unconfigured_field_shows_notice(self):
        self.b.PLATFORM_ROLE_IDS.clear()
        payload = self.b._manage_editor_components(42, self.member, "platform")
        body = payload[1]["components"][0]["content"]
        self.assertIn("No platform roles", body)
        self.assertEqual(self._selects(payload), [])


class EnvRewriteRoundtripTests(unittest.TestCase):
    """The shared .env read->replace->append skeleton (now in envstore.py,
    re-exported via bot.*). The roundtrip locks its behaviour."""

    def setUp(self):
        import bot as bot_module
        import envstore as envstore_module
        self.b = bot_module
        self.envstore = envstore_module
        import tempfile
        import pathlib
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.env_path = pathlib.Path(self._dir.name) / "config.env"
        # The env writers read envstore.ENV_FILE_PATH from their own module
        # namespace, so patch it there (bot re-exports the functions verbatim).
        self._orig = self.envstore.ENV_FILE_PATH
        self.envstore.ENV_FILE_PATH = self.env_path

    def tearDown(self):
        self.envstore.ENV_FILE_PATH = self._orig

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

    def test_update_env_value_replaces_then_appends(self):
        # Replaces an existing KEY=value line in place (commas in the value
        # are fine — this is how MR_ROLE_NAMES is persisted).
        self.env_path.write_text("MR_ROLE_NAMES=MR 1-10,LR 1-7\nBAR=1\n")
        self.assertTrue(
            self.b._update_env_value("MR_ROLE_NAMES", "MR 1,MR 2,Legendary 1")
        )
        self.assertEqual(
            self.env_path.read_text(),
            "MR_ROLE_NAMES=MR 1,MR 2,Legendary 1\nBAR=1\n",
        )
        # Missing key path appends after a blank separator.
        self.env_path.write_text("BAR=1\n")
        self.assertTrue(self.b._update_env_value("NEW_KEY", "hello"))
        self.assertEqual(self.env_path.read_text(), "BAR=1\n\nNEW_KEY=hello\n")


class ProfileAccessGateTests(unittest.TestCase):
    """Truth table for the /profile ephemeral gate. /profile itself is open to
    everyone (anyone may target any member); only the ``ephemeral`` toggle is
    gated to PROFILE_OPTIONS_ROLE_IDS (managers always allowed)."""

    def setUp(self):
        import bot as bot_module
        self.b = bot_module

    def _member(self, *, manage_guild, role_ids):
        m = Mock()
        m.guild_permissions = Mock(manage_guild=manage_guild)
        m.roles = [Mock(id=r) for r in role_ids]
        return m

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


class ClanEmojiResolveTests(unittest.TestCase):
    """Auto-pulling a custom server emoji for a clan slot with none set."""

    def setUp(self):
        import bot as bot_module
        self.b = bot_module
        self._orig_client = getattr(bot_module, "client", None)

    def tearDown(self):
        if self._orig_client is not None:
            self.b.client = self._orig_client

    @staticmethod
    def _emoji(name, eid, animated=False):
        e = Mock()
        e.name = name
        e.id = eid
        e.animated = animated
        return e

    def _set_guild_emojis(self, *emojis):
        guild = Mock()
        guild.emojis = list(emojis)
        client = Mock()
        client.guilds = [guild]
        self.b.client = client
        return guild

    def test_match_keys_full_then_words(self):
        self.assertEqual(
            self.b._clan_emoji_match_keys("Kavat Raiders"),
            ["kavatraiders", "kavat", "raiders"],
        )
        # Stopwords + sub-4-char words are dropped.
        self.assertEqual(
            self.b._clan_emoji_match_keys("Church of Slua"),
            ["churchofslua", "church", "slua"],
        )

    def test_exact_full_name_match(self):
        self._set_guild_emojis(self._emoji("Apestorm", 11))
        self.assertEqual(
            self.b._resolve_clan_emoji_literal("Apestorm"),
            "<:Apestorm:11>",
        )

    def test_word_match_when_no_full_match(self):
        # No "churchofslua" emoji, but a "slua" emoji should resolve.
        self._set_guild_emojis(self._emoji("slua", 22))
        self.assertEqual(
            self.b._resolve_clan_emoji_literal("Church of Slua"),
            "<:slua:22>",
        )

    def test_prefix_match_when_no_exact(self):
        # "death" is a >=4-char prefix of the single key "deathroe" and isn't
        # itself a key (one-word clan), so only the prefix tier can match it.
        self._set_guild_emojis(self._emoji("death", 33))
        self.assertEqual(
            self.b._resolve_clan_emoji_literal("Deathroe"),
            "<:death:33>",
        )

    def test_animated_literal(self):
        self._set_guild_emojis(self._emoji("Deathroe", 44, animated=True))
        self.assertEqual(
            self.b._resolve_clan_emoji_literal("Deathroe"),
            "<a:Deathroe:44>",
        )

    def test_no_match_returns_none(self):
        self._set_guild_emojis(self._emoji("unrelated", 55))
        self.assertIsNone(
            self.b._resolve_clan_emoji_literal("Grand Warhorde")
        )

    def test_full_name_beats_word(self):
        # Both present: the full-name emoji wins over a single-word emoji.
        self._set_guild_emojis(
            self._emoji("raiders", 66),
            self._emoji("kavatraiders", 77),
        )
        self.assertEqual(
            self.b._resolve_clan_emoji_literal("Kavat Raiders"),
            "<:kavatraiders:77>",
        )

    def test_emblem_suffix_match(self):
        # Clan emblems are named "<Clan>_Emblem" (e.g. KavatRaiders_Emblem),
        # so the normalized emoji name *extends* the full clan key. This must
        # resolve even though it's neither an exact nor a shortened-prefix
        # match.
        self._set_guild_emojis(
            self._emoji("KavatRaiders_Emblem", 88),
        )
        self.assertEqual(
            self.b._resolve_clan_emoji_literal("Kavat Raiders"),
            "<:KavatRaiders_Emblem:88>",
        )

    def test_emblem_suffix_picks_correct_clan(self):
        # Several "<Clan>_Emblem" emojis present: the one extending *this*
        # clan's full key wins (no cross-clan bleed).
        self._set_guild_emojis(
            self._emoji("GoldenPagoda_Emblem", 1),
            self._emoji("GoldenTenno_Emblem", 2),
            self._emoji("GrandWarhorde_Emblem", 3),
        )
        self.assertEqual(
            self.b._resolve_clan_emoji_literal("Golden Tenno"),
            "<:GoldenTenno_Emblem:2>",
        )
        self.assertEqual(
            self.b._resolve_clan_emoji_literal("Grand Warhorde"),
            "<:GrandWarhorde_Emblem:3>",
        )

    def test_exact_beats_emblem_suffix(self):
        # An exact-name emoji still wins over an "_Emblem"-suffixed one.
        self._set_guild_emojis(
            self._emoji("Apestorm_Emblem", 10),
            self._emoji("Apestorm", 20),
        )
        self.assertEqual(
            self.b._resolve_clan_emoji_literal("Apestorm"),
            "<:Apestorm:20>",
        )


if __name__ == "__main__":
    unittest.main()

