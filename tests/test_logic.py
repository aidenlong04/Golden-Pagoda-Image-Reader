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

    def _icon_in_title_bar(self, key: str, size: int = 40) -> Image.Image:
        """Paste the real reference icon into a synthetic title bar."""
        from logic import load_default_references  # local import to avoid cycle
        refs = load_default_references()
        if key not in refs:
            self.skipTest(f"reference icon for {key} not available")
        canvas = Image.new("RGB", (1200, 600), (15, 15, 18))
        icon = refs[key].resize((size, size), Image.LANCZOS)
        canvas.paste(icon, (1200 - 80, 12), icon)
        return canvas

    def test_detects_xbox_icon(self) -> None:
        self.assertEqual(
            detect_platform_from_image(self._icon_in_title_bar("Xbox")),
            PLATFORM_XBOX,
        )

    def test_detects_switch_icon(self) -> None:
        self.assertEqual(
            detect_platform_from_image(self._icon_in_title_bar("Switch")),
            PLATFORM_SWITCH,
        )

    def test_detects_pc_icon(self) -> None:
        self.assertEqual(
            detect_platform_from_image(self._icon_in_title_bar("PC")),
            PLATFORM_PC,
        )

    def test_detects_playstation_icon(self) -> None:
        self.assertEqual(
            detect_platform_from_image(self._icon_in_title_bar("PlayStation")),
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


class DetectPlatformRobustnessTests(unittest.TestCase):
    """Stress the ensemble against rescaling, blur, and JPEG compression."""

    PLATFORMS = ("PC", "Xbox", "PlayStation", "Switch", "Mobile")

    def _canvas_with(self, icon: Image.Image) -> Image.Image:
        canvas = Image.new("RGB", (1200, 600), (15, 15, 18))
        canvas.paste(icon, (1200 - 80, 12), icon)
        return canvas

    def _resized_icon(self, key: str, size: int) -> Image.Image:
        from logic import load_default_references
        refs = load_default_references()
        if key not in refs:
            self.skipTest(f"reference icon for {key} not available")
        return refs[key].resize((size, size), Image.LANCZOS)

    def test_detects_at_small_scale_24px(self) -> None:
        from logic import detect_platform_from_image
        for key in self.PLATFORMS:
            with self.subTest(platform=key):
                canvas = self._canvas_with(self._resized_icon(key, 24))
                self.assertEqual(detect_platform_from_image(canvas), key)

    def test_detects_at_large_scale_56px(self) -> None:
        from logic import detect_platform_from_image
        for key in self.PLATFORMS:
            with self.subTest(platform=key):
                canvas = self._canvas_with(self._resized_icon(key, 56))
                self.assertEqual(detect_platform_from_image(canvas), key)

    def test_detects_through_gaussian_blur(self) -> None:
        from PIL import ImageFilter
        from logic import detect_platform_from_image
        for key in self.PLATFORMS:
            with self.subTest(platform=key):
                icon = self._resized_icon(key, 48)
                blurred = icon.filter(ImageFilter.GaussianBlur(radius=0.6))
                canvas = self._canvas_with(blurred)
                self.assertEqual(detect_platform_from_image(canvas), key)

    def test_detects_after_jpeg_round_trip(self) -> None:
        import io
        from logic import detect_platform_from_image
        for key in self.PLATFORMS:
            with self.subTest(platform=key):
                canvas = self._canvas_with(self._resized_icon(key, 40))
                buf = io.BytesIO()
                canvas.convert("RGB").save(buf, "JPEG", quality=70)
                buf.seek(0)
                restored = Image.open(buf)
                self.assertEqual(detect_platform_from_image(restored), key)


if __name__ == "__main__":
    unittest.main()
