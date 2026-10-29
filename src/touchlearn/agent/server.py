"""Local HTTP server for the TouchLearn interactive front end.

The server is deliberately dependency-free: ``http.server`` plus the standard
library is enough for a competition demo that must run on a Windows laptop
without network access.  Uploads arrive as base64 inside JSON so the entire
API is one code path, and every generated artifact is served from the job
directory with path-traversal checks.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import io
import json
import mimetypes
import os
import socket
import threading
import time
import uuid
import webbrowser
import zipfile
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlparse, urlsplit

from . import __version__
from .agent import TouchLearnAgent
from .audio import available_voices
from .llm import llm_status, providers_payload
from .scene import scene_templates
from .vision import vision_status

STATIC_DIR = Path(__file__).with_name("static")
MAX_BODY_BYTES = 96 * 1024 * 1024
BUILD_LOCK = threading.Lock()

def running_in_container() -> bool:
    return os.environ.get("TOUCHLEARN_CONTAINER") == "1" or Path("/.dockerenv").exists()


def content_disposition(filename: str) -> str:
    """Build an RFC 6266 header that keeps the real filename on every browser."""
    fallback = filename.encode("ascii", "ignore").decode("ascii").replace('"', "").replace("\\", "")
    fallback = fallback or "download"
    return f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{quote(filename, safe="")}'


MIME_OVERRIDES = {
    ".stl": "model/stl",
    ".obj": "text/plain; charset=utf-8",
    ".svg": "image/svg+xml",
    ".wav": "audio/wav",
    ".md": "text/markdown; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".png": "image/png",
}


def detect_lan_ip() -> str:
    """Return the address a phone on the same Wi-Fi can usually reach."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return str(sock.getsockname()[0])
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


class AppState:
    def __init__(self, output_root: Path, public_base_url: str = "", access_code: str = "") -> None:
        self.agent = TouchLearnAgent(output_root=output_root)
        self.public_base_url = public_base_url.rstrip("/")
        self.output_root = output_root
        self.access_code = access_code.strip()
        self.access_token = (
            hashlib.sha256(f"touchlearn-access:{self.access_code}".encode("utf-8")).hexdigest()
            if self.access_code
            else ""
        )
        self._login_failures: dict[str, list[float]] = {}
        self._login_lock = threading.Lock()
        self._voices: list[str] | None = None

    @property
    def auth_required(self) -> bool:
        return bool(self.access_code)

    def voices(self) -> list[str]:
        if self._voices is None:
            self._voices = available_voices()
        return self._voices

    def login_allowed(self, ip: str) -> bool:
        now = time.time()
        with self._login_lock:
            attempts = [stamp for stamp in self._login_failures.get(ip, []) if now - stamp < 300]
            self._login_failures[ip] = attempts
            return len(attempts) < 10

    def note_login_failure(self, ip: str) -> None:
        with self._login_lock:
            self._login_failures.setdefault(ip, []).append(time.time())

    def prepare_job(self, draft_id: str) -> str:
        job_id = uuid.uuid4().hex[:12]
        self.agent.prepare_job(job_id, draft_id)
        return job_id


class Handler(BaseHTTPRequestHandler):
    server_version = f"TouchLearn/{__version__}"
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @property
    def state(self) -> AppState:
        return self.server.state  # type: ignore[attr-defined]

    def _send_json(self, payload: dict[str, Any], status: int = 200, headers: dict[str, str] | None = None) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def _send_error_json(self, status: int, message: str) -> None:
        self._send_json({"error": message}, status)

    def _send_file(self, path: Path, *, download: bool = False) -> None:
        if not path.is_file():
            self._send_error_json(HTTPStatus.NOT_FOUND, f"文件不存在：{path.name}")
            return
        data = path.read_bytes()
        content_type = MIME_OVERRIDES.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        if download:
            self.send_header("Content-Disposition", content_disposition(path.name))
        self.end_headers()
        self.wfile.write(data)

    def _request_host(self) -> str:
        forwarded = self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or ""
        return forwarded.split(",")[0].strip()

    def _request_proto(self) -> str:
        proto = (self.headers.get("X-Forwarded-Proto") or "http").split(",")[0].strip().lower()
        return proto if proto in {"http", "https"} else "http"

    def _request_base_url(self) -> str:
        host = self._request_host()
        if not host:
            host = f"127.0.0.1:{self.server.server_address[1]}"
        try:
            parsed = urlsplit(f"//{host}")
            hostname = (parsed.hostname or "").lower()
            port = parsed.port
        except ValueError:
            hostname, port = "", None
        if not hostname:
            hostname = host.split(":")[0].strip("[]").lower()
        if hostname in {"127.0.0.1", "localhost", "::1"}:
            # Keep the port the browser actually used.  In Docker the
            # container listens on 8765 but the published port may be 8766;
            # using server_address here would produce an unreachable QR URL.
            # A container cannot discover the host's LAN address, so keep the
            # host it was reached on and let TOUCHLEARN_PUBLIC_URL override.
            if not running_in_container():
                hostname = detect_lan_ip()
            if port is None:
                port = self.server.server_address[1]
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        netloc = f"{hostname}:{port}" if port else hostname
        return f"{self._request_proto()}://{netloc}"

    def _cookies(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for part in (self.headers.get("Cookie") or "").split(";"):
            if "=" in part:
                key, value = part.split("=", 1)
                result[key.strip()] = value.strip()
        return result

    def _is_authorized(self) -> bool:
        if not self.state.auth_required:
            return True
        token = self._cookies().get("touchlearn_access", "")
        return bool(token) and hmac.compare_digest(token, self.state.access_token)

    def _require_auth(self) -> bool:
        if self._is_authorized():
            return True
        self._send_error_json(HTTPStatus.UNAUTHORIZED, "需要访问码。请输入部署者提供的访问码。")
        return False

    def _access_cookie_header(self) -> str:
        secure = "; Secure" if self._request_proto() == "https" else ""
        return (
            f"touchlearn_access={self.state.access_token}; Path=/; HttpOnly; "
            f"SameSite=Lax; Max-Age=2592000{secure}"
        )

    def _client_ip(self) -> str:
        forwarded = self.headers.get("CF-Connecting-IP") or self.headers.get("X-Forwarded-For") or ""
        return (forwarded.split(",")[0].strip() or self.client_address[0])

    def _safe_under(self, base: Path, relative: str) -> Path | None:
        try:
            candidate = (base / relative).resolve()
            base_resolved = base.resolve()
            if candidate == base_resolved or base_resolved in candidate.parents:
                return candidate
        except (OSError, ValueError):
            return None
        return None

    def _read_json(self) -> dict[str, Any] | None:
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            self._send_error_json(HTTPStatus.BAD_REQUEST, "Content-Length 无效")
            return None
        if length <= 0 or length > MAX_BODY_BYTES:
            self._send_error_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "请求体为空或超过 96 MB 限制")
            return None
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_error_json(HTTPStatus.BAD_REQUEST, "请求体不是合法 JSON")
            return None
        if not isinstance(payload, dict):
            self._send_error_json(HTTPStatus.BAD_REQUEST, "请求体必须是 JSON 对象")
            return None
        return payload

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        print(f"[{self.log_date_time_string()}] {self.address_string()} {fmt % args}")

    # ------------------------------------------------------------------
    # Routes
    # ------------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = unquote(parsed.path)

        if path in {"/", "/index.html"}:
            self._send_file(STATIC_DIR / "index.html")
            return
        if path.startswith("/static/"):
            relative = path[len("/static/") :]
            target = self._safe_under(STATIC_DIR, relative)
            if target is None:
                self._send_error_json(HTTPStatus.FORBIDDEN, "非法路径")
                return
            self._send_file(target)
            return
        if path == "/api/health":
            authorized = self._is_authorized()
            locked = {"configured": False, "mode": "locked", "message": "需要访问码"}
            self._send_json(
                {
                    "ok": True,
                    "version": __version__,
                    "public_base_url": self.state.public_base_url or self._request_base_url(),
                    "lan_ip": detect_lan_ip(),
                    "access_required": self.state.auth_required,
                    "authenticated": authorized,
                    "voices": self.state.voices() if authorized else [],
                    "vision": vision_status() if authorized else locked,
                    "llm": llm_status() if authorized else locked,
                    "limits": {"file_mb": 30, "total_mb": 80},
                }
            )
            return
        if path in {"/api/providers", "/api/scene-templates", "/api/examples"}:
            if not self._require_auth():
                return
        if path == "/api/providers":
            self._send_json(providers_payload())
            return
        if path == "/api/scene-templates":
            self._send_json({"ok": True, "templates": scene_templates()})
            return
        if path == "/api/examples":
            self._send_json(
                {
                    "function": {
                        "expression": "x^2-2x-3",
                        "title": "二次函数",
                        "note": "A5 触觉板，含顶点、零点、y 轴截距与缩放条。",
                    },
                    "robot": {
                        "description": "六自由度机械臂，六个关节，连杆约 150 毫米，末端为两指夹爪。",
                        "title": "六轴机械臂",
                        "note": "可生成可缩放立体模型，或板面浮雕示意图。",
                    },
                    "scene": {
                        "text": "一个串联电路示意图：电池、开关、电阻和灯泡依次连接成回路。",
                        "title": "串联电路",
                        "note": "通用场景编译器会把图块排成 A5 触觉板，而不是只支持机械臂和函数。",
                    },
                    "auto": [
                        "把这张教材里的细胞结构图做成可触摸的 A5 板。",
                        "画一个说明水循环流程的触觉示意图：蒸发、凝结、降水、收集。",
                        "上传一张机械臂照片，做成板上浮雕示意图。",
                    ],
                }
            )
            return
        if path.startswith("/api/drafts/"):
            if not self._require_auth():
                return
            draft_id = path[len("/api/drafts/") :].strip("/")
            target = self._safe_under(self.state.agent.root / "drafts", f"{draft_id}/draft.json")
            if target is None:
                self._send_error_json(HTTPStatus.FORBIDDEN, "非法路径")
                return
            if not target.is_file():
                self._send_error_json(HTTPStatus.NOT_FOUND, "草稿不存在")
                return
            self._send_json(json.loads(target.read_text(encoding="utf-8")))
            return
        if path.startswith("/api/jobs/"):
            if not self._require_auth():
                return
            self._handle_job_get(path[len("/api/jobs/") :])
            return
        # Generated artifacts are intentionally readable by unguessable job ID
        # so a QR code or shared link works on a phone without a login cookie.
        if path.startswith("/files/"):
            self._handle_job_file(path[len("/files/") :], download="download" in parsed.query)
            return
        if path.startswith("/audio/"):
            self._handle_audio(path[len("/audio/") :])
            return
        if path.startswith("/preview/") and path.endswith(".svg"):
            job_id = path[len("/preview/") : -4]
            target = self._safe_under(self.state.agent.root / "jobs", f"{job_id}/preview.svg")
            if target is None:
                self._send_error_json(HTTPStatus.FORBIDDEN, "非法路径")
                return
            self._send_file(target)
            return
        self._send_error_json(HTTPStatus.NOT_FOUND, "未知路径")

    def _handle_job_get(self, suffix: str) -> None:
        parts = [part for part in suffix.split("/") if part]
        if not parts:
            self._send_error_json(HTTPStatus.NOT_FOUND, "缺少作业编号")
            return
        job_id = parts[0]
        if len(parts) == 1:
            target = self._safe_under(self.state.agent.root / "jobs", f"{job_id}/job.json")
            if target is None:
                self._send_error_json(HTTPStatus.FORBIDDEN, "非法路径")
                return
            if not target.is_file():
                self._send_error_json(HTTPStatus.NOT_FOUND, "作业尚未创建")
                return
            self._send_json(json.loads(target.read_text(encoding="utf-8")))
            return
        if parts[1] == "report":
            target = self._safe_under(self.state.agent.root / "jobs", f"{job_id}/compliance.json")
            if target is None or not target.is_file():
                self._send_error_json(HTTPStatus.NOT_FOUND, "合规报告尚未生成")
                return
            self._send_json(json.loads(target.read_text(encoding="utf-8")))
            return
        if parts[1] == "bundle.zip":
            job_dir = self._safe_under(self.state.agent.root / "jobs", job_id)
            if job_dir is None or not job_dir.is_dir():
                self._send_error_json(HTTPStatus.NOT_FOUND, "作业不存在")
                return
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for item in sorted(job_dir.iterdir()):
                    if item.is_file() and item.name != "bundle.zip":
                        archive.write(item, arcname=f"{job_id}/{item.name}")
            data = buffer.getvalue()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition", content_disposition(f"touchlearn-{job_id}.zip"))
            self.end_headers()
            self.wfile.write(data)
            return
        self._send_error_json(HTTPStatus.NOT_FOUND, "未知作业资源")

    def _handle_job_file(self, suffix: str, *, download: bool) -> None:
        parts = [part for part in suffix.split("/") if part]
        if len(parts) != 2:
            self._send_error_json(HTTPStatus.NOT_FOUND, "文件路径无效")
            return
        job_id, name = parts
        target = self._safe_under(self.state.agent.root / "jobs", f"{job_id}/{name}")
        if target is None:
            self._send_error_json(HTTPStatus.FORBIDDEN, "非法路径")
            return
        self._send_file(target, download=download)

    def _handle_audio(self, suffix: str) -> None:
        parts = [part for part in suffix.split("/") if part]
        if not parts:
            self._send_error_json(HTTPStatus.NOT_FOUND, "缺少作业编号")
            return
        job_id = parts[0]
        if len(parts) == 1:
            target = self._safe_under(self.state.agent.root / "jobs", f"{job_id}/audio.html")
            if target is None:
                self._send_error_json(HTTPStatus.FORBIDDEN, "非法路径")
                return
            self._send_file(target)
            return
        target = self._safe_under(self.state.agent.root / "jobs", f"{job_id}/{parts[-1]}")
        if target is None:
            self._send_error_json(HTTPStatus.FORBIDDEN, "非法路径")
            return
        self._send_file(target)

    def _handle_login(self) -> None:
        payload = self._read_json()
        if payload is None:
            return
        code = str(payload.get("code") or "")
        ip = self._client_ip()
        if not self.state.login_allowed(ip):
            self._send_error_json(HTTPStatus.TOO_MANY_REQUESTS, "尝试次数过多，请 5 分钟后再试。")
            return
        if not self.state.auth_required or hmac.compare_digest(code, self.state.access_code):
            self._send_json(
                {"ok": True, "access_required": self.state.auth_required},
                headers={"Set-Cookie": self._access_cookie_header()},
            )
            return
        self.state.note_login_failure(ip)
        self._send_error_json(HTTPStatus.UNAUTHORIZED, "访问码不正确。")

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/login":
            self._handle_login()
            return
        if path == "/api/logout":
            self._send_json(
                {"ok": True},
                headers={"Set-Cookie": "touchlearn_access=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"},
            )
            return
        if not self._require_auth():
            return
        if path == "/api/analyze":
            payload = self._read_json()
            if payload is None:
                return
            try:
                draft = self.state.agent.analyze(payload)
            except ValueError as error:
                self._send_error_json(HTTPStatus.BAD_REQUEST, str(error))
                return
            except Exception as error:  # pragma: no cover - defensive
                self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, f"分析失败：{error}")
                return
            self._send_json({"ok": True, "draft": draft})
            return
        if path == "/api/build":
            payload = self._read_json()
            if payload is None:
                return
            draft_id = str(payload.get("draft_id") or "").strip()
            if not draft_id:
                self._send_error_json(HTTPStatus.BAD_REQUEST, "缺少 draft_id")
                return
            draft_path = self.state.agent.root / "drafts" / draft_id / "draft.json"
            if not draft_path.is_file():
                self._send_error_json(HTTPStatus.NOT_FOUND, "草稿不存在，请先让智能体分析输入")
                return
            overrides = payload.get("overrides") if isinstance(payload.get("overrides"), dict) else {}
            job_id = self.state.prepare_job(draft_id)
            # Capture the address the user actually used (LAN IP, tunnel host,
            # or a fixed public URL) so the QR code points somewhere reachable.
            public_base_url = self.state.public_base_url or self._request_base_url()

            def run_build() -> None:
                with BUILD_LOCK:
                    try:
                        self.state.agent.build(
                            draft_id,
                            overrides=overrides,
                            public_base_url=public_base_url,
                            job_id=job_id,
                        )
                    except Exception as error:  # the job.json already records the failure
                        print(f"[build {job_id}] failed: {error}")

            thread = threading.Thread(target=run_build, name=f"build-{job_id}", daemon=True)
            thread.start()
            self._send_json({"ok": True, "job_id": job_id, "status": "queued"})
            return
        self._send_error_json(HTTPStatus.NOT_FOUND, "未知接口")


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], state: AppState) -> None:
        super().__init__(address, Handler)
        self.state = state


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="TouchLearn 交互式前端服务")
    parser.add_argument(
        "--host",
        default=os.environ.get("TOUCHLEARN_HOST", "0.0.0.0"),
        help="监听地址，默认 0.0.0.0（同一 Wi-Fi 可扫码）",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PORT") or os.environ.get("TOUCHLEARN_PORT") or 8765),
        help="端口，默认 8765；云平台会注入 PORT",
    )
    parser.add_argument("--outputs", default=os.environ.get("TOUCHLEARN_OUTPUTS", "outputs"), help="产物目录，默认 outputs")
    parser.add_argument(
        "--public-url",
        default=os.environ.get("TOUCHLEARN_PUBLIC_URL", ""),
        help="固定公网地址；默认按请求 Host/X-Forwarded-* 自动推导",
    )
    parser.add_argument(
        "--access-code",
        default=os.environ.get("TOUCHLEARN_ACCESS_CODE", ""),
        help="可选访问码；设置后分析、生成和作业查询需要登录",
    )
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output_root = Path(args.outputs).expanduser()
    port = int(args.port)
    public_base_url = args.public_url.rstrip("/") if args.public_url else ""
    access_code = args.access_code.strip()
    state = AppState(output_root=output_root, public_base_url=public_base_url, access_code=access_code)
    server = Server((args.host, port), state)
    local_url = f"http://127.0.0.1:{port}"
    lan_url = f"http://{detect_lan_ip()}:{port}"
    print("TouchLearn Agent", __version__)
    print(f"  本机界面: {local_url}")
    print(f"  局域网:   {lan_url}")
    if public_base_url:
        print(f"  公网地址: {public_base_url}")
    if access_code:
        print(f"  访问码:   {access_code}")
    else:
        print("  访问码:   未设置（公网暴露时请设置 TOUCHLEARN_ACCESS_CODE）")
    print(f"  产物目录: {output_root.resolve()}")
    print("  按 Ctrl+C 停止服务。")
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(local_url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止服务……")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
