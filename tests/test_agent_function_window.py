from __future__ import annotations

import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import EXAMPLES  # noqa: F401  (ensures src is on sys.path)

from touchlearn.agent.agent import TouchLearnAgent
from touchlearn.agent.expression import ExpressionError, parse_expression
from touchlearn.agent.parametric import _simplify_canvas_points


class ExpressionPrefixTests(unittest.TestCase):
    def test_prefix_forms_are_accepted(self) -> None:
        for source in ("y=x^2-2x-4", "y = x^2 - 2x - 4", "f(x)=x^2-2x-4", "f ( x ) = x^2-2x-4"):
            expression = parse_expression(source)
            self.assertEqual(expression.canonical, "X^2-2X-4")

    def test_long_expression_is_rejected_instead_of_truncated(self) -> None:
        source = "x" + "+x" * 250
        with self.assertRaises(ExpressionError):
            parse_expression(source)

    def test_auto_y_range_keeps_vertex_and_roots(self) -> None:
        expression = parse_expression("x^2-2x-4")
        y_min, y_max = expression.auto_y_range(-2.8, 4.8)
        self.assertLessEqual(y_min, -4.999)
        self.assertGreaterEqual(y_max, 0.0)

    def test_suggest_window_finds_features_outside_default_domain(self) -> None:
        expression = parse_expression("(x-10)^2")
        x_min, x_max, _y_min, _y_max = expression.suggest_window()
        self.assertLess(x_min, 10.0)
        self.assertGreater(x_max, 10.0)
        self.assertGreaterEqual(x_max - x_min, 2.0)
        self.assertGreater(x_max - 10.0, 0.5)

    def test_suggest_window_frames_one_sided_domains(self) -> None:
        for source in ("sqrt(x)", "ln(x)"):
            x_min, x_max, _y_min, _y_max = parse_expression(source).suggest_window()
            self.assertGreaterEqual(x_min, -0.1, f"{source} 左边界仍落在定义域外: {x_min}")
            self.assertLess(x_min, 0.2, f"{source} 没有从定义域边界开始取景: {x_min}")
            self.assertGreater(x_max, 3.5)
        x_min, x_max, _y_min, _y_max = parse_expression("asin(x)").suggest_window()
        self.assertGreaterEqual(x_min, -1.2)
        self.assertLessEqual(x_max, 1.2)
        self.assertLess(x_min, -0.8)
        self.assertGreater(x_max, 0.8)

    def test_suggest_window_keeps_both_sides_of_a_pole(self) -> None:
        x_min, x_max, _y_min, _y_max = parse_expression("1/x").suggest_window()
        self.assertLess(x_min, -4.0)
        self.assertGreater(x_max, 4.0)


class KeyPointQualityTests(unittest.TestCase):
    def test_tangent_vertex_and_y_intercept_share_one_marker(self) -> None:
        expression = parse_expression("x^2")
        points = expression.key_points(-1.995, 2.005, -0.444, 4.14, max_points=6)
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0].kind, "minimum")
        self.assertAlmostEqual(points[0].x, 0.0, places=2)

    def test_distinct_roots_and_vertex_are_kept(self) -> None:
        expression = parse_expression("x^2-4")
        points = expression.key_points(-3.0, 3.0, -5.0, 6.0, max_points=6)
        kinds = [point.kind for point in points]
        self.assertEqual(kinds.count("root"), 2)
        self.assertEqual(kinds.count("minimum"), 1)
        self.assertNotIn("y_intercept", kinds)

    def test_simplify_drops_short_tail_instead_of_keeping_a_sliver(self) -> None:
        simplified = _simplify_canvas_points([(0.0, 0.0), (10.0, 0.0), (10.1, 0.0)], minimum=0.8)
        self.assertEqual(simplified, [(0.0, 0.0), (10.1, 0.0)])

    def test_pole_shoulders_are_not_key_points(self) -> None:
        for source in ("tan(x)", "1/x", "1/x^2"):
            points = parse_expression(source).key_points(-5.0, 5.0, max_points=10)
            self.assertFalse(
                [point for point in points if point.kind in {"minimum", "maximum"}],
                f"{source} 把极点肩部误判成了极值: {points}",
            )

    def test_underflow_tail_does_not_create_one_root_per_sample(self) -> None:
        points = parse_expression("exp(-x^2)").key_points(-5.0, 5.0, max_points=10)
        self.assertEqual([point.kind for point in points], ["maximum"])
        self.assertAlmostEqual(points[0].x, 0.0, places=3)
        self.assertAlmostEqual(points[0].y, 1.0, places=6)

    def test_jump_function_does_not_produce_step_extrema(self) -> None:
        for source in ("sign(x)", "floor(x)"):
            points = parse_expression(source).key_points(-5.0, 5.0, max_points=10)
            self.assertFalse(
                [point for point in points if point.kind in {"minimum", "maximum"}],
                f"{source} 把阶跃跳变误判成了极值: {points}",
            )

    def test_suggest_window_widens_to_reveal_hidden_minima(self) -> None:
        x_min, x_max, y_min, y_max = parse_expression("x^4-100x^2").suggest_window()
        self.assertLess(x_min, -7.0)
        self.assertGreater(x_max, 7.0)
        self.assertLessEqual(y_min, -2500.0)
        self.assertGreaterEqual(y_max, 0.0)


class FunctionPreviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.agent = TouchLearnAgent(output_root=Path(self.temp.name))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_preview_recomputes_window_when_auto(self) -> None:
        preview = self.agent.preview_function(
            {
                "expression": "x^2-2x-4",
                "auto_window": True,
                # Stale values from the previous expression must be ignored.
                "x_min": -1.5,
                "x_max": 4.4,
                "y_min": -5.8,
                "y_max": 1.2,
            }
        )
        self.assertTrue(preview["auto_window"])
        self.assertLess(preview["x_min"], -1.2)
        self.assertGreater(preview["x_max"], 3.2)
        self.assertLessEqual(preview["y_min"], -5.0)
        self.assertFalse(preview["clipped"])
        kinds = {point["kind"] for point in preview["key_points"]}
        self.assertIn("root", kinds)
        self.assertIn("minimum", kinds)

    def test_preview_keeps_manual_window_but_reports_clipping(self) -> None:
        preview = self.agent.preview_function(
            {
                "expression": "x^2-2x-4",
                "auto_window": False,
                "x_min": -1.5,
                "x_max": 4.4,
                "y_min": -1.0,
                "y_max": 1.0,
            }
        )
        self.assertAlmostEqual(preview["x_min"], -1.5)
        self.assertAlmostEqual(preview["y_max"], 1.0)
        self.assertTrue(preview["clipped"])
        self.assertTrue(preview["warnings"])
        self.assertIn("裁掉约", preview["warnings"][0])

    def test_auto_window_clipping_warning_does_not_advise_enabling_auto(self) -> None:
        preview = self.agent.preview_function(
            {"expression": "tan(x)", "auto_window": True}
        )
        self.assertTrue(preview["auto_window"])
        self.assertTrue(preview["clipped"])
        self.assertTrue(any("自动视窗" in warning for warning in preview["warnings"]))
        self.assertFalse(any("勾选自动视窗" in warning for warning in preview["warnings"]))

    def test_partial_x_window_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.agent.preview_function(
                {"expression": "x^2-2x-4", "auto_window": False, "x_min": -1.0}
            )

    def test_build_recomputes_auto_window_instead_of_using_stale_fields(self) -> None:
        draft = self.agent.analyze(
            {"subject": "function", "text": "把函数 y=x^2-2x-4 做成 A5 触觉板，标出零点和顶点。"}
        )
        self.assertTrue(draft["ready"])
        with patch("touchlearn.agent.agent.synthesize_windows_tts", return_value=(False, "test environment")):
            result = self.agent.build(
                draft["id"],
                overrides={
                    "fields": {
                        "auto_window": True,
                        "x_min": -1.5,
                        "x_max": 4.4,
                        "y_min": -5.8,
                        "y_max": 1.2,
                    }
                },
                public_base_url="http://127.0.0.1:8765",
            )
        job_dir = Path(self.temp.name) / "jobs" / result["job_id"]
        payload = json.loads((job_dir / "diagram.json").read_text(encoding="utf-8"))
        options = payload["layout"]["options"]
        self.assertTrue(options["auto_window"])
        self.assertLess(options["x_min"], -1.2)
        self.assertGreater(options["x_max"], 3.2)
        self.assertLessEqual(options["y_min"], -5.0)
        self.assertFalse(math.isclose(options["x_min"], -1.5))

    def test_x_squared_build_has_no_degenerate_mesh(self) -> None:
        draft = self.agent.analyze(
            {"subject": "function", "text": "把函数 y=x^2 做成 A5 触觉板，标出零点和顶点。"}
        )
        self.assertTrue(draft["ready"])
        with patch("touchlearn.agent.agent.synthesize_windows_tts", return_value=(False, "test environment")):
            result = self.agent.build(
                draft["id"],
                overrides={"fields": {"auto_window": True}},
                public_base_url="http://127.0.0.1:8765",
            )
        job_dir = Path(self.temp.name) / "jobs" / result["job_id"]
        payload = json.loads((job_dir / "diagram.json").read_text(encoding="utf-8"))
        point_ids = [element["id"] for element in payload["elements"] if element["kind"] == "point"]
        self.assertEqual(len(point_ids), 1, "顶点与 y 轴截距不应生成两个重叠的关键点")
        compliance = json.loads((job_dir / "compliance.json").read_text(encoding="utf-8"))
        self.assertTrue(compliance["printable"])
        self.assertGreaterEqual(compliance["metrics"]["mesh_min_edge_mm"], 0.2)


if __name__ == "__main__":
    unittest.main()
