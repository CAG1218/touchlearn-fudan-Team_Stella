from __future__ import annotations

import unittest
from unittest.mock import patch

from tests import EXAMPLES  # noqa: F401  (ensures src is on sys.path)

from touchlearn.agent.router import classify_request, extract_function


class RouterTests(unittest.TestCase):
    def classify(self, text: str, **kwargs):
        return classify_request(text, use_llm=False, **kwargs)

    def test_function_expression(self) -> None:
        decision = self.classify("画一个函数 y=x^2-2x-3 的图像")
        self.assertEqual(decision.subject, "function")
        self.assertGreaterEqual(decision.confidence, 0.9)

    def test_robot_description(self) -> None:
        decision = self.classify("六自由度机械臂，末端为两指夹爪，连杆约 150 毫米")
        self.assertEqual(decision.subject, "robot")

    def test_scene_description(self) -> None:
        decision = self.classify("画一个串联电路示意图：电池、开关、电阻和灯泡依次连接成回路")
        self.assertEqual(decision.subject, "scene")

    def test_unsupported_request(self) -> None:
        decision = self.classify("帮我写一篇关于春天的作文")
        self.assertEqual(decision.subject, "unsupported")

    def test_mesh_wins_when_only_mesh(self) -> None:
        decision = self.classify("", has_mesh=True)
        self.assertEqual(decision.subject, "mesh")

    def test_explicit_expression_with_image_still_function(self) -> None:
        decision = self.classify(
            "请把这张图做成触觉板",
            has_expression=True,
            images=[EXAMPLES / "quadratic" / "preview.png"] if (EXAMPLES / "quadratic" / "preview.png").is_file() else [],
        )
        self.assertEqual(decision.subject, "function")

    def test_extract_function_heuristic_without_llm(self) -> None:
        with patch("touchlearn.agent.router.llm_status", return_value={"configured": False}):
            result = extract_function("请画函数 y=x^2-2x-3 的图像，定义域从 -2 到 4")
        self.assertTrue(result["ok"])
        self.assertEqual(result["provider"], "heuristic")
        self.assertIn("x^2-2x-3", result["expression"].replace(" ", ""))

    def test_extract_function_shape_without_expression(self) -> None:
        with patch("touchlearn.agent.router.llm_status", return_value={"configured": False}):
            result = extract_function("画一条正弦曲线")
        self.assertTrue(result["ok"])
        self.assertEqual(result["expression"], "sin(x)")
        self.assertTrue(result["questions"])


if __name__ == "__main__":
    unittest.main()
