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

from .. import __version__ as core_version
from ..geometry import Mesh, build_mesh
from ..layout import LayoutResult, build_layout
from ..model import Diagram, diagram_from_dict
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
from .expression import Expression, ExpressionError, parse_expression
from .llm import llm_status
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
from .router import RouteDecision, classify_request, extract_function
from .scene import (
    SceneError,
    generate_scene,
    match_template,
    normalize_scene,
    scene_narration,
    scene_summary,
    scene_templates,
)
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
        if subject not in {"", "auto", "function", "robot", "mesh", "scene", "unsupported"}:
            raise ValueError(f"unsupported subject: {subject}")

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
            document = extract_document(draft_dir / str(item["path"]))
            item["document"] = document.metadata()
            if not document.ok:
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
            has_scene = isinstance(payload.get("scene"), (dict, list)) or bool(str(payload.get("scene") or "").strip())
            requested_template = str(payload.get("template") or "").strip()
            if has_scene or requested_template:
                # 用户显式给出场景结构或选择了离线模板，这是硬证据，优先于文本意图判断。
                decision = RouteDecision(
                    subject="scene",
                    confidence=0.95,
                    reason="用户提供了场景 JSON 或选择了离线场景模板。",
                    provider="manual",
                )
            else:
                decision = classify_request(
                    text,
                    files=files,
                    images=images,
                    has_expression=bool(str(payload.get("expression") or "").strip()),
                    has_spec=bool(payload.get("spec")),
                    has_mesh=bool(mesh_files),
                )
            subject = decision.subject
            route = decision.to_dict()

        if subject == "robot" and text and not str(payload.get("description") or "").strip():
            payload["description"] = text

        if subject == "function":
            draft = self._analyze_function(payload, draft_id, files)
        elif subject == "mesh":
            draft = self._analyze_mesh(payload, draft_id, files, mesh_files)
        elif subject == "scene":
            draft = self._analyze_scene(payload, draft_id, files, images=images, route=route)
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
            draft = self._analyze_robot(payload, draft_id, files)

        for note in document_notes:
            if note not in draft.notes:
                draft.notes.append(note)
        if route:
            draft.fields.setdefault("routing", route)
            for item in route.get("notes") or []:
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
    ):
        path = self.root / "drafts" / draft_id / str(image_info.get("path") or "")
        if not path.is_file():
            return None
        return interpret_image(path, subject, hint=hint)

    def _save_uploads(self, raw_files: Sequence[Any], uploads_dir: Path) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        total = 0
        for index, item in enumerate(raw_files):
            if not isinstance(item, dict):
                continue
            name = _slug(str(item.get("name") or f"upload-{index + 1}"), f"upload-{index + 1}")
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

    def _analyze_function(self, payload: dict[str, Any], draft_id: str, files: list[dict[str, Any]]) -> Draft:
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
                candidate = extract_function(raw_text)
                if candidate.get("ok") and candidate.get("expression"):
                    expression_text = str(candidate["expression"]).strip()
                    fields["expression"] = expression_text
                    fields["extraction"] = {
                        "provider": candidate.get("provider"),
                        "model": candidate.get("model"),
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
            candidate = self._vision_candidate(draft_id, images[0], "function")
            if candidate and candidate.ok:
                proposed = str(candidate.data.get("expression") or "").strip()
                if proposed:
                    expression_text = proposed
                    fields["expression"] = proposed
                    vision_confidence = candidate.confidence or 0.6
                    fields["vision"] = {
                        "provider": candidate.provider,
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

        explicit_x = payload.get("x_min") is not None and payload.get("x_max") is not None
        if explicit_x:
            x_min, x_max = float(payload["x_min"]), float(payload["x_max"])
            if x_max <= x_min:
                questions.append("x 的上界必须大于下界。")
                return Draft(draft_id, "function", "tactile_plate", 0.2, False, fields, questions, notes, files)
            y_min, y_max = expression.auto_y_range(x_min, x_max)
        else:
            x_min, x_max, y_min, y_max = expression.suggest_window()
        if payload.get("y_min") is not None and payload.get("y_max") is not None:
            y_min, y_max = float(payload["y_min"]), float(payload["y_max"])
        if y_max <= y_min:
            questions.append("y 的上界必须大于下界。")
            return Draft(draft_id, "function", "tactile_plate", 0.2, False, fields, questions, notes, files)

        points = expression.key_points(x_min, x_max, y_min, y_max, max_points=6)
        fields.update(
            {
                "expression": expression.canonical,
                "x_min": round(x_min, 4),
                "x_max": round(x_max, 4),
                "y_min": round(y_min, 4),
                "y_max": round(y_max, 4),
                "key_points": [point.to_dict() for point in points],
                "auto_window": not explicit_x,
            }
        )
        notes.append(f"解析成功：{expression.canonical}；窗口 x∈[{_number(x_min)}, {_number(x_max)}]，y∈[{_number(y_min)}, {_number(y_max)}]。")
        notes.append(f"识别到 {len(points)} 个关键点。")
        if not explicit_x:
            questions.append("窗口是根据极值点和交点自动选择的；如需固定定义域，请在确认页修改 x 范围。")
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
                else (0.96 if explicit_x else 0.9)
            ),
            ready=True,
            fields=fields,
            questions=questions,
            notes=notes,
            files=files,
        )

    def _analyze_robot(self, payload: dict[str, Any], draft_id: str, files: list[dict[str, Any]]) -> Draft:
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
            candidate = self._vision_candidate(draft_id, images[0], "robot")
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
            fields["vision"] = {"confidence": round(vision_confidence, 3), "provider": "remote-vlm"}
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

    def _analyze_scene(
        self,
        payload: dict[str, Any],
        draft_id: str,
        files: list[dict[str, Any]],
        *,
        images: Sequence[Path] = (),
        route: dict[str, Any] | None = None,
    ) -> Draft:
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
        warnings: list[str] = []
        template_id = ""
        confidence = 0.0
        generation: dict[str, Any] = {}

        explicit = payload.get("scene")
        if isinstance(explicit, str):
            try:
                explicit = json.loads(explicit)
            except json.JSONDecodeError:
                explicit = None
        if isinstance(explicit, dict):
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
                notes.append(f"已使用离线模板：{entry['label']}。")

        if scene is None and (text or images):
            generation_result = generate_scene(text, images=images)
            generation = {
                "ok": generation_result.ok,
                "provider": generation_result.provider,
                "model": generation_result.model,
                "message": generation_result.message,
            }
            if generation_result.ok and generation_result.scene:
                scene = generation_result.scene
                confidence = max(0.35, min(0.9, generation_result.confidence))
                notes.extend(generation_result.notes)
                questions.extend(generation_result.questions)
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
                notes.append(f"大模型不可用或未生成结果，已匹配离线模板 {template_id}（命中 {score} 个关键词）。")
                questions.append("离线模板只覆盖常见示意结构；请核对图形、标签和阅读顺序是否符合你的教材。")

        fields["generation"] = generation
        if route:
            fields["routing"] = route

        if scene is None:
            fields["suggested_templates"] = [
                {"id": entry["id"], "label": entry["label"], "description": entry["description"], "summary": entry["summary"]}
                for entry in templates.values()
            ]
            fields["scene"] = None
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
            fields["suggested_templates"] = [
                {"id": entry["id"], "label": entry["label"], "description": entry["description"], "summary": entry["summary"]}
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
        fields.update(
            {
                "scene": scene,
                "summary": scene_summary(scene),
                "template": template_id,
                "warnings": warnings,
            }
        )
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
        auto_x_min, auto_x_max, _auto_y_min, _auto_y_max = expression.suggest_window()
        x_min = float(fields.get("x_min") if fields.get("x_min") is not None else auto_x_min)
        x_max = float(fields.get("x_max") if fields.get("x_max") is not None else auto_x_max)
        y_min_field = fields.get("y_min")
        y_max_field = fields.get("y_max")
        if y_min_field is None or y_max_field is None:
            auto_y_min, auto_y_max = expression.auto_y_range(x_min, x_max)
            y_min = float(y_min_field if y_min_field is not None else auto_y_min)
            y_max = float(y_max_field if y_max_field is not None else auto_y_max)
        else:
            y_min = float(y_min_field)
            y_max = float(y_max_field)
        if x_max <= x_min or y_max <= y_min:
            raise ValueError("函数窗口无效：上界必须大于下界")
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
        payload = {
            "schema_version": "0.2.0",
            "id": f"function-{_slug(expression.canonical, 'graph')}",
            "title": title,
            "title_zh": title_zh,
            "domain": "math",
            "canvas": {"size": "A5", "width_mm": 148, "height_mm": 210, "margin_mm": 6},
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
            "subtitle": f"{expression.canonical}  /  A5 TACTILE PLATE",
            "audio": audio,
            "report": report,
            "report_dict": report.to_dict(),
            "geometry": {
                "kind": "tactile_plate",
                "diagram": payload,
                "layout_summary": layout.summary(),
                "mesh": mesh.stats(),
                "warnings": layout.warnings,
            },
            "print": self._print_settings("tactile_plate", mesh),
        }

    def _build_scene(self, job_dir: Path, fields: dict[str, Any], progress: Progress) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        progress(10, "读取并校验场景")
        try:
            scene, warnings = normalize_scene(fields.get("scene"))
        except SceneError as error:
            raise ValueError(f"场景无效：{error}") from error

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

        title = scene["title"]
        title_zh = scene.get("title_zh") or title
        subtitle = scene.get("subtitle") or f"A5 TACTILE SCENE / {len(scene['blocks'])} BLOCKS"
        audio = scene_narration(scene)
        payload = {
            "schema_version": "0.2.0",
            "id": f"scene-{_slug(str(fields.get('template') or title), 'diagram')}",
            "title": title,
            "title_zh": title_zh,
            "domain": "scene",
            "canvas": {"size": "A5", "width_mm": 148, "height_mm": 210, "margin_mm": 6},
            "source": {"kind": "scene", "ref": str(fields.get("source_text") or fields.get("template") or "user request")[:200]},
            "elements": elements,
            "relations": relations,
            "layout": {"engine": "scene", "options": {"scene": scene}},
            "constraints": DEFAULT_CONSTRAINTS,
            "audio": audio,
        }
        _write_json(job_dir / "diagram.json", payload)
        progress(28, "排版场景图块")
        diagram = diagram_from_dict(payload)
        layout = build_layout(diagram)
        progress(45, "生成封闭网格")
        mesh = build_mesh(layout)
        write_binary_stl(mesh, job_dir / "model.stl")
        write_svg(layout, job_dir / "preview.svg")
        _write_json(
            job_dir / "decision_trace.json",
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
            "geometry": {
                "kind": "tactile_plate",
                "diagram": payload,
                "scene_summary": scene_summary(scene),
                "layout_summary": layout.summary(),
                "mesh": mesh.stats(),
                "warnings": warnings + list(layout.warnings),
            },
            "print": self._print_settings("tactile_plate", mesh),
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
        payload = {
            "schema_version": "0.2.0",
            "id": f"robot-{spec.dof}-axis",
            "title": title,
            "title_zh": title_zh,
            "domain": "robotics",
            "canvas": {"size": "A5", "width_mm": 148, "height_mm": 210, "margin_mm": 6},
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
            "subtitle": f"{spec.dof} DOF / {spec.end_effector.upper()} / A5 TACTILE PLATE",
            "audio": audio,
            "report": report,
            "report_dict": report.to_dict(),
            "geometry": {
                "kind": "tactile_plate",
                "diagram": payload,
                "layout_summary": layout.summary(),
                "mesh": mesh.stats(),
                "warnings": layout.warnings,
            },
            "print": self._print_settings("tactile_plate", mesh),
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

    # ------------------------------------------------------------------
    # Reports and bundle files
    # ------------------------------------------------------------------

    def _print_settings(self, mode: str, mesh: Mesh) -> dict[str, Any]:
        stats = mesh.stats()
        if mode == "tactile_plate":
            return {
                "orientation": "relief face up, base on the bed",
                "nozzle_mm": 0.4,
                "layer_height_mm": 0.2,
                "perimeters": 3,
                "infill_percent": 15,
                "supports": "none",
                "notes": "0.2 mm layers keep the 0.8 mm relief and 0.6 mm braille dots crisp; do not iron the top surface.",
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
                "- 中文盲文（GF 0019-2018）映射模块尚未接入；当前使用 UEB 英文字母和阿拉伯数字，报告标记 `NEEDS_REVIEW`。",
                "- GB/T 40142-2021 的公开全文只获取到封面和目录；触摸图形参数采用公开解读值，条款级核对标记为待复核。",
                "- GB/T 15720-2008 已于 2025-11-03 废止，本报告不把它作为现行依据。",
                "- 图片和工程图在离线模式下只作为来源留档，不凭空测量尺寸；确认后的解析式或参数才进入几何。",
                "- 场景板只表达图块位置、连接关系和阅读顺序，不还原照片细节，也不代表真实尺寸。",
                "",
            ]
        )
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
            "- model.stl            可直接送入切片软件",
            "- preview.svg          预览图（不是打印文件）",
            "- qr_card.svg          A6 二维码卡片，打印后贴在预留平面",
            "- qr.png               二维码原图",
            "- audio_script.txt     讲解脚本",
            "- narration.wav        讲解音频（若 TTS 成功）",
            "- audio.html           离线播放页（与 narration.wav 同目录）",
            "- compliance.md        合规报告",
            "- manifest.json        可复现清单与哈希",
            "",
            "打印建议",
            f"- 尺寸：{print_info.get('size_mm')} mm",
            f"- 摆放：{print_info.get('orientation')}",
            f"- 喷嘴：{print_info.get('nozzle_mm')} mm；层高：{print_info.get('layer_height_mm')} mm",
            f"- 周界：{print_info.get('perimeters')}；填充：{print_info.get('infill_percent')}%",
            f"- 支撑：{print_info.get('supports')}",
            f"- 备注：{print_info.get('notes')}",
            "",
            "二维码",
            "二维码指向本机音频页面。手机与电脑需在同一局域网，且本地服务正在运行。",
            "如果只在离线环境使用，可直接打开 audio.html 播放 narration.wav。",
            "",
            "合规",
            "先看 compliance.md。printable=true 不代表完全符合所有标准条款；DEVIATION 和 NEEDS_REVIEW 需要人工确认。",
            "",
        ]
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
                "Chinese Braille (GF 0019-2018) is not implemented; the current plates use UEB letters and Arabic numerals.",
                "GB/T 40142-2021 clause values are taken from public summaries; the official full text was not obtained.",
                "Images and engineering drawings are archived as provenance, not measured automatically in offline mode.",
                "Generated scenes are structural schematics; they do not reproduce photo detail or represent true dimensions.",
            ],
        }
