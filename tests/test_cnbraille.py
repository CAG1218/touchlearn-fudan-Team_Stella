from __future__ import annotations

import unittest

from tests import EXAMPLES  # noqa: F401  (ensures src is on sys.path)

from touchlearn import cnbraille


class ChineseBrailleTests(unittest.TestCase):
    def test_readings_and_cells_for_earth(self) -> None:
        self.assertEqual(cnbraille.readings("地球"), ["di4", "qiu2"])
        rows = cnbraille.describe("地球")
        self.assertEqual(rows[0]["cells"], [[1, 4, 5], [2, 4]])
        self.assertEqual(rows[1]["cells"], [[1, 3], [1, 2, 5, 6]])

    def test_tone_cell_is_present_when_not_omitted(self) -> None:
        syllable = cnbraille.encode_syllable("ma1")
        self.assertEqual(syllable.tone, 1)
        self.assertEqual(syllable.cells[-1], (1,))

    def test_tone_is_omitted_for_neutral_and_qi_di_finals(self) -> None:
        self.assertEqual(cnbraille.encode_syllable("de5").cells, ((1, 4, 5), (2, 6)))
        self.assertEqual(cnbraille.encode_syllable("di4").cells, ((1, 4, 5), (2, 4)))

    def test_width_and_dot_centres_are_physical(self) -> None:
        self.assertAlmostEqual(cnbraille.width("地球"), 23.4, places=3)
        dots = cnbraille.dot_centers("地", 0.0, 0.0)
        self.assertEqual(len(dots), 5)
        radius = cnbraille.DOT_DIAMETER_MM / 2.0
        self.assertAlmostEqual(dots[0][0], radius, places=6)
        self.assertAlmostEqual(dots[0][1], radius, places=6)

    def test_u_umlaut_finals_are_encoded(self) -> None:
        # The bundled table stores ü as ASCII "v"; without normalization every
        # lü/nü/lüe character failed and was silently dropped from a plate.
        self.assertEqual(cnbraille.readings("率"), ["lv4"])
        self.assertEqual(cnbraille.encode_syllable("lv4").cells, ((1, 2, 3), (3, 4, 6)))
        self.assertEqual(cnbraille.encode_syllable("nv3").cells[-1], (3,))
        for text in ["频率", "绿色", "略图", "旅行"]:
            self.assertTrue(cnbraille.encode_text(text))

    def test_unknown_character_is_rejected(self) -> None:
        with self.assertRaises(cnbraille.BrailleError):
            cnbraille.readings("龘")


if __name__ == "__main__":
    unittest.main()
