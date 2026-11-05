from __future__ import annotations

import json
import os
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests import EXAMPLES  # noqa: F401  (ensures src is on sys.path)

from touchlearn.agent.reconstruct import (
    ReconstructionError,
    ReconstructionPlan,
    _normalize_plan,
    _tokenhub_image_body,
    _tokenhub_multiview_body,
    _tokenhub_prompt_body,
    _tokenhub_reconstruct,
    _tokenhub_results,
    multiview_slots,
    plan_reconstruction,
    provider_model,
    provider_name,
    quality_name,
    quality_preset,
    read_glb_mesh,
    reconstruction_status,
    text_model,
    text_to_3d,
)
from touchlearn.agent.reconstruct_print import compose_printable
from touchlearn.agent.volumetric import _box_xy
from touchlearn.geometry import Mesh


def _box(width: float, depth: float, height: float) -> Mesh:
    mesh = Mesh([])
    _box_xy(mesh, -width / 2.0, -depth / 2.0, width, depth, 0.0, height)
    return mesh


def _glb_bytes(
    positions: list[tuple[float, float, float]],
    indices: list[int],
    *,
    translation: tuple[float, float, float] | None = None,
    extensions_required: list[str] | None = None,
) -> bytes:
    position_bytes = b"".join(struct.pack("<3f", *point) for point in positions)
    index_bytes = b"".join(struct.pack("<H", index) for index in indices)
    binary = position_bytes + index_bytes
    binary += b"\x00" * ((4 - len(binary) % 4) % 4)
    document: dict = {
        "asset": {"version": "2.0"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0, **({"translation": list(translation)} if translation else {})}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0}, "indices": 1}]}],
        "accessors": [
            {"bufferView": 0, "componentType": 5126, "count": len(positions), "type": "VEC3"},
            {"bufferView": 1, "componentType": 5123, "count": len(indices), "type": "SCALAR"},
        ],
        "bufferViews": [
            {"buffer": 0, "byteOffset": 0, "byteLength": len(position_bytes)},
            {"buffer": 0, "byteOffset": len(position_bytes), "byteLength": len(index_bytes)},
        ],
        "buffers": [{"byteLength": len(binary)}],
    }
    if extensions_required:
        document["extensionsRequired"] = extensions_required
    json_bytes = json.dumps(document, ensure_ascii=False).encode("utf-8")
    json_bytes += b" " * ((4 - len(json_bytes) % 4) % 4)
    total = 12 + 8 + len(json_bytes) + 8 + len(binary)
    return (
        struct.pack("<4sII", b"glTF", 2, total)
        + struct.pack("<II", len(json_bytes), 0x4E4F534A)
        + json_bytes
        + struct.pack("<II", len(binary), 0x004E4942)
        + binary
    )


class ReconstructionStatusTests(unittest.TestCase):
    def test_status_never_returns_the_key(self) -> None:
        with patch.dict(
            os.environ,
            {
                "TOUCHLEARN_3D_PROVIDER": "meshy",
                "TOUCHLEARN_3D_KEY": "super-secret-token",
                "TOUCHLEARN_3D_MODEL": "meshy-5",
            },
            clear=False,
        ):
            status = reconstruction_status()
        self.assertTrue(status["configured"])
        self.assertEqual(status["provider"], "meshy")
        self.assertEqual(status["label"], "Meshy")
        self.assertEqual(status["model"], "meshy-5")
        self.assertNotIn("super-secret-token", json.dumps(status, ensure_ascii=False))
        self.assertIn("TOUCHLEARN_3D_KEY", status["environment"])

    def test_provider_aliases_and_default(self) -> None:
        cases = {
            "tripo3d": "tripo",
            "Tripo-3D": "tripo",
            "meshy3d": "meshy",
            "Meshy-3D": "meshy",
            "tokenhub": "tokenhub",
            "TokenHub-3D": "tokenhub",
            "hy-3d-3.1": "tokenhub",
            "hunyuan3d": "tokenhub",
            "unknown": "tripo",
            "": "tripo",
        }
        for raw, expected in cases.items():
            with patch.dict(os.environ, {"TOUCHLEARN_3D_PROVIDER": raw}, clear=False):
                self.assertEqual(provider_name(), expected, raw)

    def test_tokenhub_image_defaults_to_tripo_3d_p1(self) -> None:
        with patch.dict(os.environ, {"TOUCHLEARN_3D_PROVIDER": "tokenhub"}, clear=True):
            self.assertEqual(provider_model("tokenhub"), "tripo-3d-p1")
        with patch.dict(
            os.environ,
            {"TOUCHLEARN_3D_PROVIDER": "tokenhub", "TOUCHLEARN_3D_TOKENHUB_MODEL": "hy-3d-express"},
            clear=True,
        ):
            self.assertEqual(provider_model("tokenhub"), "hy-3d-express")

    def test_tokenhub_reuses_the_llm_key_but_other_providers_do_not(self) -> None:
        with patch.dict(
            os.environ,
            {"TOUCHLEARN_3D_PROVIDER": "tokenhub", "TOUCHLEARN_LLM_KEY": "shared-key"},
            clear=True,
        ):
            status = reconstruction_status()
        self.assertTrue(status["configured"])
        self.assertTrue(status["has_key"])
        self.assertNotIn("shared-key", json.dumps(status, ensure_ascii=False))

        with patch.dict(
            os.environ,
            {"TOUCHLEARN_3D_PROVIDER": "tripo", "TOUCHLEARN_LLM_KEY": "shared-key"},
            clear=True,
        ):
            self.assertFalse(reconstruction_status()["configured"])


class ReconstructionPlanTests(unittest.TestCase):
    def test_normalization_clamps_and_cleans_model_output(self) -> None:
        plan = _normalize_plan(
            {
                "title": "一只非常可爱的小鸟模型",
                "subject": "一只站在树枝上的鸟",
                "keep_features": ["羽毛纹理", "羽毛纹理", "尾巴"],
                "changes": ["展开翅膀"],
                "height_mm": 999,
                "base": "rect",
                "margin_mm": -3,
                "labels": [
                    {"text": "小鸟", "target": "主体"},
                    {"text": "小鸟", "target": "重复"},
                    {"text": "翅膀", "target": "翅膀"},
                    {"text": "尾巴", "target": "尾巴"},
                    {"text": "树枝", "target": "树枝"},
                ],
            },
            "一只鸟",
            provider="tripo",
            model="v3",
        )
        self.assertTrue(plan.ok)
        self.assertEqual(plan.title, "一只非常可爱")
        self.assertEqual(plan.height_mm, 200.0)
        self.assertEqual(plan.base, "rounded_rect")
        self.assertEqual(plan.margin_mm, 4.0)
        self.assertEqual(plan.keep_features, ["羽毛纹理", "尾巴"])
        self.assertEqual([item["text"] for item in plan.labels], ["小鸟", "翅膀", "尾巴"])
        self.assertIn("保留：羽毛纹理", plan.provider_prompt)
        self.assertIn("调整：展开翅膀", plan.provider_prompt)

    def test_empty_labels_fall_back_to_the_title(self) -> None:
        plan = _normalize_plan({"title": "小猫", "labels": []}, "小猫", provider="", model="")
        self.assertEqual(plan.labels, [{"text": "小猫", "target": "主体"}])

    def test_plan_falls_back_when_the_model_is_unavailable(self) -> None:
        with patch(
            "touchlearn.agent.reconstruct.chat_json",
            return_value=SimpleNamespace(ok=False, data=None, message="offline", provider="", model=""),
        ):
            plan = plan_reconstruction("做一个红色小鸟模型")
        self.assertTrue(plan.ok)
        self.assertEqual(plan.title, "做一个红")
        self.assertEqual(plan.confidence, 0.3)
        self.assertTrue(plan.notes)
        self.assertTrue(plan.questions)

    def test_plan_uses_model_json_when_available(self) -> None:
        with patch(
            "touchlearn.agent.reconstruct.chat_json",
            return_value=SimpleNamespace(
                ok=True,
                data={"title": "小鸟", "subject": "鸟", "height_mm": 120, "labels": [{"text": "翅膀", "target": "翅膀"}]},
                message="",
                provider="vision",
                model="vlm",
            ),
        ):
            plan = plan_reconstruction("保留羽毛，展开翅膀")
        self.assertEqual(plan.title, "小鸟")
        self.assertEqual(plan.height_mm, 120.0)
        self.assertEqual(plan.provider, "vision")
        self.assertEqual(plan.labels[0]["text"], "翅膀")


class GlbParserTests(unittest.TestCase):
    def _read(self, raw: bytes):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "model.glb"
            path.write_bytes(raw)
            return read_glb_mesh(path)

    def test_reads_triangle_and_converts_y_up_to_z_up(self) -> None:
        mesh, meta = self._read(
            _glb_bytes(
                [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)],
                [0, 1, 2],
            )
        )
        self.assertEqual(mesh.triangle_count(), 1)
        self.assertEqual(mesh.triangles[0], ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 0.0, 1.0)))
        self.assertEqual(meta["triangles"], 1)
        self.assertFalse(meta["has_uv"])

    def test_applies_node_translation(self) -> None:
        mesh, _ = self._read(
            _glb_bytes(
                [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)],
                [0, 1, 2],
                translation=(2.0, 3.0, 4.0),
            )
        )
        first = mesh.triangles[0][0]
        self.assertAlmostEqual(first[0], 2.0)
        self.assertAlmostEqual(first[1], -4.0)
        self.assertAlmostEqual(first[2], 3.0)

    def test_rejects_compressed_extensions(self) -> None:
        with self.assertRaises(ReconstructionError):
            self._read(
                _glb_bytes(
                    [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)],
                    [0, 1, 2],
                    extensions_required=["KHR_draco_mesh_compression"],
                )
            )


class PrintableCompositionTests(unittest.TestCase):
    def test_composes_base_braille_and_build_volume_fit(self) -> None:
        plan = ReconstructionPlan(
            ok=True,
            title="小鸟",
            subject="一只鸟",
            height_mm=100.0,
            base="disc",
            margin_mm=10.0,
            labels=[{"text": "小鸟", "target": "主体"}, {"text": "翅膀", "target": "翅膀"}],
        )
        result = compose_printable(_box(20.0, 12.0, 40.0), plan)
        meta = result.meta
        self.assertTrue(result.mesh.is_watertight())
        self.assertGreater(result.mesh.volume(), 0.0)
        self.assertAlmostEqual(meta["resolved_height_mm"], 100.0, places=1)
        self.assertEqual(meta["base"]["shape"], "disc")
        self.assertGreaterEqual(meta["base"]["radius_mm"], 58.0)
        self.assertLessEqual(meta["base"]["radius_mm"], 96.0)
        self.assertEqual(meta["braille"]["title"], "小鸟")
        self.assertEqual([item["text"] for item in meta["braille"]["labels"]], ["翅膀"])
        self.assertTrue(meta["braille"]["labels"][0]["cells"])
        self.assertTrue(meta["fits_build_volume"])
        self.assertLessEqual(meta["model_footprint_mm"], 152.0 + 1e-6)

    def test_rounded_rect_base_and_extra_labels(self) -> None:
        plan = ReconstructionPlan(
            ok=True,
            title="机器人",
            height_mm=150.0,
            base="rounded_rect",
            labels=[
                {"text": "机器人", "target": "主体"},
                {"text": "头部", "target": "头部"},
                {"text": "手臂", "target": "手臂"},
            ],
        )
        result = compose_printable(_box(30.0, 30.0, 60.0), plan)
        self.assertEqual(result.meta["base"]["shape"], "rounded_rect")
        self.assertEqual(result.meta["braille"]["title"], "机器人")
        self.assertEqual([item["text"] for item in result.meta["braille"]["labels"]], ["头部", "手臂"])
        self.assertTrue(result.meta["fits_build_volume"])

    def test_wide_mesh_is_capped_to_the_printable_footprint(self) -> None:
        plan = ReconstructionPlan(ok=True, title="板", height_mm=200.0, base="disc")
        result = compose_printable(_box(400.0, 20.0, 10.0), plan)
        self.assertLessEqual(result.meta["model_footprint_mm"], 152.0 + 1e-6)
        self.assertTrue(result.meta["fits_build_volume"])

    def test_empty_mesh_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            compose_printable(Mesh([]), ReconstructionPlan(ok=True, title="空"))


class TokenHubAdapterTests(unittest.TestCase):
    def test_submit_query_and_download_use_the_documented_fields(self) -> None:
        responses = [
            {"id": "task-1", "status": "queued"},
            {"status": "in_progress"},
            {
                "status": "completed",
                "data": [
                    {"type": "obj", "url": "https://cos.example.com/model.zip"},
                    {
                        "type": "glb",
                        "url": "https://cos.example.com/model.glb",
                        "preview_image_url": "https://cos.example.com/preview.png",
                    },
                ],
            },
        ]
        calls: list[tuple[str, dict]] = []

        def fake_request(method: str, url: str, *, headers: dict, payload=None, timeout: float = 0):
            calls.append((url, payload or {}))
            return responses.pop(0)

        def fake_download(url: str, dest: Path, *, timeout: float, limit: int = 0) -> Path:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"glTF")
            return dest

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "photo.png"
            image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
            with patch.dict(
                os.environ,
                {"TOUCHLEARN_3D_PROVIDER": "tokenhub", "TOUCHLEARN_3D_KEY": "secret"},
                clear=True,
            ):
                with patch("touchlearn.agent.reconstruct._request_json", side_effect=fake_request):
                    with patch("touchlearn.agent.reconstruct._download", side_effect=fake_download):
                        with patch("touchlearn.agent.reconstruct.time.sleep", return_value=None):
                            asset = _tokenhub_reconstruct(
                                [image],
                                ReconstructionPlan(ok=True),
                                dest=root / "out",
                                key="secret",
                                timeout=60.0,
                                interval=0.01,
                                progress=None,
                            )

        self.assertEqual(asset.provider, "tokenhub")
        self.assertEqual(asset.task_id, "task-1")
        self.assertIn("glb", asset.files)
        self.assertIn("thumbnail", asset.files)
        self.assertEqual(asset.meta["model_version"], "tripo-3d-p1")
        self.assertEqual(asset.meta["mode"], "image_to_3d")
        self.assertEqual(asset.meta["image_input"], "data_uri")
        self.assertFalse(asset.meta["prompt_forwarded"])

        submit_url, submit_body = calls[0]
        self.assertTrue(submit_url.endswith("/v1/api/3d/submit"))
        # TokenHub's P1 console example is exactly model + input.
        self.assertEqual(submit_body["model"], "tripo-3d-p1")
        self.assertTrue(str(submit_body["input"]).startswith("data:image/png;base64,"))
        self.assertEqual(set(submit_body), {"model", "input"})

        query_url, query_body = calls[1]
        self.assertTrue(query_url.endswith("/v1/api/3d/query"))
        self.assertEqual(query_body, {"model": "tripo-3d-p1", "id": "task-1"})

    def test_single_image_url_matches_the_documented_input_field(self) -> None:
        body = _tokenhub_image_body(
            "tripo-3d-p1",
            "image/jpeg",
            "QUJD",
            image_url="https://example.com/test.jpg",
        )
        self.assertEqual(body, {"model": "tripo-3d-p1", "input": "https://example.com/test.jpg"})

    def test_hy3d_image_body_keeps_the_base64_contract(self) -> None:
        with patch.dict(os.environ, {"TOUCHLEARN_3D_FACES": "400000"}, clear=True):
            body = _tokenhub_image_body("hy-3d-3.1", "image/png", "QUJD")
        self.assertEqual(body["image_base64"], "QUJD")
        self.assertEqual(body["generate_type"], "Normal")
        self.assertEqual(body["face_count"], 400_000)
        self.assertEqual(body["result_format"], "GLB")

    def test_multiview_slots_use_filenames_then_documented_order(self) -> None:
        paths = [
            Path("机械臂-左侧.jpg"),
            Path("机械臂-正面.jpg"),
            Path("机械臂-背面.jpg"),
            Path("机械臂-右侧.jpg"),
        ]
        slots = multiview_slots(paths)
        self.assertEqual(slots["front"].name, "机械臂-正面.jpg")
        self.assertEqual(slots["left"].name, "机械臂-左侧.jpg")
        self.assertEqual(slots["back"].name, "机械臂-背面.jpg")
        self.assertEqual(slots["right"].name, "机械臂-右侧.jpg")

        unlabelled = [Path("a.jpg"), Path("b.jpg"), Path("c.jpg")]
        fallback = multiview_slots(unlabelled)
        self.assertEqual([slot for slot in ("front", "left", "back") if slot in fallback], ["front", "left", "back"])
        self.assertEqual(fallback["front"].name, "a.jpg")
        self.assertEqual(multiview_slots([Path("only.jpg")]), {})

    def test_multiview_body_matches_the_console_example(self) -> None:
        body = _tokenhub_multiview_body(
            "tripo-3d-p1",
            {
                "front": "https://example.com/front.jpg",
                "left": "https://example.com/left.jpg",
                "back": "https://example.com/back.jpg",
                "right": "https://example.com/right.jpg",
            },
        )
        self.assertEqual(
            body,
            {
                "model": "tripo-3d-p1",
                "inputs": [
                    {
                        "front": "https://example.com/front.jpg",
                        "left": "https://example.com/left.jpg",
                        "back": "https://example.com/back.jpg",
                        "right": "https://example.com/right.jpg",
                    }
                ],
            },
        )

    def test_multiview_reconstruction_sends_urls_when_available(self) -> None:
        responses = [
            {"id": "task-mv", "status": "queued"},
            {
                "status": "completed",
                "data": [{"type": "glb", "url": "https://cos.example.com/model.glb"}],
            },
        ]
        calls: list[tuple[str, dict]] = []

        def fake_request(method: str, url: str, *, headers: dict, payload=None, timeout: float = 0):
            calls.append((url, payload or {}))
            return responses.pop(0)

        def fake_download(url: str, dest: Path, *, timeout: float, limit: int = 0) -> Path:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"glTF")
            return dest

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            images = []
            for name in ("front.jpg", "left.jpg", "back.jpg"):
                image = root / name
                image.write_bytes(b"\xff\xd8\xff\xe0" + b"0" * 32)
                images.append(image)
            with patch.dict(
                os.environ,
                {"TOUCHLEARN_3D_PROVIDER": "tokenhub", "TOUCHLEARN_3D_KEY": "secret"},
                clear=True,
            ):
                with patch("touchlearn.agent.reconstruct._request_json", side_effect=fake_request):
                    with patch("touchlearn.agent.reconstruct._download", side_effect=fake_download):
                        with patch("touchlearn.agent.reconstruct.time.sleep", return_value=None):
                            asset = _tokenhub_reconstruct(
                                images,
                                ReconstructionPlan(ok=True),
                                dest=root / "out",
                                key="secret",
                                timeout=60.0,
                                interval=0.01,
                                progress=None,
                                image_urls=[
                                    "https://47.103.61.115:8765/files/job/input-1.jpg",
                                    "https://47.103.61.115:8765/files/job/input-2.jpg",
                                    "https://47.103.61.115:8765/files/job/input-3.jpg",
                                ],
                            )

        self.assertEqual(asset.meta["mode"], "multiview_to_3d")
        self.assertEqual(asset.meta["images_used"], 3)
        self.assertEqual(asset.meta["image_input"], "url")
        submit_body = calls[0][1]
        self.assertEqual(set(submit_body), {"model", "inputs"})
        self.assertEqual(
            submit_body["inputs"],
            [
                {
                    "front": "https://47.103.61.115:8765/files/job/input-1.jpg",
                    "left": "https://47.103.61.115:8765/files/job/input-2.jpg",
                    "back": "https://47.103.61.115:8765/files/job/input-3.jpg",
                }
            ],
        )

    def test_result_parser_accepts_a_list_or_a_single_object(self) -> None:
        listed = {
            "status": "completed",
            "data": [
                {"type": "obj", "url": "https://cos.example.com/a.zip"},
                {"type": "glb", "url": "https://cos.example.com/a.glb"},
                {"type": "glb", "url": "https://cos.example.com/second.glb"},
            ],
        }
        self.assertEqual(
            _tokenhub_results(listed),
            {"obj": "https://cos.example.com/a.zip", "glb": "https://cos.example.com/a.glb"},
        )
        single = {"data": {"type": "glb", "url": "https://cos.example.com/b.glb"}}
        self.assertEqual(_tokenhub_results(single), {"glb": "https://cos.example.com/b.glb"})
        self.assertEqual(_tokenhub_results({"data": None}), {})


class TokenHubTextGenerationTests(unittest.TestCase):
    def test_quality_defaults_to_high_and_is_configurable(self) -> None:
        with patch.dict(os.environ, {"TOUCHLEARN_3D_QUALITY": "high"}, clear=True):
            self.assertEqual(quality_name(), "high")
            self.assertEqual(quality_preset()["tripo_faces"], 1_500_000)
        with patch.dict(os.environ, {"TOUCHLEARN_3D_QUALITY": "draft"}, clear=True):
            self.assertEqual(quality_preset()["tripo_faces"], 200_000)
            self.assertEqual(quality_preset()["hy3d_faces"], 300_000)
        with patch.dict(os.environ, {"TOUCHLEARN_3D_QUALITY": "unknown"}, clear=True):
            self.assertEqual(quality_name(), "high")

    def test_text_model_defaults_to_tripo_31_on_tokenhub(self) -> None:
        with patch.dict(os.environ, {"TOUCHLEARN_3D_PROVIDER": "tokenhub"}, clear=True):
            self.assertEqual(text_model("tokenhub"), "tripo-3d-3.1")
        with patch.dict(
            os.environ,
            {"TOUCHLEARN_3D_PROVIDER": "tokenhub", "TOUCHLEARN_3D_TEXT_MODEL": "hy-3d-3.1"},
            clear=True,
        ):
            self.assertEqual(text_model("tokenhub"), "hy-3d-3.1")

    def test_tripo_prompt_body_uses_documented_quality_fields(self) -> None:
        with patch.dict(
            os.environ,
            {"TOUCHLEARN_3D_QUALITY": "high", "TOUCHLEARN_3D_FACES": "300000"},
            clear=True,
        ):
            body = _tokenhub_prompt_body("tripo-3d-3.1", "一只六轴机械臂" * 300)
        self.assertEqual(body["model"], "tripo-3d-3.1")
        self.assertEqual(body["result_format"], "GLB")
        self.assertEqual(len(body["prompt"]), 1024)
        # The image-to-3D face override must not shrink the text-to-3D budget.
        self.assertEqual(body["face_limit"], 1_500_000)
        self.assertEqual(body["geometry_quality"], "detailed")
        self.assertEqual(body["texture_quality"], "detailed")
        self.assertTrue(body["pbr"])
        self.assertTrue(body["texture"])
        self.assertNotIn("image_base64", body)
        self.assertNotIn("face_count", body)

    def test_hy3d_prompt_body_uses_generate_type_and_face_count(self) -> None:
        with patch.dict(os.environ, {"TOUCHLEARN_3D_QUALITY": "standard"}, clear=True):
            body = _tokenhub_prompt_body("hy-3d-3.1", "一个带花纹的水杯")
        self.assertEqual(body["model"], "hy-3d-3.1")
        self.assertEqual(body["generate_type"], "Normal")
        self.assertEqual(body["face_count"], 500_000)
        self.assertTrue(body["enable_pbr"])
        self.assertNotIn("face_limit", body)
        self.assertNotIn("image_base64", body)

    def test_small_tripo_models_keep_their_documented_shapes(self) -> None:
        with patch.dict(os.environ, {"TOUCHLEARN_3D_QUALITY": "high"}, clear=True):
            p1 = _tokenhub_prompt_body("tripo-3d-p1", "水杯")
            p2 = _tokenhub_prompt_body("tripo-3d-p2", "水杯")
        # The P1 console example contains only model + prompt.
        self.assertEqual(p1, {"model": "tripo-3d-p1", "prompt": "水杯"})
        self.assertEqual(p2["face_limit"], 50_000)
        self.assertNotIn("geometry_quality", p2)

    def test_express_prompt_body_matches_the_console_example(self) -> None:
        self.assertEqual(
            _tokenhub_prompt_body("hy-3d-express", "一只小狗"),
            {"model": "hy-3d-express", "prompt": "一只小狗"},
        )

    def test_motion_model_is_rejected_for_static_printing(self) -> None:
        with patch.dict(
            os.environ,
            {
                "TOUCHLEARN_3D_PROVIDER": "tokenhub",
                "TOUCHLEARN_3D_KEY": "secret",
                "TOUCHLEARN_3D_TEXT_MODEL": "hy-3d-motion",
            },
            clear=True,
        ):
            with self.assertRaises(ReconstructionError) as caught:
                text_to_3d("一个人向前走", ReconstructionPlan(ok=True), dest=Path("."))
        self.assertIn("动画", str(caught.exception))

    def test_text_to_3d_submits_the_prompt_and_parses_the_returned_glb(self) -> None:
        responses = [
            {"id": "task-text", "status": "queued"},
            {
                "status": "completed",
                "data": [
                    {"type": "glb", "url": "https://cos.example.com/text.glb"},
                    {"type": "preview", "url": "https://cos.example.com/text.png"},
                ],
            },
        ]
        calls: list[tuple[str, dict]] = []

        def fake_request(method: str, url: str, *, headers: dict, payload=None, timeout: float = 0):
            calls.append((url, payload or {}))
            return responses.pop(0)

        def fake_download(url: str, dest: Path, *, timeout: float, limit: int = 0) -> Path:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.suffix == ".glb":
                dest.write_bytes(
                    _glb_bytes(
                        [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)],
                        [0, 1, 2],
                    )
                )
            else:
                dest.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 16)
            return dest

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(
                os.environ,
                {
                    "TOUCHLEARN_3D_PROVIDER": "tokenhub",
                    "TOUCHLEARN_3D_KEY": "secret",
                    "TOUCHLEARN_3D_QUALITY": "high",
                },
                clear=True,
            ):
                with patch("touchlearn.agent.reconstruct._request_json", side_effect=fake_request):
                    with patch("touchlearn.agent.reconstruct._download", side_effect=fake_download):
                        with patch("touchlearn.agent.reconstruct.time.sleep", return_value=None):
                            asset = text_to_3d(
                                "一只六轴机械臂，带关节和夹爪",
                                ReconstructionPlan(ok=True),
                                dest=Path(tmp) / "out",
                            )

        self.assertEqual(asset.provider, "tokenhub")
        self.assertEqual(asset.task_id, "task-text")
        self.assertEqual(asset.meta["mode"], "text_to_3d")
        self.assertTrue(asset.meta["prompt_forwarded"])
        self.assertEqual(asset.meta["model_version"], "tripo-3d-3.1")
        self.assertIn("glb", asset.files)
        self.assertIsNotNone(asset.mesh)
        self.assertEqual(asset.mesh.triangle_count(), 1)

        submit_url, submit_body = calls[0]
        self.assertTrue(submit_url.endswith("/v1/api/3d/submit"))
        self.assertEqual(submit_body["model"], "tripo-3d-3.1")
        self.assertIn("六轴机械臂", submit_body["prompt"])
        self.assertEqual(submit_body["face_limit"], 1_500_000)
        self.assertNotIn("image_base64", submit_body)

        query_url, query_body = calls[1]
        self.assertTrue(query_url.endswith("/v1/api/3d/query"))
        self.assertEqual(query_body, {"model": "tripo-3d-3.1", "id": "task-text"})

    def test_text_to_3d_rejects_an_empty_prompt(self) -> None:
        with self.assertRaises(ReconstructionError):
            text_to_3d("   ", ReconstructionPlan(ok=True), dest=Path("."))


if __name__ == "__main__":
    unittest.main()
