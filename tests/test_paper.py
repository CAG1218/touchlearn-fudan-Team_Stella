from __future__ import annotations

import unittest

from tests import EXAMPLES  # noqa: F401  (ensures src is on sys.path)

from touchlearn.paper import (
    PAPER_ORDER,
    PAPER_SIZES,
    canvas_for,
    choose_paper,
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


if __name__ == "__main__":
    unittest.main()
