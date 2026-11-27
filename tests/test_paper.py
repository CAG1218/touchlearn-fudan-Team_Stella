from __future__ import annotations

import unittest

from tests import EXAMPLES  # noqa: F401  (ensures src is on sys.path)

from touchlearn.paper import (
    PAPER_ORDER,
    PAPER_SIZES,
    canvas_for,
    choose_paper,
    custom_name,
    custom_size,
    is_custom,
    paper_name,
    paper_options,
    parse_paper,
    paper_size,
)


class PaperCatalogueTests(unittest.TestCase):
    def test_catalogue_is_ordered_smallest_to_largest(self) -> None:
        self.assertEqual(PAPER_ORDER, ("A5", "B5", "16K", "A4", "B4", "A3"))
        areas = [width * height for width, height in PAPER_SIZES.values()]
        self.assertEqual(areas, sorted(areas))

    def test_options_expose_millimetres(self) -> None:
        options = paper_options()
        self.assertEqual([item["name"] for item in options], list(PAPER_ORDER))
        self.assertEqual(options[0]["width_mm"], 148.0)
        self.assertEqual(options[-1]["height_mm"], 420.0)

    def test_parse_accepts_names_aliases_and_rejects_model_numbers(self) -> None:
        self.assertEqual(parse_paper("A4"), "A4")
        self.assertEqual(parse_paper("a4纸"), "A4")
        self.assertEqual(parse_paper("16开"), "16K")
        self.assertEqual(parse_paper("B4 尺寸"), "B4")
        # A model name must not silently change the sheet.
        self.assertEqual(parse_paper("A4Mini"), "")

    def test_explicit_request_wins_over_density(self) -> None:
        self.assertEqual(
            choose_paper(requested="B4", blocks=40, primitives=400, braille_rows=60),
            "B4",
        )

    def test_auto_size_grows_with_braille_rows_and_width(self) -> None:
        small = choose_paper(blocks=2, braille_rows=4, braille_width_mm=60.0)
        tall = choose_paper(blocks=2, braille_rows=30, braille_width_mm=60.0)
        wide = choose_paper(blocks=2, braille_rows=4, braille_width_mm=200.0)
        self.assertEqual(small, "A5")
        self.assertGreater(PAPER_ORDER.index(tall), PAPER_ORDER.index(small))
        self.assertGreater(PAPER_ORDER.index(wide), PAPER_ORDER.index(small))

    def test_canvas_and_name_round_trip(self) -> None:
        canvas = canvas_for("A4")
        self.assertEqual(canvas["size"], "A4")
        self.assertEqual((canvas["width_mm"], canvas["height_mm"]), paper_size("A4"))
        self.assertEqual(paper_name(canvas["width_mm"], canvas["height_mm"]), "A4")
        self.assertFalse(canvas["custom"])


class CustomPaperTests(unittest.TestCase):
    def test_parse_free_form_keeps_orientation(self) -> None:
        self.assertEqual(parse_paper("自定义200×280"), "自定义200×280")
        self.assertEqual(parse_paper("自定义280x200"), "自定义280×200")
        self.assertTrue(is_custom(parse_paper("CUSTOM 200*280")))
        self.assertEqual(paper_size("自定义200×280"), (200.0, 280.0))

    def test_free_numbers_in_prose_are_ignored(self) -> None:
        # A size mentioned mid-sentence should not hijack automatic sizing.
        self.assertEqual(parse_paper("把 200x280 的图做成触觉板"), "")

    def test_bounds_reject_too_small_or_too_large(self) -> None:
        self.assertIsNone(custom_size("自定义100×150"))
        self.assertIsNone(custom_size("自定义500×600"))
        # 148×210 is exactly A5's floor and must pass.
        self.assertEqual(custom_size("自定义148×210"), (148.0, 210.0))
        with self.assertRaises(ValueError):
            parse_paper("自定义100×150")

    def test_canvas_marks_custom_and_explicit_request_wins(self) -> None:
        canvas = canvas_for("自定义200×280")
        self.assertEqual(canvas["size"], "自定义200×280")
        self.assertTrue(canvas["custom"])
        self.assertEqual((canvas["width_mm"], canvas["height_mm"]), (200.0, 280.0))
        self.assertEqual(
            choose_paper(requested="自定义200×280", blocks=40, braille_rows=60),
            "自定义200×280",
        )

    def test_name_falls_back_to_custom_when_far_from_catalogue(self) -> None:
        self.assertEqual(paper_name(200.0, 280.0), custom_name(200.0, 280.0))
        self.assertEqual(paper_name(210.0, 297.0), "A4")


if __name__ == "__main__":
    unittest.main()
