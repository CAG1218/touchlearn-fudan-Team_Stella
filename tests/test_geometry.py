from __future__ import annotations

import unittest

from tests import EXAMPLES
from touchlearn import braille, font
from touchlearn.geometry import (
    Mesh,
    build_mesh,
    circle_polygon,
    feature_polygons,
    prism_triangles,
    segment_polygon,
    triangulate,
)
from touchlearn.layout import Feature, Primitive, build_layout
from touchlearn.model import load_diagram


def _triangle_area(triangle) -> float:
    (ax, ay), (bx, by), (cx, cy) = triangle
    return ((bx - ax) * (cy - ay) - (by - ay) * (cx - ax)) / 2.0


class GeometryTests(unittest.TestCase):
    def test_ear_clipping_preserves_area(self) -> None:
        square = [(0, 0), (10, 0), (10, 10), (0, 10)]
        triangles = triangulate(square)
        self.assertEqual(len(triangles), 2)
        self.assertAlmostEqual(sum(_triangle_area(item) for item in triangles), 100.0)
        self.assertTrue(all(_triangle_area(item) > 0 for item in triangles))

    def test_triangulation_handles_concave_polygon(self) -> None:
        l_shape = [(0, 0), (6, 0), (6, 2), (2, 2), (2, 6), (0, 6)]
        triangles = triangulate(l_shape)
        self.assertEqual(len(triangles), 4)
        self.assertAlmostEqual(sum(_triangle_area(item) for item in triangles), 20.0)

    def test_segment_and_circle_polygons(self) -> None:
        polygon = segment_polygon((0, 0), (10, 0), 2.0)
        self.assertEqual(len(polygon), 4)
        self.assertAlmostEqual(abs(sum(_triangle_area(t) for t in triangulate(polygon))), 20.0)
        self.assertEqual(len(circle_polygon(0, 0, 5, segments=24)), 24)

    def test_text_bbox_contains_every_stroke(self) -> None:
        left, top, width, height = font.text_bbox("ROBOT", 10.0, 20.0, 6.0)
        for stroke in font.text_strokes("ROBOT", 10.0, 20.0, 6.0):
            for x, y in stroke:
                self.assertGreaterEqual(x, left - 1e-9)
                self.assertLessEqual(x, left + width + 1e-9)
                self.assertGreaterEqual(y, top - 1e-9)
                self.assertLessEqual(y, top + height + 1e-9)

    def test_text_primitive_is_a_closed_shell(self) -> None:
        primitive = Primitive("text", ((0.0, 0.0),), text="1", height=6.0)
        feature = Feature("label", "label", primitives=[primitive])
        for polygon in feature_polygons(feature):
            mesh = Mesh(prism_triangles(polygon, 0.0, 1.0, 10.0))
            self.assertGreater(mesh.triangle_count(), 0)
            self.assertTrue(mesh.is_watertight())
            self.assertGreater(mesh.volume(), 0.0)

    def test_braille_dots_match_encoding(self) -> None:
        primitive = Primitive("braille", ((0.0, 0.0),), text="1")
        feature = Feature("label", "label", primitives=[primitive])
        self.assertEqual(len(feature_polygons(feature)), 5)
        self.assertEqual(len(braille.dot_centers("1", 0, 0)), 5)

    def test_meshes_are_closed_and_positive(self) -> None:
        for name, minimum, maximum in (
            ("robot_arm", 90_000.0, 105_000.0),
            ("quadratic", 88_000.0, 102_000.0),
        ):
            layout = build_layout(load_diagram(EXAMPLES / name / "diagram.json"))
            mesh = build_mesh(layout)
            self.assertTrue(mesh.is_watertight(), name)
            self.assertGreater(mesh.volume(), minimum, name)
            self.assertLess(mesh.volume(), maximum, name)
            min_x, min_y, min_z, max_x, max_y, max_z = mesh.bounds()
            self.assertAlmostEqual(min_x, 0.0)
            self.assertAlmostEqual(min_y, 0.0)
            self.assertAlmostEqual(min_z, 0.0)
            self.assertAlmostEqual(max_x, 148.0)
            self.assertAlmostEqual(max_y, 210.0)
            self.assertGreaterEqual(max_z, 3.8)
            self.assertLessEqual(max_z, 3.801)

    def test_mesh_generation_is_deterministic(self) -> None:
        layout = build_layout(load_diagram(EXAMPLES / "robot_arm" / "diagram.json"))
        first = build_mesh(layout)
        second = build_mesh(layout)
        self.assertEqual(first.triangle_count(), second.triangle_count())
        self.assertEqual(first.triangles, second.triangles)


if __name__ == "__main__":
    unittest.main()
