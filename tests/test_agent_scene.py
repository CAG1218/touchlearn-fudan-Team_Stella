from __future__ import annotations

import unittest
from unittest.mock import patch

from tests import EXAMPLES  # noqa: F401  (ensures src is on sys.path)

from touchlearn.agent.llm import LLMResult
from touchlearn.agent.scene import (
    SceneError,
    generate_scene,
    match_template,
    normalize_roadmap,
    normalize_scene,
    placeholder_marker,
    scene_narration,
    scene_summary,
    scene_templates,
)
from touchlearn.agent.standards import STATUS_FAIL, check_plate
from touchlearn.geometry import build_mesh
from touchlearn.layout import build_layout
from touchlearn.model import diagram_from_dict
from touchlearn.paper import canvas_for

CONSTRAINTS = {
    "base_thickness_mm": 3.0,
    "relief_height_mm": 0.8,
    "braille_height_mm": 0.6,
    "min_line_width_mm": 1.0,
}


def diagram_for(scene: dict, paper: str = "A5") -> dict:
    elements = []
    relations = []
    previous = None
    for index, block in enumerate(scene["blocks"], start=1):
        element_id = block["id"]
        elements.append(
            {
                "id": element_id,
                "kind": block.get("kind") or "scene",
                "label": block.get("label") or element_id.upper(),
                "label_zh": block.get("label_zh") or element_id,
                "order": index,
                "semantics": {},
            }
        )
        if previous is not None:
            relations.append({"from": previous, "to": element_id, "kind": "reading_order", "order": index})
        previous = element_id
    return {
        "schema_version": "0.2.0",
        "id": "scene-test",
        "title": scene["title"],
        "title_zh": scene["title_zh"],
        "domain": "scene",
        "canvas": canvas_for(paper),
        "source": {"kind": "scene", "ref": "test"},
        "elements": elements,
        "relations": relations,
        "layout": {"engine": "scene", "options": {"scene": scene}},
        "constraints": CONSTRAINTS,
        "audio": scene_narration(scene),
    }


class SceneTemplateTests(unittest.TestCase):
    def test_every_template_compiles_without_fail(self) -> None:
        templates = scene_templates()
        self.assertGreaterEqual(len(templates), 8)
        for entry in templates:
            with self.subTest(template=entry["id"]):
                diagram = diagram_from_dict(diagram_for(entry["scene"]))
                layout = build_layout(diagram)
                mesh = build_mesh(layout)
                report = check_plate(layout, mesh, audio_ready=False)
                failures = [check for check in report.checks if check.status == STATUS_FAIL]
                self.assertEqual(failures, [], msg=f"{entry['id']}: {[check.rule_id for check in failures]}")
                self.assertTrue(report.printable)
                self.assertTrue(mesh.stats()["watertight"])
                # 每个图块一个编号标签，另加一条副标题标签。
                self.assertEqual(len(layout.labels()), len(entry["scene"]["blocks"]) + 1)

    def test_summary_and_narration_follow_reading_order(self) -> None:
        entry = next(item for item in scene_templates() if item["id"] == "circuit")
        scene = entry["scene"]
        summary = scene_summary(scene)
        self.assertEqual(summary["block_count"], len(scene["blocks"]))
        self.assertEqual([item["index"] for item in summary["reading_order"]], list(range(1, len(scene["blocks"]) + 1)))
        narration = scene_narration(scene)
        self.assertIn("编号 1", " ".join(narration["steps"]))
        self.assertTrue(narration["outro"])

    def test_match_template(self) -> None:
        matched = match_template("画一个串联电路，电池、开关和电阻")
        self.assertIsNotNone(matched)
        assert matched is not None
        self.assertEqual(matched[0], "circuit")

    def test_normalize_rejects_empty_scene(self) -> None:
        with self.assertRaises(SceneError):
            normalize_scene({"title": "EMPTY", "blocks": []})

    def test_normalize_clamps_and_warns(self) -> None:
        scene, warnings = normalize_scene(
            {
                "title": "Test Scene",
                "blocks": [
                    {
                        "id": "block one",
                        "label": "A",
                        "primitives": [
                            {"type": "line", "points": [[-20, 120], [140, 50]], "width": 9},
                            {"type": "text", "x": 10, "y": 20, "text": "中文", "height": 20},
                        ],
                    }
                ],
            },
            fallback_title="fallback",
        )
        self.assertEqual(scene["blocks"][0]["id"], "blockone")
        self.assertEqual(len(scene["blocks"]), 1)
        self.assertTrue(warnings)

    def test_generate_scene_honours_explicit_selection(self) -> None:
        with patch(
            "touchlearn.agent.scene.chat_json",
            return_value=LLMResult(False, message="stop"),
        ) as fake:
            generate_scene("画一个串联电路", model="glm-5.3", effort="high")
        kwargs = fake.call_args.kwargs
        self.assertEqual(kwargs["model"], "glm-5.3")
        self.assertEqual(kwargs["effort"], "high")

    def test_generate_scene_retries_without_reasoning_on_truncation(self) -> None:
        first = LLMResult(False, finish_reason="length", message="truncated")
        second = LLMResult(False, message="stop")
        with patch(
            "touchlearn.agent.scene.chat_json",
            side_effect=[first, second],
        ) as fake:
            generate_scene("画一个串联电路", model="glm-5.3", effort="high")
        self.assertEqual(fake.call_count, 2)
        self.assertEqual(fake.call_args_list[0].kwargs["effort"], "high")
        self.assertEqual(fake.call_args_list[1].kwargs["effort"], "off")
        self.assertEqual(fake.call_args_list[1].kwargs["model"], "glm-5.3")

    def test_roadmap_keeps_chinese_and_compiles(self) -> None:
        scene, warnings = normalize_roadmap(
            {
                "title": "TECH ROADMAP",
                "title_zh": "技术路线图",
                "stages": [
                    {"heading": "仿生尾翼设计", "items": ["尾翼材料选型", "扑动频率测试"]},
                    {
                        "heading": "创新点",
                        "items": ["轻量化结构设计", "机翼蒙皮材料研发与疲劳测试"],
                    },
                ],
            },
            fallback_title="技术路线图",
        )
        self.assertEqual(len(scene["blocks"]), 2)
        self.assertEqual(scene["blocks"][0]["label_zh"], "仿生尾翼设计")
        self.assertIn("扑动频率测试", scene["blocks"][0]["note"])
        braille = [
            primitive["text"]
            for block in scene["blocks"]
            for primitive in block["primitives"]
            if primitive["type"] == "braille"
        ]
        self.assertIn("仿生尾翼设计", braille)
        self.assertIn("扑动频率测试", braille)
        self.assertFalse(any("省略" in item for item in warnings))

        # A wrapped item may occupy two rows, but only its first row gets a
        # bullet; otherwise continuation rows look like extra items.
        bullets = [
            primitive
            for block in scene["blocks"]
            for primitive in block["primitives"]
            if primitive["type"] == "point" and primitive.get("r") == 1.0
        ]
        item_count = sum(len(block["note"].split("；")) for block in scene["blocks"])
        self.assertEqual(len(bullets), item_count)

        paper = scene.get("paper_hint") or "A5"
        diagram = diagram_from_dict(diagram_for(scene, paper))
        layout = build_layout(diagram)
        mesh = build_mesh(layout)
        report = check_plate(layout, mesh, audio_ready=False)
        failures = [check.rule_id for check in report.checks if check.status == STATUS_FAIL]
        self.assertEqual(failures, [])
        self.assertTrue(report.printable)

    def test_roadmap_rejects_empty_stages(self) -> None:
        with self.assertRaises(SceneError):
            normalize_roadmap({"title": "EMPTY", "stages": []})

    def test_placeholder_answer_is_rejected(self) -> None:
        data = {
            "title": "CLEAR TOUCH GRID LAYOUT",
            "blocks": [{"label": "TEXT PLACEHOLDER", "primitives": []}],
        }
        self.assertEqual(placeholder_marker(data), "GRID LAYOUT")
        with patch(
            "touchlearn.agent.scene.chat_json",
            return_value=LLMResult(True, provider="fake", model="fake", data=data),
        ):
            result = generate_scene("把这张技术路线图做成触觉板")
        self.assertFalse(result.ok)
        self.assertIn("占位", result.message)

    def test_generate_scene_compiles_roadmap_field(self) -> None:
        data = {
            "title": "TECH ROADMAP",
            "title_zh": "技术路线图",
            "confidence": 0.7,
            "roadmap": {
                "stages": [
                    {"heading": "需求分析", "items": ["明确目标"]},
                    {"heading": "方案设计", "items": ["总体架构"]},
                ]
            },
        }
        with patch(
            "touchlearn.agent.scene.chat_json",
            return_value=LLMResult(True, provider="fake", model="fake", data=data),
        ):
            result = generate_scene("画一张技术路线图")
        self.assertTrue(result.ok)
        self.assertEqual(len(result.scene["blocks"]), 2)
        self.assertEqual(result.scene["blocks"][0]["label_zh"], "需求分析")

    def test_normalize_keeps_paper_hint(self) -> None:
        scene, _ = normalize_roadmap(
            {"stages": [{"heading": "阶段一", "items": ["要点"]}]},
            fallback_title="技术路线图",
        )
        again, _ = normalize_scene(scene, fallback_title="技术路线图")
        self.assertEqual(again.get("paper_hint") or "", scene.get("paper_hint") or "")

    def test_generate_scene_reports_the_reasoning_fallback(self) -> None:
        scene = scene_templates()[0]["scene"]
        first = LLMResult(False, finish_reason="length", message="truncated")
        second = LLMResult(
            True,
            provider="tokenhub",
            model="glm-5.3",
            data={"scene": scene, "confidence": 0.8},
        )
        with patch("touchlearn.agent.scene.chat_json", side_effect=[first, second]):
            result = generate_scene("画一个串联电路", model="glm-5.3", effort="high")
        self.assertTrue(result.ok)
        self.assertTrue(any("自动关闭隐藏推理重试" in item for item in result.notes))


if __name__ == "__main__":
    unittest.main()
