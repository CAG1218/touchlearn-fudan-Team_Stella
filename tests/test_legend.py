import unittest
import tempfile
from pathlib import Path

from touchlearn.layout import build_layout
from touchlearn.legend import build_legend_layout, indexed_entries, legend_to_svg, other_marks, write_legend_svg
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


if __name__ == "__main__":
    unittest.main()
