import unittest

from logic import (
    ClanSlot,
    find_clan_slot,
    parse_clan_name,
    parse_mastery_rank,
    parse_profile_name,
)


class ParseProfileNameTests(unittest.TestCase):
    def test_extracts_clan_tag_and_handle(self) -> None:
        text = "[DE]KickBot#072\nPROFILE EQUIPMENT"
        self.assertEqual(parse_profile_name(text), "[DE]KickBot#072")

    def test_extracts_plain_handle(self) -> None:
        self.assertEqual(parse_profile_name("Tenno#1234 stats"), "Tenno#1234")

    def test_returns_none_when_missing(self) -> None:
        self.assertIsNone(parse_profile_name("no handle here"))

    def test_does_not_return_clan_discriminator_when_title_bar_missing(self) -> None:
        text = (
            "PROFILE EQUIPMENT STATS SYNDICATES TROPHIES "
            "MASTERY RANK 1 SILVER SEEKER 349,957 "
            "CLAN Kavat Raiders#474 MOON CLAN RANK 10 "
            "MARKED FOR DEATH BY STALKER"
        )
        self.assertIsNone(parse_profile_name(text))


class ParseClanNameTests(unittest.TestCase):
    def test_returns_clan_name_after_header(self) -> None:
        text = "MASTERY RANK\nUNRANKED\nCLAN\nGolden Pagoda\nINVITE"
        self.assertEqual(parse_clan_name(text), "Golden Pagoda")

    def test_returns_none_for_unaffiliated(self) -> None:
        text = "CLAN\nUNAFFILIATED\nNO CLAN"
        self.assertIsNone(parse_clan_name(text))

    def test_returns_none_without_header(self) -> None:
        self.assertIsNone(parse_clan_name("nothing relevant"))


class ParseMasteryRankTests(unittest.TestCase):
    def test_extracts_numeric_rank(self) -> None:
        text = "MASTERY RANK\n12\nCLAN\nGolden Pagoda"
        self.assertEqual(parse_mastery_rank(text), "MR 12")

    def test_extracts_unranked(self) -> None:
        text = "MASTERY RANK\nUNRANKED\nCLAN"
        self.assertEqual(parse_mastery_rank(text), "Unranked")

    def test_returns_none_when_missing(self) -> None:
        self.assertIsNone(parse_mastery_rank("no header here"))

    def test_skips_thousands_separated_xp_and_credits(self) -> None:
        # Real failure: top-bar credits "3,837,977" landed on the line
        # right after "MASTERY RANK", causing the parser to return MR 3
        # before seeing the actual badge "28" further down.
        text = (
            "MASTERY RANK\n"
            "3,837,977\n"
            "28\n"
            "MASTER\n"
            "2,037,372\n"
            "NEXT RANK: MIDDLE MASTER IN 65,128\n"
        )
        self.assertEqual(parse_mastery_rank(text), "MR 28")

    def test_ignores_implausibly_large_rank_value(self) -> None:
        text = "MASTERY RANK\n200\n14\nCLAN\n"
        self.assertEqual(parse_mastery_rank(text), "MR 14")

    def test_skips_stray_zero_below_header(self) -> None:
        # A bare "0" below the header is a misread UI digit, not MR 0 — there
        # is no MR 0 role and storing "MR 0" sticks forever (preferred over the
        # real rank role). With no other candidate the parse returns None.
        self.assertIsNone(parse_mastery_rank("MASTERY RANK\n0\nCLAN\n"))

    def test_skips_zero_then_reads_real_rank(self) -> None:
        text = "MASTERY RANK\n0\n25\nCLAN\n"
        self.assertEqual(parse_mastery_rank(text), "MR 25")

    def test_extracts_legendary_same_line(self) -> None:
        text = "MASTERY RANK\nLEGENDARY 3\nCLAN\nGolden Pagoda"
        self.assertEqual(parse_mastery_rank(text), "LR 3")

    def test_extracts_legendary_number_on_next_line(self) -> None:
        text = "MASTERY RANK\nLEGENDARY RANK\n2\nCLAN"
        self.assertEqual(parse_mastery_rank(text), "LR 2")

    def test_extracts_legendary_shorthand(self) -> None:
        text = "MASTERY RANK\nLR 5\nCLAN"
        self.assertEqual(parse_mastery_rank(text), "LR 5")

    def test_legendary_wins_over_credit_leak(self) -> None:
        # The legendary marker must win even when the top-bar credit count
        # leaks onto the line just below the header.
        text = "MASTERY RANK\n3,837,977\nLEGENDARY 1\nCLAN"
        self.assertEqual(parse_mastery_rank(text), "LR 1")

    def test_legendary_label_then_credits_then_number(self) -> None:
        text = "MASTERY RANK\nLEGENDARY RANK\n2,037,372\n4\nCLAN"
        self.assertEqual(parse_mastery_rank(text), "LR 4")

    def test_plain_numeric_unaffected_by_legendary_scan(self) -> None:
        text = "MASTERY RANK\n28\nCLAN\nGolden Pagoda"
        self.assertEqual(parse_mastery_rank(text), "MR 28")


class FindClanSlotTests(unittest.TestCase):
    def test_matches_case_insensitively(self) -> None:
        slots = [
            ClanSlot(slot=1, clan_name="Golden Pagoda", role_id=111),
            ClanSlot(slot=2, clan_name="Other", role_id=222),
        ]
        match = find_clan_slot(slots, "golden pagoda")
        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.slot, 1)

    def test_ignores_trailing_clan_tag(self) -> None:
        slots = [ClanSlot(slot=1, clan_name="Grand Warhorde", role_id=111)]
        match = find_clan_slot(slots, "Grand Warhorde#245")
        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.slot, 1)

    def test_returns_none_without_match(self) -> None:
        slots = [ClanSlot(slot=1, clan_name="A", role_id=1)]
        self.assertIsNone(find_clan_slot(slots, "B"))


if __name__ == "__main__":
    unittest.main()
