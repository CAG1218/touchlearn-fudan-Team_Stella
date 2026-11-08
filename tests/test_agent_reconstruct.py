from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import EXAMPLES  # noqa: F401  (ensures src is on sys.path)

from touchlearn.agent.agent import TouchLearnAgent, _public_http_base
from touchlearn.agent.reconstruct import ReconstructionAsset, ReconstructionPlan
from touchlearn.agent.volumetric import _box_xy
from touchlearn.geometry import Mesh

TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _three_view_sheet() -> bytes:
    from io import BytesIO

    from PIL import Image, ImageDraw

    image = Image.new("RGB", (900, 300), (252, 252, 252))
    draw = ImageDraw.Draw(image)
    for index, left in enumerate((40, 340, 640)):
        draw.rectangle(
            (left, 40, left + 220, 260),
            fill=(40 + index * 30, 90, 200 - index * 30),
        )
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()

STATUS = {
    "configured": True,
    "kind": "reconstruction",
    "provider": "tripo",
    "label": "Tripo 3D",
    "model": "v3",
    "has_key": True,
    "mode": "remote-3d",
    "timeout_s": 600.0,
    "poll_s": 4.0,
    "message": "照片级三维重建接口已配置。",
    "environment": [],
    "privacy": "",
}

STATUS_TEXT = {**STATUS, "text_to_3d": True, "generative": True}


def _plan() -> ReconstructionPlan:
    return ReconstructionPlan(
        ok=True,
        title="小鸟",
        subject="一只站在树枝上的小鸟",
        keep_features=["羽毛纹理", "尾巴"],
        changes=["展开翅膀"],
        height_mm=110.0,
        base="disc",
        margin_mm=10.0,
        labels=[{"text": "小鸟", "target": "主体"}, {"text": "翅膀", "target": "翅膀"}],
        confidence=0.82,
        provider="vision",
        model="vlm",
        message="已生成重建计划。",
    )


def _box(width: float, depth: float, height: float) -> Mesh:
    mesh = Mesh([])
    _box_xy(mesh, -width / 2.0, -depth / 2.0, width, depth, 0.0, height)
    return mesh


class AgentReconstructTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.agent = TouchLearnAgent(output_root=Path(self.temp.name))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_photo_request_routes_to_reconstruction_and_prepares_fields(self) -> None:
        with patch("touchlearn.agent.agent.reconstruction_status", return_value=STATUS), patch(
            "touchlearn.agent.agent.plan_reconstruction", return_value=_plan()
        ):
            draft = self.agent.analyze(
                {
                    "text": "根据照片重建这只小鸟，保留羽毛，把翅膀展开，高 110 毫米",
                    "generation_modes": ["reconstruct"],
                    "files": [{"name": "bird.png", "data": base64.b64encode(TINY_PNG).decode("ascii")}],
                }
            )

        self.assertEqual(draft["subject"], "reconstruct")
        self.assertEqual(draft["output_mode"], "reconstruction")
        self.assertTrue(draft["ready"])
        self.assertEqual(draft["fields"]["image_count"], 1)
        self.assertEqual(draft["fields"]["height_mm"], 110.0)
        self.assertEqual([item["text"] for item in draft["fields"]["labels"]], ["小鸟", "翅膀"])
        self.assertEqual(draft["fields"]["braille"]["labels"][1]["text"], "翅膀")
        self.assertTrue(draft["fields"]["braille"]["labels"][1]["cells"])
        self.assertTrue(draft["fields"]["reconstruction"]["configured"])

    def test_reconstruction_without_a_photo_is_not_ready(self) -> None:
        with patch("touchlearn.agent.agent.reconstruction_status", return_value=STATUS), patch(
            "touchlearn.agent.agent.plan_reconstruction", return_value=_plan()
        ):
            draft = self.agent.analyze(
                {"text": "照片级重建一只小鸟", "generation_modes": ["reconstruct"]}
            )
        self.assertEqual(draft["subject"], "reconstruct")
        self.assertFalse(draft["ready"])
        self.assertEqual(draft["fields"]["image_count"], 0)
        self.assertTrue(any("至少一张照片" in note for note in draft["notes"]))

    def test_text_follow_up_reuses_the_previous_photo(self) -> None:
        with patch("touchlearn.agent.agent.reconstruction_status", return_value=STATUS), patch(
            "touchlearn.agent.agent.plan_reconstruction", return_value=_plan()
        ):
            first = self.agent.analyze(
                {
                    "text": "按照片重建这个蓝裙女仆",
                    "generation_modes": ["reconstruct"],
                    "files": [{"name": "maid.png", "data": base64.b64encode(TINY_PNG).decode("ascii")}],
                }
            )
            second = self.agent.analyze(
                {
                    "text": "这是同一个角色的三个视角，把腿改短一点",
                    "generation_modes": ["reconstruct"],
                    "reuse_draft_id": first["id"],
                }
            )

        self.assertEqual(second["subject"], "reconstruct")
        self.assertEqual(second["fields"]["image_count"], 1)
        self.assertEqual(second["fields"]["context"]["carried_images"], 1)
        self.assertEqual(second["fields"]["context"]["reused_draft_id"], first["id"])
        self.assertIn("把腿改短一点", second["fields"]["context"]["context_text"])
        self.assertTrue(any("沿用上一轮草稿" in note for note in second["notes"]))

    def test_three_view_sheet_is_split_into_multiview_inputs(self) -> None:
        with patch("touchlearn.agent.agent.reconstruction_status", return_value=STATUS), patch(
            "touchlearn.agent.agent.plan_reconstruction", return_value=_plan()
        ):
            draft = self.agent.analyze(
                {
                    "text": "这是同一个女仆角色的三视图，请合成一个 3D 模型",
                    "generation_modes": ["reconstruct"],
                    "files": [
                        {
                            "name": "maid-views.png",
                            "data": base64.b64encode(_three_view_sheet()).decode("ascii"),
                        }
                    ],
                }
            )

        self.assertTrue(draft["fields"]["sheet"])
        self.assertFalse(draft["fields"]["sheet_blocked"])
        self.assertEqual(draft["fields"]["source_image_count"], 1)
        self.assertEqual(draft["fields"]["image_count"], 3)
        self.assertEqual(draft["fields"]["view_order"], ["front", "left", "back"])
        self.assertTrue(any(item.get("skip_for_reconstruction") for item in draft["files"]))
        self.assertTrue(any("不会再把它当成 3 个人物" in note for note in draft["notes"]))

    def test_unsplittable_sheet_blocks_the_photo_route(self) -> None:
        with patch("touchlearn.agent.agent.reconstruction_status", return_value=STATUS), patch(
            "touchlearn.agent.agent.plan_reconstruction", return_value=_plan()
        ), patch("touchlearn.agent.agent.split_multiview_sheet", return_value=[]):
            draft = self.agent.analyze(
                {
                    "text": "这是同一个人的三视图，合成一个模型",
                    "generation_modes": ["reconstruct"],
                    "files": [{"name": "maid.png", "data": base64.b64encode(_three_view_sheet()).decode("ascii")}],
                }
            )

        self.assertTrue(draft["fields"]["sheet_blocked"])
        self.assertFalse(draft["ready"])
        self.assertTrue(any("自动拆分失败" in note for note in draft["notes"]))
        self.assertTrue(any("拆成 2-4 张" in question for question in draft["questions"]))

    def test_text_route_builds_without_a_photo(self) -> None:
        def fake_text_to_3d(prompt, plan, *, dest, progress=None):
            dest = Path(dest)
            glb = dest / "model_textured.glb"
            glb.write_bytes(b"glTF-demo")
            if progress:
                progress(50, "text provider running")
            return ReconstructionAsset(
                provider="tokenhub",
                task_id="task-text123456",
                files={"glb": glb},
                meta={"prompt": prompt},
                mesh=_box(20.0, 14.0, 40.0),
            )

        with patch("touchlearn.agent.agent.reconstruction_status", return_value=STATUS_TEXT), patch(
            "touchlearn.agent.agent.plan_reconstruction", return_value=_plan()
        ):
            draft = self.agent.analyze(
                {
                    "text": "生成一只展开翅膀的小鸟，不要照片",
                    "generation_modes": ["reconstruct"],
                }
            )

        self.assertEqual(draft["fields"]["generation_route"], "text")
        self.assertTrue(draft["fields"]["text_route"]["available"])
        self.assertTrue(draft["ready"])

        with patch("touchlearn.agent.agent.text_to_3d", side_effect=fake_text_to_3d), patch(
            "touchlearn.agent.agent.synthesize_speech", return_value=(False, "test environment", None)
        ):
            result = self.agent.build(
                draft["id"],
                overrides={"fields": {"generation_route": "text"}},
                public_base_url="http://127.0.0.1:8765",
            )

        job_dir = Path(self.temp.name) / "jobs" / result["job_id"]
        spec = json.loads((job_dir / "model_spec.json").read_text(encoding="utf-8"))
        self.assertEqual(spec["generation_route"], "text")
        record = json.loads((job_dir / "reconstruction.json").read_text(encoding="utf-8"))
        self.assertEqual(record["generation_route"], "text")
        self.assertIn("文字重走文生3D", record["instructions"]["note"])

    def test_build_writes_textured_and_printable_outputs(self) -> None:
        def fake_reconstruct(images, plan, *, dest, progress=None, image_urls=()):
            dest = Path(dest)
            glb = dest / "model_textured.glb"
            glb.write_bytes(b"glTF-demo")
            thumbnail = dest / "thumbnail.png"
            thumbnail.write_bytes(TINY_PNG)
            if progress:
                progress(50, "provider running")
            return ReconstructionAsset(
                provider="tripo",
                task_id="task-abc123456789",
                files={"glb": glb, "thumbnail": thumbnail},
                meta={"prompt": plan.provider_prompt, "glb": {"source": glb.name}},
                mesh=_box(20.0, 14.0, 40.0),
            )

        with patch("touchlearn.agent.agent.reconstruction_status", return_value=STATUS), patch(
            "touchlearn.agent.agent.plan_reconstruction", return_value=_plan()
        ):
            draft = self.agent.analyze(
                {
                    "text": "重建这只小鸟并展开翅膀",
                    "generation_modes": ["reconstruct"],
                    "files": [{"name": "bird.png", "data": base64.b64encode(TINY_PNG).decode("ascii")}],
                }
            )

        with patch("touchlearn.agent.agent.reconstruct", side_effect=fake_reconstruct), patch(
            "touchlearn.agent.agent.synthesize_speech", return_value=(False, "test environment", None)
        ):
            result = self.agent.build(draft["id"], public_base_url="http://127.0.0.1:8765")

        job_dir = Path(self.temp.name) / "jobs" / result["job_id"]
        for name in (
            "model.stl",
            "preview.svg",
            "model_spec.json",
            "photo_view.html",
            "reconstruction.json",
            "model_textured.glb",
            "thumbnail.png",
            "manifest.json",
            "compliance.json",
        ):
            self.assertTrue((job_dir / name).is_file(), name)

        manifest = json.loads((job_dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["subject"], "reconstruct")
        self.assertEqual(manifest["mode"], "reconstruction")
        self.assertEqual(manifest["geometry"]["kind"], "reconstruction")
        self.assertEqual(manifest["geometry"]["provider"]["provider"], "tripo")
        self.assertEqual(manifest["geometry"]["meta"]["braille"]["title"], "小鸟")
        self.assertTrue(manifest["geometry"]["meta"]["watertight"])
        self.assertTrue(manifest["geometry"]["meta"]["fits_build_volume"])

        report = json.loads((job_dir / "compliance.json").read_text(encoding="utf-8"))
        self.assertEqual(report["counts"].get("FAIL", 0), 0)
        rules = {check["rule_id"]: check for check in report["checks"]}
        self.assertEqual(rules["reconstruction.provider"]["status"], "PASS")
        self.assertEqual(rules["reconstruction.texture"]["status"], "PASS")
        self.assertEqual(rules["reconstruction.instructions"]["status"], "NEEDS_REVIEW")
        self.assertEqual(rules["reconstruction.scale"]["status"], "DEVIATION")
        self.assertEqual(rules["encoding.chinese_braille"]["status"], "PASS")

        record = json.loads((job_dir / "reconstruction.json").read_text(encoding="utf-8"))
        self.assertEqual(record["textured_model"], "model_textured.glb")
        self.assertEqual(record["height_mm"], 110.0)
        self.assertEqual(record["instructions"]["changes"], ["展开翅膀"])

        page = (job_dir / "photo_view.html").read_text(encoding="utf-8")
        self.assertIn("model_textured.glb", page)
        self.assertIn("展开翅膀", page)

    def test_build_rejects_an_empty_provider_mesh(self) -> None:
        with patch("touchlearn.agent.agent.reconstruction_status", return_value=STATUS), patch(
            "touchlearn.agent.agent.plan_reconstruction", return_value=_plan()
        ):
            draft = self.agent.analyze(
                {
                    "text": "重建这只小鸟",
                    "generation_modes": ["reconstruct"],
                    "files": [{"name": "bird.png", "data": base64.b64encode(TINY_PNG).decode("ascii")}],
                }
            )

        def fake_reconstruct(images, plan, *, dest, progress=None, image_urls=()):
            return ReconstructionAsset(provider="tripo", task_id="task-empty", files={}, mesh_error="empty")

        with patch("touchlearn.agent.agent.reconstruct", side_effect=fake_reconstruct), patch(
            "touchlearn.agent.agent.synthesize_speech", return_value=(False, "test environment", None)
        ):
            with self.assertRaises(Exception) as caught:
                self.agent.build(draft["id"], public_base_url="http://127.0.0.1:8765")
        self.assertIn("无法生成可打印 STL", str(caught.exception))


class PublicBaseUrlTests(unittest.TestCase):
    def test_only_public_http_addresses_are_accepted(self) -> None:
        self.assertEqual(_public_http_base("http://47.103.61.115:8765/"), "http://47.103.61.115:8765")
        self.assertEqual(_public_http_base("https://touchlearn.example.com"), "https://touchlearn.example.com")
        for local in (
            "http://127.0.0.1:8765",
            "http://localhost:8765",
            "http://192.168.1.10:8765",
            "http://10.0.0.5:8765",
            "http://0.0.0.0:8765",
            "http://touchlearn.local",
            "ftp://47.103.61.115",
            "",
        ):
            self.assertEqual(_public_http_base(local), "", local)


if __name__ == "__main__":
    unittest.main()
