from __future__ import annotations

import unittest
from dataclasses import replace

from tests import EXAMPLES
from touchlearn.geometry import build_mesh
from touchlearn.layout import build_layout
from touchlearn.model import load_diagram
from touchlearn.printability import check_layout, polygon_distance


class PrintabilityTests(unittest.TestCase):
    def _report(self, name: str):
        diagram = load_diagram(EXAMPLES / name / "diagram.json")
        layout = build_layout(diagram)
        mesh = build_mesh(layout)
        return diagram, layout, check_layout(diagram, layout, mesh)

    def test_both_examples_pass(self) -> None:
        for name in ("robot_arm", "quadratic"):
            _, _, report = self._report(name)
            self.assertTrue(report.passed, report.to_dict())
            self.assertTrue(report.metrics["mesh"]["watertight"])
            self.assertGreater(report.metrics["mesh"]["volume_mm3"], 0)
            self.assertGreaterEqual(report.metrics["min_line_width_mm"], 1.0)

    def test_minimum_label_spacing_is_reported(self) -> None:
        _, _, report = self._report("quadratic")
        self.assertGreaterEqual(report.metrics["min_label_spacing_mm"], 4.0)
        self.assertGreaterEqual(report.metrics["min_label_clearance_mm"], 1.2)

    def test_thin_lines_are_caught(self) -> None:
        diagram, layout, _ = self._report("quadratic")
        strict = replace(diagram, constraints={**diagram.constraints, "min_line_width_mm": 2.0})
        report = check_layout(strict, layout, build_mesh(layout))
        self.assertFalse(report.passed)
        self.assertTrue(any(issue.code == "line_width" for issue in report.issues))

    def test_edge_margin_is_caught(self) -> None:
        diagram, layout, _ = self._report("robot_arm")
        strict = replace(diagram, canvas=replace(diagram.canvas, margin_mm=20.0))
        report = check_layout(strict, layout, build_mesh(layout))
        self.assertFalse(report.passed)
        self.assertTrue(any(issue.code == "edge_margin" for issue in report.issues))

    def test_polygon_distance(self) -> None:
        first = [(0, 0), (10, 0), (10, 10), (0, 10)]
        overlapping = [(5, 5), (15, 5), (15, 15), (5, 15)]
        separate = [(13, 0), (20, 0), (20, 10), (13, 10)]
        self.assertEqual(polygon_distance(first, overlapping), 0.0)
        self.assertAlmostEqual(polygon_distance(first, separate), 3.0)


if __name__ == "__main__":
    unittest.main()
