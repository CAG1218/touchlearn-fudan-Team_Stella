from __future__ import annotations

import io
import json
import os
import struct
import tempfile
import threading
import unittest
import zipfile
from http.cookiejar import CookieJar
from unittest.mock import patch
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen

from tests import EXAMPLES  # noqa: F401  (ensures src is on sys.path)

from touchlearn.agent.server import AppState, Server, content_disposition


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
        self.assertIn("models", payload)
        self.assertIn("default", payload["models"])
        self.assertEqual(
            [item["id"] for item in payload["models"]["efforts"]],
            ["low", "medium", "high"],
        )
        self.assertFalse(payload["models"]["configured"])
        self.assertEqual(payload["models"]["models"], [])

    def test_analyze_rejects_an_unknown_model(self) -> None:
        env = {
            "TOUCHLEARN_LLM_PROVIDER": "tokenhub",
            "TOUCHLEARN_LLM_KEY": "sk-test",
            "TOUCHLEARN_LLM_MODEL": "deepseek-v4.1-flash",
            "TOUCHLEARN_VLM_PROVIDER": "",
            "TOUCHLEARN_VLM_URL": "",
            "TOUCHLEARN_VLM_KEY": "",
            "TOUCHLEARN_VLM_MODEL": "",
        }
        with patch.dict(os.environ, env, clear=False):
            with self.assertRaises(HTTPError) as caught:
                self.post_json(
                    "/api/analyze",
                    {
                        "subject": "auto",
                        "text": "画一个串联电路示意图",
                        "model": "not-offered",
                    },
                )
        self.assertEqual(caught.exception.code, 400)
        body = json.loads(caught.exception.read().decode("utf-8"))
        self.assertIn("不支持所选模型", body["error"])

    def test_analyze_rejects_an_unknown_effort(self) -> None:
        env = {
            "TOUCHLEARN_LLM_PROVIDER": "tokenhub",
            "TOUCHLEARN_LLM_KEY": "sk-test",
            "TOUCHLEARN_LLM_MODEL": "deepseek-v4.1-flash",
            "TOUCHLEARN_VLM_PROVIDER": "",
            "TOUCHLEARN_VLM_URL": "",
            "TOUCHLEARN_VLM_KEY": "",
            "TOUCHLEARN_VLM_MODEL": "",
        }
        with patch.dict(os.environ, env, clear=False):
            with self.assertRaises(HTTPError) as caught:
                self.post_json(
                    "/api/analyze",
                    {
                        "subject": "auto",
                        "text": "画一个串联电路示意图",
                        "model": "deepseek-v4.1-flash",
                        "effort": "extreme",
                    },
                )
        self.assertEqual(caught.exception.code, 400)
        body = json.loads(caught.exception.read().decode("utf-8"))
        self.assertIn("思考强度只能是", body["error"])

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

    @staticmethod
    def write_test_stl(job_dir: Path) -> None:
        header = b"TouchLearn test binary STL".ljust(80, b" ")
        triangle = struct.pack(
            "<12fH",
            0.0, 0.0, 0.0,
            0.0, 0.0, 0.0,
            1.0, 0.0, 0.0,
            0.0, 1.0, 0.0,
            0,
        )
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / "model.stl").write_bytes(header + struct.pack("<I", 1) + triangle)

    def test_stl_download_is_binary_stl_with_exact_filename(self) -> None:
        job_dir = Path(self.temp.name) / "jobs" / "stlcheck"
        self.write_test_stl(job_dir)
        with urlopen(f"{self.base}/files/stlcheck/model.stl?download=1", timeout=10) as response:
            body = response.read()
            self.assertEqual(response.headers["Content-Type"], "model/stl")
            disposition = response.headers["Content-Disposition"]
        self.assertIn('filename="model.stl"', disposition)
        self.assertIn("filename*=UTF-8''model.stl", disposition)
        self.assertEqual(len(body), 84 + 50)
        self.assertEqual(struct.unpack_from("<I", body, 80)[0], 1)

    def test_bundle_download_is_a_real_zip(self) -> None:
        job_dir = Path(self.temp.name) / "jobs" / "zipcheck"
        self.write_test_stl(job_dir)
        (job_dir / "README.txt").write_text("print me", encoding="utf-8")
        with urlopen(f"{self.base}/api/jobs/zipcheck/bundle.zip", timeout=10) as response:
            body = response.read()
            self.assertEqual(response.headers["Content-Type"], "application/zip")
            disposition = response.headers["Content-Disposition"]
        self.assertIn('filename="touchlearn-zipcheck.zip"', disposition)
        self.assertTrue(body.startswith(b"PK\x03\x04"))
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            self.assertEqual(sorted(archive.namelist()), ["zipcheck/README.txt", "zipcheck/model.stl"])

    def test_content_disposition_encodes_unicode(self) -> None:
        header = content_disposition("触觉模型.stl")
        self.assertIn("filename*=UTF-8''", header)
        self.assertIn("%E8%A7%A6", header)

    def test_function_preview_recomputes_auto_window(self) -> None:
        payload = self.post_json(
            "/api/function-preview",
            {
                "expression": "x^2-2x-4",
                "auto_window": True,
                "x_min": -1.5,
                "x_max": 4.4,
                "y_min": -5.8,
                "y_max": 1.2,
            },
        )
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["auto_window"])
        self.assertLess(payload["x_min"], -1.2)
        self.assertGreater(payload["x_max"], 3.2)
        self.assertLessEqual(payload["y_min"], -5.0)
        self.assertFalse(payload["clipped"])

    def test_function_preview_rejects_a_partial_window(self) -> None:
        with self.assertRaises(HTTPError) as caught:
            self.post_json(
                "/api/function-preview",
                {"expression": "x^2-2x-4", "auto_window": False, "x_min": -1.0},
            )
        self.assertEqual(caught.exception.code, 400)
        body = json.loads(caught.exception.read().decode("utf-8"))
        self.assertIn("同时填写", body["error"])

    def test_function_preview_rejects_an_invalid_expression(self) -> None:
        with self.assertRaises(HTTPError) as caught:
            self.post_json(
                "/api/function-preview",
                {"expression": "x^2+", "auto_window": True},
            )
        self.assertEqual(caught.exception.code, 400)


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
