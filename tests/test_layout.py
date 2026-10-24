from __future__ import annotations

import unittest

from tests import EXAMPLES
from touchlearn.layout import build_layout
from touchlearn.model import load_diagram
from touchlearn.svg import layout_to_svg


class LayoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.robot = build_layout(load_diagram(EXAMPLES / "robot_arm" / "diagram.json"))
        self.quadratic = build_layout(load_diagram(EXAMPLES / "quadratic" / "diagram.json"))

    def test_feature_inventory(self) -> None:
        self.assertEqual(len(self.robot.diagram.elements), 8)
        self.assertEqual(len(self.quadratic.diagram.elements), 8)
        self.assertEqual(len(self.robot.labels()), 8)
        self.assertEqual(len(self.quadratic.labels()), 8)
        # L3 shares J4J6's semantic id; the axes are one drawing feature for
        # three semantic elements (x_axis, y_axis, origin).
        self.assertEqual(len(self.robot.semantic_features()), 9)
        self.assertEqual(len(self.quadratic.semantic_features()), 5)
        self.assertEqual(self.robot.diagram.layout["engine"], "kinematic_chain")
        self.assertEqual(self.quadratic.diagram.layout["engine"], "cartesian_plot")

    def test_features_stay_inside_the_margin(self) -> None:
        for layout in (self.robot, self.quadratic):
            for feature in layout.features:
                left, top, width, height = feature.bbox()
                self.assertGreaterEqual(left, 5.0 - 1e-6, feature.id)
                self.assertGreaterEqual(top, 5.0 - 1e-6, feature.id)
                self.assertLessEqual(left + width, 143.0 + 1e-6, feature.id)
                self.assertLessEqual(top + height, 205.0 + 1e-6, feature.id)

    def test_robot_kinematics(self) -> None:
        base = self.robot.feature("BASE")
        self.assertEqual(base.primitives[0].kind, "polygon")
        self.assertEqual(base.primitives[0].points[0], (42.0, 180.0))
        self.assertEqual(self.robot.feature("J1").primitives[0].points[0], (55.0, 180.0))
        self.assertEqual(self.robot.feature("J2").primitives[0].points[0], (55.0, 145.0))
        self.assertEqual(self.robot.feature("J3").primitives[0].points[0], (90.0, 115.0))
        wrist = self.robot.feature("J4J6")
        self.assertEqual([primitive.kind for primitive in wrist.primitives], ["polyline", "polyline", "circle", "circle", "circle"])
        self.assertEqual(wrist.primitives[1].points[0], (124.0, 45.0))
        self.assertEqual(wrist.primitives[1].points[-1], (124.0, 87.0))
        self.assertEqual([primitive.radius for primitive in wrist.primitives[2:]], [3.0, 3.0, 3.0])
        ee = self.robot.feature("EE")
        self.assertEqual(ee.primitives[0].points[0], (124.0, 45.0))

    def test_quadratic_is_regenerated_from_coefficients(self) -> None:
        curve = self.quadratic.feature("curve")
        points = curve.primitives[0].points
        self.assertEqual(len(points), 50)
        for x_canvas, y_canvas in points:
            x_value = (x_canvas - 50.0) / 10.0
            y_value = (110.0 - y_canvas) / 10.0
            expected = x_value * x_value - 2.0 * x_value - 3.0
            self.assertAlmostEqual(y_value, expected, places=9)
        self.assertEqual(self.quadratic.feature("vertex").primitives[0].points[0], (60.0, 150.0))
        self.assertEqual(self.quadratic.feature("root_left").primitives[0].points[0], (40.0, 110.0))
        self.assertEqual(self.quadratic.feature("root_right").primitives[0].points[0], (80.0, 110.0))

    def test_symmetry_axis_is_dashed_and_vertical(self) -> None:
        symmetry = self.quadratic.feature("symmetry_axis")
        self.assertGreater(len(symmetry.primitives), 4)
        for primitive in symmetry.primitives:
            self.assertEqual(primitive.kind, "polyline")
            for point in primitive.points:
                self.assertAlmostEqual(point[0], 60.0)

    def test_layout_is_deterministic(self) -> None:
        again = build_layout(load_diagram(EXAMPLES / "quadratic" / "diagram.json"))
        self.assertEqual(layout_to_svg(self.quadratic), layout_to_svg(again))

    def test_label_indices_follow_the_narrative(self) -> None:
        indices = {feature.semantic_id: feature.index for feature in self.robot.labels()}
        self.assertEqual(indices["BASE"], 1)
        self.assertEqual(indices["EE"], 8)
        vertex = self.quadratic.feature("label_vertex")
        self.assertEqual(vertex.index, 1)


if __name__ == "__main__":
    unittest.main()
