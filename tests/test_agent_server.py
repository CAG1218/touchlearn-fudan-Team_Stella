from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.cookiejar import CookieJar
from unittest.mock import patch
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen

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


class AccessCodeServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        state = AppState(Path(cls.temp.name), access_code="test-code")
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

    def open_json(self, opener, path: str, *, payload: dict | None = None, headers: dict | None = None) -> dict:
        request = Request(
            f"{self.base}{path}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None,
            headers={"Content-Type": "application/json", **(headers or {})},
            method="POST" if payload is not None else "GET",
        )
        with opener.open(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_health_is_public_but_redacted(self) -> None:
        with urlopen(f"{self.base}/api/health", timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertTrue(payload["access_required"])
        self.assertFalse(payload["authenticated"])
        self.assertEqual(payload["llm"]["mode"], "locked")
        self.assertEqual(payload["vision"]["mode"], "locked")
        self.assertEqual(payload["voices"], [])

    def test_login_cookie_unlocks_protected_routes(self) -> None:
        opener = build_opener(HTTPCookieProcessor(CookieJar()))
        with self.assertRaises(HTTPError) as wrong:
            self.open_json(opener, "/api/login", payload={"code": "nope"})
        self.assertEqual(wrong.exception.code, 401)

        payload = self.open_json(opener, "/api/login", payload={"code": "test-code"})
        self.assertTrue(payload["ok"])
        providers = self.open_json(opener, "/api/providers")
        self.assertIn("llm", providers)
        health = self.open_json(opener, "/api/health")
        self.assertTrue(health["authenticated"])

    def test_request_host_drives_public_base_url(self) -> None:
        headers = {"Host": "touchlearn.example.com", "X-Forwarded-Proto": "https"}
        with urlopen(
            Request(f"{self.base}/api/health", headers=headers),
            timeout=10,
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertEqual(payload["public_base_url"], "https://touchlearn.example.com")

    def test_localhost_keeps_the_published_port(self) -> None:
        with urlopen(
            Request(f"{self.base}/api/health", headers={"Host": "127.0.0.1:9999"}),
            timeout=10,
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
        parsed = urlsplit(payload["public_base_url"])
        self.assertEqual(parsed.port, 9999)
        self.assertNotEqual(parsed.port, self.server.server_address[1])

    def test_container_keeps_localhost_host(self) -> None:
        with patch("touchlearn.agent.server.running_in_container", return_value=True):
            with urlopen(
                Request(f"{self.base}/api/health", headers={"Host": "127.0.0.1:9999"}),
                timeout=10,
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
        parsed = urlsplit(payload["public_base_url"])
        self.assertEqual(parsed.hostname, "127.0.0.1")
        self.assertEqual(parsed.port, 9999)


if __name__ == "__main__":
    unittest.main()
