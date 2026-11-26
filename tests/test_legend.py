import unittest
import tempfile
from pathlib import Path

from touchlearn.agent.volumetric import read_stl_mesh
from touchlearn.braille_map import build_braille_map, write_braille_map
from touchlearn.layout import build_layout
from touchlearn.legend import (
    build_legend_card_layout,
    build_legend_layout,
    indexed_entries,
    legend_card_mesh,
    legend_to_svg,
    other_marks,
    write_legend_card_stl,
    write_legend_svg,
)
from touchlearn.model import load_diagram

from . import EXAMPLES


class LegendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.robot = build_layout(load_diagram(EXAMPLES / "robot_arm" / "diagram.json"))
        cls.quadratic = build_layout(load_diagram(EXAMPLES / "quadratic" / "diagram.json"))

    def test_indexed_entries(self) -> None:
        robot = indexed_entries(self.robot)
        self.assertEqual([entry.index for entry in robot], list(range(1, 9)))
        self.assertEqual(robot[0].label, "BASE")
        self.assertEqual(robot[0].label_zh, "基座")
        self.assertEqual(robot[6].label, "J4-J6")

        quadratic = indexed_entries(self.quadratic)
        self.assertEqual([entry.index for entry in quadratic], [1, 2, 3])
        self.assertEqual([entry.label_zh for entry in quadratic], ["顶点", "左根", "右根"])

    def test_other_marks_are_only_unnumbered_labels(self) -> None:
        self.assertEqual(other_marks(self.robot), [])
        marks = dict(other_marks(self.quadratic))
        self.assertEqual(marks.get("x=1"), "对称轴")
        self.assertEqual(marks.get("x"), "横轴")
        self.assertEqual(marks.get("y"), "纵轴")
        self.assertEqual(marks.get("o"), "原点")

    def test_svg_is_a6_and_contains_names(self) -> None:
        svg = legend_to_svg(self.robot)
        self.assertIn('width="105mm"', svg)
        self.assertIn('height="148mm"', svg)
        self.assertIn("Six-Axis Robot Arm", svg)
        self.assertIn(">基座<", svg)
        self.assertIn(">末端执行器<", svg)

        quadratic = legend_to_svg(self.quadratic)
        self.assertIn(">顶点<", quadratic)
        self.assertIn('id="legend_equation"', quadratic)

    def test_legend_content_stays_inside_a6_margin(self) -> None:
        for source in (self.robot, self.quadratic):
            legend, rows = build_legend_layout(source)
            self.assertEqual(len(rows), len(indexed_entries(source)) + len(other_marks(source)))
            for feature in legend.features:
                left, top, width, height = feature.bbox()
                self.assertGreaterEqual(left, 6.0 - 1e-6, feature.id)
                self.assertGreaterEqual(top, 6.0 - 1e-6, feature.id)
                self.assertLessEqual(left + width, 105.0 - 6.0 + 1e-6, feature.id)
                self.assertLessEqual(top + height, 148.0 - 6.0 + 1e-6, feature.id)

    def test_write_legend(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = write_legend_svg(self.quadratic, Path(directory) / "legend.svg")
            self.assertTrue(path.is_file())
            self.assertIn('id="legend_entry_1"', path.read_text(encoding="utf-8"))


class TactileCardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.robot = build_layout(load_diagram(EXAMPLES / "robot_arm" / "diagram.json"))
        cls.quadratic = build_layout(load_diagram(EXAMPLES / "quadratic" / "diagram.json"))

    def test_card_is_a6_and_keeps_every_label_inside_the_plate(self) -> None:
        for source in (self.robot, self.quadratic):
            card = build_legend_card_layout(source)
            self.assertEqual((card.width, card.height), (105.0, 148.0))
            for feature in card.features:
                left, top, width, height = feature.bbox()
                self.assertGreaterEqual(left, 6.0 - 1e-6, feature.id)
                self.assertGreaterEqual(top, 6.0 - 1e-6, feature.id)
                self.assertLessEqual(left + width, 105.0 - 6.0 + 1e-6, feature.id)
                self.assertLessEqual(top + height, 148.0 - 6.0 + 1e-6, feature.id)

    def test_card_carries_every_index_and_chinese_name(self) -> None:
        for source in (self.robot, self.quadratic):
            card = build_legend_card_layout(source)
            entries = indexed_entries(source)
            for entry in entries:
                feature = card.feature(f"card_entry_{entry.index}")
                braille_texts = [
                    primitive.text for primitive in feature.primitives if primitive.kind == "braille"
                ]
                self.assertTrue(braille_texts, entry.index)
                self.assertIn(entry.label_zh, "".join(braille_texts))
            for position, (_, label_zh) in enumerate(other_marks(source)):
                feature = card.feature(f"card_mark_{position}")
                braille_texts = [
                    primitive.text for primitive in feature.primitives if primitive.kind == "braille"
                ]
                self.assertEqual("".join(braille_texts), label_zh)

    def test_written_card_stl_is_watertight(self) -> None:
        self.assertTrue(legend_card_mesh(self.robot).is_watertight())
        with tempfile.TemporaryDirectory() as directory:
            path = write_legend_card_stl(self.quadratic, Path(directory) / "legend_card.stl")
            self.assertTrue(path.is_file())
            written = read_stl_mesh(path)
        self.assertTrue(written.is_watertight())
        self.assertEqual(written.stats()["open_edges"], 0)
        self.assertEqual(written.stats()["nonmanifold_edges"], 0)
        bounds = written.bounds()
        self.assertAlmostEqual(bounds[3] - bounds[0], 105.0, places=3)
        self.assertAlmostEqual(bounds[4] - bounds[1], 148.0, places=3)
        self.assertAlmostEqual(bounds[5] - bounds[2], 2.0, places=3)


class BrailleMapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.robot = build_layout(load_diagram(EXAMPLES / "robot_arm" / "diagram.json"))
        cls.quadratic = build_layout(load_diagram(EXAMPLES / "quadratic" / "diagram.json"))

    def test_map_documents_every_braille_text_with_dot_order(self) -> None:
        for source in (self.robot, self.quadratic):
            sheet = build_braille_map(source)
            self.assertIn("GF 0019-2018", sheet)
            self.assertIn("| 内容 | 类别 | 读音 | 点序 | 说明 |", sheet)
            self.assertIn("GB/T 18028-2010", sheet)
            texts = {
                primitive.text
                for feature in source.features
                for primitive in feature.primitives
                if primitive.kind == "braille" and primitive.text
            }
            self.assertTrue(texts)
            for text in texts:
                self.assertIn(text, sheet)
            # Every documented character has a dot order, not a placeholder.
            rows = [line for line in sheet.splitlines() if line.startswith("| ") and "---" not in line]
            self.assertGreater(len(rows), 1)

    def test_map_is_written(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = write_braille_map(self.quadratic, Path(directory) / "braille_map.md")
            self.assertTrue(path.is_file())
            self.assertIn("# 中文盲文逐字对照表", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
