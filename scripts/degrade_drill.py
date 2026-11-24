#!/usr/bin/env python
"""Cloud-failure drill: unreachable, timeout and HTTP 500.

Each scenario points the primary provider at a stub, enables
``TOUCHLEARN_LOCAL_FALLBACK=1`` and checks that ``chat_json`` still returns a
parsed answer from the local llama-server.  The summary is written as JSON so a
competition reviewer can re-run it and compare results.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from touchlearn.agent.llm import chat_json, llm_status  # noqa: E402


class StubState:
    mode = "ok"


class StubHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        if StubState.mode == "timeout":
            time.sleep(12)
        if StubState.mode == "http500":
            body = b'{"error":{"message":"stub 500"}}'
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        payload = {
            "choices": [{"message": {"content": '{"ok": true}'}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        return


def start_stub() -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), StubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/v1"


def local_ready(base: str) -> bool:
    health = base.rstrip("/").removesuffix("/v1") + "/health"
    try:
        with urllib.request.urlopen(health, timeout=3) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError):
        return False


def run_case(name: str, primary_url: str, timeout: float) -> dict[str, object]:
    os.environ.update(
        {
            "TOUCHLEARN_LLM_PROVIDER": "custom",
            "TOUCHLEARN_LLM_URL": primary_url,
            "TOUCHLEARN_LLM_MODEL": "stub-cloud-model",
            "TOUCHLEARN_LLM_TIMEOUT": str(timeout),
            "TOUCHLEARN_LLM_EFFORT": "off",
            "TOUCHLEARN_LOCAL_FALLBACK": "1",
        }
    )
    started = time.monotonic()
    result = chat_json(
        '只输出 JSON：{"ok": true}',
        "这是一次降级演练，请直接返回 JSON。",
        kind="text",
    )
    elapsed = int((time.monotonic() - started) * 1000)
    fell_back = "已回落本地模型" in (result.message or "")
    return {
        "scenario": name,
        "primary_url": primary_url,
        "primary_timeout_s": timeout,
        "ok": result.ok,
        "fell_back": fell_back,
        "provider": result.provider,
        "model": result.model,
        "latency_ms": result.latency_ms,
        "wall_ms": elapsed,
        "message": (result.message or "")[:200],
        "data": result.data,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="云端异常三场景降级演练")
    parser.add_argument("--local-url", default=os.environ.get("TOUCHLEARN_LOCAL_URL", "http://127.0.0.1:8080/v1"))
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    if not local_ready(args.local_url):
        print(f"本地模型未就绪：{args.local_url}（先启动 llama-server）", file=sys.stderr)
        return 2

    os.environ["TOUCHLEARN_LOCAL_URL"] = args.local_url
    os.environ.setdefault("TOUCHLEARN_LOCAL_MODEL", "qwen2.5-3b-instruct-q4_k_m")
    os.environ.setdefault("TOUCHLEARN_LOCAL_TIMEOUT", "60")

    stub, stub_url = start_stub()
    try:
        cases = []
        StubState.mode = "ok"
        cases.append(run_case("unreachable", "http://127.0.0.1:1/v1", 2))
        StubState.mode = "timeout"
        cases.append(run_case("timeout", stub_url, 5))
        StubState.mode = "http500"
        cases.append(run_case("http500", stub_url, 5))
    finally:
        stub.shutdown()

    summary = {
        "date": time.strftime("%Y-%m-%d %H:%M:%S"),
        "local_endpoint": args.local_url,
        "status": llm_status(),
        "cases": cases,
        "passed": all(case["ok"] and case["fell_back"] for case in cases),
    }
    text = json.dumps(summary, ensure_ascii=False, indent=2)
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
