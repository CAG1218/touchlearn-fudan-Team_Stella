from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from tests import EXAMPLES  # noqa: F401  (ensures src is on sys.path)

from touchlearn.agent import llm, scene, vision
from touchlearn.agent.llm import LLMResult, ProviderConfig, chat, chat_json, extract_json


class _MockHandler(BaseHTTPRequestHandler):
    reply = {
        "choices": [{"message": {"content": '```json\n{"subject": "scene", "confidence": 0.8}\n```'}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }
    last_body = None

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or "0")
        type(self).last_body = json.loads(self.rfile.read(length).decode("utf-8"))
        data = json.dumps(self.reply).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args) -> None:
        pass


class LLMTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _MockHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_address[1]}/v1"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def test_endpoint_normalisation(self) -> None:
        self.assertEqual(llm._chat_endpoint("https://api.example.com"), "https://api.example.com/v1/chat/completions")
        self.assertEqual(llm._chat_endpoint("https://api.example.com/v1"), "https://api.example.com/v1/chat/completions")
        self.assertEqual(
            llm._chat_endpoint("https://api.example.com/v1/chat/completions"),
            "https://api.example.com/v1/chat/completions",
        )

    def test_extract_json_variants(self) -> None:
        self.assertEqual(extract_json('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(extract_json('prefix {"a": {"b": 2}} suffix'), {"a": {"b": 2}})
        self.assertEqual(extract_json("not json"), None)

    def test_chat_with_explicit_config(self) -> None:
        config = ProviderConfig(
            provider="custom",
            label="mock",
            endpoint=f"{self.base_url}/chat/completions",
            model="mock-model",
            timeout=5,
        )
        result = chat([{"role": "user", "content": "hello"}], config=config)
        self.assertTrue(result.ok)
        self.assertIn("subject", result.text)
        self.assertEqual(result.model, "mock-model")
        self.assertEqual(_MockHandler.last_body["messages"][0]["content"], "hello")

    def test_chat_json_through_environment(self) -> None:
        env = {
            "TOUCHLEARN_LLM_PROVIDER": "custom",
            "TOUCHLEARN_LLM_URL": self.base_url,
            "TOUCHLEARN_LLM_MODEL": "mock-model",
            "TOUCHLEARN_LLM_KEY": "",
        }
        with patch.dict(os.environ, env, clear=False):
            status = llm.llm_status()
            self.assertTrue(status["configured"])
            self.assertFalse(status["has_key"])
            self.assertEqual(status["endpoint_host"], f"127.0.0.1:{self.server.server_address[1]}")
            result = chat_json("system", "return json")
        self.assertTrue(result.ok)
        self.assertEqual(result.data["subject"], "scene")

    def test_deepseek_preset_tracks_current_models(self) -> None:
        preset = llm.PRESETS["deepseek"]
        self.assertEqual(preset["model"], "deepseek-v4-pro")
        self.assertEqual(preset["vision_model"], "deepseek-flash")
        self.assertEqual(preset["scene_model"], "deepseek-flash")
        self.assertEqual(preset["default_effort"], "low")
        self.assertEqual(preset["scene_effort"], "off")

    def test_tokenhub_preset_tracks_current_models(self) -> None:
        preset = llm.PRESETS["tokenhub"]
        self.assertEqual(preset["base_url"], "https://tokenhub.tencentmaas.com/v1")
        self.assertEqual(preset["model"], "deepseek-v4.1-flash")
        self.assertEqual(preset["vision_model"], "deepseek/deepseek-flash")
        self.assertEqual(preset["scene_model"], "deepseek-v4.1-flash")
        self.assertEqual(preset["default_effort"], "low")
        self.assertEqual(preset["scene_effort"], "off")

    def test_tokenhub_vision_reuses_text_key(self) -> None:
        env = {
            "TOUCHLEARN_LLM_PROVIDER": "tokenhub",
            "TOUCHLEARN_LLM_KEY": "sk-test",
            "TOUCHLEARN_LLM_MODEL": "deepseek-v4.1-flash",
            "TOUCHLEARN_VLM_PROVIDER": "tokenhub",
            "TOUCHLEARN_VLM_URL": "",
            "TOUCHLEARN_VLM_KEY": "",
            "TOUCHLEARN_VLM_MODEL": "",
        }
        with patch.dict(os.environ, env, clear=False):
            config = llm.resolve_config("vision")
        self.assertIsNotNone(config)
        self.assertEqual(config.model, "deepseek/deepseek-flash")
        self.assertEqual(config.api_key, "sk-test")

    def test_kimi_omits_temperature(self) -> None:
        config = ProviderConfig(
            provider="tokenhub",
            label="mock",
            endpoint=f"{self.base_url}/chat/completions",
            model="kimi-k3.1-pro",
            timeout=5,
        )
        result = chat([{"role": "user", "content": "hello"}], config=config)
        self.assertTrue(result.ok)
        self.assertNotIn("temperature", _MockHandler.last_body)
        self.assertEqual(_MockHandler.last_body["reasoning_effort"], "low")

    def test_tokenhub_effort_off_uses_reasoning_effort(self) -> None:
        config = ProviderConfig(
            provider="tokenhub",
            label="mock",
            endpoint=f"{self.base_url}/chat/completions",
            model="deepseek-v4.1-flash",
            timeout=5,
        )
        result = chat([{"role": "user", "content": "hello"}], config=config, effort="off")
        self.assertTrue(result.ok)
        self.assertEqual(_MockHandler.last_body["reasoning_effort"], "none")
        self.assertNotIn("thinking", _MockHandler.last_body)

    def test_scene_generation_uses_fast_model_without_reasoning(self) -> None:
        env = {
            "TOUCHLEARN_LLM_PROVIDER": "tokenhub",
            "TOUCHLEARN_SCENE_MODEL": "",
            "TOUCHLEARN_SCENE_EFFORT": "",
        }
        with patch.dict(os.environ, env, clear=False):
            with patch.object(scene, "chat_json", return_value=LLMResult(False, message="stop")) as fake:
                scene.generate_scene("画一张植物细胞结构图")
        kwargs = fake.call_args.kwargs
        self.assertEqual(kwargs["model"], "deepseek-v4.1-flash")
        self.assertEqual(kwargs["effort"], "off")

    def test_image_scene_also_disables_reasoning(self) -> None:
        env = {
            "TOUCHLEARN_LLM_PROVIDER": "tokenhub",
            "TOUCHLEARN_SCENE_MODEL": "",
            "TOUCHLEARN_SCENE_EFFORT": "",
        }
        with patch.dict(os.environ, env, clear=False):
            with patch.object(scene, "chat_json", return_value=LLMResult(False, message="stop")) as fake:
                scene.generate_scene("把这张图简化成触觉示意图", images=["diagram.png"])
        kwargs = fake.call_args.kwargs
        self.assertEqual(kwargs["kind"], "vision")
        self.assertIsNone(kwargs["model"])
        self.assertEqual(kwargs["effort"], "off")

    def test_opener_ignores_stale_system_proxy(self) -> None:
        with patch.dict(os.environ, {"TOUCHLEARN_LLM_PROXY": ""}, clear=False):
            opener = llm._opener()
            handlers = [item for item in opener.handlers if isinstance(item, urllib.request.ProxyHandler)]
            # build_opener drops a ProxyHandler with no proxies, leaving a
            # direct connection that cannot wait on a dead 127.0.0.1:7890.
            self.assertEqual(handlers, [])
        with patch.dict(os.environ, {"TOUCHLEARN_LLM_PROXY": "http://127.0.0.1:7890"}, clear=False):
            opener = llm._opener()
            handlers = [item for item in opener.handlers if isinstance(item, urllib.request.ProxyHandler)]
            self.assertEqual(len(handlers), 1)
            self.assertEqual(handlers[0].proxies.get("https"), "http://127.0.0.1:7890")

    def test_model_override_reaches_the_payload(self) -> None:
        config = ProviderConfig(
            provider="deepseek",
            label="mock",
            endpoint=f"{self.base_url}/chat/completions",
            model="deepseek-v4-pro",
            timeout=5,
        )
        result = chat([{"role": "user", "content": "hello"}], config=config, model="deepseek-flash")
        self.assertTrue(result.ok)
        self.assertEqual(result.model, "deepseek-flash")
        self.assertEqual(_MockHandler.last_body["model"], "deepseek-flash")

    def test_deepseek_requests_low_reasoning_effort(self) -> None:
        config = ProviderConfig(
            provider="deepseek",
            label="mock",
            endpoint=f"{self.base_url}/chat/completions",
            model="deepseek-v4-pro",
            timeout=5,
        )
        result = chat([{"role": "user", "content": "hello"}], config=config)
        self.assertTrue(result.ok)
        self.assertEqual(_MockHandler.last_body["reasoning_effort"], "low")
        self.assertNotIn("thinking", _MockHandler.last_body)

    def test_effort_off_disables_thinking(self) -> None:
        config = ProviderConfig(
            provider="deepseek",
            label="mock",
            endpoint=f"{self.base_url}/chat/completions",
            model="deepseek-v4-pro",
            timeout=5,
        )
        result = chat([{"role": "user", "content": "hello"}], config=config, effort="off")
        self.assertTrue(result.ok)
        self.assertEqual(_MockHandler.last_body["thinking"], {"type": "disabled"})
        self.assertNotIn("reasoning_effort", _MockHandler.last_body)

    def test_custom_provider_sends_no_reasoning_fields(self) -> None:
        config = ProviderConfig(
            provider="custom",
            label="mock",
            endpoint=f"{self.base_url}/chat/completions",
            model="mock-model",
            timeout=5,
        )
        chat([{"role": "user", "content": "hello"}], config=config)
        self.assertNotIn("reasoning_effort", _MockHandler.last_body)
        self.assertNotIn("thinking", _MockHandler.last_body)

    def test_length_finish_reason_is_explained(self) -> None:
        config = ProviderConfig(
            provider="deepseek",
            label="mock",
            endpoint=f"{self.base_url}/chat/completions",
            model="deepseek-v4-pro",
            timeout=5,
        )
        original = _MockHandler.reply
        _MockHandler.reply = {
            "choices": [{"message": {"content": ""}, "finish_reason": "length"}],
            "usage": {"completion_tokens": 3200},
        }
        try:
            result = chat([{"role": "user", "content": "hello"}], config=config)
        finally:
            _MockHandler.reply = original
        self.assertFalse(result.ok)
        self.assertEqual(result.finish_reason, "length")
        self.assertIn("max_tokens", result.message)

    def test_vision_reuses_text_key_for_same_provider(self) -> None:
        env = {
            "TOUCHLEARN_LLM_PROVIDER": "deepseek",
            "TOUCHLEARN_LLM_KEY": "sk-test",
            "TOUCHLEARN_LLM_MODEL": "deepseek-v4-pro",
            "TOUCHLEARN_VLM_PROVIDER": "deepseek",
            "TOUCHLEARN_VLM_URL": "",
            "TOUCHLEARN_VLM_KEY": "",
            "TOUCHLEARN_VLM_MODEL": "deepseek-flash",
        }
        with patch.dict(os.environ, env, clear=False):
            config = llm.resolve_config("vision")
        self.assertIsNotNone(config)
        self.assertEqual(config.model, "deepseek-flash")
        self.assertEqual(config.api_key, "sk-test")

    def test_missing_config_is_offline(self) -> None:
        env = {
            "TOUCHLEARN_LLM_PROVIDER": "",
            "TOUCHLEARN_LLM_URL": "",
            "TOUCHLEARN_LLM_MODEL": "",
            "TOUCHLEARN_LLM_KEY": "",
            "TOUCHLEARN_VLM_PROVIDER": "",
            "TOUCHLEARN_VLM_URL": "",
            "TOUCHLEARN_VLM_MODEL": "",
            "TOUCHLEARN_VLM_KEY": "",
        }
        with patch.dict(os.environ, env, clear=False):
            status = llm.llm_status()
            catalog = llm.model_catalog()
        self.assertFalse(status["configured"])
        self.assertIn("TOUCHLEARN_LLM", status["message"])
        self.assertFalse(catalog["configured"])
        self.assertEqual(catalog["models"], [])
        self.assertEqual([item["id"] for item in catalog["efforts"]], ["low", "medium", "high"])


TOKENHUB_ENV = {
    "TOUCHLEARN_LLM_PROVIDER": "tokenhub",
    "TOUCHLEARN_LLM_URL": "",
    "TOUCHLEARN_LLM_KEY": "sk-test",
    "TOUCHLEARN_LLM_MODEL": "deepseek-v4.1-flash",
    "TOUCHLEARN_VLM_PROVIDER": "",
    "TOUCHLEARN_VLM_URL": "",
    "TOUCHLEARN_VLM_KEY": "",
    "TOUCHLEARN_VLM_MODEL": "",
    "TOUCHLEARN_LLM_EFFORT": "",
    "TOUCHLEARN_SCENE_MODEL": "",
    "TOUCHLEARN_SCENE_EFFORT": "",
}


class ModelSelectionTests(unittest.TestCase):
    def test_catalog_lists_selectable_models_and_defaults(self) -> None:
        with patch.dict(os.environ, TOKENHUB_ENV, clear=False):
            catalog = llm.model_catalog()
        self.assertTrue(catalog["configured"])
        self.assertEqual(catalog["provider"], "tokenhub")
        self.assertEqual(catalog["default"]["model"], "deepseek-v4.1-flash")
        self.assertEqual(catalog["default"]["effort"], "low")
        self.assertEqual(catalog["default"]["vision_model"], "deepseek/deepseek-flash")
        self.assertTrue(catalog["supports_effort"])
        ids = [item["id"] for item in catalog["models"]]
        self.assertIn("deepseek-v4.1-flash", ids)
        self.assertIn("kimi-k3.1-pro", ids)
        self.assertIn("hy-vision-2.0-instruct", ids)
        default = next(item for item in catalog["models"] if item["id"] == "deepseek-v4.1-flash")
        self.assertTrue(default["default"])
        self.assertFalse(default["vision"])
        vision = next(item for item in catalog["models"] if item["id"] == "deepseek/deepseek-flash")
        self.assertTrue(vision["vision"])

    def test_default_marker_moves_with_the_environment_model(self) -> None:
        env = dict(TOKENHUB_ENV)
        env["TOUCHLEARN_LLM_MODEL"] = "glm-5.3"
        with patch.dict(os.environ, env, clear=False):
            catalog = llm.model_catalog()
        defaults = [item["id"] for item in catalog["models"] if item["default"]]
        self.assertEqual(defaults, ["glm-5.3"])
        self.assertEqual([item["id"] for item in catalog["models"] if item["tag"] == "默认"], [])

    def test_resolve_selection_accepts_catalog_model_and_medium_effort(self) -> None:
        with patch.dict(os.environ, TOKENHUB_ENV, clear=False):
            selection = llm.resolve_selection("kimi-k3.1-pro", "medium")
        self.assertEqual(selection["provider"], "tokenhub")
        self.assertEqual(selection["model"], "kimi-k3.1-pro")
        self.assertEqual(selection["effort"], "medium")
        self.assertEqual(selection["vision_model"], "deepseek/deepseek-flash")
        self.assertFalse(selection["vision_selected"])

    def test_resolve_selection_keeps_a_vision_choice_for_images(self) -> None:
        with patch.dict(os.environ, TOKENHUB_ENV, clear=False):
            selection = llm.resolve_selection("hy-vision-2.0-instruct", "high")
        self.assertEqual(selection["vision_model"], "hy-vision-2.0-instruct")
        self.assertTrue(selection["vision_selected"])

    def test_resolve_selection_rejects_unknown_values(self) -> None:
        with patch.dict(os.environ, TOKENHUB_ENV, clear=False):
            with self.assertRaisesRegex(ValueError, "不支持所选模型"):
                llm.resolve_selection("gpt-4o-mini")
            with self.assertRaisesRegex(ValueError, "思考强度"):
                llm.resolve_selection("deepseek-v4.1-flash", "max")

    def test_custom_provider_gets_exactly_its_env_model(self) -> None:
        env = {
            "TOUCHLEARN_LLM_PROVIDER": "custom",
            "TOUCHLEARN_LLM_URL": "http://127.0.0.1:9/v1",
            "TOUCHLEARN_LLM_KEY": "",
            "TOUCHLEARN_LLM_MODEL": "local-model",
            "TOUCHLEARN_VLM_PROVIDER": "",
            "TOUCHLEARN_VLM_URL": "",
            "TOUCHLEARN_VLM_KEY": "",
            "TOUCHLEARN_VLM_MODEL": "",
        }
        with patch.dict(os.environ, env, clear=False):
            catalog = llm.model_catalog()
            selection = llm.resolve_selection("local-model", "medium")
        self.assertTrue(catalog["configured"])
        self.assertFalse(catalog["supports_effort"])
        self.assertEqual([item["id"] for item in catalog["models"]], ["local-model"])
        self.assertTrue(catalog["models"][0]["default"])
        self.assertEqual(selection["model"], "local-model")
        self.assertEqual(selection["effort"], "medium")

    def test_medium_effort_reaches_the_payload(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _MockHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            config = ProviderConfig(
                provider="tokenhub",
                label="mock",
                endpoint=f"http://127.0.0.1:{server.server_address[1]}/v1/chat/completions",
                model="deepseek-v4.1-flash",
                timeout=5,
            )
            result = chat([{"role": "user", "content": "hello"}], config=config, effort="medium")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.assertTrue(result.ok)
        self.assertEqual(_MockHandler.last_body["reasoning_effort"], "medium")


class VisionSelectionTests(unittest.TestCase):
    def test_vision_call_passes_model_and_effort(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "drawing.png"
            path.write_bytes(b"not-a-real-png")
            reply = LLMResult(
                True,
                provider="tokenhub",
                data={"description": "six-axis arm", "confidence": 0.8},
            )
            with (
                patch(
                    "touchlearn.agent.vision.vision_status",
                    return_value={"configured": True, "provider": "tokenhub"},
                ),
                patch("touchlearn.agent.vision.chat_json", return_value=reply) as fake,
            ):
                result = vision.interpret_image(
                    path,
                    "robot",
                    model="hy-vision-2.0-instruct",
                    effort="medium",
                )
        self.assertTrue(result.ok)
        kwargs = fake.call_args.kwargs
        self.assertEqual(kwargs["model"], "hy-vision-2.0-instruct")
        self.assertEqual(kwargs["effort"], "medium")

    def test_vision_retries_without_reasoning_on_truncation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "drawing.png"
            path.write_bytes(b"not-a-real-png")
            first = LLMResult(False, finish_reason="length", message="truncated")
            second = LLMResult(
                True,
                provider="tokenhub",
                data={"description": "six-axis arm", "confidence": 0.7},
            )
            with (
                patch(
                    "touchlearn.agent.vision.vision_status",
                    return_value={"configured": True, "provider": "tokenhub"},
                ),
                patch(
                    "touchlearn.agent.vision.chat_json",
                    side_effect=[first, second],
                ) as fake,
            ):
                result = vision.interpret_image(
                    path,
                    "robot",
                    model="hy-vision-2.0-instruct",
                    effort="high",
                )
        self.assertTrue(result.ok)
        self.assertEqual(fake.call_count, 2)
        self.assertEqual(fake.call_args_list[1].kwargs["effort"], "off")
        self.assertIn("自动降低强度重试", result.message)


if __name__ == "__main__":
    unittest.main()
