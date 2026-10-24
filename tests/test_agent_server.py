from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.request import Request, urlopen

from tests import EXAMPLES  # noqa: F401  (ensures src is on sys.path)

from touchlearn.agent.server import AppState, Server


class AgentServerTests(unittest.TestCase):
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

    def get_json(self, path: str) -> dict:
        with urlopen(f"{self.base}{path}", timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))

    def post_json(self, path: str, payload: dict) -> dict:
        request = Request(
            f"{self.base}{path}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_health_exposes_llm_and_vision(self) -> None:
        payload = self.get_json("/api/health")
        self.assertTrue(payload["ok"])
        self.assertIn("llm", payload)
        self.assertIn("vision", payload)
        self.assertIn("configured", payload["llm"])

    def test_providers_never_return_keys(self) -> None:
        payload = self.get_json("/api/providers")
        for kind in ("llm", "vision"):
            self.assertNotIn("api_key", payload[kind])
            self.assertNotIn("key", payload[kind])
            self.assertIn("has_key", payload[kind])
        self.assertTrue(payload["presets"])
        self.assertIn("TOUCHLEARN_LLM_KEY", payload["environment"]["text"])

    def test_scene_templates_endpoint(self) -> None:
        payload = self.get_json("/api/scene-templates")
        self.assertTrue(payload["ok"])
        ids = {entry["id"] for entry in payload["templates"]}
        self.assertIn("circuit", ids)
        self.assertIn("cell", ids)

    def test_analyze_auto_scene(self) -> None:
        payload = self.post_json(
            "/api/analyze",
            {"subject": "auto", "text": "画一个串联电路示意图：电池、开关、电阻和灯泡依次连接成回路。"},
        )
        self.assertTrue(payload["ok"])
        draft = payload["draft"]
        self.assertEqual(draft["subject"], "scene")
        self.assertTrue(draft["ready"])
        self.assertEqual(draft["fields"]["template"], "circuit")


if __name__ == "__main__":
    unittest.main()
