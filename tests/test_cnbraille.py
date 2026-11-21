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
        self.assertAlmostEqual(cnbraille.width("地球"), 24.9, places=3)
        # 0.5 mm of extra print clearance beyond the 4.2 mm cell width; FDM
        # bridging was fusing neighbouring cells at the old 2.2 mm gap.
        self.assertGreaterEqual(
            cnbraille.CELL_SPACING_MM - cnbraille.CELL_WIDTH_MM, 2.7 - 1e-9
        )
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

    def test_words_are_joined_and_separated_by_one_blank_cell(self) -> None:
        # GF 0019 分词连写: no gaps inside a word, exactly one blank cell
        # between words.  The old encoder inserted a gap after every
        # character and made the plate read as isolated syllables.
        joined = cnbraille.encode_text("地球")
        self.assertNotIn((), joined)
        spaced = cnbraille.encode_text("地球 月球")
        self.assertEqual(spaced.count(()), 1)
        blank = spaced.index(())
        self.assertEqual(spaced[:blank], joined)
        self.assertAlmostEqual(
            cnbraille.width("地球 月球") - cnbraille.width("地球月球"),
            cnbraille.CELL_SPACING_MM,
            places=6,
        )

    def test_lexicon_phrase_beats_character_default(self) -> None:
        # 爪 is zhao3 alone, but 夹爪 must read zhua3.  A phrase lexicon entry
        # has to win over the single-character table.
        self.assertEqual(cnbraille.readings("夹爪"), ["jia1", "zhua3"])
        self.assertEqual(cnbraille.readings("函数图像"), ["han2", "shu4", "tu2", "xiang4"])
        self.assertEqual(cnbraille.readings("二次函数图像"), ["er4", "ci4", "han2", "shu4", "tu2", "xiang4"])

    def test_unknown_character_is_rejected(self) -> None:
        with self.assertRaises(cnbraille.BrailleError):
            cnbraille.readings("龘")


if __name__ == "__main__":
    unittest.main()
