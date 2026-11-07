from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from tests import EXAMPLES  # noqa: F401  (ensures src is on sys.path)

from touchlearn.agent import printers
from touchlearn.agent.accounts import AccountStore, AuthSettings
from touchlearn.agent.server import AppState, Server
from touchlearn.geometry import Mesh
from touchlearn.stl import write_binary_stl


def cube(size: tuple[float, float, float] = (80.0, 60.0, 20.0)) -> Mesh:
    sx, sy, sz = size
    vertices = [(x, y, z) for x in (0.0, sx) for y in (0.0, sy) for z in (0.0, sz)]
    faces = [
        (0, 1, 3), (0, 3, 2),
        (4, 6, 7), (4, 7, 5),
        (0, 2, 6), (0, 6, 4),
        (1, 5, 7), (1, 7, 3),
        (0, 4, 5), (0, 5, 1),
        (2, 3, 7), (2, 7, 6),
    ]
    return Mesh([(vertices[a], vertices[b], vertices[c]) for a, b, c in faces])


def make_job(root: Path, size: tuple[float, float, float] = (80.0, 60.0, 20.0)) -> Path:
    job_dir = root / "jobs" / "jobtest"
    job_dir.mkdir(parents=True, exist_ok=True)
    write_binary_stl(cube(size), job_dir / "model.stl")
    (job_dir / "job.json").write_text(json.dumps({"id": "jobtest", "status": "done"}), encoding="utf-8")
    return job_dir


class ConfigTests(unittest.TestCase):
    def test_json_list_is_parsed(self) -> None:
        env = {
            "TOUCHLEARN_PRINTERS": json.dumps(
                [
                    {"id": "octo", "kind": "octoprint", "url": "http://192.168.1.50/", "api_key": "a"},
                    {"id": "klipper", "kind": "moonraker", "url": "https://printer.lan", "bed_mm": "350x350x400"},
                ]
            )
        }
        configs = printers.load_printers(env)
        self.assertEqual([item.id for item in configs], ["octo", "klipper"])
        self.assertEqual(configs[0].base_url, "http://192.168.1.50")
        self.assertEqual(configs[1].bed_mm, (350.0, 350.0, 400.0))
        self.assertEqual(configs[1].api_key, "")

    def test_wrapped_object_is_accepted(self) -> None:
        env = {"TOUCHLEARN_PRINTERS": json.dumps({"printers": [{"id": "p", "kind": "moonraker", "url": "http://p.lan"}]})}
        self.assertEqual(len(printers.load_printers(env)), 1)

    def test_single_printer_env(self) -> None:
        env = {
            "TOUCHLEARN_PRINTER_URL": "http://192.168.1.9",
            "TOUCHLEARN_PRINTER_KEY": "key",
            "TOUCHLEARN_PRINTER_BED": "220 220 250",
            "TOUCHLEARN_PRINTER_LABEL": "实验室打印机",
        }
        config = printers.load_printers(env)[0]
        self.assertEqual(config.kind, "octoprint")
        self.assertEqual(config.label, "实验室打印机")
        self.assertEqual(config.bed_mm, (220.0, 220.0, 250.0))

    def test_empty_env_means_no_printer(self) -> None:
        self.assertEqual(printers.load_printers({}), [])

    def test_unknown_kind_is_rejected(self) -> None:
        env = {"TOUCHLEARN_PRINTERS": json.dumps([{"id": "x", "kind": "usb", "url": "http://x"}])}
        with self.assertRaises(printers.PrinterError):
            printers.load_printers(env)

    def test_bad_url_and_credentials_are_rejected(self) -> None:
        for url in ("ftp://x", "printer.local", "http://user:pass@printer.local"):
            env = {"TOUCHLEARN_PRINTERS": json.dumps([{"id": "x", "kind": "moonraker", "url": url}])}
            with self.assertRaises(printers.PrinterError, msg=url):
                printers.load_printers(env)

    def test_octoprint_requires_api_key(self) -> None:
        env = {"TOUCHLEARN_PRINTERS": json.dumps([{"id": "x", "kind": "octoprint", "url": "http://x"}])}
        with self.assertRaises(printers.PrinterError):
            printers.load_printers(env)

    def test_duplicate_ids_are_rejected(self) -> None:
        env = {
            "TOUCHLEARN_PRINTERS": json.dumps(
                [
                    {"id": "a", "kind": "moonraker", "url": "http://a"},
                    {"id": "a", "kind": "moonraker", "url": "http://b"},
                ]
            )
        }
        with self.assertRaises(printers.PrinterError):
            printers.load_printers(env)

    def test_public_view_never_carries_the_key(self) -> None:
        config = printers.PrinterConfig(
            id="p", label="P", kind="octoprint", base_url="https://printer.lan:443", api_key="s3cret"
        )
        payload = json.dumps(config.public(), ensure_ascii=False)
        self.assertNotIn("s3cret", payload)
        self.assertTrue(config.public()["key_configured"])
        self.assertEqual(config.public()["host"], "printer.lan:443")

    def test_allow_print_needs_explicit_flag(self) -> None:
        self.assertFalse(printers.allow_print({}))
        self.assertTrue(printers.allow_print({"TOUCHLEARN_PRINT_ALLOW": "1"}))
        self.assertFalse(printers.allow_print({"TOUCHLEARN_PRINT_ALLOW": "1", "TOUCHLEARN_PRINT_DRY_RUN": "1"}))


class StlPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.job_dir = make_job(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def printer(self, bed: tuple[float, float, float] = (220.0, 220.0, 250.0)) -> printers.PrinterConfig:
        return printers.PrinterConfig(
            id="p", label="测试打印机", kind="octoprint", base_url="http://127.0.0.1:9", api_key="k", bed_mm=bed
        )

    def test_stl_summary(self) -> None:
        info = printers.inspect_stl(self.job_dir / "model.stl")
        self.assertEqual(info["triangles"], 12)
        self.assertEqual(info["size_mm"], [80.0, 60.0, 20.0])
        self.assertEqual(len(info["sha256"]), 64)

    def test_plan_passes_on_a_big_bed(self) -> None:
        plan = printers.plan_print(self.job_dir, self.printer())
        self.assertTrue(plan["ok"], plan["checks"])
        self.assertEqual([item["id"] for item in plan["checks"]][:4], ["format", "size", "triangles", "bed_fit"])

    def test_plan_blocks_a_model_larger_than_the_bed(self) -> None:
        plan = printers.plan_print(self.job_dir, self.printer((70.0, 70.0, 200.0)))
        self.assertFalse(plan["ok"])
        bed = [item for item in plan["checks"] if item["id"] == "bed_fit"][0]
        self.assertFalse(bed["ok"])
        self.assertIn("mm", bed["detail"])

    def test_plan_suggests_rotation_when_swapping_axes_fits(self) -> None:
        job_dir = make_job(self.root, size=(60.0, 110.0, 10.0))
        plan = printers.plan_print(job_dir, self.printer((120.0, 70.0, 200.0)))
        self.assertTrue(plan["ok"])
        self.assertEqual(plan["rotate_deg"], 90)

    def test_plan_respects_the_compliance_verdict(self) -> None:
        (self.job_dir / "compliance.json").write_text(json.dumps({"printable": False}), encoding="utf-8")
        plan = printers.plan_print(self.job_dir, self.printer())
        self.assertFalse(plan["ok"])
        self.assertTrue(any(item["id"] == "compliance" and not item["ok"] for item in plan["checks"]))

    def test_missing_stl_is_reported(self) -> None:
        self.job_dir.joinpath("model.stl").unlink()
        with self.assertRaises(printers.PrinterError) as caught:
            printers.plan_print(self.job_dir, self.printer())
        self.assertEqual(caught.exception.code, "missing_stl")

    def test_only_known_artifacts_can_be_sent(self) -> None:
        with self.assertRaises(printers.PrinterError):
            printers.plan_print(self.job_dir, self.printer(), filename="../job.json")

    def test_ascii_stl_is_rejected(self) -> None:
        (self.job_dir / "model.stl").write_text("solid x\nendsolid x\n", encoding="utf-8")
        with self.assertRaises(printers.PrinterError) as caught:
            printers.plan_print(self.job_dir, self.printer())
        self.assertEqual(caught.exception.code, "bad_stl")


class FakePrinter(BaseHTTPRequestHandler):
    requests: list[dict] = []
    status = 200
    reply: object = {"done": True}

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or "0")
        body = self.rfile.read(length)
        type(self).requests.append(
            {
                "path": self.path,
                "key": self.headers.get("X-Api-Key"),
                "type": self.headers.get("Content-Type"),
                "body": body,
            }
        )
        data = json.dumps(type(self).reply).encode("utf-8")
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        return


class TransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.job_dir = make_job(Path(self.temp.name))
        FakePrinter.requests = []
        FakePrinter.status = 200
        FakePrinter.reply = {"done": True}
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakePrinter)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temp.cleanup()

    def printer(self, kind: str = "octoprint", key: str = "k") -> printers.PrinterConfig:
        return printers.PrinterConfig(
            id=kind, label=kind, kind=kind, base_url=self.base, api_key=key, bed_mm=(220.0, 220.0, 250.0)
        )

    def test_octoprint_upload_carries_the_file_and_key(self) -> None:
        record = printers.run_print(self.job_dir, self.printer(), dry_run=False)
        self.assertTrue(record["ok"], record)
        self.assertEqual(record["state"], "sent")
        request = FakePrinter.requests[0]
        self.assertEqual(request["path"], "/api/files/local")
        self.assertEqual(request["key"], "k")
        self.assertIn(b"model.stl", request["body"])
        self.assertIn(b'name="select"', request["body"])
        self.assertIn(b'name="print"', request["body"])
        self.assertGreater(len(request["body"]), 600)

    def test_moonraker_uploads_then_starts(self) -> None:
        record = printers.run_print(self.job_dir, self.printer("moonraker"), dry_run=False)
        self.assertTrue(record["ok"], record)
        paths = [item["path"] for item in FakePrinter.requests]
        self.assertEqual(paths[0], "/server/files/upload")
        self.assertEqual(paths[1], "/printer/print/start?filename=model.stl")
        self.assertIn(b"gcodes", FakePrinter.requests[0]["body"])

    def test_http_error_is_recorded_not_raised(self) -> None:
        FakePrinter.status = 403
        FakePrinter.reply = {"error": "invalid api key"}
        record = printers.run_print(self.job_dir, self.printer(), dry_run=False)
        self.assertFalse(record["ok"])
        self.assertEqual(record["state"], "failed")
        self.assertEqual(record["error_code"], "printer_rejected")
        self.assertNotIn("k", json.dumps(record.get("transport")) or "")

    def test_unreachable_printer_is_reported(self) -> None:
        printer = printers.PrinterConfig(
            id="p", label="p", kind="octoprint", base_url="http://127.0.0.1:1", api_key="k"
        )
        record = printers.run_print(self.job_dir, printer, dry_run=False, timeout=2)
        self.assertEqual(record["state"], "failed")
        self.assertEqual(record["error_code"], "printer_unreachable")

    def test_dry_run_never_opens_a_socket(self) -> None:
        printer = printers.PrinterConfig(
            id="p", label="远方的打印机", kind="octoprint", base_url="http://127.0.0.1:1", api_key="k"
        )
        record = printers.run_print(self.job_dir, printer, dry_run=True)
        self.assertTrue(record["ok"])
        self.assertEqual(record["state"], "rehearsed")
        self.assertFalse(record["transport"]["sent"])
        self.assertEqual(FakePrinter.requests, [])
        self.assertEqual(json.loads((self.job_dir / "print.json").read_text(encoding="utf-8"))["state"], "rehearsed")

    def test_record_never_contains_the_api_key(self) -> None:
        printers.run_print(self.job_dir, self.printer(key="super-secret"), dry_run=False)
        text = (self.job_dir / "print.json").read_text(encoding="utf-8")
        self.assertNotIn("super-secret", text)


class PrintApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.state = AppState(cls.root, "http://127.0.0.1:8765")
        cls.server = Server(("127.0.0.1", 0), cls.state)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        make_job(cls.root)
        cls.env = mock.patch.dict(
            os.environ,
            {
                "TOUCHLEARN_PRINTERS": json.dumps(
                    [{"id": "lab", "label": "实验室", "kind": "octoprint", "url": "http://127.0.0.1:1", "api_key": "k"}]
                ),
                "TOUCHLEARN_PRINT_ALLOW": "",
            },
            clear=False,
        )
        cls.env.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.env.stop()
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.temp.cleanup()

    def get(self, path: str) -> dict:
        with urlopen(f"{self.base}{path}", timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))

    def post(self, path: str, payload: dict) -> tuple[int, dict]:
        request = Request(
            f"{self.base}{path}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=15) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def test_printers_endpoint_hides_the_key(self) -> None:
        payload = self.get("/api/printers")
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["allow_print"])
        self.assertEqual(payload["printers"][0]["id"], "lab")
        self.assertNotIn("api_key", json.dumps(payload, ensure_ascii=False))

    def test_plan_reports_every_check(self) -> None:
        status, payload = self.post("/api/print/plan", {"job_id": "jobtest", "printer_id": "lab"})
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["mode"], "rehearsal")
        self.assertEqual(len(payload["checks"]), 4)

    def test_plan_rejects_unknown_job(self) -> None:
        with self.assertRaises(HTTPError) as caught:
            self.post("/api/print/plan", {"job_id": "nope", "printer_id": "lab"})
        self.assertEqual(caught.exception.code, 404)

    def test_print_requires_confirmation(self) -> None:
        with self.assertRaises(HTTPError) as caught:
            self.post("/api/print", {"job_id": "jobtest", "printer_id": "lab"})
        self.assertEqual(caught.exception.code, 400)
        message = json.loads(caught.exception.read().decode("utf-8"))
        self.assertIn("确认", message["error"])

    def test_print_rehearsal_writes_a_record(self) -> None:
        status, payload = self.post(
            "/api/print", {"job_id": "jobtest", "printer_id": "lab", "confirm": True}
        )
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["print"]["state"], "rehearsed")
        self.assertTrue(payload["print"]["dry_run"])
        fetched = self.get("/api/jobs/jobtest/print")
        self.assertEqual(fetched["print"]["state"], "rehearsed")

    def test_print_rejects_unknown_printer(self) -> None:
        with self.assertRaises(HTTPError) as caught:
            self.post("/api/print", {"job_id": "jobtest", "printer_id": "ghost", "confirm": True})
        self.assertEqual(caught.exception.code, 404)

    def test_no_printer_configured_returns_conflict(self) -> None:
        with mock.patch.dict(os.environ, {"TOUCHLEARN_PRINTERS": "", "TOUCHLEARN_PRINTER_URL": ""}, clear=False):
            with self.assertRaises(HTTPError) as caught:
                self.post("/api/print/plan", {"job_id": "jobtest", "printer_id": "lab"})
        self.assertEqual(caught.exception.code, 409)
        self.assertEqual(caught.exception.headers.get("Content-Type"), "application/json; charset=utf-8")


class PrintAuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        auth = AccountStore(
            AuthSettings(db_path=Path(cls.temp.name) / "accounts.sqlite3", dev_mode=True, resend_after=0)
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

    def test_printers_require_login(self) -> None:
        with self.assertRaises(HTTPError) as caught:
            urlopen(f"{self.base}/api/printers", timeout=10)
        self.assertEqual(caught.exception.code, 401)

    def test_plan_requires_login(self) -> None:
        request = Request(
            f"{self.base}/api/print/plan",
            data=b'{"job_id":"x","printer_id":"y"}',
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(HTTPError) as caught:
            urlopen(request, timeout=10)
        self.assertEqual(caught.exception.code, 401)


if __name__ == "__main__":
    unittest.main()
