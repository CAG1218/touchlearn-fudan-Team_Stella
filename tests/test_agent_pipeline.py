from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import EXAMPLES  # noqa: F401  (ensures src is on sys.path)

from touchlearn.agent.agent import TouchLearnAgent
from touchlearn.schema import has_errors, validate_payload


class AgentPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.agent = TouchLearnAgent(output_root=Path(self.temp.name))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_auto_route_to_scene_with_offline_template(self) -> None:
        draft = self.agent.analyze(
            {
                "subject": "auto",
                "text": "画一个串联电路示意图：电池、开关、电阻和灯泡依次连接成回路。",
            }
        )
        self.assertEqual(draft["subject"], "scene")
        self.assertTrue(draft["ready"])
        self.assertEqual(draft["fields"]["template"], "circuit")
        self.assertEqual(draft["fields"]["summary"]["block_count"], 5)
        self.assertTrue(draft["fields"]["scene"]["blocks"])
        self.assertEqual(draft["fields"]["routing"]["subject"], "scene")

    def test_scene_build_writes_printable_bundle(self) -> None:
        draft = self.agent.analyze({"subject": "auto", "text": "画一个水循环流程图：蒸发、凝结、降水、收集。"})
        self.assertEqual(draft["subject"], "scene")
        with patch("touchlearn.agent.agent.synthesize_windows_tts", return_value=(False, "test environment")):
            result = self.agent.build(draft["id"], public_base_url="http://127.0.0.1:8765")
        job_dir = Path(self.temp.name) / "jobs" / result["job_id"]
        self.assertTrue((job_dir / "model.stl").is_file())
        self.assertTrue((job_dir / "preview.svg").is_file())
        self.assertTrue((job_dir / "diagram.json").is_file())
        self.assertTrue((job_dir / "audio_script.txt").is_file())
        report = json.loads((job_dir / "compliance.json").read_text(encoding="utf-8"))
        self.assertTrue(report["printable"])
        self.assertEqual(report["counts"].get("FAIL", 0), 0)
        manifest = json.loads((job_dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["subject"], "scene")
        self.assertEqual(manifest["mode"], "tactile_plate")
        self.assertIn("scene_summary", manifest["geometry"])
        script = (job_dir / "audio_script.txt").read_text(encoding="utf-8")
        self.assertIn("编号 1", script)
        diagram = json.loads((job_dir / "diagram.json").read_text(encoding="utf-8"))
        issues = validate_payload(diagram)
        self.assertFalse(has_errors(issues), msg=[issue.to_dict() for issue in issues])

    def test_auto_function_from_text(self) -> None:
        draft = self.agent.analyze({"subject": "auto", "text": "请画函数 y=x^2-2x-3 的图像"})
        self.assertEqual(draft["subject"], "function")
        self.assertTrue(draft["ready"])
        self.assertIn("X^2-2X-3", draft["fields"]["expression"])

    def test_uploaded_text_document_drives_function_draft(self) -> None:
        content = "课堂练习：请画出函数 y=x^2-2x-3 的图像，并标出零点。".encode("utf-8")
        draft = self.agent.analyze(
            {
                "subject": "auto",
                "files": [{"name": "worksheet.txt", "data": base64.b64encode(content).decode("ascii")}],
            }
        )
        self.assertEqual(draft["subject"], "function")
        self.assertTrue(draft["ready"])
        self.assertIn("X^2-2X-3", draft["fields"]["expression"])
        self.assertEqual(draft["files"][0]["document"]["ok"], True)

    def test_uploaded_scene_json_is_used_directly(self) -> None:
        scene = {
            "title": "CUSTOM SCENE",
            "blocks": [
                {
                    "id": "circle",
                    "label": "CIRCLE",
                    "label_zh": "圆",
                    "primitives": [{"type": "circle", "cx": 50, "cy": 50, "r": 20}],
                }
            ],
        }
        content = json.dumps(scene, ensure_ascii=False).encode("utf-8")
        draft = self.agent.analyze(
            {
                "subject": "auto",
                "files": [{"name": "scene.json", "data": base64.b64encode(content).decode("ascii")}],
            }
        )
        self.assertEqual(draft["subject"], "scene")
        self.assertTrue(draft["ready"])
        self.assertEqual(draft["fields"]["summary"]["block_count"], 1)
        self.assertTrue(any("scene.json" in note for note in draft["notes"]))

    def test_unsupported_request_is_refused(self) -> None:
        draft = self.agent.analyze({"subject": "auto", "text": "帮我写一篇关于春天的作文"})
        self.assertEqual(draft["subject"], "unsupported")
        self.assertFalse(draft["ready"])
        self.assertTrue(draft["questions"])

    def test_explicit_template_without_text(self) -> None:
        draft = self.agent.analyze({"subject": "auto", "template": "pythagorean"})
        self.assertEqual(draft["subject"], "scene")
        self.assertTrue(draft["ready"])
        self.assertEqual(draft["fields"]["template"], "pythagorean")

    def test_scene_title_override_reaches_build(self) -> None:
        draft = self.agent.analyze({"subject": "auto", "template": "triangle"})
        scene = dict(draft["fields"]["scene"])
        scene["title"] = "RIGHT TRIANGLE"
        with patch("touchlearn.agent.agent.synthesize_windows_tts", return_value=(False, "test environment")):
            result = self.agent.build(
                draft["id"],
                overrides={"fields": {"scene": scene}},
                public_base_url="http://127.0.0.1:8765",
            )
        manifest = json.loads(
            (Path(self.temp.name) / "jobs" / result["job_id"] / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["title"], "RIGHT TRIANGLE")


if __name__ == "__main__":
    unittest.main()
