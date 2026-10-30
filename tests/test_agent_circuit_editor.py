from __future__ import annotations

import base64
import json
import tempfile
import threading
import unittest
from http.cookiejar import CookieJar
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen

from tests import EXAMPLES  # noqa: F401  (ensures src is on sys.path)

from touchlearn.agent.agent import TouchLearnAgent
from touchlearn.agent.circuit import (
    CircuitError,
    CircuitInstructionError,
    apply_op,
    circuit_from_scene,
    circuit_to_scene,
    default_circuit,
    normalize_circuit,
    parse_circuit_instruction,
)
from touchlearn.agent.llm import LLMResult
from touchlearn.agent.router import RouteDecision
from touchlearn.agent.scene import (
    SceneGeneration,
    generate_scene,
    normalize_scene,
    scene_has_wires,
    scene_templates,
)
from touchlearn.agent.scene_edit import (
    SceneEditError,
    edit_scene,
    preview_scene,
    recognize_circuit_image,
)
from touchlearn.agent.scene_svg import scene_to_svg
from touchlearn.agent.accounts import AccountStore, AuthSettings
from touchlearn.agent.server import AppState, Server

# A 1x1 transparent PNG.  The recognizer never decodes pixels in these tests
# (the vision call is mocked); it only needs a real file on disk.
TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def compiled_scene(circuit: dict | None = None) -> dict:
    return normalize_scene(circuit_to_scene(circuit or default_circuit()))[0]


def plain_scene() -> dict:
    return {
        "title": "TWO BOXES",
        "blocks": [
            {
                "id": "first",
                "label": "FIRST",
                "label_zh": "第一块",
                "primitives": [{"type": "rect", "x": 20, "y": 20, "w": 20, "h": 14, "width": 2.0}],
            },
            {
                "id": "second",
                "label": "SECOND",
                "label_zh": "第二块",
                "primitives": [{"type": "rect", "x": 60, "y": 60, "w": 20, "h": 14, "width": 2.0}],
            },
        ],
    }


class CircuitModelTests(unittest.TestCase):
    def test_default_circuit_has_the_documented_series_components(self) -> None:
        circuit = default_circuit()
        self.assertEqual(
            [(item["type"], item["side"]) for item in circuit["components"]],
            [("battery", "bottom"), ("switch", "left"), ("resistor", "top"), ("lamp", "right")],
        )
        switch = next(item for item in circuit["components"] if item["type"] == "switch")
        self.assertEqual(switch["state"], "open")

    def test_normalize_clamps_offsets_and_deduplicates_ids(self) -> None:
        circuit, warnings = normalize_circuit(
            {
                "title": "test",
                "loop": [-10, -10, 200, 200],
                "components": [
                    {"id": "same", "type": "battery", "side": "bottom", "offset": -1},
                    {"id": "same", "type": "lamp", "side": "right", "offset": 3},
                ],
            }
        )
        self.assertEqual(len({item["id"] for item in circuit["components"]}), 2)
        self.assertTrue(all(0.06 <= item["offset"] <= 0.94 for item in circuit["components"]))
        self.assertEqual(circuit["loop"], [4.0, 6.0, 96.0, 94.0])
        self.assertIsInstance(warnings, list)

    def test_normalize_rejects_components_it_cannot_identify(self) -> None:
        with self.assertRaises(CircuitError):
            normalize_circuit({"components": [{"type": "banana", "side": "top"}]})

    def test_circuit_to_scene_keeps_wires_closed_and_on_the_loop(self) -> None:
        circuit = default_circuit()
        scene, warnings = normalize_scene(circuit_to_scene(circuit))
        wires = next(block for block in scene["blocks"] if block["kind"] == "wire")
        segments = [
            primitive["points"]
            for primitive in wires["primitives"]
            if primitive.get("type") == "polyline"
        ]
        self.assertGreaterEqual(len(segments), 4)
        x0, y0, x1, y1 = circuit["loop"]
        start = tuple(segments[0][0])
        end = tuple(segments[-1][-1])
        self.assertEqual(start, (x0, y0))
        self.assertEqual(end, (x0, y0))
        for points in segments:
            for x, y in points:
                on_boundary = (
                    abs(x - x0) < 1e-6
                    or abs(x - x1) < 1e-6
                    or abs(y - y0) < 1e-6
                    or abs(y - y1) < 1e-6
                )
                self.assertTrue(on_boundary, msg=f"wire point {(x, y)} left the loop")
            for previous, current in zip(points, points[1:]):
                self.assertTrue(
                    abs(previous[0] - current[0]) < 1e-6 or abs(previous[1] - current[1]) < 1e-6,
                    msg="wire segment is not axis aligned",
                )
        self.assertEqual(
            [block["id"] for block in scene["blocks"]],
            ["battery", "switch", "resistor", "lamp", "wires"],
        )

    def test_circuit_round_trips_through_scene(self) -> None:
        original = default_circuit()
        recovered = circuit_from_scene(compiled_scene(original))
        assert recovered is not None
        self.assertEqual(
            [(item["type"], item["side"]) for item in recovered["components"]],
            [(item["type"], item["side"]) for item in original["components"]],
        )
        self.assertEqual(recovered["loop"], original["loop"])

    def test_scene_has_wires_only_for_drawn_loops(self) -> None:
        self.assertTrue(scene_has_wires(compiled_scene()))
        self.assertFalse(scene_has_wires(plain_scene()))


class CircuitOperationTests(unittest.TestCase):
    def component(self, circuit: dict, component_id: str) -> dict:
        return next(item for item in circuit["components"] if item["id"] == component_id)

    def test_move_component_updates_side_and_offset(self) -> None:
        circuit, change, _ = apply_op(
            default_circuit(),
            {"type": "move_component", "id": "switch", "side": "bottom", "offset": 0.25},
        )
        switch = self.component(circuit, "switch")
        self.assertEqual(switch["side"], "bottom")
        self.assertAlmostEqual(switch["offset"], 0.25)
        self.assertIn("开关", change)

    def test_set_state_only_accepts_switches(self) -> None:
        circuit, change, _ = apply_op(
            default_circuit(),
            {"type": "set_state", "id": "switch", "state": "closed"},
        )
        self.assertEqual(self.component(circuit, "switch")["state"], "closed")
        self.assertIn("闭合", change)
        with self.assertRaises(CircuitError):
            apply_op(circuit, {"type": "set_state", "id": "lamp", "state": "closed"})

    def test_add_component_respects_the_library_and_the_limit(self) -> None:
        circuit, change, _ = apply_op(
            default_circuit(),
            {"type": "add_component", "component_type": "电流表", "side": "top", "offset": 0.2},
        )
        added = next(item for item in circuit["components"] if item["type"] == "ammeter")
        self.assertEqual(added["side"], "top")
        self.assertIn("电流表", change)

        crowded = default_circuit()
        crowded["components"] = [
            {
                "id": f"lamp_{index}",
                "type": "lamp",
                "side": ("top", "right", "bottom", "left")[index % 4],
                "offset": 0.2 + (index // 4) * 0.4,
            }
            for index in range(8)
        ]
        with self.assertRaises(CircuitError):
            apply_op(crowded, {"type": "add_component", "component_type": "motor"})

    def test_remove_refuses_to_empty_the_loop(self) -> None:
        with self.assertRaises(CircuitError):
            apply_op(
                {"components": [{"id": "battery", "type": "battery", "side": "bottom"}]},
                {"type": "remove_component", "id": "battery"},
            )

    def test_set_type_keeps_the_original_custom_label(self) -> None:
        circuit, change, _ = apply_op(
            default_circuit(),
            {"type": "set_type", "id": "lamp", "component_type": "resistor"},
        )
        lamp = self.component(circuit, "lamp")
        self.assertEqual(lamp["type"], "resistor")
        self.assertEqual(lamp["label"], "RESISTOR")
        self.assertIn("换成了", change)

    def test_rotate_and_loop_operations_are_deterministic(self) -> None:
        rotated, _, _ = apply_op(
            default_circuit(),
            {"type": "rotate_component", "id": "resistor", "direction": "cw"},
        )
        self.assertEqual(self.component(rotated, "resistor")["side"], "right")
        scaled, _, _ = apply_op(rotated, {"type": "set_loop", "scale": 1.1})
        self.assertGreater(scaled["loop"][2] - scaled["loop"][0], rotated["loop"][2] - rotated["loop"][0])
        moved, _, _ = apply_op(scaled, {"type": "set_loop", "dx": 3, "dy": -2})
        self.assertAlmostEqual(moved["loop"][0], scaled["loop"][0] + 3)
        self.assertAlmostEqual(moved["loop"][1], scaled["loop"][1] - 2)

    def test_reorder_and_unknown_operations(self) -> None:
        reordered, change, _ = apply_op(
            default_circuit(),
            {"type": "reorder", "id": "lamp", "index": 0},
        )
        self.assertEqual(reordered["components"][0]["id"], "lamp")
        self.assertIn("阅读顺序", change)
        with self.assertRaises(CircuitError):
            apply_op(default_circuit(), {"type": "explode"})
        with self.assertRaises(CircuitError):
            apply_op(default_circuit(), {"type": "move_component", "id": "missing", "side": "top"})


class CircuitInstructionTests(unittest.TestCase):
    def test_common_chinese_edits_run_without_a_model(self) -> None:
        circuit, _, message = parse_circuit_instruction(default_circuit(), "把开关闭合")
        switch = next(item for item in circuit["components"] if item["type"] == "switch")
        self.assertEqual(switch["state"], "closed")
        self.assertIn("闭合", message)

        circuit, _, _ = parse_circuit_instruction(default_circuit(), "加一个电流表")
        self.assertTrue(any(item["type"] == "ammeter" for item in circuit["components"]))

        circuit, _, _ = parse_circuit_instruction(default_circuit(), "把灯泡换成电阻")
        lamp = next(item for item in circuit["components"] if item["id"] == "lamp")
        self.assertEqual(lamp["type"], "resistor")

        circuit, _, _ = parse_circuit_instruction(default_circuit(), "删除电阻")
        self.assertFalse(any(item["id"] == "resistor" for item in circuit["components"]))

        circuit, _, _ = parse_circuit_instruction(default_circuit(), "把开关移到左上角")
        switch = next(item for item in circuit["components"] if item["type"] == "switch")
        self.assertEqual(switch["side"], "top")
        self.assertAlmostEqual(switch["offset"], 0.14)

        circuit, _, _ = parse_circuit_instruction(default_circuit(), "把回路放大")
        self.assertGreater(circuit["loop"][2] - circuit["loop"][0], 70)

    def test_unmatched_instruction_raises_so_the_model_can_take_over(self) -> None:
        with self.assertRaises(CircuitInstructionError):
            parse_circuit_instruction(default_circuit(), "把它做得更有未来感一点")


class SceneSvgTests(unittest.TestCase):
    def test_svg_exposes_editor_structure_and_uses_the_plate_content_box(self) -> None:
        svg = scene_to_svg(compiled_scene(), show_names=True)
        self.assertIn('class="scene-svg"', svg)
        self.assertIn('data-content-box="8,24,132,142"', svg)
        for block_id in ("battery", "switch", "resistor", "lamp", "wires"):
            self.assertIn(f'data-block-id="{block_id}"', svg)
        self.assertIn('data-kind="wire"', svg)
        self.assertIn('class="block-hit"', svg)
        self.assertIn('class="block-outline"', svg)
        self.assertEqual(svg.count('class="scene-label"'), 4)

    def test_named_badges_are_optional(self) -> None:
        scene = compiled_scene()
        named = scene_to_svg(scene, show_names=True)
        plain = scene_to_svg(scene, show_names=False)
        self.assertEqual(named.count('class="scene-label"'), 4)
        self.assertNotIn('class="scene-label"', plain)
        self.assertIn('data-block-id="wires"', plain)


class SceneEditServiceTests(unittest.TestCase):
    def test_preview_defaults_to_the_built_in_circuit(self) -> None:
        result = preview_scene({})
        self.assertEqual(result["kind"], "circuit")
        self.assertEqual(result["editor"], "circuit")
        self.assertEqual(len(result["circuit"]["components"]), 4)
        self.assertIn("wires", result["svg"])
        self.assertIn("svg_plain", result)

    def test_preview_rejects_an_explicit_empty_circuit(self) -> None:
        with self.assertRaises(SceneEditError):
            preview_scene({"circuit": {"components": []}})
        with self.assertRaises(SceneEditError):
            edit_scene({"circuit": {"components": []}, "op": {"type": "add_component", "component_type": "lamp"}})

    def test_preview_promotes_a_wired_scene_but_not_two_boxes(self) -> None:
        promoted = preview_scene({"scene": compiled_scene()})
        self.assertEqual(promoted["kind"], "circuit")
        self.assertEqual(promoted["source"], "scene")

        plain = preview_scene({"scene": plain_scene()})
        self.assertEqual(plain["kind"], "scene")
        self.assertEqual(plain["editor"], "scene")
        self.assertIsNone(plain["circuit"])

    def test_edit_applies_a_semantic_operation(self) -> None:
        result = edit_scene(
            {
                "circuit": default_circuit(),
                "op": {"type": "move_component", "id": "switch", "side": "bottom", "offset": 0.3},
            }
        )
        switch = next(item for item in result["circuit"]["components"] if item["id"] == "switch")
        self.assertEqual(switch["side"], "bottom")
        self.assertTrue(result["changes"])
        self.assertEqual(result["source"], "manual")

    def test_edit_applies_an_offline_instruction(self) -> None:
        result = edit_scene({"circuit": default_circuit(), "instruction": "把开关闭合"})
        switch = next(item for item in result["circuit"]["components"] if item["id"] == "switch")
        self.assertEqual(switch["state"], "closed")
        self.assertEqual(result["source"], "rules")

    def test_unknown_instruction_uses_the_model_and_rejects_an_empty_reply(self) -> None:
        with (
            patch("touchlearn.agent.scene_edit.llm_status", return_value={"configured": True}),
            patch(
                "touchlearn.agent.scene_edit.chat_json",
                return_value=LLMResult(True, data={"circuit": {"components": []}, "message": "done"}),
            ),
        ):
            with self.assertRaises(SceneEditError) as caught:
                edit_scene({"circuit": default_circuit(), "instruction": "把它做得更有未来感一点"})
        self.assertIn("没有返回任何电路元件", str(caught.exception))

    def test_model_edit_is_normalized_and_reported(self) -> None:
        circuit = default_circuit()
        circuit["title"] = "MODEL EDITED"
        with (
            patch("touchlearn.agent.scene_edit.llm_status", return_value={"configured": True}),
            patch(
                "touchlearn.agent.scene_edit.chat_json",
                return_value=LLMResult(
                    True,
                    provider="tokenhub",
                    model="glm-5.3",
                    data={"supported": True, "message": "已改成模型电路", "circuit": circuit},
                ),
            ),
        ):
            result = edit_scene({"circuit": default_circuit(), "instruction": "把它做得更有未来感一点"})
        self.assertEqual(result["source"], "model")
        self.assertEqual(result["provider"], "tokenhub")
        self.assertEqual(result["circuit"]["title"], "MODEL EDITED")
        self.assertIn("模型电路", result["changes"][-1])


class CircuitRecognitionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.image = Path(self.temp.name) / "circuit.png"
        self.image.write_bytes(TINY_PNG)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_missing_image_is_a_client_error(self) -> None:
        with self.assertRaises(SceneEditError):
            recognize_circuit_image([Path(self.temp.name) / "missing.png"])

    def test_missing_vision_configuration_explains_the_fix(self) -> None:
        with patch(
            "touchlearn.agent.scene_edit.vision_provider_status",
            return_value={"configured": False},
        ):
            with self.assertRaises(SceneEditError) as caught:
                recognize_circuit_image([self.image])
        self.assertIn("没有配置图像理解模型", str(caught.exception))

    def test_recognized_circuit_enters_the_editor(self) -> None:
        circuit = default_circuit()
        with (
            patch(
                "touchlearn.agent.scene_edit.vision_provider_status",
                return_value={"configured": True},
            ),
            patch(
                "touchlearn.agent.scene_edit.chat_json",
                return_value=LLMResult(
                    True,
                    provider="tokenhub",
                    model="vision-model",
                    data={
                        "supported": True,
                        "is_circuit": True,
                        "confidence": 0.87,
                        "message": "识别到串联回路。",
                        "circuit": circuit,
                        "questions": ["请确认电阻标签。"],
                    },
                ),
            ),
        ):
            result = recognize_circuit_image([self.image])
        self.assertEqual(result["kind"], "circuit")
        self.assertEqual(result["editor"], "circuit")
        self.assertEqual(result["source"], "image")
        self.assertEqual(result["confidence"], 0.87)
        self.assertEqual(len(result["circuit"]["components"]), 4)
        self.assertIn("wires", result["svg"])
        self.assertEqual(result["questions"], ["请确认电阻标签。"])

    def test_non_circuit_image_returns_a_graceful_scene_result(self) -> None:
        with (
            patch(
                "touchlearn.agent.scene_edit.vision_provider_status",
                return_value={"configured": True},
            ),
            patch(
                "touchlearn.agent.scene_edit.chat_json",
                return_value=LLMResult(
                    True,
                    data={"supported": True, "is_circuit": False, "reason": "这是一张抛物线图。"},
                ),
            ),
        ):
            result = recognize_circuit_image([self.image])
        self.assertEqual(result["kind"], "scene")
        self.assertEqual(result["editor"], "none")
        self.assertIn("抛物线", result["message"])
        self.assertNotIn("circuit", result)

    def test_unparseable_or_empty_model_output_never_loads_the_default_circuit(self) -> None:
        base = (
            patch(
                "touchlearn.agent.scene_edit.vision_provider_status",
                return_value={"configured": True},
            ),
        )
        with base[0], patch(
            "touchlearn.agent.scene_edit.chat_json",
            return_value=LLMResult(False, message="模型没有返回可解析的 JSON"),
        ):
            broken = recognize_circuit_image([self.image])
        self.assertEqual(broken["kind"], "scene")
        self.assertNotIn("circuit", broken)

        with base[0], patch(
            "touchlearn.agent.scene_edit.chat_json",
            return_value=LLMResult(
                True,
                data={"supported": True, "is_circuit": True, "circuit": {"components": []}},
            ),
        ):
            empty = recognize_circuit_image([self.image])
        self.assertEqual(empty["kind"], "scene")
        self.assertIn("没有识别出任何电路元件", empty["message"])
        self.assertNotIn("circuit", empty)

    def test_real_api_failure_still_raises(self) -> None:
        with (
            patch(
                "touchlearn.agent.scene_edit.vision_provider_status",
                return_value={"configured": True},
            ),
            patch(
                "touchlearn.agent.scene_edit.chat_json",
                return_value=LLMResult(False, message="connection refused"),
            ),
        ):
            with self.assertRaises(SceneEditError) as caught:
                recognize_circuit_image([self.image])
        self.assertIn("读图失败", str(caught.exception))


class SceneGenerationGuardTests(unittest.TestCase):
    def test_empty_model_circuit_is_ignored_instead_of_becoming_the_default(self) -> None:
        data = {
            "title": "PLAIN",
            "blocks": [
                {
                    "id": "box",
                    "label": "BOX",
                    "primitives": [{"type": "rect", "x": 20, "y": 20, "w": 30, "h": 20, "width": 2.0}],
                }
            ],
            "circuit": {"components": []},
        }
        with patch(
            "touchlearn.agent.scene.chat_json",
            return_value=LLMResult(True, data=data),
        ):
            result = generate_scene("画一个示意图")
        self.assertTrue(result.ok)
        self.assertEqual(result.circuit, {})
        self.assertTrue(any("没有任何元件" in note for note in result.notes))
        self.assertEqual(len(result.scene["blocks"]), 1)

    def test_empty_circuit_without_blocks_fails_honestly(self) -> None:
        with patch(
            "touchlearn.agent.scene.chat_json",
            return_value=LLMResult(True, data={"circuit": {"components": []}}),
        ):
            result = generate_scene("画一个电路")
        self.assertFalse(result.ok)
        self.assertIn("没有返回可排版的图块或电路", result.message)


class AgentCircuitIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.agent = TouchLearnAgent(output_root=Path(self.temp.name))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_analyze_accepts_a_circuit_confirmed_in_the_editor(self) -> None:
        circuit = default_circuit()
        circuit["title"] = "MY CIRCUIT"
        circuit["title_zh"] = "我的电路"
        next(item for item in circuit["components"] if item["type"] == "switch")["state"] = "closed"
        draft = self.agent.analyze(
            {"subject": "auto", "text": "确认过的电路", "circuit": circuit}
        )
        self.assertEqual(draft["subject"], "scene")
        self.assertTrue(draft["ready"])
        fields = draft["fields"]
        self.assertEqual(fields["editor"], "circuit")
        self.assertEqual(fields["circuit_source"], "editor")
        self.assertEqual(fields["circuit"]["title"], "MY CIRCUIT")
        self.assertTrue(fields["scene_svg"])
        self.assertIn('data-block-id="wires"', fields["scene_svg"])
        self.assertTrue(any("图形编辑器" in note for note in draft["notes"]))

    def test_circuit_template_ships_an_editable_circuit(self) -> None:
        draft = self.agent.analyze({"subject": "auto", "template": "circuit"})
        fields = draft["fields"]
        self.assertEqual(fields["editor"], "circuit")
        self.assertEqual(fields["circuit_source"], "template")
        self.assertEqual(len(fields["circuit"]["components"]), 4)
        entry = next(item for item in scene_templates() if item["id"] == "circuit")
        self.assertEqual(entry["editor"], "circuit")
        self.assertTrue(entry["thumbnail"].endswith("/api/templates/circuit/thumbnail.svg"))

    def test_router_note_about_filenames_is_not_replayed_after_vision(self) -> None:
        decision = RouteDecision(
            subject="scene",
            confidence=0.9,
            reason="test route",
            provider="test",
            notes=["附件 circuit.png 仅按文件名判断为电路图，未读取图片像素。"],
        )
        with (
            patch("touchlearn.agent.agent.classify_request", return_value=decision),
            patch(
                "touchlearn.agent.agent.generate_scene",
                return_value=SceneGeneration(False, message="未配置视觉模型"),
            ),
        ):
            draft = self.agent.analyze(
                {
                    "subject": "auto",
                    "files": [
                        {
                            "name": "circuit.png",
                            "data": base64.b64encode(TINY_PNG).decode("ascii"),
                        }
                    ],
                }
            )
        self.assertEqual(draft["subject"], "scene")
        self.assertTrue(any("文件名" in note for note in draft["fields"]["routing"]["notes"]))
        self.assertFalse(any("文件名" in note for note in draft["notes"]))

    def test_build_recompiles_from_circuit_and_writes_circuit_json(self) -> None:
        draft = self.agent.analyze({"subject": "auto", "template": "circuit"})
        circuit = json.loads(json.dumps(draft["fields"]["circuit"]))
        circuit["title"] = "CIRCUIT TEST"
        circuit["title_zh"] = "测试电路"
        next(item for item in circuit["components"] if item["type"] == "switch")["state"] = "closed"
        with patch("touchlearn.agent.agent.synthesize_windows_tts", return_value=(False, "test environment")):
            result = self.agent.build(
                draft["id"],
                overrides={"fields": {"circuit": circuit}},
                public_base_url="http://127.0.0.1:8765",
                job_id="circuittest",
            )
        job_dir = Path(self.temp.name) / "jobs" / "circuittest"
        artifact_names = [
            item["name"] if isinstance(item, dict) else str(item)
            for item in result["artifacts"]
        ]
        self.assertIn("circuit.json", artifact_names)
        saved = json.loads((job_dir / "circuit.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["title"], "CIRCUIT TEST")
        self.assertEqual(
            next(item for item in saved["components"] if item["type"] == "switch")["state"],
            "closed",
        )
        diagram = json.loads((job_dir / "diagram.json").read_text(encoding="utf-8"))
        scene = diagram["layout"]["options"]["scene"]
        self.assertEqual(scene["title"], "CIRCUIT TEST")
        self.assertEqual(len([block for block in scene["blocks"] if block["id"] != "wires"]), 4)
        self.assertTrue((job_dir / "model.stl").is_file())
        self.assertGreater((job_dir / "model.stl").stat().st_size, 0)
        self.assertEqual(result["title"], "CIRCUIT TEST")


class SceneApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        state = AppState(Path(cls.temp.name), "http://127.0.0.1:8765")
        cls.server = Server(("127.0.0.1", 0), state)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.temp.cleanup()

    def post_json(self, path: str, payload: dict) -> dict:
        request = Request(
            f"{self.base}{path}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))

    def fetch(self, path: str):
        return urlopen(f"{self.base}{path}", timeout=15)

    def test_thumbnail_route_serves_three_crops(self) -> None:
        with self.fetch("/api/templates/circuit/thumbnail.svg") as response:
            default = response.read().decode("utf-8")
            self.assertEqual(response.headers["Content-Type"], "image/svg+xml; charset=utf-8")
            self.assertIn("max-age=3600", response.headers["Cache-Control"])
        with self.fetch("/api/templates/circuit/thumbnail.svg?crop=loop") as response:
            loop = response.read().decode("utf-8")
        with self.fetch("/api/templates/circuit/thumbnail.svg?crop=content") as response:
            content = response.read().decode("utf-8")
        self.assertIn('viewBox="0 0 148 210"', default)
        self.assertIn('viewBox="24 54 100 96"', loop)
        self.assertIn('viewBox="2 18 144 152"', content)
        self.assertNotEqual(default, loop)
        with self.assertRaises(HTTPError) as caught:
            self.fetch("/api/templates/not-a-template/thumbnail.svg")
        self.assertEqual(caught.exception.code, 404)

    def test_scene_preview_and_edit_round_trip(self) -> None:
        preview = self.post_json("/api/scene-preview", {})
        self.assertTrue(preview["ok"])
        self.assertEqual(preview["kind"], "circuit")
        circuit = preview["circuit"]

        moved = self.post_json(
            "/api/scene-edit",
            {
                "circuit": circuit,
                "op": {"type": "move_component", "id": "switch", "side": "bottom", "offset": 0.3},
            },
        )
        switch = next(item for item in moved["circuit"]["components"] if item["id"] == "switch")
        self.assertEqual(switch["side"], "bottom")
        self.assertTrue(moved["changes"])

        closed = self.post_json(
            "/api/scene-edit",
            {"circuit": moved["circuit"], "op": {"type": "set_state", "id": "switch", "state": "closed"}},
        )
        self.assertEqual(
            next(item for item in closed["circuit"]["components"] if item["id"] == "switch")["state"],
            "closed",
        )

        added = self.post_json(
            "/api/scene-edit",
            {"circuit": closed["circuit"], "op": {"type": "add_component", "component_type": "ammeter"}},
        )
        self.assertTrue(any(item["type"] == "ammeter" for item in added["circuit"]["components"]))

        instructed = self.post_json(
            "/api/scene-edit",
            {"circuit": added["circuit"], "instruction": "把灯泡换成电阻"},
        )
        self.assertEqual(instructed["source"], "rules")
        self.assertTrue(instructed["changes"])

    def test_scene_preview_rejects_an_empty_circuit(self) -> None:
        with self.assertRaises(HTTPError) as caught:
            self.post_json("/api/scene-preview", {"circuit": {"components": []}})
        self.assertEqual(caught.exception.code, 400)
        body = json.loads(caught.exception.read().decode("utf-8"))
        self.assertIn("至少需要 1 个元件", body["error"])

    def test_scene_recognize_rejects_a_missing_upload(self) -> None:
        with self.assertRaises(HTTPError) as caught:
            self.post_json("/api/scene-recognize", {"files": []})
        self.assertEqual(caught.exception.code, 400)
        body = json.loads(caught.exception.read().decode("utf-8"))
        self.assertIn("上传", body["error"])


class SceneAuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        auth = AccountStore(
            AuthSettings(
                db_path=Path(cls.temp.name) / "accounts.sqlite3",
                dev_mode=True,
                resend_after=0,
            )
        )
        state = AppState(Path(cls.temp.name), auth=auth)
        cls.server = Server(("127.0.0.1", 0), state)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.temp.cleanup()

    def test_thumbnails_are_public_but_edits_require_a_session(self) -> None:
        # Welcome-card artwork stays public so the login screen can render it.
        with urlopen(f"{self.base}/api/templates/circuit/thumbnail.svg", timeout=10) as response:
            self.assertEqual(response.status, 200)
            self.assertIn("svg", response.headers.get("Content-Type", ""))

        edit = Request(
            f"{self.base}/api/scene-edit",
            data=json.dumps({"instruction": "把开关闭合"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(HTTPError) as denied:
            urlopen(edit, timeout=10)
        self.assertEqual(denied.exception.code, 401)

        opener = build_opener(HTTPCookieProcessor(CookieJar()))
        request_code = Request(
            f"{self.base}/api/auth/request-code",
            data=json.dumps({"kind": "email", "value": "scene@example.com"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with opener.open(request_code, timeout=10) as response:
            dev_code = json.loads(response.read().decode("utf-8"))["dev_code"]
        login = Request(
            f"{self.base}/api/auth/login",
            data=json.dumps(
                {"kind": "email", "value": "scene@example.com", "code": dev_code}
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with opener.open(login, timeout=10) as response:
            self.assertTrue(json.loads(response.read().decode("utf-8"))["ok"])
        with opener.open(f"{self.base}/api/templates/circuit/thumbnail.svg", timeout=10) as response:
            self.assertEqual(response.status, 200)

        edit = Request(
            f"{self.base}/api/scene-edit",
            data=json.dumps({"instruction": "把开关闭合"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with opener.open(edit, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertEqual(payload["kind"], "circuit")


if __name__ == "__main__":
    unittest.main()
