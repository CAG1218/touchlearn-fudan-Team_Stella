from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import EXAMPLES  # noqa: F401  (ensures src is on sys.path)

from touchlearn import model3d
from touchlearn.agent.agent import TouchLearnAgent
from touchlearn.agent.llm import LLMResult
from touchlearn.agent.reconstruct import ReconstructionAsset
from touchlearn.agent.volumetric import _box_xy
from touchlearn.geometry import Mesh


def _generative_status() -> dict:
    return {
        "configured": True,
        "kind": "reconstruction",
        "provider": "tokenhub",
        "label": "腾讯云 TokenHub",
        "model": "hy-3d-3.1",
        "text_model": "tripo-3d-3.1",
        "text_to_3d": True,
        "generative": True,
        "quality": "high",
        "face_budget": 1_500_000,
        "model_shortlist": ["tripo-3d-3.1", "hy-3d-3.1"],
    }


def _box(width: float, depth: float, height: float) -> Mesh:
    mesh = Mesh([])
    _box_xy(mesh, -width / 2.0, -depth / 2.0, width, depth, 0.0, height)
    return mesh


class AgentModel3DTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.agent = TouchLearnAgent(output_root=Path(self.temp.name))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_both_modes_build_a_model_and_a_companion_plate(self) -> None:
        draft = self.agent.analyze(
            {
                "text": "需要一个太阳系3D模型，带底座和中文盲文标注",
                "generation_modes": ["tactile", "model3d"],
            }
        )
        self.assertEqual(draft["subject"], "model3d")
        self.assertTrue(draft["ready"])
        fields = draft["fields"]
        self.assertEqual(fields["kind"], "solar_system")
        self.assertEqual(fields["planet_count"], 7)
        self.assertEqual(fields["modes"], ["tactile", "model3d"])
        self.assertTrue(fields["companion_tactile"])
        self.assertEqual(len(fields["braille"]["labels"]), 7)

        with patch("touchlearn.agent.agent.synthesize_speech", return_value=(False, "test environment", None)):
            result = self.agent.build(draft["id"], public_base_url="http://127.0.0.1:8765")

        job_dir = Path(self.temp.name) / "jobs" / result["job_id"]
        for name in ("model.stl", "preview.svg", "model_spec.json", "plate_model.stl", "plate_preview.svg", "plate_diagram.json"):
            self.assertTrue((job_dir / name).is_file(), name)

        manifest = json.loads((job_dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["subject"], "model3d")
        self.assertEqual(manifest["mode"], "model3d")
        self.assertEqual(manifest["print"]["paper"], "3D")
        self.assertEqual(manifest["geometry"]["kind"], "model3d")
        self.assertTrue(manifest["geometry"]["mesh"]["watertight"])
        self.assertEqual(manifest["geometry"]["companion"]["paper"], "A4")
        self.assertEqual(
            [item["text"] for item in manifest["geometry"]["meta"]["braille"]["labels"]],
            ["水星", "金星", "地球", "火星", "木星", "土星", "天王星"],
        )

        report = json.loads((job_dir / "compliance.json").read_text(encoding="utf-8"))
        self.assertTrue(report["printable"])
        self.assertEqual(report["counts"].get("FAIL", 0), 0)

    def test_model_only_request_skips_the_companion_plate(self) -> None:
        draft = self.agent.analyze(
            {
                "text": "做一个八大行星的太阳系3D模型",
                "generation_modes": ["model3d"],
            }
        )
        self.assertEqual(draft["subject"], "model3d")
        self.assertEqual(draft["fields"]["planet_count"], 8)
        self.assertFalse(draft["fields"]["companion_tactile"])

    def test_follow_up_inherits_the_previous_model_description(self) -> None:
        first = self.agent.analyze(
            {
                "text": "做一个太阳系3D模型，带底座和中文盲文标注",
                "generation_modes": ["model3d"],
            }
        )
        second = self.agent.analyze(
            {
                "text": "把行星名称改短一点",
                "generation_modes": ["model3d"],
                "reuse_draft_id": first["id"],
            }
        )
        self.assertEqual(second["subject"], "model3d")
        self.assertIn("太阳系", second["fields"]["context_text"])
        self.assertIn("把行星名称改短一点", second["fields"]["context_text"])
        self.assertEqual(second["fields"]["context"]["reused_draft_id"], first["id"])
        self.assertEqual(second["fields"]["context"]["previous_subject"], "model3d")

    def test_empty_modes_keep_the_tactile_default(self) -> None:
        draft = self.agent.analyze({"text": "画一个串联电路：电池、开关、电阻和灯泡"})
        self.assertEqual(draft["subject"], "scene")
        self.assertEqual(draft["fields"]["template"], "circuit")

    def test_generative_route_uses_the_provider_instead_of_primitives(self) -> None:
        plan = model3d.Model3DPlan(
            ok=True,
            spec={
                "kind": "assembly",
                "title": "机械臂",
                "title_zh": "机械臂",
                "parts": [
                    {
                        "id": "arm",
                        "name_zh": "机械臂",
                        "shape": "box",
                        "size": [20, 20, 80],
                        "position": [0, 0, 40],
                    }
                ],
            },
            confidence=0.8,
            provider="vision",
            model="vlm",
        )
        with patch("touchlearn.agent.agent.plan_model3d", return_value=plan):
            with patch("touchlearn.agent.agent.reconstruction_status", return_value=_generative_status()):
                with patch("touchlearn.agent.agent.generative_enabled", return_value=True):
                    draft = self.agent.analyze(
                        {
                            "text": "做一个六轴机械臂3D模型",
                            "generation_modes": ["tactile", "model3d"],
                        }
                    )

        self.assertTrue(draft["ready"])
        fields = draft["fields"]
        self.assertEqual(fields["kind"], "generative")
        self.assertEqual(fields["summary"]["kind"], "generative")
        self.assertEqual(fields["part_count"], 0)
        self.assertFalse(fields["companion_tactile"])
        info = fields["provider_generation"]
        self.assertTrue(info["enabled"])
        self.assertEqual(info["mode"], "text_to_3d")
        self.assertEqual(info["provider"], "tokenhub")
        self.assertEqual(info["model"], "tripo-3d-3.1")
        self.assertEqual(info["quality"], "high")
        self.assertIn("不要包含文字", info["prompt"])
        self.assertEqual(fields["labels"], [{"text": "机械臂", "target": "主体"}])
        self.assertEqual([item["text"] for item in fields["braille"]["labels"]], ["机械臂"])

    def test_generative_build_writes_stl_generation_and_compliance(self) -> None:
        plan = model3d.Model3DPlan(
            ok=True,
            spec={"kind": "assembly", "title": "机械臂", "title_zh": "机械臂", "parts": []},
            confidence=0.8,
        )
        with patch("touchlearn.agent.agent.plan_model3d", return_value=plan):
            with patch("touchlearn.agent.agent.reconstruction_status", return_value=_generative_status()):
                with patch("touchlearn.agent.agent.generative_enabled", return_value=True):
                    draft = self.agent.analyze(
                        {"text": "做一个六轴机械臂3D模型", "generation_modes": ["model3d"]}
                    )

        asset = ReconstructionAsset(
            provider="tokenhub",
            task_id="task-generative-1",
            files={},
            meta={"mode": "text_to_3d", "model_version": "tripo-3d-3.1"},
            mesh=_box(30.0, 20.0, 80.0),
        )
        with patch("touchlearn.agent.agent.text_to_3d", return_value=asset) as fake:
            with patch("touchlearn.agent.agent.synthesize_speech", return_value=(False, "test environment", None)):
                result = self.agent.build(draft["id"], public_base_url="http://127.0.0.1:8765")

        job_dir = Path(self.temp.name) / "jobs" / result["job_id"]
        for name in ("model.stl", "preview.svg", "model_spec.json", "model_view.html", "generation.json"):
            self.assertTrue((job_dir / name).is_file(), name)
        self.assertEqual(fake.call_count, 1)
        sent_prompt = fake.call_args.args[0]
        self.assertIn("六轴机械臂", sent_prompt)

        generation = json.loads((job_dir / "generation.json").read_text(encoding="utf-8"))
        self.assertEqual(generation["mode"], "text_to_3d")
        self.assertEqual(generation["provider"], "tokenhub")
        self.assertEqual(generation["task_id"], "task-generative-1")
        self.assertEqual(generation["plan"]["title"], "机械臂")

        spec = json.loads((job_dir / "model_spec.json").read_text(encoding="utf-8"))
        self.assertEqual(spec["kind"], "generative_model3d")
        self.assertEqual(spec["provider"]["meta"]["mode"], "text_to_3d")

        report = json.loads((job_dir / "compliance.json").read_text(encoding="utf-8"))
        checks = {item["rule_id"]: item["status"] for item in report["checks"]}
        self.assertEqual(checks.get("generation.provider"), "PASS")
        self.assertEqual(checks.get("generation.orientation"), "PASS")
        self.assertEqual(checks.get("encoding.chinese_braille"), "PASS")

        manifest = json.loads((job_dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["geometry"]["kind"], "generative_model3d")
        self.assertEqual(manifest["geometry"]["provider"]["provider"], "tokenhub")

    def test_model3d_retries_without_reasoning_on_length(self) -> None:
        truncated = LLMResult(False, finish_reason="length", message="truncated")
        with patch(
            "touchlearn.model3d.chat_json",
            side_effect=[truncated, truncated],
        ) as fake:
            plan = model3d.plan_model3d("做一个尾翼三维模型", effort="high")
        self.assertFalse(plan.ok)
        self.assertEqual(fake.call_count, 2)
        self.assertEqual(fake.call_args_list[0].kwargs["effort"], "high")
        self.assertEqual(fake.call_args_list[1].kwargs["effort"], "off")
        self.assertNotIn("max_tokens", fake.call_args_list[0].kwargs)


if __name__ == "__main__":
    unittest.main()
