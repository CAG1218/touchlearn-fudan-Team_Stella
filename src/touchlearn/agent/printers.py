"""Optional hand-off of a finished STL to a 3D printer.

TouchLearn's main line ends at a verified ``model.stl``.  This module is the
optional extension that pushes that file to a printer the operator already
owns, so the whole feature can stay off by default and the competition build
still runs on a laptop with no hardware.

Two independent gates have to agree before a single byte goes out:

* the operator sets ``TOUCHLEARN_PRINT_ALLOW=1`` (or leaves it unset and the
  request is rehearsed instead), and
* the user ticks the confirmation box in the UI, which sends ``confirm=true``.

Bed-fit, format and size checks run in both modes, so a rehearsal proves the
same deterministic plan a real print would use.  Nothing here talks to a
printer unless both gates pass; the rehearsal path only computes numbers.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import struct
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote, urlsplit

from ..stl import read_stl_info, sha256_file

MAX_STL_BYTES = 200 * 1024 * 1024
MAX_TRIANGLES = 4_000_000
BED_MARGIN_MM = 3.0
ALLOWED_FILES = ("model.stl", "plate_model.stl")
SUPPORTED_KINDS = ("octoprint", "moonraker")
_CHUNK = 256 * 1024
_TRUE = {"1", "true", "yes", "on"}

# One in-flight request per job is enough to stop a double click from queueing
# two physical prints.  The lock lives in this process; a multi-worker deploy
# would need a shared lease, which the competition build does not run.
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()
_RECORDS: dict[str, dict[str, Any]] = {}


class PrinterError(Exception):
    """A request that cannot proceed, with an HTTP-friendly status."""

    def __init__(self, message: str, *, status: int = 400, code: str = "printer_error") -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


@dataclass(frozen=True)
class PrinterConfig:
    id: str
    label: str
    kind: str
    base_url: str
    api_key: str = ""
    bed_mm: tuple[float, float, float] = (220.0, 220.0, 250.0)

    def public(self) -> dict[str, Any]:
        """What the browser may see: never the API key or its length."""
        parts = urlsplit(self.base_url)
        host = parts.hostname or ""
        if parts.port:
            host = f"{host}:{parts.port}"
        return {
            "id": self.id,
            "label": self.label,
            "kind": self.kind,
            "host": host,
            "secure": parts.scheme == "https",
            "bed_mm": list(self.bed_mm),
            "key_configured": bool(self.api_key),
        }


def _env_flag(name: str, environ: dict[str, str]) -> bool:
    return str(environ.get(name, "")).strip().lower() in _TRUE


def allow_print(environ: dict[str, str] | None = None) -> bool:
    """Real printing needs an explicit opt-in on the server side."""
    env = os.environ if environ is None else environ
    if _env_flag("TOUCHLEARN_PRINT_DRY_RUN", env):
        return False
    return _env_flag("TOUCHLEARN_PRINT_ALLOW", env)


def _parse_bed(raw: Any, *, where: str) -> tuple[float, float, float]:
    if raw is None or raw == "":
        return (220.0, 220.0, 250.0)
    values: list[float] = []
    if isinstance(raw, str):
        parts = re.split(r"\s*[x×*,\s]\s*", raw.strip())
        values = [float(part) for part in parts if part]
    elif isinstance(raw, (list, tuple)):
        values = [float(item) for item in raw]
    else:
        raise PrinterError(f"{where} 的 bed_mm 必须是 [x, y, z] 或 \"220x220x250\"")
    if len(values) != 3:
        raise PrinterError(f"{where} 的打印床尺寸需要三个数：宽、深、高")
    if any(value <= 10 or value > 2000 for value in values):
        raise PrinterError(f"{where} 的打印床尺寸超出合理范围（10–2000 mm）")
    return (values[0], values[1], values[2])


def _parse_one(raw: Any, index: int) -> PrinterConfig:
    if not isinstance(raw, dict):
        raise PrinterError(f"第 {index + 1} 台打印机配置必须是 JSON 对象")
    where = f"第 {index + 1} 台打印机"
    kind = str(raw.get("kind") or "octoprint").strip().lower()
    if kind not in SUPPORTED_KINDS:
        raise PrinterError(f"{where} 的 kind 只支持 {' / '.join(SUPPORTED_KINDS)}")
    url = str(raw.get("url") or raw.get("base_url") or "").strip().rstrip("/")
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise PrinterError(f"{where} 的 url 必须是 http:// 或 https:// 开头的完整地址")
    if parts.username or parts.password:
        raise PrinterError(f"{where} 的 url 里不要放账号密码，密钥请用 api_key 字段")
    printer_id = str(raw.get("id") or f"printer-{index + 1}").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", printer_id):
        raise PrinterError(f"{where} 的 id 只能包含字母、数字、下划线和短横线")
    label = str(raw.get("label") or printer_id).strip() or printer_id
    api_key = str(raw.get("api_key") or "").strip()
    if kind == "octoprint" and not api_key:
        raise PrinterError(f"{where} 是 OctoPrint，需要 api_key")
    return PrinterConfig(
        id=printer_id,
        label=label[:40],
        kind=kind,
        base_url=url,
        api_key=api_key,
        bed_mm=_parse_bed(raw.get("bed_mm"), where=where),
    )


def load_printers(environ: dict[str, str] | None = None) -> list[PrinterConfig]:
    """Read the printer list from the environment.

    ``TOUCHLEARN_PRINTERS`` holds a JSON array (or ``{"printers": [...]}``).
    A single printer may instead be described by ``TOUCHLEARN_PRINTER_URL``,
    ``TOUCHLEARN_PRINTER_KEY`` and friends, which keeps the docker-compose
    file readable for the common case.
    """
    env = os.environ if environ is None else environ
    raw = str(env.get("TOUCHLEARN_PRINTERS") or "").strip()
    configs: list[PrinterConfig] = []
    if raw:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as error:
            raise PrinterError(f"TOUCHLEARN_PRINTERS 不是合法 JSON：{error.msg}") from error
        items = payload.get("printers") if isinstance(payload, dict) else payload
        if not isinstance(items, list):
            raise PrinterError("TOUCHLEARN_PRINTERS 需要是数组，或带有 printers 数组的对象")
        configs = [_parse_one(item, index) for index, item in enumerate(items)]
    elif str(env.get("TOUCHLEARN_PRINTER_URL") or "").strip():
        configs = [
            _parse_one(
                {
                    "id": env.get("TOUCHLEARN_PRINTER_ID") or "default",
                    "label": env.get("TOUCHLEARN_PRINTER_LABEL") or "",
                    "kind": env.get("TOUCHLEARN_PRINTER_KIND") or "octoprint",
                    "url": env.get("TOUCHLEARN_PRINTER_URL"),
                    "api_key": env.get("TOUCHLEARN_PRINTER_KEY") or "",
                    "bed_mm": env.get("TOUCHLEARN_PRINTER_BED") or "",
                },
                0,
            )
        ]
    seen: set[str] = set()
    for config in configs:
        if config.id in seen:
            raise PrinterError(f"打印机 id 重复：{config.id}")
        seen.add(config.id)
    return configs


def find_printer(configs: Iterable[PrinterConfig], printer_id: str) -> PrinterConfig:
    for config in configs:
        if config.id == printer_id:
            return config
    raise PrinterError(f"没有找到打印机：{printer_id}", status=404, code="printer_not_found")


def describe_payload(configs: list[PrinterConfig], *, allow: bool) -> dict[str, Any]:
    return {
        "ok": True,
        "allow_print": allow,
        "mode": "print" if allow else "rehearsal",
        "printers": [config.public() for config in configs],
        "supported_kinds": list(SUPPORTED_KINDS),
        "max_bytes": MAX_STL_BYTES,
        "max_triangles": MAX_TRIANGLES,
        "hint": (
            "服务器已允许真实打印，发送前仍需要用户在界面里确认。"
            if allow
            else "当前为演练模式：只做尺寸与格式检查，不会连接打印机。"
        ),
    }


def inspect_stl(path: Path) -> dict[str, Any]:
    """Triangle count, byte size and bounding box of a binary STL."""
    try:
        info = read_stl_info(path)
        size = path.stat().st_size
        aabb = _binary_stl_bounds(path, int(info["triangles"]))
    except (OSError, struct.error, ValueError) as error:
        raise PrinterError(f"只支持完整的二进制 STL：{error}", code="bad_stl") from error
    return {
        "name": path.name,
        "bytes": size,
        "triangles": int(info["triangles"]),
        "size_mm": [round(value, 3) for value in aabb],
        "sha256": sha256_file(path),
    }


def _binary_stl_bounds(path: Path, triangles: int) -> tuple[float, float, float]:
    if triangles <= 0:
        raise ValueError("STL 没有三角面")
    minimum = [float("inf")] * 3
    maximum = [float("-inf")] * 3
    remaining = triangles
    with path.open("rb") as handle:
        handle.read(84)
        while remaining > 0:
            block = handle.read(min(_CHUNK, remaining * 50))
            if len(block) < 50:
                raise ValueError("STL 在三角面中途结束")
            usable = len(block) - (len(block) % 50)
            for record in struct.iter_unpack("<12fH", block[:usable]):
                # record[0:3] is the normal; vertices start at index 3.
                for offset in (3, 6, 9):
                    for axis in range(3):
                        value = record[offset + axis]
                        if value < minimum[axis]:
                            minimum[axis] = value
                        if value > maximum[axis]:
                            maximum[axis] = value
            remaining -= usable // 50
    return (
        maximum[0] - minimum[0],
        maximum[1] - minimum[1],
        maximum[2] - minimum[2],
    )


def human_bytes(count: int | float) -> str:
    value = float(count)
    if value < 1024:
        return f"{value:.0f} B"
    if value < 1024 * 1024:
        return f"{value / 1024:.0f} KB"
    return f"{value / (1024 * 1024):.1f} MB"


def _check(checks: list[dict[str, Any]], check_id: str, label: str, ok: bool, detail: str) -> None:
    checks.append({"id": check_id, "label": label, "ok": bool(ok), "detail": detail})


def plan_print(
    job_dir: Path,
    printer: PrinterConfig,
    *,
    filename: str = "model.stl",
    max_bytes: int = MAX_STL_BYTES,
    max_triangles: int = MAX_TRIANGLES,
) -> dict[str, Any]:
    """Deterministic pre-flight: everything a real print would need to know."""
    if filename not in ALLOWED_FILES:
        raise PrinterError("只允许发送 model.stl 或 plate_model.stl")
    path = job_dir / filename
    checks: list[dict[str, Any]] = []
    if not path.is_file():
        raise PrinterError(f"作业里没有 {filename}，请先生成模型", status=409, code="missing_stl")

    info = inspect_stl(path)
    size_mm = info["size_mm"]
    bed = printer.bed_mm
    usable = [bed[0] - 2 * BED_MARGIN_MM, bed[1] - 2 * BED_MARGIN_MM, bed[2]]
    fits = size_mm[0] <= usable[0] and size_mm[1] <= usable[1] and size_mm[2] <= usable[2]
    rotate = False
    if not fits:
        rotate = size_mm[1] <= usable[0] and size_mm[0] <= usable[1] and size_mm[2] <= usable[2]

    _check(checks, "format", "文件格式", True, "二进制 STL，三角面数量与文件长度一致")
    _check(
        checks,
        "size",
        "文件体积",
        info["bytes"] <= max_bytes,
        f"{human_bytes(info['bytes'])}，上限 {human_bytes(max_bytes)}",
    )
    _check(
        checks,
        "triangles",
        "三角面数量",
        info["triangles"] <= max_triangles,
        f"{info['triangles']:,} 面，上限 {max_triangles:,} 面",
    )
    fit_detail = (
        f"{size_mm[0]:.1f} × {size_mm[1]:.1f} × {size_mm[2]:.1f} mm；"
        f"打印床 {bed[0]:.0f} × {bed[1]:.0f} × {bed[2]:.0f} mm，四周留边 {BED_MARGIN_MM:.0f} mm"
    )
    if rotate:
        fit_detail += "；需要先在切片软件里旋转 90°"
    _check(checks, "bed_fit", "打印床尺寸", fits or rotate, fit_detail)

    compliance_path = job_dir / "compliance.json"
    if compliance_path.is_file():
        try:
            report = json.loads(compliance_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            report = {}
        printable = report.get("printable") if isinstance(report, dict) else None
        if printable is not None:
            _check(
                checks,
                "compliance",
                "合规结论",
                bool(printable),
                "合规报告允许打印" if printable else "合规报告存在阻断打印的 FAIL，请先修复几何",
            )

    return {
        "ok": all(item["ok"] for item in checks),
        "checks": checks,
        "file": info,
        "path": path,
        "rotate_deg": 90 if rotate else 0,
    }


def _multipart_parts(
    fields: list[tuple[str, str]],
    file_field: str,
    filename: str,
    path: Path,
    boundary: str,
) -> list[bytes | Path]:
    parts: list[bytes | Path] = []
    for key, value in fields:
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n".encode("utf-8")
        )
    parts.append(
        (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; "
            f"filename=\"{filename}\"\r\nContent-Type: model/stl\r\n\r\n"
        ).encode("utf-8")
    )
    parts.append(path)
    parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    return parts


def _parts_length(parts: Iterable[bytes | Path]) -> int:
    total = 0
    for part in parts:
        total += part.stat().st_size if isinstance(part, Path) else len(part)
    return total


def _send_parts(
    printer: PrinterConfig,
    path: str,
    parts: list[bytes | Path],
    *,
    content_type: str,
    timeout: float,
) -> dict[str, Any]:
    parsed = urlsplit(printer.base_url)
    host = parsed.hostname or ""
    port = parsed.port
    target = parsed.path.rstrip("/") + path
    connection_cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    connection = connection_cls(host, port, timeout=timeout)
    try:
        connection.putrequest("POST", target)
        connection.putheader("Content-Type", content_type)
        connection.putheader("Content-Length", str(_parts_length(parts)))
        connection.putheader("User-Agent", "TouchLearn-printer/1.0")
        if printer.api_key:
            # Header only.  Keys are never written to job.json, logs or errors.
            connection.putheader("X-Api-Key", printer.api_key)
        connection.endheaders()
        for part in parts:
            if isinstance(part, Path):
                with part.open("rb") as handle:
                    while True:
                        chunk = handle.read(_CHUNK)
                        if not chunk:
                            break
                        connection.send(chunk)
            else:
                connection.send(part)
        response = connection.getresponse()
        raw = response.read(64 * 1024)
        body: Any
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            body = raw.decode("utf-8", errors="replace")[:300]
        if response.status >= 400:
            raise PrinterError(
                f"打印机返回 {response.status}：{_brief(body)}",
                status=502,
                code="printer_rejected",
            )
        return {"status": response.status, "body": body}
    except (OSError, http.client.HTTPException) as error:
        raise PrinterError(f"连接打印机失败：{error}", status=502, code="printer_unreachable") from error
    finally:
        connection.close()


def _brief(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:300] or "（无内容）"


def _octoprint_start(printer: PrinterConfig, plan: dict[str, Any], timeout: float) -> dict[str, Any]:
    boundary = f"----touchlearn{uuid.uuid4().hex}"
    parts = _multipart_parts(
        [("select", "true"), ("print", "true")],
        "file",
        plan["file"]["name"],
        plan["path"],
        boundary,
    )
    result = _send_parts(
        printer,
        "/api/files/local",
        parts,
        content_type=f"multipart/form-data; boundary={boundary}",
        timeout=timeout,
    )
    return {
        "kind": "octoprint",
        **result,
        "started": True,
        "note": "已请求 OctoPrint 立即打印；若服务器没有配置切片配置，文件会进入队列等待手动开始。",
    }


def _moonraker_start(printer: PrinterConfig, plan: dict[str, Any], timeout: float) -> dict[str, Any]:
    boundary = f"----touchlearn{uuid.uuid4().hex}"
    parts = _multipart_parts(
        [("root", "gcodes")],
        "file",
        plan["file"]["name"],
        plan["path"],
        boundary,
    )
    upload = _send_parts(
        printer,
        "/server/files/upload",
        parts,
        content_type=f"multipart/form-data; boundary={boundary}",
        timeout=timeout,
    )
    start = _send_parts(
        printer,
        f"/printer/print/start?filename={quote(plan['file']['name'])}",
        [],
        content_type="application/octet-stream",
        timeout=timeout,
    )
    return {
        "kind": "moonraker",
        "upload": upload,
        "start": start,
        "started": True,
        "note": "已上传 STL 并请求开始；Klipper 只执行 G-code，未接入切片器时这个文件不会真正开打，本条链路也尚未用真机验证。",
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def run_print(
    job_dir: Path,
    printer: PrinterConfig,
    *,
    filename: str = "model.stl",
    dry_run: bool,
    timeout: float = 120.0,
) -> dict[str, Any]:
    """Plan, then either rehearse or transmit.  Always returns a record."""
    plan = plan_print(job_dir, printer, filename=filename)
    started_at = _now()
    record: dict[str, Any] = {
        "ok": False,
        "state": "blocked",
        "printer": printer.public(),
        "file": plan["file"],
        "checks": plan["checks"],
        "rotate_deg": plan["rotate_deg"],
        "dry_run": bool(dry_run),
        "started_at": started_at,
        "finished_at": None,
        "transport": None,
        "message": "",
    }
    if not plan["ok"]:
        failed = [item["label"] for item in plan["checks"] if not item["ok"]]
        record["message"] = "检查未通过：" + "、".join(failed)
        _write_record(job_dir, record)
        return record

    if dry_run:
        # No socket is opened here on purpose.  The size is what a real
        # multipart upload would have carried, which makes the rehearsal
        # useful for judging upload time on a slow Wi-Fi link.
        boundary = "----touchlearn" + "0" * 32
        fields = [("select", "true"), ("print", "true")] if printer.kind == "octoprint" else [("root", "gcodes")]
        probe = _multipart_parts(fields, "file", plan["file"]["name"], plan["path"], boundary)
        record.update(
            {
                "ok": True,
                "state": "rehearsed",
                "dry_run": True,
                "finished_at": _now(),
                "message": (
                    f"演练通过：将向 {printer.label} 上传 "
                    f"{human_bytes(plan['file']['bytes'])}，"
                    "本次没有连接打印机。"
                ),
                "transport": {
                    "kind": printer.kind,
                    "planned_bytes": _parts_length(probe),
                    "sent": False,
                },
            }
        )
        _write_record(job_dir, record)
        return record

    try:
        if printer.kind == "octoprint":
            transport = _octoprint_start(printer, plan, timeout)
        else:
            transport = _moonraker_start(printer, plan, timeout)
    except PrinterError as error:
        record.update(
            {
                "ok": False,
                "state": "failed",
                "finished_at": _now(),
                "message": error.message,
                "error_code": error.code,
            }
        )
        _write_record(job_dir, record)
        return record
    record.update(
        {
            "ok": True,
            "state": "sent",
            "finished_at": _now(),
            "transport": transport,
            "message": transport.get("note", "已发送到打印机。"),
        }
    )
    _write_record(job_dir, record)
    return record


def _write_record(job_dir: Path, record: dict[str, Any]) -> None:
    try:
        job_dir.mkdir(parents=True, exist_ok=True)
        target = job_dir / "print.json"
        target.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        _RECORDS[str(job_dir)] = record
    except OSError:
        # A read-only job directory must not turn a successful upload into an
        # error; the in-memory copy still answers the status endpoint.
        _RECORDS[str(job_dir)] = record


def read_record(job_dir: Path) -> dict[str, Any] | None:
    cached = _RECORDS.get(str(job_dir))
    if cached is not None:
        return cached
    target = job_dir / "print.json"
    if not target.is_file():
        return None
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def acquire_job_lock(job_id: str) -> threading.Lock | None:
    with _LOCKS_GUARD:
        lock = _LOCKS.get(job_id)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[job_id] = lock
    if not lock.acquire(blocking=False):
        return None
    return lock


def release_job_lock(job_id: str, lock: threading.Lock) -> None:
    try:
        lock.release()
    except RuntimeError:
        pass
    with _LOCKS_GUARD:
        if _LOCKS.get(job_id) is lock:
            _LOCKS.pop(job_id, None)

