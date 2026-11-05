"""Generative 3D through a specialised text-to-3D / image-to-3D service.

The language model never invents geometry here.  It writes a small, validated
*generation plan*: what the object is, which features matter, how tall the
print should be, which base to use and which short Chinese labels go into the
braille title and base legend.  A specialised provider (Tencent TokenHub's
HY-3D / Tripo models, Tripo 3D or Meshy) produces a textured GLB; this module
downloads it, parses the geometry with the standard library and hands the
triangles to the deterministic print composer.

The division of labour is deliberate:

* the provider owns likeness, surface detail and realistic proportions;
* ``reconstruct_print`` owns the title, base, support plinth, orientation,
  scale, Chinese braille, QR pad and printability checks.

Two honest limits are repeated in the UI and the report:

* the textured GLB keeps the generated colours, but a single-material FDM
  print does not;
* the provider mesh is not measurement data.  Height is scaled to the user's
  target, and open edges are reported for repair rather than silently declared
  compliant.

No key is hard-coded.  ``TOUCHLEARN_3D_KEY`` (or the provider-specific variant)
is read from the environment or a local ``.env`` and never returned by the
status endpoint.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import struct
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from ..geometry import Mesh
from .llm import chat_json, load_env

Progress = Callable[[int, str], None]

DEFAULT_TIMEOUT_S = 600.0
DEFAULT_POLL_S = 4.0
MAX_IMAGE_BYTES = 12 * 1024 * 1024
# TokenHub's image_base64 field accepts at most 6 MB of image bytes.
MAX_TOKENHUB_IMAGE_BYTES = 6 * 1024 * 1024
# A 1.5M-triangle GLB with normals and 4K PBR textures can exceed 220 MB, so
# leave headroom for the high-quality text-to-3D route while still bounding a
# runaway download.
MAX_DOWNLOAD_BYTES = 320 * 1024 * 1024
# Tripo 3.1 advertises up to 2M faces.  The "high" preset stays at 1.5M for a
# 2 GiB deployment; TOUCHLEARN_3D_FACES can raise it to 2M when memory allows.
MAX_TRIANGLES = 2_000_000
MAX_IMAGES = 4
# Tripo-3D-P1 is the model whose image and multi-view request formats are
# documented on TokenHub, so it is the safest default for photo reconstruction.
DEFAULT_TOKENHUB_MODEL = "tripo-3d-p1"
# Tripo 3.1 is the high-fidelity route on TokenHub: detailed geometry,
# detailed textures and a face budget measured in millions.  It is used for
# text-to-3D by default; image-to-3D uses Tripo-3D-P1 above.
DEFAULT_TOKENHUB_TEXT_MODEL = "tripo-3d-3.1"
# Tripo-3D-P1 accepts front/left/back/right photos in one request.  The slot
# names are part of the documented request body, not an internal convention.
MULTIVIEW_ORDER = ("front", "left", "back", "right")
_MULTIVIEW_HINTS = {
    "front": ("front", "正面", "前面", "前视", "前"),
    "left": ("left", "左侧", "左边", "左视", "左"),
    "back": ("back", "rear", "背面", "后面", "后视", "后"),
    "right": ("right", "右侧", "右边", "右视", "右"),
}
PROVIDER_LABELS = {"tripo": "Tripo 3D", "meshy": "Meshy", "tokenhub": "腾讯云 TokenHub"}

# Quality presets.  "high" is the default for text-to-3D because the whole
# point of that route is a refined mesh rather than a primitive assembly.
QUALITY_PRESETS = {
    "draft": {"hy3d_faces": 300_000, "tripo_faces": 200_000, "tripo_texture": "standard"},
    "standard": {"hy3d_faces": 500_000, "tripo_faces": 500_000, "tripo_texture": "detailed"},
    "high": {"hy3d_faces": 1_000_000, "tripo_faces": 1_500_000, "tripo_texture": "detailed"},
}
TEXT_MODEL_SHORTLIST = (
    "tripo-3d-3.1",
    "hy-3d-3.1",
    "hy-3d-3.0",
    "tripo-3d-p1",
    "tripo-3d-p2",
    "hy-3d-express",
)

TEXTURE_SUFFIXES = {".glb", ".gltf", ".png", ".jpg", ".jpeg", ".webp", ".bin", ".mtl", ".obj"}


class ReconstructionError(ValueError):
    """A reconstruction request that cannot be completed as asked."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def _env(name: str) -> str:
    load_env()
    return os.environ.get(name, "").strip()


def _timeout_s() -> float:
    raw = _env("TOUCHLEARN_3D_TIMEOUT")
    try:
        value = float(raw) if raw else DEFAULT_TIMEOUT_S
    except ValueError:
        value = DEFAULT_TIMEOUT_S
    return max(30.0, min(1800.0, value))


def _poll_s() -> float:
    raw = _env("TOUCHLEARN_3D_POLL")
    try:
        value = float(raw) if raw else DEFAULT_POLL_S
    except ValueError:
        value = DEFAULT_POLL_S
    return max(1.0, min(30.0, value))


def provider_name() -> str:
    raw = _env("TOUCHLEARN_3D_PROVIDER").lower()
    if raw in {"tripo", "tripo3d", "tripo-3d", "tripo_3d"}:
        return "tripo"
    if raw in {"meshy", "meshy3d", "meshy-3d"}:
        return "meshy"
    if raw in {
        "tokenhub",
        "tokenhub3d",
        "tokenhub-3d",
        "tencent",
        "tencent-tokenhub",
        "hunyuan3d",
        "hunyuan-3d",
        "hy-3d",
        "hy3d",
    } or raw.startswith(("hy-3d-", "hunyuan-")):
        return "tokenhub"
    return "tripo"


def provider_key(provider: str | None = None) -> str:
    name = (provider or provider_name()).upper()
    value = _env(f"TOUCHLEARN_3D_{name}_KEY") or _env("TOUCHLEARN_3D_KEY")
    if not value and name == "TOKENHUB":
        # TokenHub is one gateway for chat and 3D, so a single key is enough.
        value = _env("TOUCHLEARN_LLM_KEY")
    return value


def provider_model(provider: str | None = None) -> str:
    name = (provider or provider_name()).upper()
    value = _env(f"TOUCHLEARN_3D_{name}_MODEL") or _env("TOUCHLEARN_3D_MODEL")
    if value:
        return value
    return DEFAULT_TOKENHUB_MODEL if (provider or provider_name()) == "tokenhub" else ""


def text_model(provider: str | None = None) -> str:
    """Model used for prompt-only generation.

    ``TOUCHLEARN_3D_TEXT_MODEL`` wins, then the general 3D model setting, then
    the provider default.  A separate switch lets a deployment keep the
    cheaper image model while asking text requests for the high-fidelity one.
    """
    name = provider or provider_name()
    raw = _env("TOUCHLEARN_3D_TEXT_MODEL") or _env("TOUCHLEARN_3D_MODEL")
    if raw:
        return raw
    if name == "tokenhub":
        return DEFAULT_TOKENHUB_TEXT_MODEL
    return provider_model(name)


def quality_name() -> str:
    raw = _env("TOUCHLEARN_3D_QUALITY").lower()
    return raw if raw in QUALITY_PRESETS else "high"


def quality_preset() -> dict[str, Any]:
    return QUALITY_PRESETS[quality_name()]


def generative_enabled() -> bool:
    """Allow a deployment to force the deterministic compiler offline."""
    raw = _env("TOUCHLEARN_3D_GENERATIVE").lower()
    return raw not in {"0", "false", "off", "no"}


def _bounded_faces(value: int) -> int:
    return max(3_000, min(MAX_TRIANGLES, int(value)))


def _face_count(default: int = 300_000) -> int:
    """Image-to-3D face budget; ``TOUCHLEARN_3D_FACES`` overrides the default."""
    raw = _env("TOUCHLEARN_3D_FACES")
    try:
        value = int(float(raw)) if raw else int(default)
    except (TypeError, ValueError):
        value = int(default)
    return _bounded_faces(value)


def reconstruction_status() -> dict[str, Any]:
    """Describe the configured provider without ever exposing the key."""
    provider = provider_name()
    key = provider_key(provider)
    model = provider_model(provider)
    text = text_model(provider)
    preset = quality_preset()
    return {
        "configured": bool(key),
        "kind": "reconstruction",
        "provider": provider,
        "label": PROVIDER_LABELS.get(provider, provider),
        "model": model,
        "image_model": model,
        "text_model": text,
        "multiview": provider == "tokenhub" and model.lower().startswith("tripo"),
        "text_to_3d": bool(key) and generative_enabled(),
        "generative": bool(key) and generative_enabled(),
        "quality": quality_name(),
        "face_budget": preset["tripo_faces"],
        "model_shortlist": list(TEXT_MODEL_SHORTLIST) if provider == "tokenhub" else [],
        "has_key": bool(key),
        "mode": "remote-3d" if key else "offline",
        "timeout_s": _timeout_s(),
        "poll_s": _poll_s(),
        "message": (
            "三维生成接口已配置：文字直接生成高质量模型，照片走图生 3D（Tripo P1 支持多视图），再由本地统一加标题、底座和方向适配。"
            if key
            else "未配置三维生成接口。设置 TOUCHLEARN_3D_KEY（TokenHub 可直接复用 "
            "TOUCHLEARN_LLM_KEY）后启用生成式建模；其余功能仍可使用本地触觉教材与确定性 3D 模型。"
        ),
        "environment": [
            "TOUCHLEARN_3D_PROVIDER",
            "TOUCHLEARN_3D_KEY",
            "TOUCHLEARN_3D_MODEL",
            "TOUCHLEARN_3D_TEXT_MODEL",
            "TOUCHLEARN_3D_QUALITY",
            "TOUCHLEARN_3D_FACES",
            "TOUCHLEARN_3D_GENERATIVE",
            "TOUCHLEARN_3D_TIMEOUT",
            "TOUCHLEARN_3D_POLL",
            "TOUCHLEARN_3D_PROMPT",
            "TOUCHLEARN_3D_PROXY",
        ],
        "privacy": "密钥只从环境变量或本机 .env 读取，不写入作业目录，也不会通过 API 返回。",
    }


# ---------------------------------------------------------------------------
# Reconstruction plan
# ---------------------------------------------------------------------------


@dataclass
class ReconstructionPlan:
    ok: bool
    title: str = "模型"
    subject: str = ""
    keep_features: list[str] = field(default_factory=list)
    changes: list[str] = field(default_factory=list)
    height_mm: float = 120.0
    base: str = "disc"
    margin_mm: float = 10.0
    labels: list[dict[str, str]] = field(default_factory=list)
    print_notes: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    confidence: float = 0.5
    provider: str = ""
    model: str = ""
    message: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def provider_prompt(self) -> str:
        parts: list[str] = []
        if self.subject:
            parts.append(self.subject)
        if self.keep_features:
            parts.append("保留：" + "、".join(self.keep_features))
        if self.changes:
            parts.append("调整：" + "、".join(self.changes))
        return "；".join(parts)[:600]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "title": self.title,
            "subject": self.subject,
            "keep_features": list(self.keep_features),
            "changes": list(self.changes),
            "height_mm": self.height_mm,
            "base": self.base,
            "margin_mm": self.margin_mm,
            "labels": [dict(item) for item in self.labels],
            "print_notes": list(self.print_notes),
            "notes": list(self.notes),
            "questions": list(self.questions),
            "confidence": self.confidence,
            "provider": self.provider,
            "model": self.model,
            "message": self.message,
        }


PLAN_SYSTEM = (
    "你是触觉照片级 3D 重建的规划助手。你看用户上传的照片和文字要求，只输出一个 JSON 对象，"
    "不要 Markdown，不要解释。你的任务不是直接生成网格，而是写清楚：要重建什么、必须保留哪些特征、"
    "用户要求改什么、打印多高、底座形状、以及底座表面和盲文图例上要刻哪些 1-3 个汉字的中文盲文标签。"
    "不要编造照片里看不到的结构，不要把不确定的尺寸写成事实。"
)

PLAN_SCHEMA = {
    "title": "1-6 个汉字，用于盲文标题",
    "subject": "物体是什么，一句话",
    "keep_features": ["照片里必须保留的特征，最多 6 条"],
    "changes": ["用户要求相对照片做出的调整，最多 6 条；没有就留空数组"],
    "height_mm": "打印高度，60-200 之间的数字",
    "base": "disc 或 rounded_rect",
    "margin_mm": "物体与底座边缘的留白，4-20 之间的数字",
    "labels": [{"text": "1-3 个汉字", "target": "指向的部位或特征"}],
    "print_notes": ["打印时需要注意的一句话，可选"],
}


def _fallback_title(text: str) -> str:
    chars = [ch for ch in (text or "") if "\u4e00" <= ch <= "\u9fff"]
    return "".join(chars[:4]) or "模型"


def _as_list(value: Any, limit: int = 8) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text[:80])
        if len(result) >= limit:
            break
    return result


def _as_float(value: Any, default: float, low: float, high: float) -> float:
    if isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number or number in {float("inf"), float("-inf")}:
        return default
    return max(low, min(high, number))


def _normalize_labels(raw: Any, title: str) -> list[dict[str, str]]:
    labels: list[dict[str, str]] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                text = str(item.get("text") or "").strip()
                target = str(item.get("target") or "").strip()
            else:
                text = str(item or "").strip()
                target = ""
            text = "".join(ch for ch in text if "\u4e00" <= ch <= "\u9fff" or ch.isalnum())[:3]
            if not text:
                continue
            if any(existing["text"] == text for existing in labels):
                continue
            labels.append({"text": text, "target": target[:24]})
            if len(labels) >= 3:
                break
    if not labels:
        labels = [{"text": title[:4] or "模型", "target": "主体"}]
    return labels


def _normalize_plan(data: dict[str, Any], text: str, *, provider: str, model: str) -> ReconstructionPlan:
    title = "".join(ch for ch in str(data.get("title") or _fallback_title(text)) if "\u4e00" <= ch <= "\u9fff" or ch.isalnum())
    title = title[:6] or _fallback_title(text)
    base = str(data.get("base") or "disc").strip().lower()
    if base not in {"disc", "rounded_rect", "rounded-rect", "rect"}:
        base = "disc"
    if base in {"rounded-rect", "rect"}:
        base = "rounded_rect"
    labels = _normalize_labels(data.get("labels"), title)
    subject = str(data.get("subject") or "").strip()[:160]
    plan = ReconstructionPlan(
        ok=True,
        title=title,
        subject=subject,
        keep_features=_as_list(data.get("keep_features"), 6),
        changes=_as_list(data.get("changes"), 6),
        height_mm=_as_float(data.get("height_mm"), 120.0, 60.0, 200.0),
        base=base,
        margin_mm=_as_float(data.get("margin_mm"), 10.0, 4.0, 20.0),
        labels=labels,
        print_notes=_as_list(data.get("print_notes"), 4),
        confidence=0.78,
        provider=provider,
        model=model,
        message="已按照片与指令生成重建计划。",
        raw=data,
    )
    plan.height_mm = round(plan.height_mm, 1)
    plan.margin_mm = round(plan.margin_mm, 1)
    return plan


def _fallback_plan(text: str, message: str, *, provider: str = "", model: str = "") -> ReconstructionPlan:
    title = _fallback_title(text)
    return ReconstructionPlan(
        ok=True,
        title=title,
        subject=(text or "用户上传的照片").strip()[:160],
        keep_features=[],
        changes=[],
        height_mm=120.0,
        base="disc",
        margin_mm=10.0,
        labels=[{"text": title, "target": "主体"}],
        print_notes=[],
        notes=[message],
        questions=["请确认重建对象的名称（1-4 个汉字），盲文标题和底座图例会使用它。"],
        confidence=0.3,
        provider=provider,
        model=model,
        message=message,
    )


def plan_reconstruction(
    text: str,
    images: Sequence[str | Path] = (),
    *,
    model: str | None = None,
    effort: str | None = None,
) -> ReconstructionPlan:
    """Ask the vision/text model for a strict reconstruction plan.

    A failed model call never blocks the reconstruction: the provider can still
    work from the photos, so the plan falls back to safe defaults and says so.
    """
    prompt = (
        "用户的文字要求：\n"
        + ((text or "").strip() or "（没有额外文字，只按照片重建）")
        + "\n\n请只输出符合下面结构的 JSON：\n"
        + json.dumps(PLAN_SCHEMA, ensure_ascii=False, indent=2)
    )
    result = chat_json(
        PLAN_SYSTEM,
        prompt,
        images=list(images)[:MAX_IMAGES],
        kind="vision" if images else "text",
        temperature=0.0,
        effort=effort,
        model=model,
    )
    if not result.ok or not isinstance(result.data, dict):
        return _fallback_plan(
            text,
            f"没有读到可用的重建计划，已使用安全默认值：{result.message}",
            provider=result.provider,
            model=result.model,
        )
    return _normalize_plan(result.data, text, provider=result.provider, model=result.model)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _opener() -> urllib.request.OpenerDirector:
    """Ignore stale system proxies unless one is configured explicitly."""
    proxy = _env("TOUCHLEARN_3D_PROXY") or _env("TOUCHLEARN_LLM_PROXY")
    if proxy:
        handler: urllib.request.BaseHandler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
    else:
        handler = urllib.request.ProxyHandler({})
    return urllib.request.build_opener(handler)


def _error_detail(body: bytes) -> str:
    text = body.decode("utf-8", errors="replace").strip()
    if not text:
        return "empty response"
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return text[:400]
    if isinstance(payload, dict):
        for key in ("message", "error", "detail"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value[:400]
            if isinstance(value, dict):
                nested = value.get("message")
                if isinstance(nested, str):
                    return nested[:400]
    return text[:400]


def _read_json(response: Any, limit: int = 4 * 1024 * 1024) -> Any:
    raw = response.read(limit + 1)
    if len(raw) > limit:
        raise ReconstructionError("三维服务返回的数据过大。")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReconstructionError("三维服务返回的不是 JSON。") from error


def _request_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    payload: Any | None = None,
    timeout: float,
) -> Any:
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _opener().open(request, timeout=timeout) as response:
            return _read_json(response)
    except urllib.error.HTTPError as error:
        raise ReconstructionError(f"三维服务返回 HTTP {error.code}：{_error_detail(error.read())}") from error
    except urllib.error.URLError as error:
        raise ReconstructionError(f"无法连接三维服务：{error.reason}") from error
    except (TimeoutError, OSError) as error:
        raise ReconstructionError(f"三维服务调用失败：{error}") from error


def _multipart_upload(url: str, path: Path, *, headers: dict[str, str], timeout: float) -> Any:
    boundary = f"----touchlearn{uuid.uuid4().hex}"
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    body = bytearray()
    body.extend(f"--{boundary}\r\n".encode("ascii"))
    body.extend(
        f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'.encode("utf-8")
    )
    body.extend(f"Content-Type: {mime}\r\n\r\n".encode("ascii"))
    body.extend(path.read_bytes())
    body.extend(f"\r\n--{boundary}--\r\n".encode("ascii"))
    request = urllib.request.Request(
        url,
        data=bytes(body),
        headers={**headers, "Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with _opener().open(request, timeout=timeout) as response:
            return _read_json(response)
    except urllib.error.HTTPError as error:
        raise ReconstructionError(f"上传照片失败 HTTP {error.code}：{_error_detail(error.read())}") from error
    except urllib.error.URLError as error:
        raise ReconstructionError(f"无法连接上传接口：{error.reason}") from error
    except (TimeoutError, OSError) as error:
        raise ReconstructionError(f"上传照片失败：{error}") from error


def _download(url: str, dest: Path, *, timeout: float, limit: int = MAX_DOWNLOAD_BYTES) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": "TouchLearn/1.0"})
    try:
        with _opener().open(request, timeout=timeout) as response:
            total = 0
            with dest.open("wb") as handle:
                while True:
                    chunk = response.read(1024 * 256)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > limit:
                        raise ReconstructionError("三维模型文件超过下载上限。")
                    handle.write(chunk)
    except urllib.error.HTTPError as error:
        raise ReconstructionError(f"下载模型失败 HTTP {error.code}。") from error
    except urllib.error.URLError as error:
        raise ReconstructionError(f"无法下载模型：{error.reason}") from error
    if dest.stat().st_size == 0:
        raise ReconstructionError("下载到的模型文件为空。")
    return dest


def _tripo_data(payload: Any) -> Any:
    if not isinstance(payload, dict):
        return payload
    code = payload.get("code")
    if code not in (None, 0, "0"):
        raise ReconstructionError(str(payload.get("message") or payload.get("msg") or f"Tripo 错误 {code}"))
    return payload.get("data", payload)


def _poll(
    url: str,
    *,
    headers: dict[str, str],
    timeout_s: float,
    interval: float,
    progress: Progress | None,
    unwrap: Callable[[Any], Any],
    start: int = 10,
    end: int = 70,
) -> Any:
    started = time.monotonic()
    last_progress = -1
    while True:
        payload = unwrap(_request_json("GET", url, headers=headers, timeout=min(60.0, timeout_s)))
        if isinstance(payload, dict):
            status = str(payload.get("status") or payload.get("state") or "").lower()
            value = payload.get("progress")
            try:
                percent = int(float(value)) if value is not None else None
            except (TypeError, ValueError):
                percent = None
            if percent is not None and percent != last_progress and progress:
                last_progress = percent
                progress(start + int((end - start) * max(0, min(100, percent)) / 100.0), f"三维重建中 {percent}%")
            if status in {"success", "succeeded", "completed", "done"}:
                return payload
            if status in {"failed", "error", "cancelled", "canceled", "banned"}:
                detail = payload.get("error") or payload.get("message") or payload.get("task_error") or status
                raise ReconstructionError(f"三维重建失败：{detail}")
        if time.monotonic() - started > timeout_s:
            raise ReconstructionError(f"三维重建超时（{int(timeout_s)} 秒）。")
        time.sleep(interval)


# ---------------------------------------------------------------------------
# Tripo adapter
# ---------------------------------------------------------------------------


def _tripo_base() -> str:
    return (_env("TOUCHLEARN_3D_URL") or "https://api.tripo3d.ai/v2/openapi").rstrip("/")


def _tripo_headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}", "Accept": "application/json", "Content-Type": "application/json"}


def _image_mime(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or ""
    if mime not in {"image/png", "image/jpeg", "image/webp"}:
        raise ReconstructionError(f"三维重建只支持 PNG/JPEG/WEBP 照片：{path.name}")
    return "jpg" if mime == "image/jpeg" else mime.split("/")[-1]


def _tripo_upload(path: Path, key: str, timeout: float) -> str:
    if path.stat().st_size > MAX_IMAGE_BYTES:
        raise ReconstructionError(f"照片超过 {MAX_IMAGE_BYTES // (1024 * 1024)} MB：{path.name}")
    payload = _multipart_upload(
        f"{_tripo_base()}/upload",
        path,
        headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
        timeout=min(120.0, timeout),
    )
    data = _tripo_data(payload)
    if not isinstance(data, dict):
        raise ReconstructionError("照片上传接口没有返回文件标识。")
    token = str(data.get("image_token") or data.get("file_token") or data.get("token") or "")
    if not token:
        raise ReconstructionError("照片上传接口没有返回文件标识。")
    return token


def _tripo_create_task(tokens: Sequence[tuple[str, str]], key: str, plan: ReconstructionPlan, timeout: float) -> str:
    base = _tripo_base()
    model = provider_model("tripo")
    prompt_enabled = _env("TOUCHLEARN_3D_PROMPT").lower() not in {"0", "false", "off", "no"}
    if len(tokens) == 1:
        mime, token = tokens[0]
        body: dict[str, Any] = {"type": "image_to_model", "file": {"type": mime, "file_id": token}}
        if model:
            body["model_version"] = model
        if prompt_enabled and plan.provider_prompt:
            body["prompt"] = plan.provider_prompt
    else:
        body = {
            "type": "multiview_to_model",
            "files": [{"type": mime, "file_id": token} for mime, token in tokens[:4]],
        }
        if model:
            body["model_version"] = model
    payload = _request_json(
        "POST",
        f"{base}/task",
        headers=_tripo_headers(key),
        payload=body,
        timeout=min(90.0, timeout),
    )
    data = _tripo_data(payload)
    task_id = str((data or {}).get("task_id") or (data or {}).get("id") or "")
    if not task_id:
        raise ReconstructionError("Tripo 没有返回任务编号。")
    return task_id


def _tripo_output(data: dict[str, Any]) -> dict[str, Any]:
    output = data.get("output") if isinstance(data.get("output"), dict) else data
    result: dict[str, str] = {}
    for name in ("pbr_model", "model", "base_model", "rendered_image", "thumbnail"):
        value = output.get(name)
        if isinstance(value, str) and value.startswith("http"):
            result[name] = value
        elif isinstance(value, dict):
            url = value.get("url") or value.get("render_url")
            if isinstance(url, str) and url.startswith("http"):
                result[name] = url
    return result


def _tripo_reconstruct(
    images: Sequence[Path],
    plan: ReconstructionPlan,
    *,
    dest: Path,
    key: str,
    timeout: float,
    interval: float,
    progress: Progress | None,
) -> "ReconstructionAsset":
    tokens: list[tuple[str, str]] = []
    for index, path in enumerate(images[:MAX_IMAGES]):
        if progress:
            progress(6 + index * 3, f"上传照片 {index + 1}/{min(len(images), MAX_IMAGES)}")
        tokens.append((_image_mime(path), _tripo_upload(path, key, timeout)))
    if progress:
        progress(18, "提交三维重建任务")
    task_id = _tripo_create_task(tokens, key, plan, timeout)
    data = _poll(
        f"{_tripo_base()}/task/{task_id}",
        headers=_tripo_headers(key),
        timeout_s=timeout,
        interval=interval,
        progress=progress,
        unwrap=_tripo_data,
    )
    urls = _tripo_output(data)
    if not urls.get("pbr_model") and not urls.get("model"):
        raise ReconstructionError("Tripo 任务成功但没有返回可下载的模型。")
    files: dict[str, Path] = {}
    if progress:
        progress(74, "下载纹理模型")
    if urls.get("pbr_model"):
        files["glb"] = _download(urls["pbr_model"], dest / "model_textured.glb", timeout=timeout)
    if urls.get("model") and "glb" not in files:
        files["glb"] = _download(urls["model"], dest / "model_textured.glb", timeout=timeout)
    if urls.get("rendered_image"):
        try:
            files["thumbnail"] = _download(urls["rendered_image"], dest / "thumbnail.png", timeout=timeout)
        except ReconstructionError:
            pass
    return ReconstructionAsset(
        provider="tripo",
        task_id=task_id,
        files=files,
        meta={"task_id": task_id, "output_keys": sorted(urls), "model_version": provider_model("tripo")},
    )


def _tripo_text(
    prompt: str,
    plan: ReconstructionPlan,
    *,
    dest: Path,
    key: str,
    timeout: float,
    interval: float,
    progress: Progress | None,
) -> "ReconstructionAsset":
    model = text_model("tripo")
    preset = quality_preset()
    body: dict[str, Any] = {
        "type": "text_to_model",
        "prompt": prompt[:1024],
        "face_limit": _face_count(int(preset["tripo_faces"])),
        "geometry_quality": "detailed",
        "texture_quality": preset["tripo_texture"],
        "pbr": True,
        "texture": True,
    }
    if model:
        body["model_version"] = model
    if progress:
        progress(10, f"提交文字到 Tripo（{model or '默认模型'}）")
    payload = _request_json(
        "POST",
        f"{_tripo_base()}/task",
        headers=_tripo_headers(key),
        payload=body,
        timeout=min(90.0, timeout),
    )
    data = _tripo_data(payload)
    task_id = str((data or {}).get("task_id") or (data or {}).get("id") or "")
    if not task_id:
        raise ReconstructionError("Tripo 没有返回任务编号。")
    data = _poll(
        f"{_tripo_base()}/task/{task_id}",
        headers=_tripo_headers(key),
        timeout_s=timeout,
        interval=interval,
        progress=progress,
        unwrap=_tripo_data,
    )
    urls = _tripo_output(data)
    if not urls.get("pbr_model") and not urls.get("model"):
        raise ReconstructionError("Tripo 任务成功但没有返回可下载的模型。")
    files: dict[str, Path] = {}
    if progress:
        progress(74, "下载纹理模型")
    source = urls.get("pbr_model") or urls.get("model") or ""
    files["glb"] = _download(source, dest / "model_textured.glb", timeout=timeout)
    if urls.get("rendered_image"):
        try:
            files["thumbnail"] = _download(urls["rendered_image"], dest / "thumbnail.png", timeout=timeout)
        except ReconstructionError:
            pass
    return ReconstructionAsset(
        provider="tripo",
        task_id=task_id,
        files=files,
        meta={
            "task_id": task_id,
            "mode": "text_to_3d",
            "model_version": model,
            "quality": quality_name(),
            "prompt_sent": prompt[:1024],
            "output_keys": sorted(urls),
        },
    )


# ---------------------------------------------------------------------------
# Meshy adapter
# ---------------------------------------------------------------------------


def _meshy_base() -> str:
    return (_env("TOUCHLEARN_3D_URL") or "https://api.meshy.ai/openapi/v1").rstrip("/")


def _meshy_headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}", "Accept": "application/json", "Content-Type": "application/json"}


def _data_uri(path: Path) -> str:
    if path.stat().st_size > MAX_IMAGE_BYTES:
        raise ReconstructionError(f"照片超过 {MAX_IMAGE_BYTES // (1024 * 1024)} MB：{path.name}")
    mime = mimetypes.guess_type(path.name)[0] or ""
    if mime not in {"image/png", "image/jpeg", "image/webp"}:
        raise ReconstructionError(f"三维重建只支持 PNG/JPEG/WEBP 照片：{path.name}")
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


def _meshy_reconstruct(
    images: Sequence[Path],
    plan: ReconstructionPlan,
    *,
    dest: Path,
    key: str,
    timeout: float,
    interval: float,
    progress: Progress | None,
) -> "ReconstructionAsset":
    if progress:
        progress(8, "提交照片到 Meshy")
    body: dict[str, Any] = {
        "image_url": _data_uri(images[0]),
        "enable_pbr": True,
        "should_remesh": True,
        "topology": "triangle",
        "target_polycount": 50000,
    }
    model = provider_model("meshy")
    if model:
        body["ai_model"] = model
    if _env("TOUCHLEARN_3D_PROMPT").lower() not in {"0", "false", "off", "no"} and plan.provider_prompt:
        body["texture_prompt"] = plan.provider_prompt[:600]
    payload = _request_json(
        "POST",
        f"{_meshy_base()}/image-to-3d",
        headers=_meshy_headers(key),
        payload=body,
        timeout=min(90.0, timeout),
    )
    if not isinstance(payload, dict):
        raise ReconstructionError("Meshy 没有返回任务编号。")
    task_id = str(payload.get("result") or payload.get("id") or "")
    if not task_id:
        raise ReconstructionError("Meshy 没有返回任务编号。")
    if progress:
        progress(18, f"Meshy 任务 {task_id[:8]} 已排队")
    data = _poll(
        f"{_meshy_base()}/image-to-3d/{task_id}",
        headers=_meshy_headers(key),
        timeout_s=timeout,
        interval=interval,
        progress=progress,
        unwrap=lambda value: value,
    )
    urls = data.get("model_urls") if isinstance(data.get("model_urls"), dict) else {}
    files: dict[str, Path] = {}
    if progress:
        progress(74, "下载纹理模型")
    if isinstance(urls.get("glb"), str):
        files["glb"] = _download(urls["glb"], dest / "model_textured.glb", timeout=timeout)
    if isinstance(urls.get("obj"), str):
        try:
            files["obj"] = _download(urls["obj"], dest / "model_textured.obj", timeout=timeout)
        except ReconstructionError:
            pass
    thumb = data.get("thumbnail_url")
    if isinstance(thumb, str) and thumb.startswith("http"):
        try:
            files["thumbnail"] = _download(thumb, dest / "thumbnail.png", timeout=timeout)
        except ReconstructionError:
            pass
    if "glb" not in files and "obj" not in files:
        raise ReconstructionError("Meshy 任务成功但没有返回可下载的模型。")
    return ReconstructionAsset(
        provider="meshy",
        task_id=task_id,
        files=files,
        meta={"task_id": task_id, "model_urls": sorted(urls), "ai_model": provider_model("meshy")},
    )


def _meshy_text(
    prompt: str,
    plan: ReconstructionPlan,
    *,
    dest: Path,
    key: str,
    timeout: float,
    interval: float,
    progress: Progress | None,
) -> "ReconstructionAsset":
    if progress:
        progress(10, "提交文字到 Meshy")
    preset = quality_preset()
    body: dict[str, Any] = {
        "mode": "refine",
        "prompt": prompt[:1024],
        "art_style": "realistic",
        "should_remesh": True,
        "topology": "triangle",
        "target_polycount": int(preset["tripo_faces"]),
    }
    model = provider_model("meshy")
    if model:
        body["ai_model"] = model
    payload = _request_json(
        "POST",
        f"{_meshy_base()}/text-to-3d",
        headers=_meshy_headers(key),
        payload=body,
        timeout=min(90.0, timeout),
    )
    if not isinstance(payload, dict):
        raise ReconstructionError("Meshy 没有返回任务编号。")
    task_id = str(payload.get("result") or payload.get("id") or "")
    if not task_id:
        raise ReconstructionError("Meshy 没有返回任务编号。")
    data = _poll(
        f"{_meshy_base()}/text-to-3d/{task_id}",
        headers=_meshy_headers(key),
        timeout_s=timeout,
        interval=interval,
        progress=progress,
        unwrap=lambda value: value,
    )
    urls = data.get("model_urls") if isinstance(data.get("model_urls"), dict) else {}
    files: dict[str, Path] = {}
    if progress:
        progress(74, "下载纹理模型")
    if isinstance(urls.get("glb"), str):
        files["glb"] = _download(urls["glb"], dest / "model_textured.glb", timeout=timeout)
    if isinstance(urls.get("obj"), str):
        try:
            files["obj"] = _download(urls["obj"], dest / "model_textured.obj", timeout=timeout)
        except ReconstructionError:
            pass
    thumb = data.get("thumbnail_url")
    if isinstance(thumb, str) and thumb.startswith("http"):
        try:
            files["thumbnail"] = _download(thumb, dest / "thumbnail.png", timeout=timeout)
        except ReconstructionError:
            pass
    if "glb" not in files and "obj" not in files:
        raise ReconstructionError("Meshy 任务成功但没有返回可下载的模型。")
    return ReconstructionAsset(
        provider="meshy",
        task_id=task_id,
        files=files,
        meta={
            "task_id": task_id,
            "mode": "text_to_3d",
            "model_urls": sorted(urls),
            "ai_model": model,
            "quality": quality_name(),
            "prompt_sent": prompt[:1024],
        },
    )


# ---------------------------------------------------------------------------
# Tencent Cloud TokenHub adapter (HY-3D / Tripo through one gateway)
# ---------------------------------------------------------------------------


def _tokenhub_base() -> str:
    value = _env("TOUCHLEARN_3D_TOKENHUB_URL") or _env("TOUCHLEARN_3D_URL")
    return (value or "https://tokenhub.tencentmaas.com").rstrip("/")


def _tokenhub_headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}", "Accept": "application/json", "Content-Type": "application/json"}


def _tokenhub_image(path: Path) -> tuple[str, str]:
    """Return ``(mime, base64)`` for one photo, enforcing TokenHub's 6 MB cap."""
    if path.stat().st_size > MAX_TOKENHUB_IMAGE_BYTES:
        raise ReconstructionError(
            f"TokenHub 单张照片不能超过 {MAX_TOKENHUB_IMAGE_BYTES // (1024 * 1024)} MB：{path.name}"
        )
    mime = mimetypes.guess_type(path.name)[0] or ""
    if mime not in {"image/png", "image/jpeg", "image/webp"}:
        raise ReconstructionError(f"三维重建只支持 PNG/JPEG/WEBP 照片：{path.name}")
    return mime, base64.b64encode(path.read_bytes()).decode("ascii")


def _tokenhub_family(model: str) -> str:
    name = (model or "").lower()
    if name.startswith("tripo"):
        if "p1" in name:
            return "tripo-p1"
        if "p2" in name:
            return "tripo-p2"
        return "tripo-3"
    if name.startswith("hy-3d"):
        return "hy3d"
    return "generic"


def multiview_slots(images: Sequence[Path]) -> dict[str, Path]:
    """Map photos to Tripo's documented front/left/back/right slots.

    Filenames win when they carry a direction word; remaining photos fill the
    empty slots in the documented order.  A single photo is *not* treated as a
    multi-view request.
    """
    if len(images) < 2:
        return {}
    slots: dict[str, Path] = {}
    unlabelled: list[Path] = []
    for path in images[: len(MULTIVIEW_ORDER)]:
        stem = path.stem.lower()
        matched = ""
        for slot in MULTIVIEW_ORDER:
            if any(hint in stem for hint in _MULTIVIEW_HINTS[slot]):
                matched = slot
                break
        if matched and matched not in slots:
            slots[matched] = path
        else:
            unlabelled.append(path)
    for path in unlabelled:
        for slot in MULTIVIEW_ORDER:
            if slot not in slots:
                slots[slot] = path
                break
    return slots


def _tokenhub_image_body(
    model: str,
    mime: str,
    encoded: str,
    *,
    image_url: str = "",
) -> dict[str, Any]:
    """Build the documented image-to-3D body for the selected TokenHub model.

    Tripo takes ``input`` (an HTTPS URL, with a data URI fallback for local
    previews).  HY-3D takes ``image_base64``.  The P1 example in TokenHub's
    console contains only ``model`` and ``input``, so no extra tuning fields are
    added for P1.
    """
    family = _tokenhub_family(model)
    if family.startswith("tripo"):
        body: dict[str, Any] = {
            "model": model,
            "input": image_url or f"data:{mime};base64,{encoded}",
        }
        if family == "tripo-3":
            preset = quality_preset()
            body["face_limit"] = _bounded_faces(int(preset["tripo_faces"]))
            body["texture_quality"] = preset["tripo_texture"]
            body["pbr"] = True
        return body
    body = {
        "model": model,
        "image_base64": encoded,
        "result_format": "GLB",
        "enable_pbr": True,
    }
    if model.lower().startswith("hy-3d-3"):
        body["generate_type"] = "Normal"
        body["face_count"] = _face_count()
    return body


def _tokenhub_multiview_body(model: str, sources: dict[str, str]) -> dict[str, Any]:
    """Build the documented Tripo multi-view body.

    The console example is exactly ``{"model": ..., "inputs": [{"front": ...,
    "left": ..., "back": ..., "right": ...}]}``; slots without a photo are
    omitted rather than sent as empty strings.
    """
    ordered = {slot: sources[slot] for slot in MULTIVIEW_ORDER if sources.get(slot)}
    return {"model": model, "inputs": [ordered]}


def _tokenhub_prompt_body(model: str, prompt: str) -> dict[str, Any]:
    """Build the documented prompt-only body for HY-3D or Tripo on TokenHub."""
    clean = prompt[:1024]
    lower = model.lower()
    family = _tokenhub_family(model)
    # The console examples for Express and P1 contain only model + prompt.
    # Keep those minimal so a strict gateway cannot reject unknown fields.
    if lower == "hy-3d-express" or family == "tripo-p1":
        return {"model": model, "prompt": clean}
    body: dict[str, Any] = {
        "model": model,
        "prompt": clean,
        "result_format": "GLB",
    }
    preset = quality_preset()
    if family == "hy3d":
        body["generate_type"] = "Normal"
        # Text-to-3D is driven by the quality preset; TOUCHLEARN_3D_FACES is
        # reserved for image-to-3D so the two paths do not fight each other.
        body["face_count"] = _bounded_faces(int(preset["hy3d_faces"]))
        body["enable_pbr"] = True
    elif family == "tripo-3":
        body["face_limit"] = _bounded_faces(int(preset["tripo_faces"]))
        body["geometry_quality"] = "detailed"
        body["texture_quality"] = preset["tripo_texture"]
        body["pbr"] = True
        body["texture"] = True
    elif family == "tripo-p2":
        body["face_limit"] = _bounded_faces(50_000)
        body["texture"] = True
        body["pbr"] = True
    else:
        # Unknown marketplace model: send only universally documented fields.
        body["enable_pbr"] = True
    return body


def _tokenhub_submit_body(body: dict[str, Any], *, key: str, timeout: float) -> str:
    payload = _request_json(
        "POST",
        f"{_tokenhub_base()}/v1/api/3d/submit",
        headers=_tokenhub_headers(key),
        payload=body,
        timeout=min(120.0, timeout),
    )
    if not isinstance(payload, dict):
        raise ReconstructionError("TokenHub 没有返回任务编号。")
    if str(payload.get("status") or "").lower() in {"failed", "error"} or payload.get("error"):
        detail = ""
        error = payload.get("error")
        if isinstance(error, dict):
            detail = str(error.get("message") or error.get("code") or "").strip()
        elif isinstance(error, str):
            detail = error.strip()
        raise ReconstructionError(f"TokenHub 提交失败：{detail or payload.get('status') or '未知错误'}")
    task_id = str(payload.get("id") or payload.get("task_id") or "")
    if not task_id:
        raise ReconstructionError("TokenHub 没有返回任务编号。")
    return task_id


def _tokenhub_wait(model: str, task_id: str, *, key: str, timeout: float, interval: float, progress: Progress | None, label: str) -> dict[str, Any]:
    if progress:
        progress(18, f"TokenHub 任务 {task_id[:8]} 已排队")
    started = time.monotonic()
    step = 0
    while True:
        payload = _request_json(
            "POST",
            f"{_tokenhub_base()}/v1/api/3d/query",
            headers=_tokenhub_headers(key),
            payload={"model": model, "id": task_id},
            timeout=min(90.0, timeout),
        )
        status = str((payload or {}).get("status") or "").lower() if isinstance(payload, dict) else ""
        if status in {"completed", "succeeded", "success", "done"}:
            return payload if isinstance(payload, dict) else {}
        if status in {"failed", "error", "cancelled", "canceled"}:
            detail = ""
            if isinstance(payload, dict):
                detail = str(payload.get("error") or payload.get("message") or "").strip()
            raise ReconstructionError(f"{label}失败：{detail or status}")
        if time.monotonic() - started > timeout:
            raise ReconstructionError(f"{label}超时（{int(timeout)} 秒）。")
        if progress:
            step = min(step + 1, 8)
            progress(20 + step * 5, f"{label}中")
        time.sleep(interval)


def _tokenhub_download(
    payload: dict[str, Any],
    *,
    dest: Path,
    timeout: float,
    progress: Progress | None,
) -> tuple[dict[str, Path], dict[str, str]]:
    urls = _tokenhub_results(payload)
    if progress:
        progress(74, "下载纹理模型")
    glb_url = urls.get("glb") or urls.get("gltf")
    if not glb_url:
        kinds = "、".join(sorted(k for k in urls if k != "preview")) or "无"
        raise ReconstructionError(f"TokenHub 任务完成但没有返回 GLB（返回格式：{kinds}）。")
    files: dict[str, Path] = {"glb": _download(glb_url, dest / "model_textured.glb", timeout=timeout)}
    if urls.get("preview"):
        try:
            files["thumbnail"] = _download(urls["preview"], dest / "thumbnail.png", timeout=timeout)
        except ReconstructionError:
            pass
    return files, urls


def _tokenhub_text(
    prompt: str,
    plan: ReconstructionPlan,
    *,
    dest: Path,
    key: str,
    timeout: float,
    interval: float,
    progress: Progress | None,
) -> "ReconstructionAsset":
    model = text_model("tokenhub") or DEFAULT_TOKENHUB_TEXT_MODEL
    _reject_motion_model(model)
    if progress:
        progress(10, f"提交文字到 TokenHub（{model}，{quality_name()} 质量）")
    task_id = _tokenhub_submit_body(_tokenhub_prompt_body(model, prompt), key=key, timeout=timeout)
    payload = _tokenhub_wait(
        model,
        task_id,
        key=key,
        timeout=timeout,
        interval=interval,
        progress=progress,
        label="TokenHub 文字生成三维模型",
    )
    files, urls = _tokenhub_download(payload, dest=dest, timeout=timeout, progress=progress)
    return ReconstructionAsset(
        provider="tokenhub",
        task_id=task_id,
        files=files,
        meta={
            "task_id": task_id,
            "model_version": model,
            "quality": quality_name(),
            "mode": "text_to_3d",
            "prompt_sent": prompt[:1024],
            "prompt_forwarded": True,
            "output_types": sorted(k for k in urls if k != "preview"),
            "note": "文字由 TokenHub 生成高质量网格；标题、底座、方向适配与盲文由本地确定性后处理完成。",
        },
    )


def _reject_motion_model(model: str) -> None:
    if "motion" in (model or "").lower():
        raise ReconstructionError(
            "hy-3d-motion 生成的是人物/物体动画，不是可打印的静态网格。"
            "触觉教材请改用 tripo-3d-3.1（文字）或 tripo-3d-p1（照片/多视图）。"
        )


def _tokenhub_reconstruct(
    images: Sequence[Path],
    plan: ReconstructionPlan,
    *,
    dest: Path,
    key: str,
    timeout: float,
    interval: float,
    progress: Progress | None,
    image_urls: Sequence[str] = (),
) -> "ReconstructionAsset":
    model = provider_model("tokenhub") or DEFAULT_TOKENHUB_MODEL
    _reject_motion_model(model)
    urls_by_path = {str(path): str(url) for path, url in zip(images, image_urls) if url}
    slots = multiview_slots(images)
    use_multiview = len(slots) >= 2 and _tokenhub_family(model).startswith("tripo")
    if use_multiview:
        if progress:
            progress(10, f"提交 {len(slots)} 视角照片到 TokenHub（{model}，多视图）")
        sources: dict[str, str] = {}
        for slot, path in slots.items():
            url = urls_by_path.get(str(path), "")
            if not url:
                mime, encoded = _tokenhub_image(path)
                url = f"data:{mime};base64,{encoded}"
            sources[slot] = url
        body = _tokenhub_multiview_body(model, sources)
        mode = "multiview_to_3d"
        images_used = len(sources)
    else:
        if progress:
            progress(10, f"提交照片到 TokenHub（{model}，单图）")
        mime, encoded = _tokenhub_image(images[0])
        body = _tokenhub_image_body(model, mime, encoded, image_url=urls_by_path.get(str(images[0]), ""))
        mode = "image_to_3d"
        images_used = 1
    task_id = _tokenhub_submit_body(body, key=key, timeout=timeout)
    payload = _tokenhub_wait(
        model,
        task_id,
        key=key,
        timeout=timeout,
        interval=interval,
        progress=progress,
        label="TokenHub 照片重建",
    )
    files, urls = _tokenhub_download(payload, dest=dest, timeout=timeout, progress=progress)
    note = (
        "TokenHub 按 front/left/back/right 多视图生成；标题、底座、方向适配与盲文仍由本地确定性后处理完成。"
        if use_multiview
        else "TokenHub 图生 3D 使用单张照片；上传 2–4 张不同角度照片且文件名含 front/left/back/right 时会自动走多视图。"
    )
    return ReconstructionAsset(
        provider="tokenhub",
        task_id=task_id,
        files=files,
        meta={
            "task_id": task_id,
            "model_version": model,
            "mode": mode,
            "output_types": sorted(k for k in urls if k != "preview"),
            "prompt_forwarded": False,
            "images_received": len(images),
            "images_used": images_used,
            "image_input": "url" if any(urls_by_path.values()) else "data_uri",
            "note": note,
        },
    )


def _url_kind(url: str) -> str:
    path = url.split("?", 1)[0].lower()
    for suffix in ("glb", "gltf", "obj", "fbx", "usdz"):
        if path.endswith(f".{suffix}"):
            return suffix
    return ""


def _tokenhub_results(payload: Any) -> dict[str, str]:
    """Extract downloadable files from the documented query response.

    TokenHub returns ``data`` (or ``result``) as a list of
    ``{"type": "glb", "url": ...}`` items in the console examples.  A mapping
    with ``glb_url`` / ``model_url`` is accepted too, so a gateway change does
    not silently lose the mesh.
    """
    if not isinstance(payload, dict):
        return {}
    raw = payload.get("data", payload.get("result"))
    if isinstance(raw, dict):
        items = [raw]
    elif isinstance(raw, list):
        items = [item for item in raw if isinstance(item, dict)]
    else:
        items = []
    urls: dict[str, str] = {}
    for item in items:
        url = ""
        for key in ("url", "glb_url", "model_url", "file_url", "download_url"):
            candidate = item.get(key)
            if isinstance(candidate, str) and candidate.startswith("http"):
                url = candidate
                break
        kind = str(item.get("type") or item.get("format") or item.get("file_type") or "").lower()
        if not kind and url:
            kind = _url_kind(url)
        if kind and url:
            urls.setdefault(kind, url)
        for key in ("preview_image_url", "thumbnail_url", "preview"):
            preview = item.get(key)
            if isinstance(preview, str) and preview.startswith("http"):
                urls.setdefault("preview", preview)
    return urls


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


@dataclass
class ReconstructionAsset:
    provider: str
    task_id: str
    files: dict[str, Path]
    meta: dict[str, Any] = field(default_factory=dict)
    mesh: Mesh | None = None
    mesh_error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "task_id": self.task_id,
            "files": {name: path.name for name, path in self.files.items()},
            "meta": dict(self.meta),
            "mesh_triangles": self.mesh.triangle_count() if self.mesh else 0,
            "mesh_error": self.mesh_error,
        }


def _parse_asset_mesh(asset: ReconstructionAsset, progress: Progress | None) -> ReconstructionAsset:
    if progress:
        progress(80, "解析生成网格")
    try:
        if "obj" in asset.files:
            from .volumetric import load_mesh

            asset.mesh = load_mesh(asset.files["obj"])
        elif "glb" in asset.files:
            mesh, glb_meta = read_glb_mesh(asset.files["glb"])
            asset.mesh = mesh
            asset.meta["glb"] = glb_meta
    except Exception as error:
        asset.mesh_error = str(error)
    return asset


def reconstruct(
    images: Sequence[str | Path],
    plan: ReconstructionPlan,
    *,
    dest: str | Path,
    progress: Progress | None = None,
    image_urls: Sequence[str] = (),
) -> ReconstructionAsset:
    """Run the image-to-3D provider and parse the returned geometry.

    ``image_urls`` is optional and parallel to ``images``.  When the server has
    a public base URL it publishes the uploaded photos and TokenHub can fetch
    them exactly as its console examples show.  Without a public URL the
    adapter falls back to a data URI so local previews still work.

    The textured file stays untouched as the photo-real deliverable.  Geometry
    is parsed separately so the printable STL can be composed deterministically.
    """
    paths = [Path(item) for item in images if Path(item).is_file()]
    if not paths:
        raise ReconstructionError("照片级三维重建至少需要一张照片。")
    provider = provider_name()
    key = provider_key(provider)
    if not key:
        raise ReconstructionError(
            "未配置三维生成接口。请设置 TOUCHLEARN_3D_KEY（TokenHub、Tripo 或 Meshy），"
            "或改用“触觉3D模型”由本地编译器建模。"
        )
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    timeout = _timeout_s()
    interval = _poll_s()
    if provider == "meshy":
        asset = _meshy_reconstruct(paths, plan, dest=dest, key=key, timeout=timeout, interval=interval, progress=progress)
    elif provider == "tokenhub":
        asset = _tokenhub_reconstruct(
            paths,
            plan,
            dest=dest,
            key=key,
            timeout=timeout,
            interval=interval,
            progress=progress,
            image_urls=image_urls,
        )
    else:
        asset = _tripo_reconstruct(paths, plan, dest=dest, key=key, timeout=timeout, interval=interval, progress=progress)
    return _parse_asset_mesh(asset, progress)


def text_to_3d(
    prompt: str,
    plan: ReconstructionPlan,
    *,
    dest: str | Path,
    progress: Progress | None = None,
) -> ReconstructionAsset:
    """Generate a mesh from a text prompt, then parse it for the print composer.

    This is the generative half of the product.  It never assembles primitives:
    the provider returns one textured mesh, and the deterministic composer adds
    the display base, title plaque, braille legend and orientation.
    """
    clean = (prompt or "").strip()
    if not clean:
        raise ReconstructionError("文字生成三维模型需要对象描述。")
    provider = provider_name()
    key = provider_key(provider)
    if not key:
        raise ReconstructionError(
            "未配置三维生成接口。请设置 TOUCHLEARN_3D_KEY（TokenHub、Tripo 或 Meshy），"
            "或改用“触觉3D模型”由本地编译器建模。"
        )
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    timeout = _timeout_s()
    interval = _poll_s()
    if provider == "meshy":
        asset = _meshy_text(clean, plan, dest=dest, key=key, timeout=timeout, interval=interval, progress=progress)
    elif provider == "tokenhub":
        asset = _tokenhub_text(clean, plan, dest=dest, key=key, timeout=timeout, interval=interval, progress=progress)
    else:
        asset = _tripo_text(clean, plan, dest=dest, key=key, timeout=timeout, interval=interval, progress=progress)
    return _parse_asset_mesh(asset, progress)


# ---------------------------------------------------------------------------
# GLB geometry parser (standard library only)
# ---------------------------------------------------------------------------

_COMPONENT = {
    5120: ("b", 1),
    5121: ("B", 1),
    5122: ("h", 2),
    5123: ("H", 2),
    5125: ("I", 4),
    5126: ("f", 4),
}
_TYPE_COUNT = {
    "SCALAR": 1,
    "VEC2": 2,
    "VEC3": 3,
    "VEC4": 4,
    "MAT2": 4,
    "MAT3": 9,
    "MAT4": 16,
}


def _glb_chunks(raw: bytes) -> tuple[dict[str, Any], bytes]:
    if len(raw) < 20:
        raise ReconstructionError("GLB 文件不完整。")
    magic, version, length = struct.unpack_from("<4sII", raw, 0)
    if magic != b"glTF" or version != 2:
        raise ReconstructionError("不是 glTF 2.0 二进制文件。")
    offset = 12
    document: dict[str, Any] | None = None
    binary = b""
    while offset + 8 <= min(length, len(raw)):
        chunk_length, chunk_type = struct.unpack_from("<II", raw, offset)
        offset += 8
        chunk = raw[offset : offset + chunk_length]
        offset += chunk_length
        if chunk_type == 0x4E4F534A:
            document = json.loads(chunk.decode("utf-8").rstrip("\x00 "))
        elif chunk_type == 0x004E4942:
            binary = chunk
    if document is None:
        raise ReconstructionError("GLB 缺少 JSON 块。")
    required = document.get("extensionsRequired") or []
    blocked = [str(item) for item in required if "draco" in str(item).lower() or "meshopt" in str(item).lower()]
    if blocked:
        raise ReconstructionError(f"GLB 使用了无法在本地解析的压缩扩展：{', '.join(blocked)}")
    return document, binary


def _accessor_values(document: dict[str, Any], binary: bytes, index: int) -> list[tuple[float, ...]]:
    accessors = document.get("accessors") or []
    views = document.get("bufferViews") or []
    if index < 0 or index >= len(accessors):
        raise ReconstructionError("GLB 访问器索引越界。")
    accessor = accessors[index]
    view = views[int(accessor.get("bufferView", -1))]
    component = int(accessor.get("componentType", 5126))
    fmt, size = _COMPONENT.get(component, ("f", 4))
    count = int(accessor.get("count", 0))
    width = _TYPE_COUNT.get(str(accessor.get("type") or "SCALAR"), 1)
    stride = int(view.get("byteStride") or size * width)
    start = int(view.get("byteOffset") or 0) + int(accessor.get("byteOffset") or 0)
    values: list[tuple[float, ...]] = []
    unpack = struct.Struct("<" + fmt * width).unpack_from
    for item in range(count):
        offset = start + item * stride
        if offset + size * width > len(binary):
            break
        values.append(tuple(float(value) for value in unpack(binary, offset)))
    return values


def _node_matrix(node: dict[str, Any]) -> list[float]:
    if isinstance(node.get("matrix"), list) and len(node["matrix"]) == 16:
        return [float(value) for value in node["matrix"]]
    tx, ty, tz = [float(value) for value in (node.get("translation") or [0.0, 0.0, 0.0])]
    qx, qy, qz, qw = [float(value) for value in (node.get("rotation") or [0.0, 0.0, 0.0, 1.0])]
    sx, sy, sz = [float(value) for value in (node.get("scale") or [1.0, 1.0, 1.0])]
    xx, yy, zz = qx * qx, qy * qy, qz * qz
    xy, xz, yz = qx * qy, qx * qz, qy * qz
    wx, wy, wz = qw * qx, qw * qy, qw * qz
    return [
        (1 - 2 * (yy + zz)) * sx,
        2 * (xy + wz) * sx,
        2 * (xz - wy) * sx,
        0.0,
        2 * (xy - wz) * sy,
        (1 - 2 * (xx + zz)) * sy,
        2 * (yz + wx) * sy,
        0.0,
        2 * (xz + wy) * sz,
        2 * (yz - wx) * sz,
        (1 - 2 * (xx + yy)) * sz,
        0.0,
        tx,
        ty,
        tz,
        1.0,
    ]


def _multiply(a: list[float], b: list[float]) -> list[float]:
    result = [0.0] * 16
    for row in range(4):
        for column in range(4):
            result[column * 4 + row] = sum(a[index * 4 + row] * b[column * 4 + index] for index in range(4))
    return result


def _apply(matrix: list[float], point: tuple[float, float, float]) -> tuple[float, float, float]:
    x, y, z = point
    return (
        matrix[0] * x + matrix[4] * y + matrix[8] * z + matrix[12],
        matrix[1] * x + matrix[5] * y + matrix[9] * z + matrix[13],
        matrix[2] * x + matrix[6] * y + matrix[10] * z + matrix[14],
    )


def read_glb_mesh(path: str | Path) -> tuple[Mesh, dict[str, Any]]:
    """Read triangle geometry from a GLB, including node transforms.

    The provider's GLB is Y-up.  The mesh is rotated to the Z-up convention used
    by the STL kernel.  Textures, materials and UVs are deliberately ignored:
    they belong to the photo-real preview, not to the single-material print.
    """
    path = Path(path)
    document, binary = _glb_chunks(path.read_bytes())
    nodes = document.get("nodes") or []
    meshes = document.get("meshes") or []
    scenes = document.get("scenes") or []
    roots: list[int]
    if scenes:
        scene = scenes[int(document.get("scene") or 0)]
        roots = [int(item) for item in scene.get("nodes") or []]
    else:
        roots = [index for index in range(len(nodes)) if not any(index in (node.get("children") or []) for node in nodes)]

    triangles: list[Any] = []
    primitive_count = 0
    mesh_count = 0
    has_uv = False
    identity = [1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0]
    stack: list[tuple[int, list[float]]] = [(index, identity) for index in roots]
    seen: set[int] = set()
    while stack:
        node_index, parent = stack.pop()
        if node_index < 0 or node_index >= len(nodes) or node_index in seen:
            continue
        seen.add(node_index)
        node = nodes[node_index]
        world = _multiply(parent, _node_matrix(node))
        mesh_index = node.get("mesh")
        if isinstance(mesh_index, int) and 0 <= mesh_index < len(meshes):
            mesh_count += 1
            for primitive in meshes[mesh_index].get("primitives") or []:
                if int(primitive.get("mode", 4)) != 4:
                    continue
                attributes = primitive.get("attributes") or {}
                if "POSITION" not in attributes:
                    continue
                if "TEXCOORD_0" in attributes:
                    has_uv = True
                positions = [_apply(world, value[:3]) for value in _accessor_values(document, binary, int(attributes["POSITION"]))]
                if "indices" in primitive:
                    indices = [int(value[0]) for value in _accessor_values(document, binary, int(primitive["indices"]))]
                else:
                    indices = list(range(len(positions)))
                primitive_count += 1
                for start in range(0, len(indices) - 2, 3):
                    a, b, c = indices[start : start + 3]
                    if a >= len(positions) or b >= len(positions) or c >= len(positions):
                        continue
                    pa, pb, pc = positions[a], positions[b], positions[c]
                    # glTF is Y-up: (x, y, z) -> (x, -z, y)
                    triangles.append(((pa[0], -pa[2], pa[1]), (pb[0], -pb[2], pb[1]), (pc[0], -pc[2], pc[1])))
                    if len(triangles) > MAX_TRIANGLES:
                        raise ReconstructionError("重建网格三角形过多，无法在浏览器端处理。")
        for child in node.get("children") or []:
            stack.append((int(child), world))

    if not triangles:
        raise ReconstructionError("GLB 中没有可用的三角面。")
    mesh = Mesh(triangles)
    return mesh, {
        "source": path.name,
        "nodes": len(nodes),
        "meshes": mesh_count,
        "primitives": primitive_count,
        "triangles": len(triangles),
        "has_uv": has_uv,
        "materials": len(document.get("materials") or []),
        "textures": len(document.get("textures") or []),
    }
