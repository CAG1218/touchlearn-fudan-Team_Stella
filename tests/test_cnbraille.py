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
        joined = cnbraille.encode_text("苹果")
        self.assertNotIn((), joined)
        # 苹果/香蕉 are deliberately outside the lexicon: the fallback writes an
        # unmatched run as one word, so the only blank cell here is the explicit
        # space.  Using a pair the lexicon knows (地球/月球) would add its own
        # 分词空方 and the width difference would collapse to zero.
        spaced = cnbraille.encode_text("苹果 香蕉")
        self.assertEqual(spaced.count(()), 1)
        blank = spaced.index(())
        self.assertEqual(spaced[:blank], joined)
        self.assertAlmostEqual(
            cnbraille.width("苹果 香蕉") - cnbraille.width("苹果香蕉"),
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

    def test_secondary_tone_omission_matches_gf0019_tables(self) -> None:
        # 去声省写只适用于 b d l g k j x zh sh z s；c 只在阳平省写，
        # 所以 次 cì 必须带第四声，不能漏。
        self.assertEqual(cnbraille.encode_syllable("ci4").cells, ((1, 4), (2, 3)))
        self.assertEqual(cnbraille.encode_syllable("shu4").cells, ((1, 5, 6), (1, 3, 6)))
        self.assertEqual(cnbraille.encode_syllable("wei4").cells, ((2, 4, 5, 6),))
        # 韵母自成音节去声省写，但 3.2.1 的 è 和 3.2.6 的“再”例外。
        # 二 èr 不在例外表里，按 2.4 省去声；ér 才是 3.1.5 的省第二声。
        self.assertEqual(cnbraille.encode_syllable("er4").cells, ((1, 2, 3, 5),))
        self.assertEqual(len(cnbraille.encode_syllable("ai4").cells), 1)
        self.assertEqual(cnbraille.encode_text("再"), [(1, 3, 5, 6), (2, 4, 6), (2, 3)])
        self.assertEqual(cnbraille.encode_text("在"), [(1, 3, 5, 6), (2, 4, 6)])

    def test_zero_initial_syllables_follow_their_own_tone_rule(self) -> None:
        # 3.1 只省列出的那个声调（我/也/有省三声、一省一声、儿省二声）；
        # 其余声调仍按 2.4 省去声，所以 二/亿/卧 都是单方。
        self.assertEqual(len(cnbraille.encode_syllable("wo3").cells), 1)
        self.assertEqual(len(cnbraille.encode_syllable("er2").cells), 1)
        self.assertEqual(len(cnbraille.encode_syllable("yi1").cells), 1)
        self.assertEqual(len(cnbraille.encode_syllable("er4").cells), 1)
        self.assertEqual(len(cnbraille.encode_syllable("yi4").cells), 1)
        # 3.2.6 原例“问”保留声调，以便提供点位参照。
        self.assertEqual(cnbraille.encode_syllable("wen4").cells, ((2, 5), (2, 3)))

    def test_adjacent_syllabic_initial_keeps_tone(self) -> None:
        # GF 0019 3.2.5：声母自成音节后接韵母自成音节时，前一首节不省调。
        rows = cnbraille.describe_text("慈爱")
        self.assertEqual(rows[0]["pinyin"], "ci2")
        self.assertEqual(rows[0]["cells"][-1], [2])

    def test_chinese_punctuation_uses_gf0019_cells(self) -> None:
        # §8 uses the Chinese punctuation table, not UEB.  Most marks take two
        # cells, so a single-cell table silently produced the wrong dots.
        self.assertEqual(cnbraille.encode_text("，"), [(5,)])
        self.assertEqual(cnbraille.encode_text("、"), [(4,)])
        self.assertEqual(cnbraille.encode_text("。"), [(5,), (2, 3)])
        self.assertEqual(cnbraille.encode_text("？"), [(5,), (3,)])
        self.assertEqual(cnbraille.encode_text("！"), [(5, 6), (2,)])
        self.assertEqual(cnbraille.encode_text("……"), [(5,), (5,), (5,)])

    def test_decode_sheet_matches_printed_cells(self) -> None:
        # braille_map.md 必须和实际点阵逐格一致，否则图上的点与对照表会对不上。
        for text in ["二次函数", "触摸学习", "1 地球", "y=x2-2x-3"]:
            cells = cnbraille.encode_text(text)
            flat = [tuple(cell) for row in cnbraille.describe_text(text) for cell in row["cells"]]
            self.assertEqual(cells, flat, text)

    def test_one_blank_cell_between_words(self) -> None:
        self.assertEqual(cnbraille.encode_text("触摸学习").count(()), 1)
        self.assertEqual(cnbraille.encode_text("六轴机械臂").count(()), 1)
        rows = cnbraille.describe_text("触摸学习")
        self.assertEqual(sum(1 for row in rows if row["kind"] == "blank"), 1)

    def test_shorthand_cells_match_gf0019_section11(self) -> None:
        # §11.1 定字简写：一方（她为两方）代替整字。
        self.assertEqual(cnbraille.encode_text("的"), [(1, 4, 5)])
        self.assertEqual(cnbraille.encode_text("么"), [(1, 3, 4)])
        self.assertEqual(cnbraille.encode_text("你"), [(1, 3, 4, 5)])
        self.assertEqual(cnbraille.encode_text("他"), [(2, 3, 4, 5)])
        self.assertEqual(cnbraille.encode_text("她"), [(2, 3, 4, 5), (1,)])
        self.assertEqual(cnbraille.encode_text("它"), [(4,), (2, 3, 4, 5)])
        # 她 不在 §11.2 的阻断名单里，后接零声母音节仍然简写。
        self.assertEqual(cnbraille.encode_text("她用")[:2], [(2, 3, 4, 5), (1,)])

    def test_shorthand_is_blocked_before_a_zero_initial_syllable(self) -> None:
        # §11.2：的、么、你、他、它 后面连写韵母自成的音节时不能简写。
        # 他的“他”简写成一方 ⠞；被阻断时必须回到 t+a 两方全拼。
        full = list(cnbraille.encode_syllable("ta1").cells)
        self.assertEqual(len(full), 3)
        self.assertEqual(cnbraille.encode_text("他用")[: len(full)], full)
        self.assertEqual(cnbraille.encode_text("他"), [(2, 3, 4, 5)])
        # 么 在“怎么”里简写，在“怎么样”里因为后面是零声母的“样”必须全拼。
        # 不能直接断言 (1, 3, 4) 不在结果里：全拼的 m 声母恰好也是 ⠍，
        # 只有“占两方、第二方是 e 韵母”才能证明它是全拼。
        self.assertEqual(cnbraille.encode_text("怎么")[-1], (1, 3, 4))
        self.assertEqual(len(cnbraille.encode_text("怎么样")), 6)
        me5 = list(cnbraille.encode_syllable("me5").cells)
        self.assertEqual(len(me5), 2)
        self.assertEqual(cnbraille.encode_text("怎么样")[3 : 3 + len(me5)], me5)

    def test_shorthand_is_skipped_when_the_reading_is_not_de_me_or_ta(self) -> None:
        # 的 只在读 de 时是助词；目的 mùdì、的确 díquè 读 dì，必须按 d+i 拼。
        self.assertEqual(cnbraille.encode_text("目的")[3:], [(1, 4, 5), (2, 4)])
        self.assertEqual(cnbraille.encode_text("的确")[:2], [(1, 4, 5), (2, 4)])
        self.assertNotEqual(cnbraille.encode_text("的确")[2], (2, 3))

    def test_structural_particle_is_separated_but_suffix_is_joined(self) -> None:
        # 结构助词“的”与前面的词分写（6.1.9.1）；“们”是后附成分，连写（5.5）。
        self.assertEqual(cnbraille.encode_text("他的"), [(2, 3, 4, 5), (), (1, 4, 5)])
        self.assertNotIn((), cnbraille.encode_text("它们是"))

    def test_full_tone_disables_the_shorthand(self) -> None:
        self.assertEqual(
            cnbraille.encode_text("他", full_tone=True),
            list(cnbraille.encode_syllable("ta1", full_tone=True).cells),
        )
        self.assertEqual(
            cnbraille.encode_text("的", full_tone=True),
            list(cnbraille.encode_syllable("de5", full_tone=True).cells),
        )

    def test_template_labels_are_split_into_words(self) -> None:
        # 这些是场景模板、图例和页脚里真实会出现的中文。词典漏一个词，
        # 空方就会少一个，读起来会粘成一串，所以按空方数量锁住。
        self.assertEqual(cnbraille.encode_text("其它标记").count(()), 1)
        self.assertEqual(cnbraille.encode_text("四根柱子").count(()), 1)
        self.assertEqual(cnbraille.encode_text("类别编号").count(()), 1)
        self.assertEqual(cnbraille.encode_text("读取输入").count(()), 1)
        self.assertEqual(cnbraille.encode_text("细胞核与细胞膜").count(()), 2)

    def test_unverified_words_reports_runs_the_lexicon_missed(self) -> None:
        self.assertEqual(cnbraille.unverified_words("二次函数图像"), [])
        self.assertEqual(cnbraille.unverified_words("其它标记"), [])
        self.assertEqual(
            cnbraille.unverified_words("请沿曲线从左到右摸一遍"),
            ["从左到右摸一遍"],
        )


if __name__ == "__main__":
    unittest.main()
