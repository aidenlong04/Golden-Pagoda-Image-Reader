import unittest

from PIL import Image

from logic import (
    ClanSlot,
    PLATFORM_PC,
    PLATFORM_PLAYSTATION,
    PLATFORM_SWITCH,
    PLATFORM_XBOX,
    detect_platform,
    detect_platform_from_image,
    find_clan_slot,
    parse_clan_name,
    parse_mastery_rank,
    parse_profile_name,
    _binarize_and_crop,
    _iou,
)


class ParseProfileNameTests(unittest.TestCase):
    def test_extracts_clan_tag_and_handle(self) -> None:
        text = "[DE]KickBot#072\nPROFILE EQUIPMENT"
        self.assertEqual(parse_profile_name(text), "[DE]KickBot#072")

    def test_extracts_plain_handle(self) -> None:
        self.assertEqual(parse_profile_name("Tenno#1234 stats"), "Tenno#1234")

    def test_returns_none_when_missing(self) -> None:
        self.assertIsNone(parse_profile_name("no handle here"))


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


class DetectPlatformFromImageTests(unittest.TestCase):
    def _solid_top_bar(self, color: tuple[int, int, int]) -> Image.Image:
        img = Image.new("RGB", (200, 200), (0, 0, 0))
        for y in range(0, 15):
            for x in range(60, 140):
                img.putpixel((x, y), color)
        return img

    def test_detects_xbox_green(self) -> None:
        self.assertEqual(
            detect_platform_from_image(self._solid_top_bar((16, 124, 16))),
            PLATFORM_XBOX,
        )

    def test_detects_switch_red(self) -> None:
        self.assertEqual(
            detect_platform_from_image(self._solid_top_bar((230, 0, 18))),
            PLATFORM_SWITCH,
        )

    def test_detects_pc_bright_blue(self) -> None:
        self.assertEqual(
            detect_platform_from_image(self._solid_top_bar((0, 164, 239))),
            PLATFORM_PC,
        )

    def test_detects_playstation_deep_blue(self) -> None:
        self.assertEqual(
            detect_platform_from_image(self._solid_top_bar((0, 55, 145))),
            PLATFORM_PLAYSTATION,
        )

    def test_returns_none_for_blank_image(self) -> None:
        img = Image.new("RGB", (200, 200), (0, 0, 0))
        self.assertIsNone(detect_platform_from_image(img))

    def test_rejects_purple_blue_as_not_playstation(self) -> None:
        img = self._solid_top_bar((120, 60, 180))
        result = detect_platform_from_image(img)
        self.assertNotEqual(result, PLATFORM_PLAYSTATION)

    def test_rejects_cyan_as_not_playstation(self) -> None:
        img = self._solid_top_bar((0, 180, 200))
        result = detect_platform_from_image(img)
        self.assertNotEqual(result, PLATFORM_PLAYSTATION)

    def test_rejects_orange_red_as_not_switch(self) -> None:
        img = self._solid_top_bar((240, 80, 20))
        result = detect_platform_from_image(img)
        self.assertNotEqual(result, PLATFORM_SWITCH)

    def test_rejects_pink_as_not_switch(self) -> None:
        img = self._solid_top_bar((255, 100, 120))
        result = detect_platform_from_image(img)
        self.assertNotEqual(result, PLATFORM_SWITCH)

    def test_insufficient_saturated_pixels_returns_none(self) -> None:
        img = Image.new("RGB", (200, 200), (0, 0, 0))
        for y in range(0, 8):
            for x in range(60, 64):
                img.putpixel((x, y), (0, 55, 145))
        self.assertIsNone(detect_platform_from_image(img))


class IoUTests(unittest.TestCase):
    def test_identical_masks_return_one(self) -> None:
        mask_a = [1, 1, 0, 0, 1, 1]
        mask_b = [1, 1, 0, 0, 1, 1]
        self.assertAlmostEqual(_iou(mask_a, mask_b), 1.0)

    def test_disjoint_masks_return_zero(self) -> None:
        mask_a = [1, 1, 0, 0, 0, 0]
        mask_b = [0, 0, 0, 1, 1, 1]
        self.assertAlmostEqual(_iou(mask_a, mask_b), 0.0)

    def test_half_overlap_returns_expected_value(self) -> None:
        mask_a = [1, 1, 1, 0]
        mask_b = [0, 1, 1, 1]
        iou = _iou(mask_a, mask_b)
        self.assertAlmostEqual(iou, 0.5, places=2)


class BinarizeAndCropTests(unittest.TestCase):
    def test_centered_icon_vs_padded_icon_same_mask(self) -> None:
        centered = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
        for y in range(8, 24):
            for x in range(8, 24):
                centered.putpixel((x, y), (255, 255, 255, 255))
        
        padded = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        for y in range(24, 40):
            for x in range(24, 40):
                padded.putpixel((x, y), (255, 255, 255, 255))
        
        mask_centered, _ = _binarize_and_crop(centered, size=(32, 32))
        mask_padded, _ = _binarize_and_crop(padded, size=(32, 32))
        
        self.assertEqual(len(mask_centered), len(mask_padded))
        diff = sum(1 for a, b in zip(mask_centered, mask_padded) if a != b)
        self.assertLess(diff / len(mask_centered), 0.05)


class DetectPlatformAmbiguityTests(unittest.TestCase):
    def test_split_colors_returns_none(self) -> None:
        img = Image.new("RGB", (200, 200), (0, 0, 0))
        for y in range(0, 15):
            for x in range(60, 100):
                img.putpixel((x, y), (16, 124, 16))
        for y in range(0, 15):
            for x in range(100, 140):
                img.putpixel((x, y), (230, 0, 18))
        
        platform, scores = detect_platform(img)
        self.assertIsNone(platform)

    def test_white_square_not_classified_as_colored_platform(self) -> None:
        img = Image.new("RGB", (200, 200), (0, 0, 0))
        for y in range(0, 15):
            for x in range(60, 140):
                img.putpixel((x, y), (255, 255, 255))

        platform, scores = detect_platform(img)
        self.assertNotIn(
            platform,
            [PLATFORM_SWITCH, PLATFORM_PLAYSTATION, PLATFORM_XBOX],
        )


if __name__ == "__main__":
    unittest.main()
