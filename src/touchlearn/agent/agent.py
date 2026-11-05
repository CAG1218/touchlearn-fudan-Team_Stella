"""TouchLearn agent orchestration.

The agent has one job: turn a messy, multimodal request into a reproducible
bundle whose geometry is built by the deterministic kernel.  It does not let a
language model invent coordinates.  Perception proposes a draft; the user
confirms it; the kernel builds; the standards gate reports what is printable
and what still needs a human or a missing module.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import html
import ipaddress
import json
import math
import platform
import re
import shutil
import sys
import traceback
import uuid
import wave
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence
from urllib.parse import urlsplit

from .. import __version__ as core_version
from .. import cnbraille
from ..geometry import Mesh, build_mesh
from ..layout import LayoutResult, build_layout
from ..model import Diagram, diagram_from_dict
from ..model3d import (
    Model3DError,
    braille_summary,
    build_model3d,
    companion_scene,
    model3d_narration,
    plan_model3d,
)
from ..paper import canvas_for, choose_paper, paper_name, paper_size
from ..stl import sha256_file, write_binary_stl
from ..svg import write_svg
from . import __version__ as agent_version
from .audio import (
    available_voices,
    function_narration,
    mesh_narration,
    narration_text,
    robot_relief_narration,
    robot_volume_narration,
    spoken_expression,
    synthesize_windows_tts,
)
from .documents import extract_document, scene_from_text
from .expression import Expression, ExpressionError, parse_expression, strip_function_prefix
from .circuit import CircuitError, circuit_from_scene, circuit_to_scene, normalize_circuit
from .llm import llm_status, resolve_selection
from .parametric import (
    DEFAULT_ROBOT,
    RobotSpec,
    build_function_layout,
    build_robot_layout,
    robot_spec_from_brief,
    robot_spec_from_mapping,
)
from .preview import mesh_preview_svg
from .qr import audio_page_html, make_qr_card_svg
from .reconstruct import (
    ReconstructionError,
    ReconstructionPlan,
    generative_enabled,
    plan_reconstruction,
    reconstruction_status,
    reconstruct,
    text_to_3d,
)
from .reconstruct_print import compose_printable
from .router import RouteDecision, classify_request, extract_function
from .scene import (
    SceneError,
    generate_scene,
    match_template,
    normalize_roadmap,
    normalize_scene,
    scene_has_wires,
    scene_narration,
    scene_summary,
    scene_templates,
    template_preview,
)
from .scene_edit import (
    SceneEditError,
    edit_scene as apply_scene_edit,
    preview_scene as build_scene_preview,
    recognize_circuit_image,
)
from .scene_svg import scene_to_svg
from .standards import (
    STATUS_DEVIATION,
    STATUS_FAIL,
    STATUS_NA,
    STATUS_PASS,
    STATUS_REVIEW,
    Check,
    ComplianceReport,
    check_plate,
    check_relief,
    check_volumetric,
    rules_registry,
    standards_registry,
)
from .vision import interpret_image, vision_status
from .volumetric import (
    VolumeResult,
    build_robot_volume,
    load_mesh,
    mesh_health,
    mesh_relief,
    normalize_mesh,
)

Progress = Callable[[int, str], None]

ALLOWED_UPLOADS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".pdf", ".stl", ".obj", ".txt", ".json"}
MAX_FILE_BYTES = 30 * 1024 * 1024
MAX_TOTAL_BYTES = 80 * 1024 * 1024

DEFAULT_CONSTRAINTS = {
    "max_features": 12,
    "min_line_width_mm": 1.0,
    "min_spacing_mm": 4.0,
    "base_thickness_mm": 3.0,
    "relief_height_mm": 0.8,
    "braille_dot_mm": 1.5,
    "braille_height_mm": 0.6,
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _slug(value: str, fallback: str = "touchlearn") -> str:
    clean = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-._")
    return clean[:80] or fallback


def _upload_name(value: str, fallback: str = "upload") -> str:
    """Sanitise an upload name without losing the extension.

    ``_slug`` is built for ASCII ids: it replaces every non-ASCII run with a
    dash and then strips leading/trailing ``-._``, so ``路线图.jpg`` collapses to
    ``jpg`` and the suffix check reports an unknown file type.  Uploads keep
    their suffix and allow non-ASCII letters, which the Windows and Linux
    filesystems both accept; everything that is not a word character, dot or
    dash is still replaced.
    """
    base = str(value).replace("\\", "/").split("/")[-1]
    path = Path(base)
    suffix = path.suffix.lower()
    stem = re.sub(r"[^\w.-]+", "-", path.stem, flags=re.UNICODE).strip("-._")
    return (stem[:80] or fallback) + suffix


def _public_http_base(value: str) -> str:
    """Return a base URL the 3D provider can actually fetch, or ``""``.

    A local preview server reports ``http://127.0.0.1:8765``.  Sending that to
    TokenHub would make it fetch its own loopback, so photo reconstruction
    falls back to an inline data URI unless the deployment configured a real
    public address.
    """
    base = (value or "").strip().rstrip("/")
    if not base.startswith(("http://", "https://")):
        return ""
    host = urlsplit(base).hostname or ""
    if not host or host.lower() in {"localhost", "0.0.0.0"} or host.lower().endswith(".local"):
        return ""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return base
    if address.is_loopback or address.is_private or address.is_link_local or address.is_reserved:
        return ""
    return base


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return round(value, 6)
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    return str(value)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_json_safe(payload), ensure_ascii=False, indent=2), encoding="utf-8")


def _letter_map(points: Sequence[Any]) -> list[str]:
    letters = "ABCDEFGH"
    mapping: list[str] = []
    cursor = 0
    for point in points:
        if getattr(point, "kind", "") == "y_intercept" and "I" not in mapping:
            mapping.append("I")
        else:
            mapping.append(letters[cursor] if cursor < len(letters) else "?")
            cursor += 1
    return mapping


def _number(value: float) -> str:
    if abs(value - round(value)) < 1e-6:
        return str(int(round(value)))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _optional_float(value: Any, label: str) -> float | None:
    """Read an optional finite number; reject booleans and ``NaN``/``Inf``."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise ValueError(f"{label}必须是数字。")
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label}必须是数字。") from error
    if not math.isfinite(number):
        raise ValueError(f"{label}必须是有限数字。")
    return number


def _spoken_expression(expression: str) -> str:
    return spoken_expression(expression)


def _decode_base64(data: str) -> bytes:
    payload = data.strip()
    if "," in payload and payload.startswith("data:"):
        payload = payload.split(",", 1)[1]
    try:
        return base64.b64decode(payload, validate=False)
    except (binascii.Error, ValueError) as error:
        raise ValueError(f"文件内容不是有效的 base64：{error}") from error


def _image_info(path: Path) -> dict[str, Any]:
    try:
        from PIL import Image  # type: ignore

        with Image.open(path) as image:
            return {"width": image.width, "height": image.height, "format": image.format}
    except Exception:
        return {}


def _mesh_info(path: Path) -> dict[str, Any]:
    try:
        mesh = load_mesh(path)
    except Exception as error:
        return {"error": str(error)}
    return {**mesh_health(mesh), "source": path.name}


def _generation_modes(payload: dict[str, Any]) -> list[str]:
    """Read the product switches from the composer.

    ``tactile`` is the flat teaching plate, ``model3d`` is the deterministic
    display model and ``reconstruct`` turns uploaded photos into a textured
    GLB plus a printable stand.  Unknown values are ignored; an empty request
    keeps the historical tactile default so older clients keep working.
    """
    raw = payload.get("generation_modes")
    if isinstance(raw, str):
        raw = [raw]
    modes: list[str] = []
    if isinstance(raw, list):
        for item in raw:
            value = str(item).strip().lower()
            if value in {"tactile", "tactile_plate", "plate", "教材"}:
                value = "tactile"
            elif value in {"model3d", "3d", "model", "volumetric", "3d_model", "模型"}:
                value = "model3d"
            elif value in {
                "reconstruct",
                "photo3d",
                "photo_3d",
                "photo-real",
                "photoreal",
                "照片级",
                "照片级3d重建",
                "照片重建",
                "重建",
            }:
                value = "reconstruct"
            else:
                continue
            if value not in modes:
                modes.append(value)
    return modes or ["tactile"]


def _looks_model3d(text: str) -> bool:
    lowered = (text or "").lower()
    return any(
        keyword in lowered
        for keyword in (
            "3d",
            "三维",
            "立体",
            "摆件",
            "手办",
            "太阳系",
            "行星",
            "地球仪",
            "分子结构",
            "火箭",
            "模型",
        )
    )


def _short_title(value: str) -> str:
    text = "".join(ch for ch in (value or "") if "\u4e00" <= ch <= "\u9fff" or ch.isalnum())
    return text[:6] or "模型"


def _validate_print_labels(title: str, labels: list[dict[str, str]]) -> None:
    """Reject titles the Chinese braille table cannot spell.

    The plaque must never print a different name than the one shown in the
    app.  Checking before the provider call turns an unlisted character into
    an immediate, readable error instead of a three-minute wait followed by a
    failed job.
    """
    texts = [title]
    for item in labels:
        text = str(item.get("text") or "").strip()
        if text and text not in texts:
            texts.append(text)
    for text in texts:
        try:
            cnbraille.readings(text)
        except cnbraille.BrailleError as error:
            raise ValueError(
                f"标题或标签“{text}”含有中文盲文表（GF 0019-2018）未收录的字符：{error}。"
                "请改用常用汉字后重试，避免底座印出与名称不符的盲文。"
            ) from error


def _generation_prompt(text: str, spec: dict[str, Any], title: str) -> str:
    """Write one prompt for the generative provider.

    The prompt asks only for the object itself.  Base, plaque, stand, labels
    and orientation are added later by ``compose_printable``; letting the
    provider invent them would duplicate the deterministic layer.
    """
    subject = (text or "").strip() or title
    parts = [
        str(part.get("name_zh") or "").strip()
        for part in (spec.get("parts") or [])
        if str(part.get("name_zh") or "").strip()
    ][:8]
    structure = f"主要结构包括：{'、'.join(parts)}。" if parts else ""
    return (
        f"高精度三维模型：{subject}。{structure}"
        "单个完整物体，结构、比例和表面细节真实准确，轮廓清楚，"
        "表面连续闭合，底面平整，可稳定放置。"
        "不要包含文字、铭牌、支架、底座、场景或背景。"
    )[:1000]


def parse_robot_description(text: str) -> tuple[RobotSpec, float, list[str], list[str]]:
    """Best-effort offline extraction from a natural-language robot description.

    The defaults are explicit and visible in the draft.  The function never
    pretends an image was measured; it only reads text that the user typed.
    """
    notes: list[str] = []
    questions: list[str] = []
    confidence = 0.45
    source = (text or "").strip()
    if not source:
        return DEFAULT_ROBOT, 0.3, ["没有收到机械臂文字描述，已使用通用六轴示例构型。"], ["请确认自由度、连杆长度和末端执行器类型。"]

    explicit_spec = None
    if source.startswith("{"):
        try:
            explicit_spec = json.loads(source)
        except json.JSONDecodeError:
            explicit_spec = None
    if isinstance(explicit_spec, dict):
        try:
            spec = robot_spec_from_mapping(explicit_spec)
            return spec, 0.95, ["已按 JSON 参数构建机械臂。"], []
        except (TypeError, ValueError) as error:
            notes.append(f"JSON 参数无法解析：{error}")

    dof = DEFAULT_ROBOT.dof
    for token, count in (("七轴", 7), ("七自由度", 7), ("六轴", 6), ("六自由度", 6), ("五轴", 5), ("四轴", 4), ("三轴", 3)):
        if token in source:
            dof = count
            confidence = 0.65
            break
    else:
        match = re.search(r"\b([3-7])\s*(?:轴|dof|DOF)", source)
        if match:
            dof = int(match.group(1))
            confidence = 0.65

    lengths = list(DEFAULT_ROBOT.link_lengths)
    match = re.search(r"(?:连杆|link)[^\d]{0,10}((?:\d+(?:\.\d+)?[\s,，、]*){2,})", source, flags=re.IGNORECASE)
    if match:
        values = [float(value) for value in re.findall(r"\d+(?:\.\d+)?", match.group(1))]
        if len(values) >= 2:
            lengths = values
            confidence = max(confidence, 0.7)
            notes.append(f"从文字中读取到 {len(values)} 段连杆长度。")

    angles = list(DEFAULT_ROBOT.joint_angles_deg)
    end_effector = DEFAULT_ROBOT.end_effector
    if "吸盘" in source or "suction" in source.lower():
        end_effector = "suction"
        confidence = max(confidence, 0.7)
    elif "夹爪" in source or "抓手" in source or "gripper" in source.lower():
        end_effector = "gripper"
        confidence = max(confidence, 0.7)
    elif "无末端" in source or "no tool" in source.lower():
        end_effector = "none"

    base_width = DEFAULT_ROBOT.base_width
    base_height = DEFAULT_ROBOT.base_height
    match = re.search(r"底座(?:宽度|直径)[^\d]{0,6}(\d+(?:\.\d+)?)", source)
    if match:
        base_width = float(match.group(1))
    match = re.search(r"底座(?:高度|厚度)[^\d]{0,6}(\d+(?:\.\d+)?)", source)
    if match:
        base_height = float(match.group(1))

    def resize(values: list[float], fallback: float) -> tuple[float, ...]:
        if len(values) >= dof:
            return tuple(values[:dof])
        return tuple(values + [values[-1] if values else fallback] * (dof - len(values)))

    spec = RobotSpec(
        link_lengths=resize(lengths, 10.0),
        link_widths=resize(list(DEFAULT_ROBOT.link_widths), 8.0),
        joint_radii=resize(list(DEFAULT_ROBOT.joint_radii), 4.0),
        joint_angles_deg=tuple(angles[:dof]) if len(angles) >= dof else tuple(angles + [0.0] * (dof - len(angles))),
        base_width=base_width,
        base_height=base_height,
        end_effector=end_effector,
    )
    notes.append(f"已提取：{spec.dof} 轴、{spec.end_effector}、底座 {_number(base_width)}×{_number(base_height)} mm。")
    if confidence < 0.7:
        questions.append("请确认自由度和连杆长度；当前使用的是通用示例值。")
    questions.append("如果要精确复刻照片或工程图，请上传 3D 文件（STL/OBJ），或在确认页微调参数。")
    return spec, confidence, notes, questions


@dataclass
class Draft:
    id: str
    subject: str
    output_mode: str
    confidence: float
    ready: bool
    fields: dict[str, Any]
    questions: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    files: list[dict[str, Any]] = field(default_factory=list)
    created_at: str = field(default_factory=_utc_now)

    def to_dict(self) -> dict[str, Any]:
        return _json_safe(
            {
                "id": self.id,
                "subject": self.subject,
                "output_mode": self.output_mode,
                "confidence": self.confidence,
                "ready": self.ready,
                "fields": self.fields,
                "questions": self.questions,
                "notes": self.notes,
                "files": self.files,
                "created_at": self.created_at,
            }
        )


class TouchLearnAgent:
    def __init__(self, output_root: str | Path = "outputs") -> None:
        self.root = Path(output_root)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "drafts").mkdir(parents=True, exist_ok=True)
        (self.root / "jobs").mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Draft analysis
    # ------------------------------------------------------------------

    def analyze(self, payload: dict[str, Any]) -> dict[str, Any]:
        subject = str(payload.get("subject") or "").strip().lower()
        if subject not in {
            "",
            "auto",
            "function",
            "robot",
            "mesh",
            "scene",
            "model3d",
            "reconstruct",
            "unsupported",
        }:
            raise ValueError(f"unsupported subject: {subject}")
        generation_modes = _generation_modes(payload)
        wants_tactile = "tactile" in generation_modes
        wants_model3d = "model3d" in generation_modes
        wants_reconstruct = "reconstruct" in generation_modes

        # Validate the browser's per-request choice before touching uploads or
        # writing a draft.  An unknown model is a client error, not a reason to
        # silently fall back and bill the default one.
        selection = resolve_selection(
            str(payload.get("model") or "").strip() or None,
            str(payload.get("effort") or "").strip().lower() or None,
        )

        draft_id = uuid.uuid4().hex[:12]
        draft_dir = self.root / "drafts" / draft_id
        uploads_dir = draft_dir / "uploads"
        uploads_dir.mkdir(parents=True, exist_ok=True)
        files = self._save_uploads(payload.get("files") or [], uploads_dir)
        mesh_files = [item for item in files if item["extension"] in {".stl", ".obj"}]
        images = [
            draft_dir / str(item["path"])
            for item in files
            if item["extension"] in {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
        ]
        scanned_pages: list[Path] = []

        text = str(
            payload.get("text")
            or payload.get("description")
            or payload.get("prompt")
            or payload.get("request")
            or ""
        ).strip()

        # Text-bearing uploads are part of the request.  Images and meshes are
        # handled by their own readers; PDF/TXT/JSON are read here so a user can
        # drop a worksheet or a scene JSON without retyping its content.
        document_texts: list[str] = []
        document_notes: list[str] = []
        scene_document: dict[str, Any] | None = None
        scene_document_name = ""
        for item in files:
            if item["extension"] not in {".pdf", ".txt", ".json"}:
                continue
            document = extract_document(
                draft_dir / str(item["path"]),
                raster_dir=draft_dir / "pages",
            )
            item["document"] = document.metadata()
            if document.page_images:
                for raw_page in document.page_images:
                    page_path = Path(raw_page)
                    if page_path.is_file():
                        scanned_pages.append(page_path)
                document_notes.append(f"{item['name']}：{document.message}")
                continue
            if not document.ok:
                document_notes.append(f"{item['name']}：{document.message}")
                continue
            if not document.text.strip():
                document_notes.append(f"{item['name']}：{document.message}")
                continue
            document_texts.append(document.text)
            suffix = "（内容较长，已截断）" if document.truncated else ""
            document_notes.append(f"已读取 {item['name']} 的文本：{len(document.text)} 字符{suffix}。")
            if item["extension"] in {".txt", ".json"} and scene_document is None:
                candidate = scene_from_text(document.text)
                if candidate is not None:
                    scene_document = candidate
                    scene_document_name = str(item["name"])
        if scanned_pages:
            images.extend(scanned_pages)
            document_notes.append(
                f"已把 {len(scanned_pages)} 页扫描 PDF 转成图片，交给视觉模型重建结构；图片不会被逐像素描摹。"
            )
        if document_texts:
            joined = "\n\n".join(document_texts).strip()
            payload["document_text"] = joined
            text = f"{text}\n\n{joined}".strip() if text else joined
            payload["text"] = text
        if scene_document is not None and not payload.get("scene") and not payload.get("template"):
            payload["scene"] = scene_document
            document_notes.append(f"已将 {scene_document_name} 识别为场景结构，按其中的图块与阅读顺序排版。")

        route: dict[str, Any] | None = None
        if subject in {"", "auto"}:
            has_scene = (
                isinstance(payload.get("scene"), (dict, list))
                or isinstance(payload.get("circuit"), dict)
                or bool(str(payload.get("scene") or "").strip())
                or bool(str(payload.get("circuit") or "").strip())
            )
            requested_template = str(payload.get("template") or "").strip()
            # Photo reconstruction is the strongest signal: it needs photos and
            # is explicitly selected by the user.  A scene JSON or a chosen
            # template is hard evidence for the tactile plate, so it wins only
            # when the user did not select reconstruction (or supplied no photo).
            if wants_reconstruct and (images or not (has_scene or requested_template)):
                subject = "reconstruct"
                route = {
                    "subject": "reconstruct",
                    "confidence": 0.95 if images else 0.4,
                    "reason": (
                        "用户选择了照片级 3D 重建并上传了照片。"
                        if images
                        else "用户选择了照片级 3D 重建，但还没有上传照片。"
                    ),
                    "provider": "mode",
                }
            # An explicit 3-D request wins when the user selected the 3-D
            # product.  A scene JSON or a chosen template is hard evidence for
            # the tactile plate, so it wins when both products are selected.
            elif wants_model3d and (not wants_tactile or _looks_model3d(text)) and not (has_scene and wants_tactile) and not requested_template:
                subject = "model3d"
                route = {
                    "subject": "model3d",
                    "confidence": 0.9,
                    "reason": "用户选择了触觉 3D 模型，需求指向立体建模。",
                    "provider": "mode",
                }
            elif has_scene or requested_template:
                # 用户显式给出场景结构或选择了离线模板，这是硬证据，优先于文本意图判断。
                decision = RouteDecision(
                    subject="scene",
                    confidence=0.95,
                    reason="用户提供了场景 JSON 或选择了离线场景模板。",
                    provider="manual",
                )
                subject = decision.subject
                route = decision.to_dict()
            elif wants_model3d and not wants_tactile:
                subject = "model3d"
                route = {
                    "subject": "model3d",
                    "confidence": 0.75,
                    "reason": "用户只选择了触觉 3D 模型。",
                    "provider": "mode",
                }
            else:
                decision = classify_request(
                    text,
                    files=files,
                    images=images,
                    has_expression=bool(str(payload.get("expression") or "").strip()),
                    has_spec=bool(payload.get("spec")),
                    has_mesh=bool(mesh_files),
                    model=selection.get("model") or None,
                    effort=selection.get("effort") or None,
                    vision_model=selection.get("vision_model") or None,
                )
                subject = decision.subject
                route = decision.to_dict()

        if subject == "robot" and text and not str(payload.get("description") or "").strip():
            payload["description"] = text

        if subject == "function":
            draft = self._analyze_function(payload, draft_id, files, selection=selection)
        elif subject == "mesh":
            draft = self._analyze_mesh(payload, draft_id, files, mesh_files)
        elif subject == "scene":
            draft = self._analyze_scene(payload, draft_id, files, images=images, route=route, selection=selection)
        elif subject == "model3d":
            draft = self._analyze_model3d(
                payload,
                draft_id,
                files,
                images=images,
                route=route,
                selection=selection,
                generation_modes=generation_modes,
            )
        elif subject == "reconstruct":
            draft = self._analyze_reconstruct(
                payload,
                draft_id,
                files,
                images=images,
                route=route,
                selection=selection,
                generation_modes=generation_modes,
            )
        elif subject == "unsupported":
            draft = Draft(
                id=draft_id,
                subject="unsupported",
                output_mode="",
                confidence=0.7 if route else 0.5,
                ready=False,
                fields={"source_text": text, "routing": route},
                questions=["这个请求目前无法可靠地转成可触摸的 STL。请换成二维示意图、函数图像、机械臂结构，或上传 STL/OBJ。"],
                notes=["智能体不会为了出图而编造结构；无法可靠表达的请求会被明确拒绝，而不是生成一块无意义的板子。"],
                files=files,
            )
        else:
            draft = self._analyze_robot(payload, draft_id, files, selection=selection)

        if selection.get("provider"):
            draft.fields["llm"] = {
                "provider": selection.get("provider"),
                "label": selection.get("label"),
                "model": selection.get("model"),
                "effort": selection.get("effort"),
                "vision_model": selection.get("vision_model"),
                "requested": bool(
                    str(payload.get("model") or "").strip()
                    or str(payload.get("effort") or "").strip()
                ),
            }
        for note in document_notes:
            if note not in draft.notes:
                draft.notes.append(note)
        if route:
            draft.fields.setdefault("routing", route)
            for item in route.get("notes") or []:
                # The router never sees image pixels and is told to say so.
                # When the scene pipeline did read the image, replaying that
                # disclaimer would contradict the recognition note right above
                # it, so keep it only in fields.routing for debugging.
                if images and "文件名" in str(item):
                    continue
                if item not in draft.notes:
                    draft.notes.append(item)
            for item in route.get("questions") or []:
                if item not in draft.questions:
                    draft.questions.append(item)

        _write_json(draft_dir / "draft.json", draft.to_dict())
        return draft.to_dict()

    def _vision_candidate(
        self,
        draft_id: str,
        image_info: dict[str, Any],
        subject: str,
        *,
        hint: str = "",
        selection: dict[str, Any] | None = None,
    ):
        path = self.root / "drafts" / draft_id / str(image_info.get("path") or "")
        if not path.is_file():
            return None
        selection = selection or {}
        return interpret_image(
            path,
            subject,
            hint=hint,
            model=selection.get("vision_model") or None,
            effort=selection.get("effort") or None,
        )

    def _save_uploads(self, raw_files: Sequence[Any], uploads_dir: Path) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        total = 0
        for index, item in enumerate(raw_files):
            if not isinstance(item, dict):
                continue
            name = _upload_name(str(item.get("name") or f"upload-{index + 1}"), f"upload-{index + 1}")
            extension = Path(name).suffix.lower()
            if extension not in ALLOWED_UPLOADS:
                raise ValueError(f"不支持的文件类型：{extension or '未知'}（支持 PNG/JPG/PDF/STL/OBJ/TXT/JSON）")
            data = item.get("data")
            if not isinstance(data, str):
                continue
            content = _decode_base64(data)
            if len(content) > MAX_FILE_BYTES:
                raise ValueError(f"文件 {name} 超过 30 MB 限制")
            total += len(content)
            if total > MAX_TOTAL_BYTES:
                raise ValueError("上传文件总大小超过 80 MB 限制")
            target = uploads_dir / name
            if target.exists():
                target = uploads_dir / f"{target.stem}-{index + 1}{target.suffix}"
            target.write_bytes(content)
            info: dict[str, Any] = {
                "name": name,
                "path": str(target.relative_to(uploads_dir.parent)),
                "bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "extension": extension,
            }
            if extension in {".png", ".jpg", ".jpeg", ".webp", ".bmp"}:
                info["image"] = _image_info(target)
            elif extension in {".stl", ".obj"}:
                info["mesh"] = _mesh_info(target)
            result.append(info)
        return result

    @staticmethod
    def _function_window(expression: Expression, payload: dict[str, Any]) -> dict[str, Any]:
        """Resolve a coherent x/y window from an expression and optional overrides.

        The same helper powers the confirmation-card preview and the final
        build, so the numbers a user reviews cannot drift from the numbers the
        geometry engine uses.
        """
        x_min = _optional_float(payload.get("x_min"), "x 最小")
        x_max = _optional_float(payload.get("x_max"), "x 最大")
        y_min = _optional_float(payload.get("y_min"), "y 最小")
        y_max = _optional_float(payload.get("y_max"), "y 最大")
        if (x_min is None) != (x_max is None):
            raise ValueError("x 最小和 x 最大需要同时填写，或都留空使用自动视窗。")
        if (y_min is None) != (y_max is None):
            raise ValueError("y 最小和 y 最大需要同时填写，或都留空使用自动视窗。")

        requested_auto = payload.get("auto_window")
        if isinstance(requested_auto, str):
            requested_auto = requested_auto.strip().lower() not in {"", "0", "false", "no", "off"}
        auto_window = (
            x_min is None and y_min is None
            if requested_auto is None
            else bool(requested_auto)
        )
        if auto_window:
            x_min, x_max, y_min, y_max = expression.suggest_window()
            x_source = y_source = "auto"
        else:
            if x_min is None:
                x_min, x_max, _auto_y_min, _auto_y_max = expression.suggest_window()
                x_source = "auto"
            else:
                x_source = "manual"
                if x_max <= x_min:
                    raise ValueError("x 的上界必须大于下界。")
            if y_min is None:
                y_min, y_max = expression.auto_y_range(x_min, x_max)
                y_source = "auto"
            else:
                y_source = "manual"
                if y_max <= y_min:
                    raise ValueError("y 的上界必须大于下界。")

        if x_max <= x_min:
            raise ValueError("x 的上界必须大于下界。")
        if y_max <= y_min:
            raise ValueError("y 的上界必须大于下界。")
        return {
            "x_min": float(x_min),
            "x_max": float(x_max),
            "y_min": float(y_min),
            "y_max": float(y_max),
            "auto_window": auto_window,
            "auto_y": y_source == "auto",
            "x_source": x_source,
            "y_source": y_source,
        }

    @staticmethod
    def _function_clip_warning(expression: Expression, window: dict[str, Any]) -> dict[str, Any]:
        segments = expression.sample(window["x_min"], window["x_max"], 800)
        values = [y for segment in segments for _, y in segment]
        if not values:
            return {"clipped": False, "clipped_ratio": 0.0}
        span = max(window["y_max"] - window["y_min"], 1e-9)
        outside = sum(
            1
            for y in values
            if y < window["y_min"] - span * 0.02 or y > window["y_max"] + span * 0.02
        )
        ratio = outside / len(values)
        return {"clipped": ratio > 0.02, "clipped_ratio": round(ratio, 4)}

    def preview_function(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Deterministic preview used while the user edits a function draft."""
        expression = parse_expression(str(payload.get("expression") or ""))
        window = self._function_window(expression, payload)
        points = expression.key_points(
            window["x_min"],
            window["x_max"],
            window["y_min"],
            window["y_max"],
            max_points=6,
        )
        clip = self._function_clip_warning(expression, window)
        warnings: list[str] = []
        if clip["clipped"]:
            ratio = clip["clipped_ratio"] * 100.0
            if window["auto_window"]:
                warnings.append(
                    f"自动视窗按分位数避开极端值，约 {ratio:.1f}% 的远端采样点不在板面内；"
                    "这是自动取景，如需完整曲线可取消自动视窗后手动扩大 y 范围。"
                )
            else:
                warnings.append(
                    f"当前 y 视窗会裁掉约 {ratio:.1f}% 的曲线采样点；"
                    "可勾选自动视窗或手动增大 y 范围。"
                )
        if not points:
            warnings.append("当前视窗内没有自动关键点；请检查解析式与范围。")
        return {
            "expression": expression.canonical,
            "source": expression.source,
            **window,
            "key_points": [point.to_dict() for point in points],
            "warnings": warnings,
            **clip,
        }

    def _analyze_function(
        self,
        payload: dict[str, Any],
        draft_id: str,
        files: list[dict[str, Any]],
        *,
        selection: dict[str, Any] | None = None,
    ) -> Draft:
        selection = selection or {}
        notes: list[str] = []
        questions: list[str] = []
        explicit_expression = str(payload.get("expression") or "").strip()
        expression_text = explicit_expression
        if expression_text.lower().startswith("y="):
            expression_text = expression_text[2:].strip()
        fields: dict[str, Any] = {"expression": expression_text}
        raw_text = str(payload.get("text") or payload.get("prompt") or payload.get("request") or "").strip()
        if not expression_text and raw_text:
            try:
                parse_expression(raw_text)
                expression_text = raw_text
                fields["expression"] = expression_text
            except ExpressionError:
                candidate = extract_function(
                    raw_text,
                    model=selection.get("model") or None,
                    effort=selection.get("effort") or None,
                    vision_model=selection.get("vision_model") or None,
                )
                if candidate.get("ok") and candidate.get("expression"):
                    expression_text = str(candidate["expression"]).strip()
                    fields["expression"] = expression_text
                    fields["extraction"] = {
                        "provider": candidate.get("provider"),
                        "model": candidate.get("model"),
                        "effort": selection.get("effort"),
                        "confidence": candidate.get("confidence"),
                    }
                    for item in candidate.get("notes") or []:
                        if isinstance(item, str) and item.strip():
                            notes.append(f"文本解析：{item.strip()}")
                    for item in candidate.get("questions") or []:
                        if isinstance(item, str) and item.strip():
                            questions.append(item.strip())
                    if payload.get("x_min") is None and payload.get("x_max") is None:
                        read_x_min = candidate.get("x_min")
                        read_x_max = candidate.get("x_max")
                        if (
                            isinstance(read_x_min, (int, float))
                            and isinstance(read_x_max, (int, float))
                            and not isinstance(read_x_min, bool)
                            and not isinstance(read_x_max, bool)
                            and float(read_x_max) > float(read_x_min)
                        ):
                            payload["x_min"] = float(read_x_min)
                            payload["x_max"] = float(read_x_max)
                            notes.append(
                                f"从文本中读取到横轴范围 x∈[{_number(float(read_x_min))}, {_number(float(read_x_max))}]，可修改。"
                            )
                elif candidate.get("message"):
                    notes.append(str(candidate["message"]))
        images = [item for item in files if item["extension"] in {".png", ".jpg", ".jpeg", ".webp", ".bmp"}]
        vision_confidence = 0.0
        if not expression_text and images:
            candidate = self._vision_candidate(draft_id, images[0], "function", selection=selection)
            if candidate and candidate.ok:
                proposed = str(candidate.data.get("expression") or "").strip()
                if proposed:
                    expression_text = proposed
                    fields["expression"] = proposed
                    vision_confidence = candidate.confidence or 0.6
                    fields["vision"] = {
                        "provider": candidate.provider,
                        "model": selection.get("vision_model") or "",
                        "confidence": round(candidate.confidence, 3),
                    }
                    notes.append(f"视觉模型从图片提取到候选解析式：{proposed}。")
                    read_x_min = candidate.data.get("x_min")
                    read_x_max = candidate.data.get("x_max")
                    if (
                        payload.get("x_min") is None
                        and payload.get("x_max") is None
                        and isinstance(read_x_min, (int, float))
                        and isinstance(read_x_max, (int, float))
                        and not isinstance(read_x_min, bool)
                        and not isinstance(read_x_max, bool)
                        and float(read_x_max) > float(read_x_min)
                    ):
                        payload["x_min"] = float(read_x_min)
                        payload["x_max"] = float(read_x_max)
                        notes.append(
                            f"视觉模型给出的横轴范围 x∈[{_number(float(read_x_min))}, {_number(float(read_x_max))}] 已填入，可修改。"
                        )
                    for item in candidate.data.get("notes") or []:
                        if isinstance(item, str) and item.strip():
                            notes.append(f"读图依据：{item.strip()}")
                    questions.append("解析式由视觉模型提取，请人工核对后再生成；看不清的系数不应直接采用。")
                elif candidate.message:
                    notes.append(candidate.message)
            elif candidate and candidate.message:
                notes.append(candidate.message)
        if not expression_text:
            notes.append("已收到输入，但当前没有可靠的解析式来源；离线模式不会从图片中猜测公式。")
            questions.append("请确认函数解析式，例如 x^2-2x-3；确认后才会生成 STL。")
            if images:
                notes.append("图片已保存到作业目录，作为来源留档；配置视觉模型后可直接从图片提取解析式。")
            return Draft(
                id=draft_id,
                subject="function",
                output_mode="tactile_plate",
                confidence=0.3,
                ready=False,
                fields=fields,
                questions=questions,
                notes=notes,
                files=files,
            )

        try:
            expression = parse_expression(expression_text)
        except ExpressionError as error:
            questions.append(f"解析式无法解析：{error}。请修改为 x 的单变量表达式。")
            return Draft(
                id=draft_id,
                subject="function",
                output_mode="tactile_plate",
                confidence=0.2,
                ready=False,
                fields=fields,
                questions=questions,
                notes=notes,
                files=files,
            )

        try:
            window = self._function_window(expression, payload)
        except ValueError as error:
            questions.append(str(error))
            fields["auto_window"] = False
            fields["window_error"] = str(error)
            return Draft(draft_id, "function", "tactile_plate", 0.2, False, fields, questions, notes, files)

        x_min = window["x_min"]
        x_max = window["x_max"]
        y_min = window["y_min"]
        y_max = window["y_max"]
        points = expression.key_points(x_min, x_max, y_min, y_max, max_points=6)
        clip = self._function_clip_warning(expression, window)
        window_warnings: list[str] = []
        if clip["clipped"]:
            ratio = clip["clipped_ratio"] * 100.0
            if window["auto_window"]:
                window_warnings.append(
                    f"自动视窗按分位数避开极端值，约 {ratio:.1f}% 的远端采样点不在板面内；"
                    "如需完整曲线，可取消自动视窗后手动扩大 y 范围。"
                )
            else:
                window_warnings.append(
                    f"当前 y 视窗会裁掉约 {ratio:.1f}% 的曲线采样点，建议扩大 y 范围或改用自动视窗。"
                )
        fields.update(
            {
                "expression": expression.canonical,
                "x_min": round(x_min, 4),
                "x_max": round(x_max, 4),
                "y_min": round(y_min, 4),
                "y_max": round(y_max, 4),
                "key_points": [point.to_dict() for point in points],
                "auto_window": window["auto_window"],
                "auto_y": window["auto_y"],
                "x_source": window["x_source"],
                "y_source": window["y_source"],
                "window_warnings": window_warnings,
            }
        )
        notes.append(f"解析成功：{expression.canonical}；窗口 x∈[{_number(x_min)}, {_number(x_max)}]，y∈[{_number(y_min)}, {_number(y_max)}]。")
        notes.append(f"识别到 {len(points)} 个关键点。")
        if window["auto_window"]:
            questions.append("窗口由解析式自动计算；修改解析式后会重新计算。如需固定定义域，请取消“自动视窗”后填写 x 范围。")
        elif window["x_source"] == "auto":
            questions.append("当前 x 范围为空，已临时按自动视窗计算；请在确认页补全 x 范围或勾选自动视窗。")
        if window["auto_y"]:
            notes.append("y 范围根据当前 x 区间的曲线与关键点自动计算。")
        if clip["clipped"] and window_warnings:
            notes.append(f"注意：{window_warnings[-1]}")
        questions.append("默认使用英文字母 + UEB 盲文、中文讲解音频；中文盲文模块尚未接入，报告会标记 NEEDS_REVIEW。")
        if images:
            notes.append("图片已保存为来源证据；本次几何使用你确认的解析式。")
        return Draft(
            id=draft_id,
            subject="function",
            output_mode="tactile_plate",
            confidence=(
                min(0.9, max(0.55, vision_confidence))
                if vision_confidence
                else (0.96 if window["x_source"] == "manual" else 0.9)
            ),
            ready=True,
            fields=fields,
            questions=questions,
            notes=notes,
            files=files,
        )

    def _analyze_robot(
        self,
        payload: dict[str, Any],
        draft_id: str,
        files: list[dict[str, Any]],
        *,
        selection: dict[str, Any] | None = None,
    ) -> Draft:
        selection = selection or {}
        notes: list[str] = []
        questions: list[str] = []
        mode = str(payload.get("output_mode") or "").strip().lower()
        if mode in {"volume", "volumetric", "volumetric_model", "3d"}:
            output_mode = "volumetric_model"
        elif mode in {"relief", "plate", "tactile_plate", "relief_plate"}:
            output_mode = "tactile_plate"
        else:
            output_mode = ""
            questions.append("请选择输出形态：三维立体缩小模型，还是刻在板子上的浮雕示意图？")

        images = [item for item in files if item["extension"] in {".png", ".jpg", ".jpeg", ".webp", ".bmp"}]
        spec_payload = payload.get("spec")
        description_text = str(payload.get("description") or "").strip()
        vision_confidence = 0.0
        if not spec_payload and not description_text and images:
            candidate = self._vision_candidate(draft_id, images[0], "robot", selection=selection)
            if candidate and candidate.ok:
                data = candidate.data
                proposed_description = str(data.get("description") or "").strip()
                if proposed_description:
                    payload["description"] = proposed_description
                    description_text = proposed_description
                read_dof = data.get("dof")
                if isinstance(read_dof, (int, float)) and 3 <= int(read_dof) <= 7:
                    payload["dof"] = int(read_dof)
                read_end = str(data.get("end_effector") or "").strip().lower()
                if read_end in {"gripper", "suction", "none"}:
                    payload["end_effector"] = read_end
                read_lengths = data.get("link_lengths_mm")
                if isinstance(read_lengths, list) and read_lengths:
                    try:
                        values = [float(value) for value in read_lengths if float(value) > 0]
                    except (TypeError, ValueError):
                        values = []
                    if values:
                        payload["link_length_mm"] = max(values)
                        payload["spec"] = {
                            "joint_angles_deg": data.get("joint_angles_deg")
                            if isinstance(data.get("joint_angles_deg"), list)
                            else [0.0] * int(payload.get("dof") or len(values)),
                            "link_lengths_mm": values,
                            "end_effector": payload.get("end_effector") or "gripper",
                        }
                        spec_payload = payload["spec"]
                vision_confidence = candidate.confidence or 0.55
                notes.append(f"视觉模型从图片提取到机械臂候选描述：{description_text or '未给出文字描述'}。")
                for item in data.get("notes") or []:
                    if isinstance(item, str) and item.strip():
                        notes.append(f"读图依据：{item.strip()}")
                questions.append("读图得到的连杆尺寸与姿态必须人工核对；没有标注的尺寸仍按比例化估算。")
            elif candidate and candidate.message:
                notes.append(candidate.message)

        confidence = 0.3
        if vision_confidence:
            confidence = max(0.3, min(0.78, vision_confidence))
        if isinstance(spec_payload, dict):
            try:
                spec = robot_spec_from_mapping(spec_payload)
                confidence = min(0.95, max(confidence, 0.78 if vision_confidence else 0.95))
                notes.append(f"已按参数构建 {spec.dof} 轴机械臂。")
            except (ValueError, TypeError) as error:
                notes.append(f"机械臂参数无效：{error}；已回退到文字提取。")
                spec, confidence, extra_notes, extra_questions = parse_robot_description(str(payload.get("description") or ""))
                notes.extend(extra_notes)
                questions.extend(extra_questions)
        elif payload.get("dof") or payload.get("link_length_mm"):
            spec = robot_spec_from_brief(
                dof=int(payload.get("dof") or DEFAULT_ROBOT.dof),
                link_length_mm=float(payload.get("link_length_mm") or 40.0),
                end_effector=str(payload.get("end_effector") or "gripper"),
            )
            confidence = min(0.82, max(confidence, 0.72 if vision_confidence else 0.82))
            notes.append(f"已按快速参数构建 {spec.dof} 轴机械臂；连杆长度为比例化估算，不是工程测量。")
            questions.append("如果需要精确复刻，请上传工程图或 STL/OBJ，或逐项给出各连杆尺寸。")
        else:
            spec, confidence, extra_notes, extra_questions = parse_robot_description(str(payload.get("description") or ""))
            if vision_confidence:
                confidence = max(confidence, 0.6)
            notes.extend(extra_notes)
            questions.extend(extra_questions)

        if images and not description_text and not isinstance(spec_payload, dict) and not vision_confidence:
            confidence = min(confidence, 0.38)
            notes.append("已收到机械臂图片；当前没有可靠的读图结果，不会凭空测量图片尺寸。")
            questions.append("请补充自由度、连杆长度或末端执行器，或上传 STL/OBJ 三维文件。")
        fields = {
            "spec": spec.to_dict(),
            "description": str(payload.get("description") or "").strip(),
            "target_height_mm": float(payload.get("target_height_mm") or 135.0),
            "image_received": bool(images),
        }
        if vision_confidence:
            fields["vision"] = {
                "confidence": round(vision_confidence, 3),
                "provider": "remote-vlm",
                "model": selection.get("vision_model") or "",
            }
        notes.append("板式示意图用侧视运动链表达关节顺序，不表示真实姿态和尺寸。")
        if output_mode == "volumetric_model":
            notes.append("体积模式会生成底座、连杆、关节和末端夹爪，并预留 34 mm 二维码平面。")
        if confidence < 0.7:
            questions.append("当前构型含通用参数；请确认后再生成，避免把示意图当成测量结果。")
        return Draft(
            id=draft_id,
            subject="robot",
            output_mode=output_mode,
            confidence=confidence,
            ready=bool(output_mode) and confidence >= 0.3,
            fields=fields,
            questions=questions,
            notes=notes,
            files=files,
        )

    def _analyze_mesh(self, payload: dict[str, Any], draft_id: str, files: list[dict[str, Any]], mesh_files: list[dict[str, Any]]) -> Draft:
        notes: list[str] = []
        questions: list[str] = []
        if not mesh_files:
            return Draft(
                id=draft_id,
                subject="mesh",
                output_mode="",
                confidence=0.2,
                ready=False,
                fields={},
                questions=["请上传 STL 或 OBJ 三维文件，或改用文字/图片生成机械臂。"],
                notes=["三维文件模式只接受 STL/OBJ。"],
                files=files,
            )
        primary = mesh_files[0]
        mesh_info = primary.get("mesh") or {}
        mode = str(payload.get("output_mode") or "").strip().lower()
        if mode in {"relief", "plate", "tactile_plate", "silhouette_relief"}:
            output_mode = "silhouette_relief"
        else:
            output_mode = "volumetric_model"
        confidence = 0.85 if mesh_info and not mesh_info.get("error") else 0.45
        if mesh_info.get("error"):
            notes.append(f"三维文件读取失败：{mesh_info['error']}")
        else:
            notes.append(
                f"已读取 {primary['name']}：{mesh_info.get('triangles', 0)} 个三角面，"
                f"包围盒 {mesh_info.get('bounds_mm')}，水密：{mesh_info.get('watertight')}。"
            )
            if not mesh_info.get("watertight"):
                questions.append("模型不是水密网格；可以生成 STL，但合规报告会标记 mesh.watertight 失败。建议先在 CAD 中修复。")
        fields = {
            "mesh_path": primary["path"],
            "mesh_name": primary["name"],
            "mesh_info": mesh_info,
            "target_height_mm": float(payload.get("target_height_mm") or 120.0),
            "resolution_mm": float(payload.get("resolution_mm") or 1.0),
            "output_mode": output_mode,
        }
        if output_mode == "silhouette_relief":
            notes.append("轮廓浮雕只做投影，不恢复内部结构、标签和隐藏特征。")
        return Draft(
            id=draft_id,
            subject="mesh",
            output_mode=output_mode,
            confidence=confidence,
            ready=not bool(mesh_info.get("error")),
            fields=fields,
            questions=questions,
            notes=notes,
            files=files,
        )

    def _analyze_model3d(
        self,
        payload: dict[str, Any],
        draft_id: str,
        files: list[dict[str, Any]],
        *,
        images: Sequence[Path] = (),
        route: dict[str, Any] | None = None,
        selection: dict[str, Any] | None = None,
        generation_modes: Sequence[str] = ("tactile",),
    ) -> Draft:
        selection = selection or {}
        text = str(
            payload.get("text")
            or payload.get("description")
            or payload.get("prompt")
            or payload.get("request")
            or ""
        ).strip()
        plan = plan_model3d(
            text,
            images=images,
            model=(selection.get("vision_model") if images else selection.get("model")) or None,
            effort=selection.get("effort") or None,
        )
        notes = list(plan.notes)
        questions = list(plan.questions)
        spec = plan.spec if isinstance(plan.spec, dict) else {}
        kind = str(spec.get("kind") or "assembly")
        planets = spec.get("planets") or []
        parts = spec.get("parts") or []
        status = reconstruction_status()
        # The deterministic compiler stays for the solar catalogue and for
        # offline deployments.  Everything else is generated as one mesh by
        # the 3D provider, then annotated by the local print composer.
        use_generative = (
            bool(text)
            and bool(status.get("configured"))
            and bool(status.get("text_to_3d"))
            and generative_enabled()
            and kind != "solar_system"
        )
        companion = "tactile" in generation_modes and not use_generative

        generation_plan: ReconstructionPlan | None = None
        generation_info: dict[str, Any] = {}
        if use_generative:
            title = _short_title(str(spec.get("title_zh") or spec.get("title") or text))
            generation_plan = ReconstructionPlan(
                ok=True,
                title=title,
                subject=(text or title)[:160],
                keep_features=[
                    str(item.get("name_zh") or "").strip()
                    for item in (spec.get("parts") or [])
                    if isinstance(item, dict) and str(item.get("name_zh") or "").strip()
                ][:6],
                height_mm=120.0,
                base="disc",
                margin_mm=10.0,
                labels=[{"text": title, "target": "主体"}],
                provider=str(status.get("provider") or ""),
                model=str(status.get("text_model") or status.get("model") or ""),
                message="文字交给三维生成模型输出完整网格，本地再统一加标题、底座和方向适配。",
            )
            generation_info = {
                "enabled": True,
                "prompt": _generation_prompt(text, spec, title),
                "provider": status.get("provider"),
                "provider_label": status.get("label"),
                "model": status.get("text_model") or status.get("model"),
                "quality": status.get("quality"),
                "face_budget": status.get("face_budget"),
                "mode": "text_to_3d",
            }

        labels: list[dict[str, Any]] = []
        if use_generative and generation_plan is not None:
            label = generation_plan.title
            try:
                labels.append(
                    {
                        "text": label,
                        "target": "主体",
                        "pinyin": cnbraille.readings(label),
                        "cells": [list(cell) for cell in cnbraille.encode_text(label)],
                        "width_mm": round(cnbraille.width(label, syllable_gap_mm=cnbraille.SYLLABLE_GAP_MM), 2),
                    }
                )
            except Exception:
                pass
            notes.append(
                f"已接入{status.get('label') or '三维生成'}的 {generation_info.get('model') or '默认模型'}"
                f"（{status.get('quality') or 'high'} 质量）；输出是单个完整网格，不是基础几何体拼装。"
            )
            notes.append("部件名不再刻到模型表面。生成后由本地统一加盲文标题、底座、前后方向与二维码位。")
        elif kind == "solar_system":
            for planet in planets:
                name = str(planet.get("name_zh") or "")
                if not name:
                    continue
                try:
                    labels.append(
                        {
                            "text": name,
                            "planet": planet.get("id"),
                            "pinyin": cnbraille.readings(name),
                            "cells": [list(cell) for cell in cnbraille.encode_text(name)],
                            "width_mm": round(cnbraille.width(name), 2),
                        }
                    )
                except Exception:
                    continue
            title_text = str(spec.get("title_zh") or "太阳系")
            notes.append(
                f"已生成太阳和 {len(planets)} 颗行星："
                + "、".join(str(item.get("name_zh") or "") for item in planets)
                + "。"
            )
            if len(planets) == 7:
                notes.append("按你的要求默认生成 7 颗行星，海王星未包含；说“八大行星”可生成 8 颗。")
            notes.append("行星半径保持真实大小顺序，但经过压缩；轨道表示相对顺序，不表示真实距离。")
            notes.append(
                "中文盲文没有刻在行星球面上：行星直径只有 9-25 mm，而一个两字名称的盲文长约 36-53 mm。"
                "行星名称按离太阳由近到远的顺序刻在底座外缘的圆柱面上，从正面标题牌左侧起绕底座一周摸读；不生成竖立名牌。"
            )
        else:
            for part in parts:
                name = str(part.get("name_zh") or part.get("id") or "")
                if not name:
                    continue
                try:
                    labels.append(
                        {
                            "text": name[:4],
                            "part": part.get("id"),
                            "pinyin": cnbraille.readings(name[:4]),
                            "cells": [list(cell) for cell in cnbraille.encode_text(name[:4])],
                            "width_mm": round(cnbraille.width(name[:4]), 2),
                        }
                    )
                except Exception:
                    continue
            notes.append("三维模型由本地编译器生成：大模型只描述部件、相对大小和位置，毫米尺寸由编译器决定。")

        if not plan.ok and not use_generative:
            notes.append(plan.message or "这个需求还不能可靠地转成三维模型。")
        if companion and plan.ok:
            notes.append("你同时选择了触觉教材；生成三维模型时会附带一张 A5-A3 的结构总览板，帮助先建立整体方位。")
        elif companion and not plan.ok:
            notes.append("这次三维模型没有通过校验，所以不会生成配套触觉板；可以先只生成触觉教材，或补充描述后重试。")

        summary_kind = "generative" if use_generative else kind
        summary: dict[str, Any] = {
            "kind": summary_kind,
            "title": spec.get("title"),
            "title_zh": spec.get("title_zh"),
            "planet_count": len(planets),
            "part_count": 0 if use_generative else len(parts),
            "scale_note": spec.get("scale_note"),
            "planets": [
                {
                    "name_zh": item.get("name_zh"),
                    "name_en": item.get("name_en"),
                    "radius_mm": item.get("radius_mm"),
                    "relative_radius": item.get("relative_radius"),
                    "orbit_mm": item.get("orbit_mm"),
                }
                for item in planets
            ],
            "parts": [] if use_generative else [
                {
                    "name_zh": item.get("name_zh"),
                    "shape": item.get("shape"),
                    "size": item.get("size"),
                    "position": item.get("position"),
                }
                for item in parts[:16]
            ],
        }
        if use_generative and generation_plan is not None:
            summary.update(
                {
                    "title": generation_plan.title,
                    "title_zh": generation_plan.title,
                    "provider": generation_info.get("provider"),
                    "provider_label": generation_info.get("provider_label"),
                    "model": generation_info.get("model"),
                    "quality": generation_info.get("quality"),
                    "mode": "text_to_3d",
                    "scale_note": "生成式网格按目标高度缩放；底座、标题与前后方向由本地统一添加。",
                }
            )

        fields: dict[str, Any] = {
            "source_text": text,
            "output_mode": "model3d",
            "spec": spec,
            "kind": summary_kind,
            "planet_count": len(planets),
            "part_count": 0 if use_generative else len(parts),
            "provider_generation": generation_info,
            "reconstruction": status,
            "title": generation_plan.title if generation_plan else "",
            "height_mm": generation_plan.height_mm if generation_plan else 120.0,
            "base": generation_plan.base if generation_plan else "disc",
            "margin_mm": generation_plan.margin_mm if generation_plan else 10.0,
            "labels": generation_plan.labels if generation_plan else [],
            "summary": summary,
            "braille": {
                "standard": "GF 0019-2018",
                "dot_mm": 1.6,
                "labels": labels,
            },
            "generation": {
                "ok": plan.ok or use_generative,
                "provider": generation_info.get("provider") or plan.provider,
                "model": generation_info.get("model") or plan.model,
                "effort": selection.get("effort"),
                "message": generation_plan.message if generation_plan else plan.message,
            },
            "modes": list(generation_modes),
            "companion_tactile": companion and plan.ok,
            "image_received": bool(images),
            "target_height_mm": generation_plan.height_mm if generation_plan else None,
        }
        if route:
            fields["routing"] = route
        if not plan.ok and not use_generative:
            questions.append("也可以只选择“触觉教材”，先做一张二维结构板；或把三维对象拆成更具体的部件再描述。")
        if use_generative:
            questions.append("请确认盲文标题（1–4 个汉字）、打印高度和底座形状，再提交三维生成。")
        else:
            questions.append("请确认部件/行星数量、中文名称和底座图例的阅读方向，再生成 STL。")
        return Draft(
            id=draft_id,
            subject="model3d",
            output_mode="model3d",
            confidence=plan.confidence or (0.7 if use_generative else 0.35),
            ready=bool(use_generative or plan.ok),
            fields=fields,
            questions=questions,
            notes=notes,
            files=files,
        )

    def _analyze_reconstruct(
        self,
        payload: dict[str, Any],
        draft_id: str,
        files: list[dict[str, Any]],
        *,
        images: Sequence[Path] = (),
        route: dict[str, Any] | None = None,
        selection: dict[str, Any] | None = None,
        generation_modes: Sequence[str] = ("reconstruct",),
    ) -> Draft:
        selection = selection or {}
        text = str(
            payload.get("text")
            or payload.get("description")
            or payload.get("prompt")
            or payload.get("request")
            or ""
        ).strip()
        status = reconstruction_status()
        plan = plan_reconstruction(
            text,
            images=images,
            model=(selection.get("vision_model") if images else selection.get("model")) or None,
            effort=selection.get("effort") or None,
        )
        notes = list(plan.notes)
        questions = list(plan.questions)
        labels: list[dict[str, Any]] = []
        for item in plan.labels:
            label = str(item.get("text") or "")
            if not label:
                continue
            try:
                labels.append(
                    {
                        "text": label,
                        "target": item.get("target") or "",
                        "pinyin": cnbraille.readings(label),
                        "cells": [list(cell) for cell in cnbraille.encode_text(label)],
                        "width_mm": round(cnbraille.width(label, syllable_gap_mm=cnbraille.SYLLABLE_GAP_MM), 2),
                    }
                )
            except Exception:
                continue

        if images:
            notes.append(f"已收到 {len(images)} 张照片，将交给三维重建服务还原细节与纹理。")
        else:
            notes.append("照片级重建需要至少一张照片；只有文字描述时请改用“触觉3D模型”，由本地编译器建模。")
            questions.append("请上传 1-4 张同一物体的照片，正面、侧面和顶面各一张效果最好。")
        if not status["configured"]:
            notes.append(str(status["message"]))
            questions.append("服务端尚未配置 TOUCHLEARN_3D_KEY；可先使用“触觉3D模型”，或配置密钥后重试。")
        notes.append("重建会输出两份文件：保留颜色与细节的纹理 GLB，以及带底座、盲文标题、底座盲文图例和二维码位的单色可打印 STL。")
        notes.append("高度、底座、盲文名称由本地确定；照片里看不到的结构不会被编造。")
        notes.append("文字指令会写入重建计划并尽量传给三维服务；改变姿态等几何编辑取决于服务能力，服务不支持时会保留照片原样。")
        if "tactile" in generation_modes:
            notes.append("你同时选择了触觉教材；重建时会附带一张 A5-A3 的结构总览板，帮助先建立整体方位。")

        fields: dict[str, Any] = {
            "source_text": text,
            "output_mode": "reconstruction",
            "plan": plan.to_dict(),
            "title": plan.title,
            "subject_text": plan.subject,
            "height_mm": plan.height_mm,
            "base": plan.base,
            "margin_mm": plan.margin_mm,
            "labels": plan.labels,
            "keep_features": plan.keep_features,
            "changes": plan.changes,
            "print_notes": plan.print_notes,
            "braille": {
                "standard": "GF 0019-2018",
                "dot_mm": cnbraille.DOT_DIAMETER_MM,
                "labels": labels,
            },
            "generation": {
                "ok": plan.ok,
                "provider": plan.provider,
                "model": plan.model,
                "effort": selection.get("effort"),
                "message": plan.message,
            },
            "reconstruction": status,
            "image_count": len(images),
            "modes": list(generation_modes),
            "companion_tactile": "tactile" in generation_modes,
            "target_height_mm": plan.height_mm,
        }
        if route:
            fields["routing"] = route
        questions.append("请确认盲文标题和特征名（每个 1-4 个汉字）、打印高度与底座形状，再开始重建。")
        return Draft(
            id=draft_id,
            subject="reconstruct",
            output_mode="reconstruction",
            confidence=plan.confidence or (0.4 if images else 0.2),
            ready=bool(images) and bool(status["configured"]) and bool(plan.ok),
            fields=fields,
            questions=questions,
            notes=notes,
            files=files,
        )

    def _analyze_scene(
        self,
        payload: dict[str, Any],
        draft_id: str,
        files: list[dict[str, Any]],
        *,
        images: Sequence[Path] = (),
        route: dict[str, Any] | None = None,
        selection: dict[str, Any] | None = None,
    ) -> Draft:
        selection = selection or {}
        notes: list[str] = []
        questions: list[str] = []
        text = str(
            payload.get("text")
            or payload.get("description")
            or payload.get("prompt")
            or payload.get("request")
            or ""
        ).strip()
        fields: dict[str, Any] = {
            "source_text": text,
            "output_mode": "tactile_plate",
            "image_received": bool(images),
        }
        scene: dict[str, Any] | None = None
        circuit: dict[str, Any] | None = None
        circuit_source = ""
        warnings: list[str] = []
        template_id = ""
        confidence = 0.0
        generation: dict[str, Any] = {}

        # A circuit coming back from the visual editor is already a confirmed
        # semantic model.  Compile it here so the build cannot drift from the
        # geometry the user just saw.
        explicit_circuit = payload.get("circuit")
        if isinstance(explicit_circuit, str):
            try:
                explicit_circuit = json.loads(explicit_circuit)
            except json.JSONDecodeError:
                explicit_circuit = None
        if isinstance(explicit_circuit, dict):
            raw_components = explicit_circuit.get("components")
            raw_blocks = explicit_circuit.get("blocks")
            if (not isinstance(raw_components, list) or not raw_components) and not isinstance(raw_blocks, list):
                # normalize_circuit would silently substitute the built-in
                # four-component circuit; an explicit empty circuit means the
                # editor sent nothing usable, not that four parts are expected.
                questions.append("电路结构无法使用：电路至少需要 1 个元件；已忽略这份空电路。")
                explicit_circuit = None
        if isinstance(explicit_circuit, dict):
            try:
                circuit, circuit_warnings = normalize_circuit(explicit_circuit)
                scene, scene_warnings = normalize_scene(
                    circuit_to_scene(circuit),
                    fallback_title=text[:32],
                )
                warnings.extend(circuit_warnings)
                warnings.extend(scene_warnings)
                confidence = 0.9
                circuit_source = "editor"
                notes.append("已使用你在图形编辑器里确认的电路；导线按元件位置重新生成。")
            except (CircuitError, SceneError) as error:
                circuit = None
                questions.append(f"电路结构无法使用：{error}")

        explicit_roadmap = payload.get("roadmap")
        if isinstance(explicit_roadmap, str):
            try:
                explicit_roadmap = json.loads(explicit_roadmap)
            except json.JSONDecodeError:
                explicit_roadmap = None
        if scene is None and isinstance(explicit_roadmap, dict):
            try:
                scene, warnings = normalize_roadmap(explicit_roadmap, fallback_title=text[:32])
                confidence = 0.8
                notes.append("已按阶段把路线图排成中文盲文板。")
            except SceneError as error:
                questions.append(f"路线图无法排版：{error}")

        explicit = payload.get("scene")
        if isinstance(explicit, str):
            try:
                explicit = json.loads(explicit)
            except json.JSONDecodeError:
                explicit = None
        if scene is None and isinstance(explicit, dict):
            try:
                scene, warnings = normalize_scene(explicit, fallback_title=text[:32])
                confidence = 0.8
                notes.append("已使用你确认过的场景 JSON 排版。")
            except SceneError as error:
                questions.append(f"场景 JSON 无法使用：{error}")

        requested_template = str(payload.get("template") or "").strip().lower()
        templates = {entry["id"]: entry for entry in scene_templates()}
        if scene is None and requested_template:
            entry = templates.get(requested_template)
            if entry is None:
                questions.append(f"未找到模板 {requested_template}；请从模板列表中选择。")
            else:
                scene = entry["scene"]
                template_id = entry["id"]
                warnings = []
                confidence = 0.6
                if isinstance(entry.get("circuit"), dict):
                    try:
                        circuit, circuit_warnings = normalize_circuit(entry["circuit"])
                        warnings.extend(circuit_warnings)
                        circuit_source = "template"
                    except CircuitError:
                        circuit = None
                notes.append(f"已使用离线模板：{entry['label']}。")
                if circuit is not None:
                    notes.append("这个模板带可编辑电路，可在图形编辑器里移动元件或输入一句话修改。")

        if scene is None and (text or images):
            # The picker's vision choice drives image reconstruction; a text
            # model never receives an image payload it cannot read.
            chosen_model = (
                selection.get("vision_model") if images else selection.get("model")
            )
            generation_result = generate_scene(
                text,
                images=images,
                model=chosen_model or None,
                effort=selection.get("effort") or None,
            )
            generation = {
                "ok": generation_result.ok,
                "provider": generation_result.provider,
                "model": generation_result.model,
                "effort": selection.get("effort"),
                "message": generation_result.message,
            }
            if generation_result.ok and generation_result.scene:
                scene = generation_result.scene
                confidence = max(0.35, min(0.9, generation_result.confidence))
                notes.extend(generation_result.notes)
                questions.extend(generation_result.questions)
                if generation_result.circuit:
                    try:
                        circuit, circuit_warnings = normalize_circuit(generation_result.circuit)
                        warnings.extend(circuit_warnings)
                        circuit_source = "image" if images else "generated"
                        notes.append(
                            "从图片识别出可编辑电路。" if images else "已整理成可编辑的串联电路。"
                        )
                    except CircuitError:
                        circuit = None
            else:
                notes.append(generation_result.message or "大模型未能生成场景候选。")
                for item in generation_result.questions:
                    if item not in questions:
                        questions.append(item)

        if scene is None and text:
            matched = match_template(text)
            if matched is not None:
                template_id, scene, score = matched
                confidence = 0.5
                warnings = []
                preview = template_preview(template_id)
                if preview and isinstance(preview.get("circuit"), dict):
                    try:
                        circuit, circuit_warnings = normalize_circuit(preview["circuit"])
                        warnings.extend(circuit_warnings)
                        circuit_source = "template"
                    except CircuitError:
                        circuit = None
                notes.append(f"大模型不可用或未生成结果，已匹配离线模板 {template_id}（命中 {score} 个关键词）。")
                if circuit is not None:
                    notes.append("该模板带可编辑电路，可在图形编辑器里移动元件或输入一句话修改。")
                questions.append("离线模板只覆盖常见示意结构；请核对图形、标签和阅读顺序是否符合你的教材。")

        fields["generation"] = generation
        if route:
            fields["routing"] = route

        if scene is None:
            fields["suggested_templates"] = [
                {"id": entry["id"], "label": entry["label"], "description": entry["description"], "summary": entry["summary"], "thumbnail": entry.get("thumbnail", ""), "editor": entry.get("editor", "scene")}
                for entry in templates.values()
            ]
            fields["scene"] = None
            fields["circuit"] = None
            fields["editor"] = "scene"
            if images:
                notes.append("图片已保存为来源证据；配置视觉模型后会据此重建结构示意图。")
            questions.append("请补充图块组成和连接关系，或选择一个离线模板；智能体不会凭空编造结构。")
            return Draft(
                id=draft_id,
                subject="scene",
                output_mode="tactile_plate",
                confidence=0.3,
                ready=False,
                fields=fields,
                questions=questions,
                notes=notes,
                files=files,
            )

        try:
            scene, extra_warnings = normalize_scene(scene, fallback_title=text[:32])
        except SceneError as error:
            fields["scene"] = None
            fields["circuit"] = None
            fields["editor"] = "scene"
            fields["suggested_templates"] = [
                {"id": entry["id"], "label": entry["label"], "description": entry["description"], "summary": entry["summary"], "thumbnail": entry.get("thumbnail", ""), "editor": entry.get("editor", "scene")}
                for entry in templates.values()
            ]
            questions.append(f"场景无法排版：{error}")
            return Draft(
                id=draft_id,
                subject="scene",
                output_mode="tactile_plate",
                confidence=0.3,
                ready=False,
                fields=fields,
                questions=questions,
                notes=notes,
                files=files,
            )
        warnings.extend(extra_warnings)

        # Any scene that carries standard electrical symbols plus wires can be
        # promoted into the semantic circuit model, so the browser editor and
        # the printed plate stay in sync.  Without wires we refuse: a random
        # diagram that happens to contain two labelled boxes is not a circuit.
        if circuit is None and scene_has_wires(scene):
            try:
                recovered = circuit_from_scene(scene)
                if recovered is not None:
                    recovered_circuit, circuit_warnings = normalize_circuit(recovered)
                    compiled, compile_warnings = normalize_scene(
                        circuit_to_scene(recovered_circuit),
                        fallback_title=text[:32],
                    )
                    circuit = recovered_circuit
                    scene = compiled
                    warnings.extend(circuit_warnings)
                    warnings.extend(compile_warnings)
                    circuit_source = "recovered"
                    notes.append("已把图中的电气元件整理成可编辑回路，导线由编译器重新生成。")
            except (CircuitError, SceneError):
                circuit = None

        fields.update(
            {
                "scene": scene,
                "summary": scene_summary(scene),
                "template": template_id,
                "warnings": warnings,
                "circuit": circuit,
                "editor": "circuit" if circuit is not None else "scene",
                "circuit_source": circuit_source,
                # Inline preview so the chat card is visual immediately; the
                # editable version is fetched separately when the editor opens.
                "scene_svg": scene_to_svg(scene, show_names=False),
            }
        )
        if circuit is not None:
            questions.append("可以在图形编辑器里拖动元件、切换开关状态，或输入一句话让智能体改电路。")
        for warning in warnings[:6]:
            notes.append(f"排版提示：{warning}")
        notes.append(
            f"场景包含 {len(scene['blocks'])} 个图块；按阅读顺序自动编号，完整标签只在你勾选的图块上出现。"
        )
        notes.append("板面是结构示意图：位置和连接关系可触摸，绝对尺寸以打印设置为准。")
        questions.append("请确认图块顺序、标签和连接方向；确认后才生成 STL。")
        if images:
            questions.append("图片只作为结构参考，没有逐像素还原；请核对是否遗漏关键部件。")
        return Draft(
            id=draft_id,
            subject="scene",
            output_mode="tactile_plate",
            confidence=confidence or 0.5,
            ready=True,
            fields=fields,
            questions=questions,
            notes=notes,
            files=files,
        )

    # ------------------------------------------------------------------
    # Visual scene editor
    # ------------------------------------------------------------------

    def preview_scene(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Compile a circuit/scene into the editor preview the browser renders."""
        try:
            return build_scene_preview(payload)
        except (SceneEditError, ValueError) as error:
            raise ValueError(str(error)) from error

    def edit_scene(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Apply a semantic operation or a natural-language edit to a circuit."""
        try:
            return apply_scene_edit(payload)
        except (SceneEditError, ValueError) as error:
            raise ValueError(str(error)) from error

    def recognize_scene(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Recognize an uploaded circuit image and open it in the editor."""
        recogn_dir = self.root / "edits" / uuid.uuid4().hex[:12]
        uploads_dir = recogn_dir / "uploads"
        uploads_dir.mkdir(parents=True, exist_ok=True)
        files = self._save_uploads(payload.get("files") or [], uploads_dir)
        images = [
            uploads_dir.parent / str(item["path"])
            for item in files
            if item["extension"] in {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
        ]
        if not images:
            raise ValueError("请先上传一张电路图图片（PNG/JPG/WebP）。")
        text = str(payload.get("text") or "").strip()
        try:
            result = recognize_circuit_image(
                images,
                text=text,
                model=str(payload.get("model") or "").strip() or None,
                effort=str(payload.get("effort") or "").strip().lower() or None,
            )
        except (SceneEditError, ValueError) as error:
            raise ValueError(str(error)) from error
        result["file"] = files[0] if files else None
        return result

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def prepare_job(self, job_id: str, draft_id: str) -> Path:
        """Create a queued job record before the worker thread starts."""
        clean_job_id = "".join(ch for ch in job_id if ch.isalnum() or ch in "-_")[:48]
        if not clean_job_id:
            raise ValueError("invalid job id")
        job_dir = self.root / "jobs" / clean_job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        self._write_job(job_dir, status="queued", progress=0, message="排队中", draft_id=draft_id)
        return job_dir

    def build(
        self,
        draft_id: str,
        *,
        overrides: dict[str, Any] | None = None,
        public_base_url: str = "http://127.0.0.1:8765",
        progress: Progress | None = None,
        job_id: str | None = None,
    ) -> dict[str, Any]:
        draft_dir = self.root / "drafts" / draft_id
        draft_path = draft_dir / "draft.json"
        if not draft_path.exists():
            raise FileNotFoundError(f"draft not found: {draft_id}")
        draft_payload = json.loads(draft_path.read_text(encoding="utf-8"))
        overrides = overrides or {}
        fields = dict(draft_payload.get("fields") or {})
        fields.update(overrides.get("fields") or {})
        if overrides.get("output_mode"):
            draft_payload["output_mode"] = overrides["output_mode"]

        if job_id:
            clean_job_id = "".join(ch for ch in job_id if ch.isalnum() or ch in "-_")[:48]
            if not clean_job_id:
                raise ValueError("invalid job id")
            job_id = clean_job_id
        else:
            job_id = draft_id
            if (self.root / "jobs" / job_id).exists():
                job_id = f"{draft_id}-{uuid.uuid4().hex[:4]}"
        job_dir = self.root / "jobs" / job_id
        job_dir.mkdir(parents=True, exist_ok=True)

        def report_progress(value: int, message: str) -> None:
            self._write_job(job_dir, status="building", progress=value, message=message, draft_id=draft_id)
            if progress:
                progress(value, message)

        self._write_job(job_dir, status="building", progress=1, message="开始构建", draft_id=draft_id)
        try:
            subject = str(draft_payload.get("subject") or "function")
            output_mode = str(draft_payload.get("output_mode") or "")
            if subject == "function":
                artifacts, metadata = self._build_function(job_dir, fields, report_progress)
            elif subject == "robot":
                spec = robot_spec_from_mapping(fields.get("spec") or {})
                if output_mode == "volumetric_model":
                    artifacts, metadata = self._build_robot_volume(job_dir, spec, fields, report_progress)
                else:
                    artifacts, metadata = self._build_robot_relief(job_dir, spec, fields, report_progress)
            elif subject == "mesh":
                artifacts, metadata = self._build_mesh(job_dir, draft_dir, fields, output_mode, report_progress)
            elif subject == "scene":
                artifacts, metadata = self._build_scene(job_dir, fields, report_progress)
            elif subject == "model3d":
                artifacts, metadata = self._build_model3d(job_dir, fields, report_progress)
            elif subject == "reconstruct":
                artifacts, metadata = self._build_reconstruct(
                    job_dir,
                    draft_dir,
                    fields,
                    draft_payload,
                    report_progress,
                    public_base_url=public_base_url,
                )
            else:
                raise ValueError(f"unsupported subject: {subject}")

            report_progress(72, "生成讲解音频")
            script = narration_text(metadata["audio"])
            (job_dir / "audio_script.txt").write_text(script, encoding="utf-8")
            wav_ok, wav_message = synthesize_windows_tts(script, job_dir / "narration.wav")
            metadata["audio"]["wav_ready"] = wav_ok
            metadata["audio"]["tts_message"] = wav_message
            metadata["audio"]["script_file"] = "audio_script.txt"
            metadata["audio"]["wav_file"] = "narration.wav" if wav_ok else None

            # The audio check is only truthful after TTS has run.
            report = metadata["report"]
            if isinstance(report, ComplianceReport):
                report.checks = [check for check in report.checks if check.rule_id != "audio.script"]
                if wav_ok:
                    report.checks.append(Check("audio.script", STATUS_PASS, "wav + script", "讲解脚本和 WAV 均已生成"))
                else:
                    report.checks.append(
                        Check("audio.script", STATUS_REVIEW, "script-only", f"脚本已生成，但 TTS 未成功：{wav_message}")
                    )
                metadata["report_dict"] = report.to_dict()
            else:
                metadata["report_dict"] = report

            report_progress(82, "生成二维码与音频页面")
            audio_url = f"{public_base_url.rstrip('/')}/audio/{job_id}/"
            make_qr_card_svg(audio_url, job_dir / "qr_card.svg", title=str(metadata.get("title") or "TOUCHLEARN"), subtitle="扫描二维码，收听触觉图讲解")
            page = audio_page_html(
                title=str(metadata.get("title") or "TOUCHLEARN"),
                subtitle=str(metadata.get("subtitle") or ""),
                script=script,
                audio_url="narration.wav",
                download_url="narration.wav" if wav_ok else "",
                model_url="model.stl",
                audio_available=wav_ok,
                script_url="audio_script.txt",
            )
            (job_dir / "audio.html").write_text(page, encoding="utf-8")

            report_progress(90, "写入合规报告与清单")
            self._write_compliance(job_dir, metadata)
            self._write_readme(job_dir, metadata)
            manifest = self._manifest(job_dir, draft_payload, fields, metadata, job_id, audio_url)
            _write_json(job_dir / "manifest.json", manifest)
            _write_json(job_dir / "compliance.json", metadata["report_dict"])
            report_progress(100, "完成")
            artifacts = self._artifact_list(job_dir)
            self._write_job(
                job_dir,
                status="done",
                progress=100,
                message="完成",
                draft_id=draft_id,
                artifacts=artifacts,
                mode=metadata["mode"],
                printable=metadata["report_dict"]["printable"],
                standards_clear=metadata["report_dict"]["standards_clear"],
                title=metadata.get("title"),
                audio_url=f"/audio/{job_id}/",
            )
            return {
                "job_id": job_id,
                "status": "done",
                "artifacts": artifacts,
                "manifest": manifest,
                "mode": metadata["mode"],
                "title": metadata.get("title"),
            }
        except Exception as error:
            trace = traceback.format_exc()
            (job_dir / "error.txt").write_text(trace, encoding="utf-8")
            self._write_job(
                job_dir,
                status="failed",
                progress=100,
                message=str(error),
                draft_id=draft_id,
                error=str(error),
            )
            raise

    def _build_function(self, job_dir: Path, fields: dict[str, Any], progress: Progress) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        progress(10, "解析函数与关键点")
        expression_text = str(fields.get("expression") or "").strip()
        expression = parse_expression(expression_text)
        # The build runs the same window resolver as the confirmation preview.
        # In auto mode this deliberately ignores any x/y values carried over
        # from an older expression, so stale numbers can never reach geometry.
        window = self._function_window(expression, fields)
        x_min = window["x_min"]
        x_max = window["x_max"]
        y_min = window["y_min"]
        y_max = window["y_max"]
        points = expression.key_points(x_min, x_max, y_min, y_max, max_points=6)
        letters = _letter_map(points)
        title = "QUADRATIC FUNCTION" if "^2" in expression.canonical else "FUNCTION GRAPH"
        title_zh = "二次函数图像" if "^2" in expression.canonical else "函数图像"
        audio = function_narration(expression.canonical, x_min, x_max, y_min, y_max, points, letters, title_zh=title_zh)

        elements: list[dict[str, Any]] = [
            {
                "id": "curve",
                "kind": "curve",
                "label": "CURVE",
                "label_zh": "曲线",
                "order": 1,
                "semantics": {"expression": expression.canonical, "domain": [x_min, x_max]},
            },
            {
                "id": "x_axis",
                "kind": "axis",
                "label": "X",
                "label_zh": "横轴",
                "order": 2,
                "semantics": {"direction": "horizontal", "min": x_min, "max": x_max},
            },
            {
                "id": "y_axis",
                "kind": "axis",
                "label": "Y",
                "label_zh": "纵轴",
                "order": 3,
                "semantics": {"direction": "vertical", "min": y_min, "max": y_max},
            },
        ]
        relations: list[dict[str, Any]] = []
        for index, (point, letter) in enumerate(zip(points, letters), start=4):
            element_id = f"kp_{index}"
            elements.append(
                {
                    "id": element_id,
                    "kind": "point",
                    "label": letter,
                    "label_zh": point.kind,
                    "order": index,
                    "semantics": {"x": point.x, "y": point.y, "kind": point.kind},
                }
            )
            relations.append({"from": element_id, "to": "curve", "kind": "on_curve", "order": index})
        paper = choose_paper(
            requested=fields.get("paper"),
            blocks=len(elements),
            primitives=len(elements) + 12,
            chars=len(expression.canonical),
        )
        canvas = canvas_for(paper)
        payload = {
            "schema_version": "0.2.0",
            "id": f"function-{_slug(expression.canonical, 'graph')}",
            "title": title,
            "title_zh": title_zh,
            "domain": "math",
            "canvas": canvas,
            "source": {"kind": "expression", "ref": expression.canonical},
            "elements": elements,
            "relations": relations,
            "layout": {
                "engine": "function_plot",
                "options": {
                    "expression": expression.canonical,
                    "x_min": x_min,
                    "x_max": x_max,
                    "y_min": y_min,
                    "y_max": y_max,
                    "auto_window": window["auto_window"],
                    "x_source": window["x_source"],
                    "y_source": window["y_source"],
                },
            },
            "constraints": DEFAULT_CONSTRAINTS,
            "audio": audio,
        }
        _write_json(job_dir / "diagram.json", payload)
        progress(28, "排版触觉图")
        diagram = diagram_from_dict(payload)
        layout = build_layout(diagram)
        progress(45, "生成封闭网格")
        mesh = build_mesh(layout)
        write_binary_stl(mesh, job_dir / "model.stl")
        write_svg(layout, job_dir / "preview.svg")
        _write_json(job_dir / "decision_trace.json", layout.trace + [{"stage": "layout", "action": "summary", "decision": layout.summary()}])
        report = check_plate(layout, mesh, audio_ready=False)
        return [], {
            "mode": "tactile_plate",
            "title": title,
            "subtitle": f"{expression.canonical}  /  {paper} TACTILE PLATE",
            "audio": audio,
            "report": report,
            "report_dict": report.to_dict(),
            "paper": paper,
            "canvas": canvas,
            "geometry": {
                "kind": "tactile_plate",
                "diagram": payload,
                "layout_summary": layout.summary(),
                "mesh": mesh.stats(),
                "warnings": layout.warnings,
            },
            "print": self._print_settings("tactile_plate", mesh, paper=paper),
        }

    def _build_scene(
        self,
        job_dir: Path,
        fields: dict[str, Any],
        progress: Progress,
        *,
        prefix: str = "",
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        progress(10, "读取并校验场景")
        # The semantic circuit is the source of truth when present: recompiling
        # here guarantees the printed plate matches what the editor showed,
        # even if an older scene snapshot was passed in overrides.
        circuit: dict[str, Any] | None = None
        circuit_warnings: list[str] = []
        raw_circuit = fields.get("circuit")
        if isinstance(raw_circuit, str):
            try:
                raw_circuit = json.loads(raw_circuit)
            except json.JSONDecodeError:
                raw_circuit = None
        if isinstance(raw_circuit, dict):
            try:
                circuit, circuit_warnings = normalize_circuit(raw_circuit)
                fields = {**fields, "scene": circuit_to_scene(circuit)}
            except CircuitError as error:
                raise ValueError(f"电路无效：{error}") from error
        try:
            scene, warnings = normalize_scene(fields.get("scene"))
        except SceneError as error:
            raise ValueError(f"场景无效：{error}") from error
        warnings = [*circuit_warnings, *warnings]
        if circuit is not None:
            _write_json(job_dir / "circuit.json", circuit)

        elements: list[dict[str, Any]] = []
        relations: list[dict[str, Any]] = []
        previous: str | None = None
        for index, block in enumerate(scene["blocks"], start=1):
            element_id = str(block["id"])
            elements.append(
                {
                    "id": element_id,
                    "kind": "scene",
                    "label": str(block.get("label") or element_id.upper()),
                    "label_zh": str(block.get("label_zh") or block.get("label") or element_id),
                    "order": index,
                    "semantics": {
                        "original_kind": block.get("kind") or "scene",
                        "note": block.get("note") or "",
                        "primitive_count": len(block.get("primitives") or []),
                    },
                }
            )
            if previous is not None:
                relations.append({"from": previous, "to": element_id, "kind": "reading_order", "order": index})
            previous = element_id

        summary = scene_summary(scene)
        paper = choose_paper(
            # A roadmap asks for a larger sheet when its braille rows would sit
            # closer than the readable minimum pitch; a user choice still wins.
            requested=fields.get("paper") or scene.get("paper_hint") or "",
            blocks=summary["block_count"],
            primitives=summary["primitive_count"],
            chars=len(str(scene.get("title_zh") or "")) + sum(len(str(block.get("note") or "")) for block in scene["blocks"]),
        )
        canvas = canvas_for(paper)
        title = scene["title"]
        title_zh = scene.get("title_zh") or title
        subtitle = scene.get("subtitle") or f"{paper} TACTILE SCENE / {len(scene['blocks'])} BLOCKS"
        audio = scene_narration(scene)
        payload = {
            "schema_version": "0.2.0",
            "id": f"scene-{_slug(str(fields.get('template') or title), 'diagram')}",
            "title": title,
            "title_zh": title_zh,
            "domain": "scene",
            "canvas": canvas,
            "source": {"kind": "scene", "ref": str(fields.get("source_text") or fields.get("template") or "user request")[:200]},
            "elements": elements,
            "relations": relations,
            "layout": {"engine": "scene", "options": {"scene": scene}},
            "constraints": DEFAULT_CONSTRAINTS,
            "audio": audio,
        }
        _write_json(job_dir / f"{prefix}diagram.json", payload)
        progress(28, "排版场景图块")
        diagram = diagram_from_dict(payload)
        layout = build_layout(diagram)
        progress(45, "生成封闭网格")
        mesh = build_mesh(layout)
        write_binary_stl(mesh, job_dir / f"{prefix}model.stl")
        write_svg(layout, job_dir / f"{prefix}preview.svg")
        _write_json(
            job_dir / f"{prefix}decision_trace.json",
            layout.trace + [{"stage": "layout", "action": "summary", "decision": layout.summary()}],
        )
        report = check_plate(layout, mesh, audio_ready=False)
        return [], {
            "mode": "tactile_plate",
            "title": title,
            "subtitle": subtitle,
            "audio": audio,
            "report": report,
            "report_dict": report.to_dict(),
            "paper": paper,
            "canvas": canvas,
            "geometry": {
                "kind": "tactile_plate",
                "diagram": payload,
                "circuit": circuit,
                "scene_summary": summary,
                "layout_summary": layout.summary(),
                "mesh": mesh.stats(),
                "warnings": warnings + list(layout.warnings),
            },
            "print": self._print_settings("tactile_plate", mesh, paper=paper),
        }

    def _build_robot_relief(self, job_dir: Path, spec: RobotSpec, fields: dict[str, Any], progress: Progress) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        progress(12, "构建机械臂运动链")
        elements: list[dict[str, Any]] = [
            {"id": "BASE", "kind": "link", "label": "BASE", "label_zh": "基座", "order": 1, "semantics": {"length_mm": spec.base_width, "width_mm": spec.base_height}},
        ]
        relations: list[dict[str, Any]] = []
        order = 2
        previous = "BASE"
        for index in range(spec.dof):
            joint_id = f"J{index + 1}"
            link_id = f"L{index + 1}"
            elements.append(
                {
                    "id": joint_id,
                    "kind": "joint",
                    "label": joint_id,
                    "label_zh": f"第{index + 1}关节",
                    "order": order,
                    "semantics": {"radius_mm": spec.joint_radii[index], "axis": "horizontal"},
                }
            )
            relations.append({"from": previous, "to": joint_id, "kind": "joint", "joint": joint_id, "order": order})
            order += 1
            elements.append(
                {
                    "id": link_id,
                    "kind": "link",
                    "label": link_id,
                    "label_zh": f"第{index + 1}连杆",
                    "order": order,
                    "semantics": {"length_mm": spec.link_lengths[index], "width_mm": spec.link_widths[index]},
                }
            )
            relations.append({"from": joint_id, "to": link_id, "kind": "connect", "order": order})
            order += 1
            previous = link_id
        elements.append(
            {
                "id": "EE",
                "kind": "end_effector",
                "label": "EE",
                "label_zh": "末端执行器",
                "order": order,
                "semantics": {"type": spec.end_effector},
            }
        )
        relations.append({"from": previous, "to": "EE", "kind": "connect", "order": order})
        title = f"{spec.dof} AXIS ROBOT ARM"
        title_zh = f"{spec.dof}轴机械臂触觉示意图"
        audio = robot_relief_narration(spec)
        paper = choose_paper(
            requested=fields.get("paper"),
            dof=spec.dof,
            blocks=len(elements),
            primitives=len(elements) * 2 + 10,
            chars=len(str(fields.get("description") or "")),
        )
        canvas = canvas_for(paper)
        payload = {
            "schema_version": "0.2.0",
            "id": f"robot-{spec.dof}-axis",
            "title": title,
            "title_zh": title_zh,
            "domain": "robotics",
            "canvas": canvas,
            "source": {"kind": "text", "ref": str(fields.get("description") or "robot spec")},
            "elements": elements,
            "relations": relations,
            "layout": {"engine": "robot_relief", "options": {"robot": spec.to_dict()}},
            "constraints": DEFAULT_CONSTRAINTS,
            "audio": audio,
        }
        _write_json(job_dir / "diagram.json", payload)
        progress(28, "排版侧视触觉图")
        diagram = diagram_from_dict(payload)
        layout = build_layout(diagram)
        progress(45, "生成封闭网格")
        mesh = build_mesh(layout)
        write_binary_stl(mesh, job_dir / "model.stl")
        write_svg(layout, job_dir / "preview.svg")
        _write_json(job_dir / "decision_trace.json", layout.trace + [{"stage": "layout", "action": "summary", "decision": layout.summary()}])
        report = check_plate(layout, mesh, audio_ready=False)
        return [], {
            "mode": "tactile_plate",
            "title": title,
            "subtitle": f"{spec.dof} DOF / {spec.end_effector.upper()} / {paper} TACTILE PLATE",
            "audio": audio,
            "report": report,
            "report_dict": report.to_dict(),
            "paper": paper,
            "canvas": canvas,
            "geometry": {
                "kind": "tactile_plate",
                "diagram": payload,
                "layout_summary": layout.summary(),
                "mesh": mesh.stats(),
                "warnings": layout.warnings,
            },
            "print": self._print_settings("tactile_plate", mesh, paper=paper),
        }

    def _build_robot_volume(self, job_dir: Path, spec: RobotSpec, fields: dict[str, Any], progress: Progress) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        progress(15, "构建三维机械臂")
        target_height = float(fields.get("target_height_mm") or 135.0)
        result = build_robot_volume(spec, target_height_mm=target_height)
        progress(42, "写入 STL 与预览")
        write_binary_stl(result.mesh, job_dir / "model.stl")
        mesh_preview_svg(result.mesh, job_dir / "preview.svg", title=f"{spec.dof} AXIS ROBOT ARM", subtitle="VOLUMETRIC MODEL / PEDESTAL + QR PAD")
        _write_json(job_dir / "decision_trace.json", [{"stage": "volumetric", "action": "build_robot_volume", "decision": result.meta}])
        report = check_volumetric(result.mesh, qr_area_mm=34.0)
        report.checks.append(Check("encoding.chinese_braille", STATUS_REVIEW, "UEB + Arabic numerals", "底座铭牌使用 UEB 英文；中文盲文模块尚未接入。"))
        size = result.meta["size_mm"]
        audio = robot_volume_narration(spec, size)
        return [], {
            "mode": "volumetric_model",
            "title": f"{spec.dof} AXIS ROBOT ARM",
            "subtitle": "VOLUMETRIC MODEL / SCALED DISPLAY PIECE",
            "audio": audio,
            "report": report,
            "report_dict": report.to_dict(),
            "geometry": {
                "kind": "volumetric_model",
                "meta": result.meta,
                "mesh": mesh_health(result.mesh),
                "spec": spec.to_dict(),
            },
            "print": self._print_settings("volumetric_model", result.mesh),
        }

    def _build_mesh(self, job_dir: Path, draft_dir: Path, fields: dict[str, Any], output_mode: str, progress: Progress) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        progress(12, "读取三维文件")
        mesh_path = draft_dir / str(fields.get("mesh_path") or "")
        if not mesh_path.exists():
            raise FileNotFoundError(f"上传的三维文件不存在：{fields.get('mesh_name')}")
        mesh = load_mesh(mesh_path)
        if output_mode == "silhouette_relief":
            progress(30, "投影为轮廓浮雕")
            result = mesh_relief(mesh, resolution_mm=float(fields.get("resolution_mm") or 1.0))
            report = check_relief(
                result.mesh,
                margin_mm=float(result.meta.get("margin_mm") or 6.0),
                base_thickness_mm=float(result.meta.get("base_thickness_mm") or 3.0),
                relief_height_mm=float(result.meta.get("relief_height_mm") or 2.4),
                qr_area_mm=float(result.meta.get("qr_area_mm") or 30.0),
            )
            title = "3D RELIEF"
            subtitle = "SILHOUETTE PROJECTION / OUTLINE ONLY"
            audio = mesh_narration(result.meta, "silhouette_relief")
        else:
            progress(30, "缩放并居中")
            result = normalize_mesh(mesh, target_height_mm=float(fields.get("target_height_mm") or 120.0))
            report = check_volumetric(result.mesh, qr_area_mm=None)
            title = "IMPORTED 3D MODEL"
            subtitle = "SCALED TO TARGET HEIGHT / CHECK SUPPORTS"
            audio = mesh_narration(result.meta, "volumetric_model")
        progress(55, "写入 STL 与预览")
        write_binary_stl(result.mesh, job_dir / "model.stl")
        mesh_preview_svg(result.mesh, job_dir / "preview.svg", title=title, subtitle=subtitle)
        _write_json(job_dir / "decision_trace.json", [{"stage": "mesh", "action": output_mode, "decision": result.meta}])
        return [], {
            "mode": output_mode,
            "title": title,
            "subtitle": subtitle,
            "audio": audio,
            "report": report,
            "report_dict": report.to_dict(),
            "geometry": {"kind": output_mode, "meta": result.meta, "mesh": mesh_health(result.mesh)},
            "print": self._print_settings(output_mode, result.mesh),
        }

    def _build_model3d(self, job_dir: Path, fields: dict[str, Any], progress: Progress) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        provider_generation = fields.get("provider_generation")
        if isinstance(provider_generation, dict) and provider_generation.get("enabled"):
            return self._build_generated_model3d(job_dir, fields, progress)

        progress(8, "校验三维模型规格")
        spec = fields.get("spec")
        if not isinstance(spec, dict) or not spec:
            raise ValueError("缺少三维模型规格；请重新理解需求后再生成。")
        spec = json.loads(json.dumps(spec))
        target_height = fields.get("target_height_mm")
        try:
            target_value = float(target_height) if target_height not in {None, ""} else None
        except (TypeError, ValueError):
            target_value = None

        progress(22, "生成底座、支架与曲面盲文标注")
        result = build_model3d(spec, target_height_mm=target_value)
        meta = result.meta
        title = str(meta.get("title") or spec.get("title") or "TACTILE 3D MODEL")
        title_zh = str(meta.get("title_zh") or spec.get("title_zh") or "触觉三维模型")
        if meta.get("kind") == "solar_system":
            subtitle = f"{meta.get('planet_count', 0)} PLANETS / BRAILLE RIM LEGEND / DISPLAY BASE"
        else:
            subtitle = f"{len(meta.get('parts') or [])} PARTS / SURFACE BRAILLE + BASE LEGEND"

        progress(44, "写入 STL 与三维预览")
        write_binary_stl(result.mesh, job_dir / "model.stl")
        mesh_preview_svg(
            result.mesh,
            job_dir / "preview.svg",
            title=title,
            subtitle=subtitle,
            width_mm=210.0,
            height_mm=148.0,
        )
        _write_json(job_dir / "model_spec.json", spec)
        _write_json(
            job_dir / "decision_trace.json",
            [
                {
                    "stage": "model3d",
                    "action": "deterministic_compile",
                    "decision": {
                        "kind": meta.get("kind"),
                        "size_mm": meta.get("size_mm"),
                        "watertight": meta.get("watertight"),
                        "triangles": meta.get("triangles"),
                    },
                }
            ],
        )

        progress(58, "检查打印几何与盲文标注")
        report = check_volumetric(result.mesh, qr_area_mm=float(meta.get("qr_pad_mm") or 32.0))
        labels = (meta.get("braille") or {}).get("labels") or []
        label_count = len(labels)
        report.checks.append(
            Check(
                "model.kind",
                STATUS_PASS,
                str(meta.get("kind") or "assembly"),
                "底座 + 支架 + 可触摸主体的确定性三维装配模型。",
            )
        )
        if label_count:
            report.checks.append(
                Check(
                    "encoding.chinese_braille",
                    STATUS_PASS,
                    f"GF 0019-2018 / {label_count} labels / 1.6 mm dots",
                    "中文盲文按 GF 0019-2018 二级标调编码；点径 1.6 mm，点高 0.8 mm，优先刻在部件表面（顶面、侧壁、球冠或轮廓侧面），放不下时按编号刻在底座图例上。",
                )
            )
        else:
            report.checks.append(
                Check(
                    "encoding.chinese_braille",
                    STATUS_REVIEW,
                    "no labels",
                    "模型没有中文盲文标注；请补充 2-4 个字的中文名称。",
                )
            )
        report.checks.append(
            Check(
                "model.braille_reachability",
                STATUS_PASS,
                "surface labels + base legend",
                "部件名称优先直接刻在可触摸表面；行星和过小部件按编号或轨道顺序刻在底座表面，不生成竖立名牌。",
            )
        )
        if meta.get("kind") == "solar_system":
            report.checks.append(
                Check(
                    "model.scale",
                    STATUS_DEVIATION,
                    "compressed radii",
                    "行星半径保持真实大小顺序但经过压缩，轨道只表示相对顺序；不是等比例太阳系。",
                )
            )
        else:
            report.checks.append(
                Check(
                    "model.scale",
                    STATUS_REVIEW,
                    "relative sizes",
                    "部件大小为相对比例，不是工程尺寸；需要工程尺寸时应提供具体毫米值并由人工复核。",
                )
            )

        companion_meta: dict[str, Any] | None = None
        companion_files: list[str] = []
        if fields.get("companion_tactile"):
            progress(64, "生成配套触觉总览板")
            scene = companion_scene(spec, meta)
            paper = choose_paper(
                blocks=len(scene.get("blocks") or []),
                primitives=sum(len(block.get("primitives") or []) for block in scene.get("blocks") or []),
                chars=sum(len(str(block.get("label") or "")) + len(str(block.get("label_zh") or "")) for block in scene.get("blocks") or []),
            )

            def companion_progress(value: int, message: str) -> None:
                progress(64 + int(value * 0.18), f"配套触觉板：{message}")

            _, companion = self._build_scene(
                job_dir,
                {"scene": scene, "paper": paper, "source_text": fields.get("source_text")},
                companion_progress,
                prefix="plate_",
            )
            companion_meta = {
                "paper": companion.get("paper"),
                "canvas": companion.get("canvas"),
                "scene": companion.get("geometry", {}).get("scene_summary"),
                "mesh": companion.get("geometry", {}).get("mesh"),
                "printable": companion.get("report_dict", {}).get("printable"),
                "files": ["plate_model.stl", "plate_preview.svg", "plate_diagram.json"],
            }
            companion_files = ["plate_model.stl", "plate_preview.svg", "plate_diagram.json"]

        audio = model3d_narration(spec, meta)
        size = meta.get("size_mm") or [0, 0, 0]
        progress(86, "整理三维模型说明")
        return companion_files, {
            "mode": "model3d",
            "title": title,
            "title_zh": title_zh,
            "subtitle": subtitle,
            "audio": audio,
            "report": report,
            "report_dict": report.to_dict(),
            "geometry": {
                "kind": "model3d",
                "spec": spec,
                "meta": meta,
                "mesh": mesh_health(result.mesh),
                "size_mm": size,
                "companion": companion_meta,
            },
            "paper": "3D",
            "print": self._print_settings("model3d", result.mesh),
        }

    def _build_generated_model3d(
        self,
        job_dir: Path,
        fields: dict[str, Any],
        progress: Progress,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Generate one complex mesh, then apply the deterministic tactile layer.

        The provider is responsible only for the object.  Title, base, support
        plinth, front direction, braille, QR pad and print checks are all added
        here so every generated model is annotated the same way.
        """
        info = fields.get("provider_generation") if isinstance(fields.get("provider_generation"), dict) else {}
        prompt = str(info.get("prompt") or fields.get("source_text") or "").strip()
        if not prompt:
            raise ValueError("缺少三维生成提示词；请重新理解需求后再生成。")

        title = _short_title(str(fields.get("title") or info.get("prompt") or "模型"))
        try:
            height = float(fields.get("height_mm") or 120.0)
        except (TypeError, ValueError):
            height = 120.0
        height = max(60.0, min(200.0, height))
        try:
            margin = float(fields.get("margin_mm") or 10.0)
        except (TypeError, ValueError):
            margin = 10.0
        base_shape = str(fields.get("base") or "disc")
        if base_shape not in {"disc", "rounded_rect"}:
            base_shape = "disc"
        labels = [
            {"text": str(item.get("text") or "")[:3], "target": str(item.get("target") or "")}
            for item in (fields.get("labels") or [])
            if isinstance(item, dict) and str(item.get("text") or "").strip()
        ][:3]
        if not labels:
            labels = [{"text": title, "target": "主体"}]
        _validate_print_labels(title, labels)

        plan = ReconstructionPlan(
            ok=True,
            title=title,
            subject=str(fields.get("source_text") or "")[:160],
            height_mm=height,
            base=base_shape,
            margin_mm=max(4.0, min(20.0, margin)),
            labels=labels,
            provider=str(info.get("provider") or ""),
            model=str(info.get("model") or ""),
            message="由三维生成模型输出完整网格，本地统一添加标题、底座与方向适配。",
        )

        def provider_progress(value: int, message: str) -> None:
            progress(8 + int(max(0, min(100, value)) * 0.56), message)

        progress(6, "提交文字描述到三维生成服务")
        asset = text_to_3d(prompt, plan, dest=job_dir, progress=provider_progress)
        if asset.mesh is None or not asset.mesh.triangles:
            detail = asset.mesh_error or "三维服务没有返回可解析的三角网格。"
            raise ReconstructionError(
                f"{detail} 纹理 GLB 已保存，但无法生成可打印 STL；请换一个描述重试，"
                "或改用“照片级 3D 重建”。"
            )

        progress(66, "生成底座、盲文标题与方向适配")
        result = compose_printable(asset.mesh, plan, target_height_mm=plan.height_mm, title=plan.title)
        meta = result.meta
        subtitle = f"GENERATIVE 3D / {plan.subject or plan.title} / BRAILLE TITLE + BASE LEGEND"

        progress(74, "写入 STL 与三维预览")
        write_binary_stl(result.mesh, job_dir / "model.stl")
        mesh_preview_svg(
            result.mesh,
            job_dir / "preview.svg",
            title=title,
            subtitle=subtitle,
            width_mm=210.0,
            height_mm=148.0,
        )
        print_settings = self._print_settings("model3d", result.mesh)
        _write_json(
            job_dir / "model_spec.json",
            {
                "kind": "generative_model3d",
                "plan": plan.to_dict(),
                "prompt": prompt,
                "print_meta": meta,
                "provider": asset.to_dict(),
            },
        )
        view_html = self._photo_view_html(title, plan, asset, kind="generated")
        (job_dir / "model_view.html").write_text(view_html, encoding="utf-8")
        _write_json(
            job_dir / "generation.json",
            {
                "schema_version": "1.0",
                "mode": "text_to_3d",
                "provider": asset.provider,
                "task_id": asset.task_id,
                "model": plan.model,
                "prompt": prompt,
                "plan": plan.to_dict(),
                "height_mm": plan.height_mm,
                "base": plan.base,
                "margin_mm": plan.margin_mm,
                "labels": plan.labels,
                "textured_model": "model_textured.glb" if "glb" in asset.files else "",
                "thumbnail": "thumbnail.png" if "thumbnail" in asset.files else "",
                "print": print_settings,
                "print_meta": meta,
                "source_mesh": mesh_health(asset.mesh),
                "provider_meta": asset.meta,
                "instructions": {
                    "keep_features": plan.keep_features,
                    "note": "生成模型只负责外观与细节；标题、底座、方向、盲文、二维码位由本地确定性后处理完成。",
                },
            },
        )
        _write_json(
            job_dir / "decision_trace.json",
            [
                {
                    "stage": "model3d",
                    "action": "provider_generation",
                    "decision": {
                        "provider": asset.provider,
                        "task_id": asset.task_id,
                        "model": plan.model,
                        "quality": info.get("quality"),
                        "source_triangles": asset.mesh.triangle_count(),
                        "target_height_mm": plan.height_mm,
                    },
                },
                {
                    "stage": "model3d",
                    "action": "deterministic_tactile_composition",
                    "decision": {
                        "strategy": meta.get("strategy"),
                        "watertight": meta.get("watertight"),
                        "triangles": meta.get("triangles"),
                        "size_mm": meta.get("size_mm"),
                        "front": "title_plaque_and_qr_on_base_front_tab",
                    },
                },
            ],
        )

        progress(82, "检查打印几何、盲文与生成来源")
        report = check_volumetric(result.mesh, qr_area_mm=float(meta.get("qr_pad_mm") or 30.0))
        softened: list[Check] = []
        for check in report.checks:
            if check.rule_id == "encoding.chinese_braille":
                continue
            if check.rule_id in {"mesh.watertight", "mesh.min_feature"} and check.status == STATUS_FAIL:
                softened.append(
                    Check(
                        check.rule_id,
                        STATUS_REVIEW,
                        check.measured,
                        "AI 生成网格可能存在开放边或退化面；切片前请用网格修复工具检查，再决定是否打印。",
                    )
                )
            else:
                softened.append(check)
        report.checks = softened
        report.checks.append(
            Check(
                "generation.provider",
                STATUS_PASS,
                f"{asset.provider} / {asset.task_id[:12]}",
                "文字由三维生成模型还原为一个完整网格，不是基础几何体拼装；生成过程不伪造照片里不存在的测量数据。",
            )
        )
        report.checks.append(
            Check(
                "generation.texture",
                STATUS_PASS if "glb" in asset.files else STATUS_REVIEW,
                "GLB + STL" if "glb" in asset.files else "STL only",
                "GLB 保留生成的颜色与材质；STL 为单色打印，不携带纹理。",
            )
        )
        report.checks.append(
            Check(
                "generation.mesh_repair",
                STATUS_PASS if meta.get("watertight") else STATUS_REVIEW,
                "watertight" if meta.get("watertight") else "repair recommended",
                "生成网格与底座、盲文图例合并后若仍有开放边，切片前需要修复；报告不会把未验证的网格标成完全合规。",
            )
        )
        report.checks.append(
            Check(
                "generation.orientation",
                STATUS_PASS,
                "front = title plaque + QR pad",
                "正面由本地底座定义：标题牌和二维码位所在的一侧为前方，读图与摆放方向一致；不旋转未知朝向的主体。",
            )
        )
        report.checks.append(
            Check(
                "generation.scale",
                STATUS_DEVIATION,
                f"target {plan.height_mm:.1f} mm",
                "生成式网格不是测量数据；高度按用户目标缩放，不能当作真实尺寸。",
            )
        )
        label_count = len(meta.get("braille", {}).get("labels") or [])
        report.checks.append(
            Check(
                "encoding.chinese_braille",
                STATUS_PASS,
                f"GF 0019-2018 / title + {label_count} base labels",
                "中文盲文按 GF 0019-2018 编码；标题在正面名牌，特征名按编号刻在底座前缘的盲文图例上，不在生成网格表面刻猜出来的名字。",
            )
        )

        size = meta.get("size_mm") or [0, 0, 0]
        audio = {
            "intro": f"这是一个由三维生成模型制作的{plan.subject or plan.title}立体模型。",
            "steps": [
                "先从正面底座摸起，正面有一块标题牌和一块方形二维码位。",
                f"标题是{plan.title}，用中文盲文刻在标题牌上。",
                "沿底座前缘摸到编号盲文图例，可以读出各个特征的名称。",
                "再向上摸主体，感受生成模型还原出的轮廓、转折和表面起伏。",
                f"主体高约 {plan.height_mm:.0f} 毫米，连同底座总高约 {float(size[2]):.0f} 毫米。",
            ],
            "outro": "这是生成式展示模型，不是测量件；单色打印不保留颜色，颜色和纹理请看带纹理的 GLB。",
        }
        return [], {
            "mode": "model3d",
            "title": title,
            "title_zh": plan.title,
            "subtitle": subtitle,
            "audio": audio,
            "report": report,
            "report_dict": report.to_dict(),
            "geometry": {
                "kind": "generative_model3d",
                "plan": plan.to_dict(),
                "provider": asset.to_dict(),
                "prompt": prompt,
                "meta": meta,
                "mesh": mesh_health(result.mesh),
                "source_mesh": mesh_health(asset.mesh),
                "size_mm": size,
            },
            "paper": "3D",
            "print": print_settings,
        }

    def _build_reconstruct(
        self,
        job_dir: Path,
        draft_dir: Path,
        fields: dict[str, Any],
        draft_payload: dict[str, Any],
        progress: Progress,
        *,
        public_base_url: str = "",
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        progress(6, "读取重建计划")
        raw_plan = fields.get("plan") if isinstance(fields.get("plan"), dict) else {}
        try:
            height = float(fields.get("height_mm") or raw_plan.get("height_mm") or 120.0)
        except (TypeError, ValueError):
            height = 120.0
        height = max(60.0, min(200.0, height))
        try:
            margin = float(fields.get("margin_mm") or raw_plan.get("margin_mm") or 10.0)
        except (TypeError, ValueError):
            margin = 10.0
        base_shape = str(fields.get("base") or raw_plan.get("base") or "disc")
        if base_shape not in {"disc", "rounded_rect"}:
            base_shape = "disc"
        plan = ReconstructionPlan(
            ok=True,
            title=str(fields.get("title") or raw_plan.get("title") or "模型")[:6],
            subject=str(fields.get("subject_text") or raw_plan.get("subject") or ""),
            keep_features=[str(item) for item in (fields.get("keep_features") or raw_plan.get("keep_features") or [])][:6],
            changes=[str(item) for item in (fields.get("changes") or raw_plan.get("changes") or [])][:6],
            height_mm=height,
            base=base_shape,
            margin_mm=max(4.0, min(20.0, margin)),
            labels=[
                {"text": str(item.get("text") or "")[:3], "target": str(item.get("target") or "")}
                for item in (fields.get("labels") or raw_plan.get("labels") or [])
                if isinstance(item, dict) and str(item.get("text") or "").strip()
            ][:3],
            print_notes=[str(item) for item in (fields.get("print_notes") or raw_plan.get("print_notes") or [])][:4],
        )
        if not plan.labels:
            plan.labels = [{"text": plan.title, "target": "主体"}]
        _validate_print_labels(plan.title, plan.labels)

        extensions = {".png", ".jpg", ".jpeg", ".webp"}
        images = [
            draft_dir / str(item["path"])
            for item in (draft_payload.get("files") or [])
            if str(item.get("extension") or "").lower() in extensions
        ]
        images = [path for path in images if path.is_file()]
        if not images:
            raise ReconstructionError("没有收到可用于重建的照片。请上传 PNG/JPEG/WEBP 照片。")

        def provider_progress(value: int, message: str) -> None:
            progress(10 + int(max(0, min(100, value)) * 0.50), message)

        # TokenHub's Tripo examples pass HTTPS image URLs.  When this deployment
        # has a real public address, publish the first four photos through the
        # existing unguessable /files/<job>/<name> route and hand those URLs to
        # the provider.  On a local-only preview we keep the data-URI fallback.
        public_base = _public_http_base(public_base_url)
        image_urls: list[str] = []
        published: list[Path] = []
        if public_base:
            for index, path in enumerate(images[:4]):
                suffix = path.suffix.lower() if path.suffix.lower() in extensions else ".jpg"
                name = f"input-{index + 1}{suffix}"
                target = job_dir / name
                if path.resolve() != target.resolve():
                    shutil.copyfile(path, target)
                published.append(target)
                image_urls.append(f"{public_base}/files/{job_dir.name}/{name}")

        progress(10, "提交照片到三维重建服务")
        try:
            asset = reconstruct(
                images,
                plan,
                dest=job_dir,
                progress=provider_progress,
                image_urls=image_urls,
            )
        finally:
            # The provider has fetched the images by the time reconstruction
            # returns; do not ship the learner's photos inside the artifact zip.
            for target in published:
                try:
                    target.unlink()
                except OSError:
                    pass
        if asset.mesh is None or not asset.mesh.triangles:
            detail = asset.mesh_error or "三维服务没有返回可解析的三角网格。"
            raise ReconstructionError(
                f"{detail} 纹理 GLB 已保存，但无法生成可打印 STL；请换一组照片重试，或改用“触觉3D模型”。"
            )

        progress(62, "生成底座、盲文标题与底座图例")
        result = compose_printable(asset.mesh, plan, target_height_mm=plan.height_mm, title=plan.title)
        meta = result.meta
        title = str(meta.get("title") or plan.title or "PHOTO 3D")
        subtitle = f"PHOTO RECONSTRUCTION / {plan.subject or title} / BRAILLE TITLE + BASE LEGEND"

        progress(70, "写入 STL 与三维预览")
        write_binary_stl(result.mesh, job_dir / "model.stl")
        mesh_preview_svg(
            result.mesh,
            job_dir / "preview.svg",
            title=title,
            subtitle=subtitle,
            width_mm=210.0,
            height_mm=148.0,
        )
        print_settings = self._print_settings("reconstruction", result.mesh)
        _write_json(
            job_dir / "model_spec.json",
            {
                "kind": "reconstruction",
                "plan": plan.to_dict(),
                "print_meta": meta,
                "provider": asset.to_dict(),
            },
        )

        photo_html = self._photo_view_html(title, plan, asset)
        (job_dir / "photo_view.html").write_text(photo_html, encoding="utf-8")
        _write_json(
            job_dir / "reconstruction.json",
            {
                "schema_version": "1.0",
                "provider": asset.provider,
                "task_id": asset.task_id,
                "plan": plan.to_dict(),
                "height_mm": plan.height_mm,
                "base": plan.base,
                "margin_mm": plan.margin_mm,
                "labels": plan.labels,
                "textured_model": "model_textured.glb" if "glb" in asset.files else "",
                "thumbnail": "thumbnail.png" if "thumbnail" in asset.files else "",
                "print": print_settings,
                "print_meta": meta,
                "source_mesh": mesh_health(asset.mesh),
                "provider_meta": asset.meta,
                "instructions": {
                    "keep_features": plan.keep_features,
                    "changes": plan.changes,
                    "note": "几何编辑由三维服务执行；本地确定高度、底座、盲文与二维码位。",
                },
            },
        )
        _write_json(
            job_dir / "decision_trace.json",
            [
                {
                    "stage": "reconstruct",
                    "action": "provider_reconstruction",
                    "decision": {
                        "provider": asset.provider,
                        "task_id": asset.task_id,
                        "source_triangles": asset.mesh.triangle_count(),
                        "target_height_mm": plan.height_mm,
                        "base": plan.base,
                        "labels": [item["text"] for item in plan.labels],
                    },
                },
                {
                    "stage": "reconstruct",
                    "action": "deterministic_print_composition",
                    "decision": {
                        "strategy": meta.get("strategy"),
                        "watertight": meta.get("watertight"),
                        "triangles": meta.get("triangles"),
                        "size_mm": meta.get("size_mm"),
                    },
                },
            ],
        )

        progress(78, "检查打印几何、盲文与重建来源")
        report = check_volumetric(result.mesh, qr_area_mm=float(meta.get("qr_pad_mm") or 30.0))
        softened: list[Check] = []
        for check in report.checks:
            if check.rule_id == "encoding.chinese_braille":
                continue
            if check.rule_id in {"mesh.watertight", "mesh.min_feature"} and check.status == STATUS_FAIL:
                softened.append(
                    Check(
                        check.rule_id,
                        STATUS_REVIEW,
                        check.measured,
                        "AI 重建网格可能存在开放边或退化面；切片前请用网格修复工具检查，再决定是否打印。",
                    )
                )
            else:
                softened.append(check)
        report.checks = softened
        report.checks.append(
            Check(
                "reconstruction.provider",
                STATUS_PASS,
                f"{asset.provider} / {asset.task_id[:12]}",
                "照片由专门的三维重建服务还原为带纹理的 GLB；本地不伪造照片里看不到的结构。",
            )
        )
        report.checks.append(
            Check(
                "reconstruction.texture",
                STATUS_PASS if "glb" in asset.files else STATUS_REVIEW,
                "GLB + STL" if "glb" in asset.files else "STL only",
                "GLB 保留照片颜色与纹理；STL 为单色打印，不携带纹理。",
            )
        )
        report.checks.append(
            Check(
                "reconstruction.mesh_repair",
                STATUS_PASS if meta.get("watertight") else STATUS_REVIEW,
                "watertight" if meta.get("watertight") else "repair recommended",
                "重建网格与底座、盲文图例合并后若仍有开放边，切片前需要修复；报告不会把未验证的网格标成完全合规。",
            )
        )
        report.checks.append(
            Check(
                "reconstruction.instructions",
                STATUS_REVIEW if plan.changes else STATUS_PASS,
                " / ".join(plan.changes)[:120] if plan.changes else "photo only",
                "文字指令已写入重建计划并尽量传给三维服务；改变姿态等几何编辑取决于服务能力，服务不支持时保留照片原样。",
            )
        )
        report.checks.append(
            Check(
                "reconstruction.scale",
                STATUS_DEVIATION,
                f"target {plan.height_mm:.1f} mm",
                "AI 重建网格不是测量数据；高度按用户目标缩放，不能当作真实尺寸。",
            )
        )
        label_count = len(meta.get("braille", {}).get("labels") or [])
        report.checks.append(
            Check(
                "encoding.chinese_braille",
                STATUS_PASS,
                f"GF 0019-2018 / title + {label_count} feature labels",
                "中文盲文按 GF 0019-2018 编码；标题在正面名牌，特征名按编号刻在底座前缘的盲文图例上。重建网格没有可靠的特征坐标，所以不在模型表面刻猜出来的名字，也不生成竖立名牌。",
            )
        )

        companion_meta: dict[str, Any] | None = None
        companion_files: list[str] = []
        if fields.get("companion_tactile"):
            progress(84, "生成配套触觉总览板")
            scene = self._reconstruction_companion(plan)
            paper = choose_paper(
                blocks=len(scene.get("blocks") or []),
                primitives=sum(len(block.get("primitives") or []) for block in scene.get("blocks") or []),
                chars=sum(len(str(block.get("label") or "")) + len(str(block.get("label_zh") or "")) for block in scene.get("blocks") or []),
            )

            def companion_progress(value: int, message: str) -> None:
                progress(84 + int(value * 0.08), f"配套触觉板：{message}")

            _, companion = self._build_scene(
                job_dir,
                {"scene": scene, "paper": paper, "source_text": fields.get("source_text")},
                companion_progress,
                prefix="plate_",
            )
            companion_meta = {
                "paper": companion.get("paper"),
                "canvas": companion.get("canvas"),
                "scene": companion.get("geometry", {}).get("scene_summary"),
                "mesh": companion.get("geometry", {}).get("mesh"),
                "printable": companion.get("report_dict", {}).get("printable"),
                "files": ["plate_model.stl", "plate_preview.svg", "plate_diagram.json"],
            }
            companion_files = ["plate_model.stl", "plate_preview.svg", "plate_diagram.json"]

        size = meta.get("size_mm") or [0, 0, 0]
        feature_text = "、".join(str(item.get("target") or item.get("text") or "") for item in plan.labels if item.get("target"))
        audio = {
            "intro": f"这是一个根据照片重建的{plan.subject or plan.title}立体模型。",
            "steps": [
                "先从正面底座摸起，正面有一块标题牌和一块方形二维码位；标题牌上刻着中文盲文。",
                f"标题是{plan.title}。",
                "在底座前缘摸到编号盲文图例，按编号读出各个特征的名称。"
                + (f"这次标注了{feature_text}。" if feature_text else ""),
                "再向上摸主体，感受照片里物体的轮廓、转折和主要起伏。",
                f"主体高约 {plan.height_mm:.0f} 毫米，连同底座总高约 {float(size[2]):.0f} 毫米。",
            ],
            "outro": "这是照片级重建的展示模型，不是测量件；单色打印不保留照片颜色，颜色和纹理请看带纹理的 GLB。",
        }
        return companion_files, {
            "mode": "reconstruction",
            "title": title,
            "title_zh": plan.title,
            "subtitle": subtitle,
            "audio": audio,
            "report": report,
            "report_dict": report.to_dict(),
            "geometry": {
                "kind": "reconstruction",
                "plan": plan.to_dict(),
                "provider": asset.to_dict(),
                "meta": meta,
                "mesh": mesh_health(result.mesh),
                "source_mesh": mesh_health(asset.mesh),
                "size_mm": size,
                "companion": companion_meta,
            },
            "paper": "3D",
            "print": print_settings,
        }

    @staticmethod
    def _photo_view_html(title: str, plan: ReconstructionPlan, asset: Any, *, kind: str = "photo") -> str:
        glb = asset.files.get("glb")
        thumbnail = asset.files.get("thumbnail")
        if glb is not None:
            viewer = (
                f'<model-viewer src="{html.escape(glb.name)}" alt="{html.escape(title)}" '
                'camera-controls auto-rotate shadow-intensity="1" exposure="1" '
                'style="width:100%;height:70vh;background:#eef1f4;"></model-viewer>'
            )
            script = (
                '<script type="module" '
                'src="https://unpkg.com/@google/model-viewer/dist/model-viewer.min.js"></script>'
            )
        elif thumbnail is not None:
            viewer = f'<img src="{html.escape(thumbnail.name)}" alt="{html.escape(title)}" style="max-width:100%;">'
            script = ""
        else:
            viewer = '<p>三维服务没有返回可预览文件，请打开 <a href="preview.svg">preview.svg</a> 查看打印轮廓。</p>'
            script = ""
        changes = "".join(f"<li>{html.escape(item)}</li>" for item in plan.changes)
        keeps = "".join(f"<li>{html.escape(item)}</li>" for item in plan.keep_features)
        generated = kind == "generated"
        heading = "生成说明" if generated else "重建说明"
        lead = "文字生成的三维模型" if generated else "照片级三维重建"
        keep_title = "生成要点" if generated else "保留特征"
        keep_empty = "<li>按文字描述生成完整主体</li>" if generated else "<li>按照片原样保留</li>"
        color_note = (
            "GLB 保留生成的颜色与材质；model.stl 是带底座、盲文标题、底座盲文图例和二维码位的单色打印件。"
            if generated
            else "GLB 保留照片颜色与纹理；model.stl 是带底座、盲文标题、底座盲文图例和二维码位的单色打印件。"
        )
        return (
            "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            f"<title>{html.escape(title)} · {html.escape(lead)}</title>"
            "<style>body{font-family:system-ui,'Microsoft YaHei',sans-serif;margin:0;background:#f6f7f9;color:#1d2530;}"
            "main{max-width:960px;margin:0 auto;padding:24px;}h1{font-size:22px;}"
            "section{background:#fff;border:1px solid #dde3ea;border-radius:12px;padding:18px;margin-top:16px;}"
            "li{line-height:1.7}a{color:#1d5fd0}</style></head><body><main>"
            f"<h1>{html.escape(title)}</h1>"
            f"<p>{html.escape(plan.subject or lead)}</p>"
            f"{viewer}{script}"
            f"<section><h2>{heading}</h2>"
            f"<p>目标高度 {plan.height_mm:.0f} mm；底座 {html.escape(plan.base)}；纹理模型：{html.escape(glb.name if glb else '无')}。</p>"
            f"<p>{color_note}</p>"
            f"<h3>{keep_title}</h3><ul>{keeps or keep_empty}</ul>"
            f"<h3>按要求调整</h3><ul>{changes or '<li>本次没有额外几何调整</li>'}</ul>"
            "<p><a href=\"model_textured.glb\" download>下载纹理 GLB</a> · "
            "<a href=\"model.stl\" download>下载可打印 STL</a> · "
            "<a href=\"preview.svg\">打印预览</a></p>"
            "</section></main></body></html>"
        )

    @staticmethod
    def _reconstruction_companion(plan: ReconstructionPlan) -> dict[str, Any]:
        labels = [item for item in plan.labels if str(item.get("text") or "").strip()][:4] or [
            {"text": plan.title, "target": "主体"}
        ]
        blocks: list[dict[str, Any]] = []
        order: list[str] = []
        for index, item in enumerate(labels, start=1):
            text = str(item.get("text") or "")[:4]
            target = str(item.get("target") or "")
            x = 24.0 + (index - 1) * (52.0 / max(1, len(labels) - 1)) if len(labels) > 1 else 50.0
            y = 50.0
            blocks.append(
                {
                    "id": f"feature_{index}",
                    "label": f"FEATURE {index}",
                    "label_zh": text,
                    "note": target,
                    "marker": [round(x, 3), round(y, 3)],
                    "show_label": True,
                    "primitives": [
                        {"type": "circle", "cx": round(x, 3), "cy": round(y, 3), "r": 7.0, "width": 1.8},
                        {"type": "text", "x": round(x - 3.2, 3), "y": round(y - 2.2, 3), "text": str(index), "height": 4.4},
                        {"type": "braille", "x": round(x - 4.0, 3), "y": round(y + 9.0, 3), "text": text},
                    ],
                }
            )
            order.append(f"feature_{index}")
        return {
            "version": 1,
            "title": "PHOTO RECONSTRUCTION",
            "title_zh": plan.title,
            "subtitle": f"FEATURE OVERVIEW / {len(blocks)} LABELS / NOT TO SCALE",
            "blocks": blocks,
            "reading_order": order,
            "footer_lines": ["FEATURE ORDER, NOT TRUE SHAPE", "SCAN QR FOR AUDIO"],
            "notes": [
                "这张板只标出重建对象和特征名的阅读顺序，不还原照片细节。",
                "物体的真实轮廓、颜色和纹理以带纹理的 GLB 与三维打印件为准。",
            ],
            "scale_note": "SCHEMATIC / NOT TO SCALE",
        }

    # ------------------------------------------------------------------
    # Reports and bundle files
    # ------------------------------------------------------------------

    def _print_settings(self, mode: str, mesh: Mesh, paper: str | None = None) -> dict[str, Any]:
        stats = mesh.stats()
        if mode == "tactile_plate":
            notes = "0.2 mm layers keep the 0.8 mm relief and 0.6 mm braille dots crisp; do not iron the top surface."
            if paper and paper.upper() != "A5":
                notes += f" 板面为 {paper.upper()}，请确认打印机热床不小于 {paper.upper()} 并预留 6 mm 边距。"
            return {
                "orientation": "relief face up, base on the bed",
                "nozzle_mm": 0.4,
                "layer_height_mm": 0.2,
                "perimeters": 3,
                "infill_percent": 15,
                "supports": "none",
                "paper": paper or "A5",
                "notes": notes,
                "size_mm": stats["size_mm"],
            }
        if mode == "model3d":
            return {
                "orientation": "base flat on the bed; print orbits and planets as previewed",
                "nozzle_mm": 0.4,
                "layer_height_mm": 0.16,
                "perimeters": 3,
                "infill_percent": 15,
                "supports": "tree supports only under long overhangs and the sun pedestal; keep them away from braille surfaces",
                "paper": "3D",
                "notes": "0.16 mm layers and a 0.4 mm nozzle keep the 1.6 mm braille dots legible; verify the model fits the 220×220×250 mm build volume before slicing.",
                "size_mm": stats["size_mm"],
            }
        if mode == "reconstruction":
            return {
                "orientation": "display base flat on the bed; reconstructed object upright as previewed",
                "nozzle_mm": 0.4,
                "layer_height_mm": 0.16,
                "perimeters": 3,
                "infill_percent": 15,
                "supports": "tree supports under overhangs, kept away from braille dots and the QR pad",
                "paper": "3D",
                "notes": (
                    "STL 是单色打印件，不保留照片颜色；需要颜色时请用原始 GLB 在多材料切片器中上色。"
                    "AI 重建网格可能含开放边或退化面，切片前先用修复工具检查；"
                    "0.16 mm 层高与 0.4 mm 喷嘴可保留 1.6 mm 盲文点。"
                ),
                "size_mm": stats["size_mm"],
            }
        return {
            "orientation": "as previewed; pedestal on the bed",
            "nozzle_mm": 0.4,
            "layer_height_mm": 0.16,
            "perimeters": 3,
            "infill_percent": 15,
            "supports": "tree supports under joints, gripper and any overhang",
            "notes": "Check the slicer preview; the standards report flags overhang above 5% as a process warning, not a geometry error.",
            "size_mm": stats["size_mm"],
        }

    def _write_job(self, job_dir: Path, **payload: Any) -> None:
        job_path = job_dir / "job.json"
        current: dict[str, Any] = {}
        if job_path.exists():
            try:
                current = json.loads(job_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                current = {}
        current.update(payload)
        current["updated_at"] = _utc_now()
        _write_json(job_path, current)

    def _artifact_list(self, job_dir: Path) -> list[dict[str, Any]]:
        artifacts = []
        for path in sorted(job_dir.iterdir()):
            if not path.is_file() or path.name == "job.json":
                continue
            artifacts.append({"name": path.name, "bytes": path.stat().st_size, "sha256": sha256_file(path)})
        return artifacts

    def _write_compliance(self, job_dir: Path, metadata: dict[str, Any]) -> None:
        report = metadata["report_dict"]
        lines = [
            "# 合规检查报告",
            "",
            f"- 输出模式：`{metadata['mode']}`",
            f"- 可打印（无 FAIL）：`{str(report['printable']).lower()}`",
            f"- 完全合规（无 FAIL / DEVIATION / NEEDS_REVIEW）：`{str(report['standards_clear']).lower()}`",
            f"- 计数：{report['counts']}",
            "",
            "## 判定口径",
            "",
            "`printable` 只表示几何没有阻断打印的 FAIL；`standards_clear` 还要求没有偏离项和待人工复核项。两者不能混为一谈。",
            "",
            "## 检查项",
            "",
            "| 状态 | 规则 | 实测 | 说明 |",
            "|---|---|---|---|",
        ]
        for check in report["checks"]:
            lines.append(f"| {check['status']} | `{check['rule_id']}` | {check['measured']} | {check['message']} |")
        lines.extend(
            [
                "",
                "## 依据标准",
                "",
                "| 标准号 | 名称 | 状态 | 实施日期 | 链接 |",
                "|---|---|---|---|---|",
            ]
        )
        for standard in standards_registry():
            lines.append(
                f"| {standard['number']} | {standard['title']} | {standard['status']} | {standard.get('effective','')} | {standard['url']} |"
            )
        lines.extend(
            [
                "",
                "## 已知限制",
                "",
                "- 三维模型的中文名称使用 GF 0019-2018 盲文点字（1.6 mm 点径、0.8 mm 点高），优先刻在部件可触摸表面；行星和过小部件刻在底座表面，不生成竖立名牌。平面场景板仍使用 UEB 英文字母和阿拉伯数字，报告标记 `NEEDS_REVIEW`。",
                "- GB/T 40142-2021 的公开全文只获取到封面和目录；触摸图形参数采用公开解读值，条款级核对标记为待复核。",
                "- GB/T 15720-2008 已于 2025-11-03 废止，本报告不把它作为现行依据。",
                "- 图片和工程图在离线模式下只作为来源留档，不凭空测量尺寸；确认后的解析式或参数才进入几何。",
                "- 场景板只表达图块位置、连接关系和阅读顺序，不还原照片细节，也不代表真实尺寸。",
            ]
        )
        if metadata.get("mode") == "reconstruction":
            lines.extend(
                [
                    "- 照片级重建的纹理与颜色只保留在 GLB；STL 是单色打印件。",
                    "- AI 重建网格不是测量数据；高度按用户目标缩放，不能当作真实尺寸。",
                    "- 文字指令的几何编辑（例如展开翅膀）取决于三维服务能力；服务不支持时会保留照片原样。",
                    "- 重建网格可能含开放边或退化面；报告把水密性问题标为 NEEDS_REVIEW，切片前必须用修复工具检查。",
                ]
            )
        lines.append("")
        (job_dir / "compliance.md").write_text("\n".join(lines), encoding="utf-8")

    def _write_readme(self, job_dir: Path, metadata: dict[str, Any]) -> None:
        print_info = metadata["print"]
        lines = [
            "TOUCHLEARN 输出包",
            "=" * 40,
            f"标题：{metadata.get('title')}",
            f"模式：{metadata['mode']}",
            "",
            "文件",
            "- model.stl            触觉板或三维模型，可直接送入切片软件",
            "- preview.svg          预览图（不是打印文件）",
            "- plate_model.stl      双模式输出的配套触觉总览板（若有）",
            "- plate_preview.svg    配套触觉板预览（若有）",
            "- model_spec.json      三维模型的结构化规格（若有）",
            "- model_textured.glb   照片级重建的带纹理模型（若有）",
            "- thumbnail.png        三维服务返回的渲染缩略图（若有）",
            "- photo_view.html      可旋转查看 GLB 的重建页面（若有）",
            "- reconstruction.json  重建计划、指令与来源记录（若有）",
            "- qr_card.svg          A6 二维码卡片，打印后贴在预留平面",
            "- qr.png               二维码原图",
            "- audio_script.txt     讲解脚本",
            "- narration.wav        讲解音频（若 TTS 成功）",
            "- audio.html           离线播放页（与 narration.wav 同目录）",
            "- compliance.md        合规报告",
            "- manifest.json        可复现清单与哈希",
            "",
            "打印建议",
            f"- 板面：{print_info.get('paper')}",
            f"- 尺寸：{print_info.get('size_mm')} mm",
            f"- 摆放：{print_info.get('orientation')}",
            f"- 喷嘴：{print_info.get('nozzle_mm')} mm；层高：{print_info.get('layer_height_mm')} mm",
            f"- 周界：{print_info.get('perimeters')}；填充：{print_info.get('infill_percent')}%",
            f"- 支撑：{print_info.get('supports')}",
            f"- 备注：{print_info.get('notes')}",
            "",
        ]
        braille = ((metadata.get("geometry") or {}).get("meta") or {}).get("braille") or {}
        if braille:
            lines.extend(
                [
                    "盲文对照（GF 0019-2018）",
                    f"- 标题：{braille.get('title')}（{' '.join(braille.get('pinyin') or [])}），共 {braille.get('title_dots')} 个点",
                    f"- 铭牌排版：{' / '.join(braille.get('title_lines') or [])}",
                ]
            )
            for row in braille.get("title_cells") or []:
                cells = " / ".join("-".join(str(dot) for dot in cell) or "空" for cell in row.get("cells") or [])
                pinyin = f"  {row.get('pinyin')}" if row.get("pinyin") else ""
                lines.append(f"  {row.get('char')}{pinyin}  {cells}")
            lines.append("  点格顺序：左上 1、左中 2、左下 3；右上 4、右中 5、右下 6。")
            for item in braille.get("labels") or []:
                suffix = "（名称过长，图例只放下了部分字符）" if item.get("truncated") else ""
                lines.append(f"- 图例 {item.get('legend_index')}：{item.get('text')} → {item.get('legend_line')}{suffix}")
            if braille.get("title_truncated"):
                lines.append("- 注意：标题过长，当前底座只放下了部分字符，请缩短标题后重新生成。")
            lines.append("")
        lines.extend(
            [
                "二维码",
                "二维码指向本机音频页面。手机与电脑需在同一局域网，且本地服务正在运行。",
                "如果只在离线环境使用，可直接打开 audio.html 播放 narration.wav。",
                "",
                "合规",
                "先看 compliance.md。printable=true 不代表完全符合所有标准条款；DEVIATION 和 NEEDS_REVIEW 需要人工确认。",
                "",
            ]
        )
        (job_dir / "README.txt").write_text("\n".join(lines), encoding="utf-8")

    def _manifest(
        self,
        job_dir: Path,
        draft_payload: dict[str, Any],
        fields: dict[str, Any],
        metadata: dict[str, Any],
        job_id: str,
        audio_url: str,
    ) -> dict[str, Any]:
        report = metadata["report_dict"]
        artifacts = self._artifact_list(job_dir)
        return {
            "schema_version": "0.2.0",
            "job_id": job_id,
            "generated_at": _utc_now(),
            "software": {
                "name": "TouchLearn Agent",
                "agent_version": agent_version,
                "core_version": core_version,
                "python": platform.python_version(),
                "platform": platform.platform(),
            },
            "subject": draft_payload.get("subject"),
            "mode": metadata["mode"],
            "title": metadata.get("title"),
            "subtitle": metadata.get("subtitle"),
            "input": {
                "files": draft_payload.get("files") or [],
                "fields": fields,
            },
            "geometry": metadata.get("geometry"),
            "standards": {
                **report,
                "registry": standards_registry(),
                "rules": rules_registry(),
            },
            "audio": {
                **metadata["audio"],
                "audio_page": "audio.html",
                "public_url": audio_url,
            },
            "qr": {
                "payload": audio_url,
                "png": "qr.png",
                "card": "qr_card.svg",
            },
            "print": metadata.get("print"),
            "artifacts": artifacts,
            "known_limitations": [
                "3D models label parts with Chinese Braille (GF 0019-2018) directly on touchable surfaces; planets and parts too small for a name use a braille legend on the base instead of standing flags. Flat scene plates still use UEB letters and Arabic numerals.",
                "GB/T 40142-2021 clause values are taken from public summaries; the official full text was not obtained.",
                "Images and engineering drawings are archived as provenance, not measured automatically in offline mode.",
                "Generated scenes are structural schematics; they do not reproduce photo detail or represent true dimensions.",
                "3D part and planet sizes keep their real relative order but are compressed to fit a printable display piece; they are not museum-scale replicas.",
                "Photo-real reconstruction keeps colour and texture only in the GLB; the STL is a single-material print.",
                "A reconstructed mesh is not a measurement; its height follows the user's target and must not be read as a real dimension.",
                "Geometry edits requested in text depend on the reconstruction provider; the local pipeline always applies height, base, braille and QR placement.",
            ],
        }
