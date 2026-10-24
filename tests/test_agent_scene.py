from __future__ import annotations

import unittest

from tests import EXAMPLES  # noqa: F401  (ensures src is on sys.path)

from touchlearn.agent.scene import (
    SceneError,
    match_template,
    normalize_scene,
    scene_narration,
    scene_summary,
    scene_templates,
)
from touchlearn.agent.standards import STATUS_FAIL, check_plate
from touchlearn.geometry import build_mesh
from touchlearn.layout import build_layout
from touchlearn.model import diagram_from_dict

CONSTRAINTS = {
    "base_thickness_mm": 3.0,
    "relief_height_mm": 0.8,
    "braille_height_mm": 0.6,
    "min_line_width_mm": 1.0,
}


def diagram_for(scene: dict) -> dict:
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
        "canvas": {"size": "A5", "width_mm": 148, "height_mm": 210, "margin_mm": 6},
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


if __name__ == "__main__":
    unittest.main()
