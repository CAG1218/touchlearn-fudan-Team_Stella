from __future__ import annotations

import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from tests import EXAMPLES  # noqa: F401  (ensures src is on sys.path)

from touchlearn.agent import llm
from touchlearn.agent.llm import ProviderConfig, chat, chat_json, extract_json


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
        self.assertFalse(status["configured"])
        self.assertIn("TOUCHLEARN_LLM", status["message"])


if __name__ == "__main__":
    unittest.main()
