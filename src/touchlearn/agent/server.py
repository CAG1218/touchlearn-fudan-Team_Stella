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
import io
import json
import mimetypes
import os
import socket
import threading
import uuid
import webbrowser
import zipfile
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlparse, urlsplit

from . import __version__
from .accounts import (
    LEGACY_ACCESS_COOKIE,
    SESSION_COOKIE,
    AccountStore,
    AuthError,
    AuthSettings,
)
from .agent import TouchLearnAgent
from .audio import available_voices
from .llm import llm_status, load_env, providers_payload
from .scene import scene_templates, template_preview
from .scene_svg import scene_to_svg
from .vision import vision_status

STATIC_DIR = Path(__file__).with_name("static")
MAX_BODY_BYTES = 128 * 1024 * 1024
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
    def __init__(
        self,
        output_root: Path,
        public_base_url: str = "",
        auth: AccountStore | None = None,
        icp: str = "",
    ) -> None:
        self.agent = TouchLearnAgent(output_root=output_root)
        self.public_base_url = public_base_url.rstrip("/")
        self.output_root = output_root
        self.auth = auth
        self.icp = icp.strip()
        self._voices: list[str] | None = None

    @property
    def auth_required(self) -> bool:
        return self.auth is not None

    def voices(self) -> list[str]:
        if self._voices is None:
            self._voices = available_voices()
        return self._voices

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

    def _send_json(
        self,
        payload: dict[str, Any],
        status: int = 200,
        headers: dict[str, str] | list[tuple[str, str]] | None = None,
    ) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        items = headers.items() if isinstance(headers, dict) else (headers or [])
        for key, value in items:
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

    def _session_token(self) -> str:
        return self._cookies().get(SESSION_COOKIE, "")

    def _current_user(self) -> dict[str, Any] | None:
        if self.state.auth is None:
            return None
        return self.state.auth.user_for_token(self._session_token())

    def _is_authorized(self) -> bool:
        if self.state.auth is None:
            return True
        return self._current_user() is not None

    def _require_auth(self) -> bool:
        if self._is_authorized():
            return True
        self._send_error_json(HTTPStatus.UNAUTHORIZED, "请先登录。")
        return False

    def _require_user(self) -> dict[str, Any] | None:
        user = self._current_user()
        if user is None:
            self._send_error_json(HTTPStatus.UNAUTHORIZED, "请先登录。")
        return user

    def _session_cookie(self, token: str, max_age: int) -> str:
        secure = "; Secure" if self._request_proto() == "https" else ""
        return (
            f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; "
            f"SameSite=Lax; Max-Age={max_age}{secure}"
        )

    @staticmethod
    def _clear_cookie(name: str) -> str:
        return f"{name}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"

    def _auth_failure(self, error: AuthError) -> None:
        payload: dict[str, Any] = {"error": error.message, "code": error.code}
        if error.retry_after is not None:
            payload["retry_after"] = error.retry_after
        self._send_json(payload, status=error.status)

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
            user = self._current_user() if self.state.auth is not None else None
            authorized = self.state.auth is None or user is not None
            locked = {"configured": False, "mode": "locked", "message": "登录后可查看"}
            auth_config = (
                self.state.auth.public_config()
                if self.state.auth is not None
                else {"phone": False, "email": False, "dev_mode": False, "code_ttl": 600, "resend_after": 60, "session_days": 30}
            )
            self._send_json(
                {
                    "ok": True,
                    "version": __version__,
                    "public_base_url": self.state.public_base_url or self._request_base_url(),
                    "lan_ip": detect_lan_ip(),
                    "auth_required": self.state.auth_required,
                    "authenticated": authorized,
                    "user": user,
                    "auth": auth_config,
                    "icp": self.state.icp,
                    "voices": self.state.voices() if authorized else [],
                    "vision": vision_status() if authorized else locked,
                    "llm": llm_status() if authorized else locked,
                    "limits": {"file_mb": 30, "total_mb": 80},
                }
            )
            return
        if path == "/api/auth/me":
            user = self._current_user()
            if user is None:
                self._send_json(
                    {"ok": False, "authenticated": False, "error": "未登录", "auth": self.state.auth.public_config() if self.state.auth else None},
                    status=HTTPStatus.UNAUTHORIZED,
                )
                return
            self._send_json({"ok": True, "authenticated": True, "user": user})
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
        if path.startswith("/api/templates/") and path.endswith("/thumbnail.svg"):
            # Welcome-card artwork: public and cacheable so it still renders on
            # the login screen, before a session exists.
            template_id = path[len("/api/templates/") : -len("/thumbnail.svg")].strip("/")
            preview = template_preview(template_id)
            if preview is None or not isinstance(preview.get("scene"), dict):
                self._send_error_json(HTTPStatus.NOT_FOUND, "模板不存在")
                return
            svg = scene_to_svg(preview["scene"], show_names=False)
            query = parsed.query or ""
            if "crop=loop" in query:
                # Tight crop around the component loop: used by the welcome
                # card where the symbols must stay legible at ~100 px.
                svg = svg.replace(
                    'viewBox="0 0 148 210"',
                    'viewBox="24 54 100 96" preserveAspectRatio="xMidYMid meet"',
                    1,
                )
            elif "crop=content" in query:
                # Template tiles need the whole diagram area but not the
                # footer and QR block.
                svg = svg.replace(
                    'viewBox="0 0 148 210"',
                    'viewBox="2 18 144 152" preserveAspectRatio="xMidYMid meet"',
                    1,
                )
            data = svg.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "image/svg+xml; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            # Templates are immutable until the server restarts, so a short
            # public cache keeps the welcome screen instant on phones.
            self.send_header("Cache-Control", "public, max-age=3600")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)
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
        if parts[1] == "verify":
            job_dir = self._safe_under(self.state.agent.root / "jobs", job_id)
            if job_dir is None or not job_dir.is_dir():
                self._send_error_json(HTTPStatus.NOT_FOUND, "作业不存在")
                return
            manifest_path = job_dir / "manifest.json"
            if not manifest_path.is_file():
                self._send_error_json(HTTPStatus.NOT_FOUND, "清单尚未生成")
                return
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "清单无法读取")
                return
            artifacts = manifest.get("artifacts") if isinstance(manifest, dict) else None
            artifacts = artifacts if isinstance(artifacts, list) else []
            checked = 0
            mismatches: list[dict[str, Any]] = []
            for item in artifacts:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "")
                expected = str(item.get("sha256") or "")
                if not name or not expected:
                    continue
                target = self._safe_under(job_dir, name)
                if target is None or not target.is_file():
                    mismatches.append({"name": name, "reason": "missing"})
                    continue
                actual = hashlib.sha256(target.read_bytes()).hexdigest()
                checked += 1
                if actual != expected:
                    mismatches.append(
                        {
                            "name": name,
                            "reason": "sha256",
                            "expected": expected,
                            "actual": actual,
                        }
                    )
            self._send_json(
                {
                    "ok": not mismatches,
                    "checked": checked,
                    "total": len(artifacts),
                    "mismatches": mismatches,
                    "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                }
            )
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

    def _auth_json(self, action) -> None:  # type: ignore[no-untyped-def]
        try:
            payload = action()
        except AuthError as error:
            self._auth_failure(error)
            return
        except Exception as error:  # pragma: no cover - defensive
            print(f"[auth] {type(error).__name__}: {error}")
            self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "账号服务暂时不可用，请稍后重试。")
            return
        self._send_json(payload)

    def _read_auth_identity(self) -> tuple[str, str] | None:
        payload = self._read_json()
        if payload is None:
            return None
        return str(payload.get("kind") or "").strip(), str(payload.get("value") or "").strip()

    def _handle_auth_request_code(self) -> None:
        if self.state.auth is None:
            self._send_error_json(HTTPStatus.NOT_FOUND, "账号服务未启用。")
            return
        payload = self._read_json()
        if payload is None:
            return
        kind = str(payload.get("kind") or "").strip()
        value = str(payload.get("value") or "").strip()
        purpose = str(payload.get("purpose") or "login").strip()
        ip = self._client_ip()
        auth = self.state.auth
        if purpose == "link":
            user = self._require_user()
            if user is None:
                return
            self._auth_json(lambda: auth.request_link_code(user["id"], kind, value, ip=ip))
            return
        self._auth_json(lambda: auth.request_code(kind, value, ip=ip))

    def _handle_auth_login(self) -> None:
        if self.state.auth is None:
            self._send_error_json(HTTPStatus.NOT_FOUND, "账号服务未启用。")
            return
        payload = self._read_json()
        if payload is None:
            return
        kind = str(payload.get("kind") or "").strip()
        value = str(payload.get("value") or "").strip()
        code = str(payload.get("code") or "").strip()
        auth = self.state.auth
        try:
            result = auth.login(
                kind,
                value,
                code,
                ip=self._client_ip(),
                user_agent=self.headers.get("User-Agent") or "",
            )
        except AuthError as error:
            self._auth_failure(error)
            return
        except Exception as error:  # pragma: no cover - defensive
            print(f"[auth] login failed: {type(error).__name__}: {error}")
            self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "登录失败，请稍后重试。")
            return
        max_age = auth.settings.session_ttl
        self._send_json(
            {"ok": True, "user": result["user"], "expires_at": result["expires_at"]},
            headers=[
                ("Set-Cookie", self._session_cookie(result["token"], max_age)),
                ("Set-Cookie", self._clear_cookie(LEGACY_ACCESS_COOKIE)),
            ],
        )

    def _handle_auth_logout(self) -> None:
        if self.state.auth is not None:
            try:
                self.state.auth.logout(self._session_token())
            except Exception as error:  # pragma: no cover - defensive
                print(f"[auth] logout failed: {error}")
        self._send_json(
            {"ok": True, "authenticated": False},
            headers=[
                ("Set-Cookie", self._clear_cookie(SESSION_COOKIE)),
                ("Set-Cookie", self._clear_cookie(LEGACY_ACCESS_COOKIE)),
            ],
        )

    def _handle_auth_link_code(self) -> None:
        if self.state.auth is None:
            self._send_error_json(HTTPStatus.NOT_FOUND, "账号服务未启用。")
            return
        user = self._require_user()
        if user is None:
            return
        identity = self._read_auth_identity()
        if identity is None:
            return
        kind, value = identity
        ip = self._client_ip()
        auth = self.state.auth
        self._auth_json(lambda: auth.request_link_code(user["id"], kind, value, ip=ip))

    def _handle_auth_link(self) -> None:
        if self.state.auth is None:
            self._send_error_json(HTTPStatus.NOT_FOUND, "账号服务未启用。")
            return
        user = self._require_user()
        if user is None:
            return
        payload = self._read_json()
        if payload is None:
            return
        kind = str(payload.get("kind") or "").strip()
        value = str(payload.get("value") or "").strip()
        code = str(payload.get("code") or "").strip()
        auth = self.state.auth
        self._auth_json(
            lambda: {"ok": True, "user": auth.link_identity(user["id"], kind, value, code)}
        )

    def _handle_auth_unlink(self) -> None:
        if self.state.auth is None:
            self._send_error_json(HTTPStatus.NOT_FOUND, "账号服务未启用。")
            return
        user = self._require_user()
        if user is None:
            return
        payload = self._read_json()
        if payload is None:
            return
        try:
            identity_id = int(payload.get("identity_id"))
        except (TypeError, ValueError):
            self._send_error_json(HTTPStatus.BAD_REQUEST, "缺少绑定关系编号。")
            return
        auth = self.state.auth
        self._auth_json(lambda: {"ok": True, "user": auth.unlink_identity(user["id"], identity_id)})

    def _handle_auth_profile(self) -> None:
        if self.state.auth is None:
            self._send_error_json(HTTPStatus.NOT_FOUND, "账号服务未启用。")
            return
        user = self._require_user()
        if user is None:
            return
        payload = self._read_json()
        if payload is None:
            return
        display_name = str(payload.get("display_name") or "")
        auth = self.state.auth
        self._auth_json(lambda: {"ok": True, "user": auth.update_profile(user["id"], display_name)})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        origin = (self.headers.get("Origin") or "").strip()
        if origin:
            origin_host = urlsplit(origin).netloc.lower()
            host = (self.headers.get("Host") or "").lower()
            if origin_host and origin_host != host:
                self._send_error_json(HTTPStatus.FORBIDDEN, "跨站请求被拒绝。")
                return
        if path == "/api/auth/request-code":
            self._handle_auth_request_code()
            return
        if path == "/api/auth/login":
            self._handle_auth_login()
            return
        if path == "/api/auth/logout":
            self._handle_auth_logout()
            return
        if path == "/api/auth/link-code":
            self._handle_auth_link_code()
            return
        if path == "/api/auth/link":
            self._handle_auth_link()
            return
        if path == "/api/auth/unlink":
            self._handle_auth_unlink()
            return
        if path == "/api/auth/profile":
            self._handle_auth_profile()
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
        if path == "/api/function-preview":
            payload = self._read_json()
            if payload is None:
                return
            try:
                preview = self.state.agent.preview_function(payload)
            except ValueError as error:
                self._send_error_json(HTTPStatus.BAD_REQUEST, str(error))
                return
            except Exception as error:  # pragma: no cover - defensive
                self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, f"预览失败：{error}")
                return
            self._send_json({"ok": True, **preview})
            return
        if path == "/api/scene-preview":
            payload = self._read_json()
            if payload is None:
                return
            try:
                preview = self.state.agent.preview_scene(payload)
            except ValueError as error:
                self._send_error_json(HTTPStatus.BAD_REQUEST, str(error))
                return
            except Exception as error:  # pragma: no cover - defensive
                self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, f"预览失败：{error}")
                return
            self._send_json({"ok": True, **preview})
            return
        if path == "/api/scene-edit":
            payload = self._read_json()
            if payload is None:
                return
            try:
                preview = self.state.agent.edit_scene(payload)
            except ValueError as error:
                self._send_error_json(HTTPStatus.BAD_REQUEST, str(error))
                return
            except Exception as error:  # pragma: no cover - defensive
                self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, f"编辑失败：{error}")
                return
            self._send_json({"ok": True, **preview})
            return
        if path == "/api/scene-recognize":
            payload = self._read_json()
            if payload is None:
                return
            try:
                result = self.state.agent.recognize_scene(payload)
            except ValueError as error:
                self._send_error_json(HTTPStatus.BAD_REQUEST, str(error))
                return
            except Exception as error:  # pragma: no cover - defensive
                self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, f"图片识别失败：{error}")
                return
            self._send_json({"ok": True, **result})
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
        "--auth-db",
        default=os.environ.get("TOUCHLEARN_AUTH_DB", ""),
        help="账号数据库路径；默认 <产物目录>/accounts.sqlite3",
    )
    parser.add_argument(
        "--auth-dev-mode",
        action="store_true",
        help="开发模式：验证码直接显示在页面上，仅限本机测试",
    )
    parser.add_argument(
        "--icp",
        default=os.environ.get("TOUCHLEARN_ICP_BEIAN", ""),
        help="ICP备案号；设置后展示在登录页和设置页",
    )
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    return parser


def main(argv: list[str] | None = None) -> int:
    # Pick up a local .env before reading CLI defaults, auth channels and
    # deployment settings; llm.load_env never overrides real environment vars.
    load_env()
    args = build_parser().parse_args(argv)
    output_root = Path(args.outputs).expanduser()
    port = int(args.port)
    public_base_url = args.public_url.rstrip("/") if args.public_url else ""
    auth_db = Path(args.auth_db).expanduser() if args.auth_db else output_root / "accounts.sqlite3"
    settings = AuthSettings.from_env(auth_db, dev_mode=True if args.auth_dev_mode else None)
    if settings.dev_mode is None:
        # Without a delivery channel nobody could ever sign in, so fall back to
        # the console code and make the trade-off loud and explicit.
        settings.dev_mode = not (settings.email_ready or settings.phone_ready)
    auth = AccountStore(settings)
    state = AppState(
        output_root=output_root,
        public_base_url=public_base_url,
        auth=auth,
        icp=args.icp.strip(),
    )
    server = Server((args.host, port), state)
    local_url = f"http://127.0.0.1:{port}"
    lan_url = f"http://{detect_lan_ip()}:{port}"
    print("TouchLearn Agent", __version__)
    print(f"  本机界面: {local_url}")
    print(f"  局域网:   {lan_url}")
    if public_base_url:
        print(f"  公网地址: {public_base_url}")
    print(f"  账号库:   {auth_db.resolve()}")
    methods = auth.methods()
    channels = []
    if methods.get("phone"):
        channels.append("手机号")
    if methods.get("email"):
        channels.append("邮箱")
    print(f"  登录方式: {' / '.join(channels) or '未配置'}")
    if auth.dev_mode:
        print("  验证码:   开发模式，验证码直接显示在登录页（仅限测试）")
        print("  [警告] 公网部署请配置 SMTP 或阿里云短信，并关闭 TOUCHLEARN_AUTH_DEV_MODE。")
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
