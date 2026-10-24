"""Generic tactile-scene compiler.

The robot and function paths are deliberately narrow.  This module handles the
long tail: a circuit, a geometry proof, a flowchart, a route map, a bar chart,
a cell diagram, or any other two-dimensional schematic that can be reduced to
lines, outlines, arrows, text and braille.

A scene spec uses normalized coordinates (0-100) inside one content box, so a
language model never has to guess millimetres.  The compiler maps the spec onto
the A5 plate, keeps a 6 mm margin, reserves the QR corner, and reuses the same
deterministic mesh and compliance pipeline as the specialized engines.
"""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from .. import braille, font
from ..layout import (
    Feature,
    LayoutResult,
    Point,
    Primitive,
    _Builder,
    arc_points,
    base_plate_polygon,
    rect_polygon,
    text_width,
)
from ..model import Diagram
from .llm import chat_json
from .parametric import _axis_arrow, _fit_text, _footer, _place_label, _qr_zone

SCENE_VERSION = 1
CONTENT_BOX = (8.0, 24.0, 132.0, 142.0)
MAX_BLOCKS = 12
MAX_PRIMITIVES = 48
ALLOWED = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 ()+-./:=?^_")
TYPE_ALIASES = {
    "line": "polyline",
    "path": "polyline",
    "polyline": "polyline",
    "polygon": "polygon",
    "triangle": "polygon",
    "rect": "rect",
    "rectangle": "rect",
    "box": "rect",
    "circle": "circle",
    "ellipse": "ellipse",
    "arrow": "arrow",
    "arc": "arc",
    "point": "point",
    "dot": "point",
    "text": "text",
    "label": "text",
    "braille": "braille",
}


class SceneError(ValueError):
    """Raised when a scene spec cannot be compiled without guessing."""


@dataclass
class SceneGeneration:
    ok: bool
    scene: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    provider: str = ""
    model: str = ""
    message: str = ""
    notes: list[str] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "confidence": round(self.confidence, 3),
            "provider": self.provider,
            "model": self.model,
            "message": self.message,
            "notes": self.notes,
            "questions": self.questions,
            "block_count": len(self.scene.get("blocks") or []),
        }


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def _number(value: Any, default: float | None = None) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    if not math.isfinite(float(value)):
        return default
    return float(value)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _point(value: Any, default: Point | None = None) -> Point | None:
    if isinstance(value, dict):
        x, y = value.get("x"), value.get("y")
    elif isinstance(value, (list, tuple)) and len(value) >= 2:
        x, y = value[0], value[1]
    else:
        return default
    nx, ny = _number(x), _number(y)
    if nx is None or ny is None:
        return default
    return (_clamp(nx, 0.0, 100.0), _clamp(ny, 0.0, 100.0))


def _points(value: Any, minimum: int = 2, maximum: int = 72) -> list[Point]:
    if not isinstance(value, (list, tuple)):
        return []
    result: list[Point] = []
    for item in value[:maximum]:
        point = _point(item)
        if point is not None:
            result.append(point)
    return result if len(result) >= minimum else []


def _ascii(text: Any, limit: int = 32) -> str:
    if not isinstance(text, str):
        return ""
    cleaned = []
    for character in text.upper():
        if character in ALLOWED:
            cleaned.append(character)
        elif character in {"×", "✕", "·"}:
            cleaned.append("X")
        elif character in {"−", "–", "—"}:
            cleaned.append("-")
        else:
            cleaned.append(" ")
    value = " ".join("".join(cleaned).split())
    return value[:limit].strip()


def _chinese(text: Any, limit: int = 60) -> str:
    if not isinstance(text, str):
        return ""
    value = " ".join(text.split())
    return value[:limit]


def _slug(text: Any, fallback: str) -> str:
    cleaned = []
    for character in str(text or "").lower():
        if character.isalnum() and character.isascii():
            cleaned.append(character)
        elif character in {"-", "_"}:
            cleaned.append("_")
    value = "".join(cleaned).strip("_")
    return (value or fallback)[:24]


def _width(value: Any, default: float = 2.0) -> float:
    number = _number(value, default)
    return round(_clamp(number if number is not None else default, 1.0, 3.2), 3)


def _height(value: Any, default: float = 4.5) -> float:
    number = _number(value, default)
    return round(_clamp(number if number is not None else default, 3.2, 6.2), 3)


def _ellipse_points(cx: float, cy: float, rx: float, ry: float, count: int = 36) -> list[Point]:
    return [
        (
            cx + rx * math.cos(2.0 * math.pi * index / count),
            cy + ry * math.sin(2.0 * math.pi * index / count),
        )
        for index in range(count)
    ]


def _normalize_primitive(raw: Any, warnings: list[str]) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    kind = TYPE_ALIASES.get(str(raw.get("type") or raw.get("kind") or "").strip().lower())
    if kind is None:
        warnings.append(f"忽略了未知图元类型：{raw.get('type') or raw.get('kind') or '空'}")
        return None

    if kind in {"polyline", "polygon", "arrow"}:
        points = _points(raw.get("points"), minimum=3 if kind == "polygon" else 2)
        if not points:
            return None
        if kind == "arrow":
            return {"type": "arrow", "points": points[:4], "width": _width(raw.get("width"), 2.0), "head": _width(raw.get("head"), 4.0)}
        return {"type": kind, "points": points, "width": _width(raw.get("width", raw.get("stroke")), 2.0)}

    if kind == "rect":
        x = _number(raw.get("x"))
        y = _number(raw.get("y"))
        width = _number(raw.get("w", raw.get("width")))
        height = _number(raw.get("h", raw.get("height")))
        if None in {x, y, width, height}:
            return None
        x, y = _clamp(float(x), 0.0, 100.0), _clamp(float(y), 0.0, 100.0)
        width = _clamp(float(width), 0.5, 100.0 - x)
        height = _clamp(float(height), 0.5, 100.0 - y)
        return {"type": "rect", "x": x, "y": y, "w": width, "h": height, "width": _width(raw.get("width", raw.get("stroke")), 2.0)}

    if kind == "circle":
        cx, cy = _point(raw.get("center") if raw.get("center") is not None else [raw.get("cx"), raw.get("cy")], (50.0, 50.0))
        radius = _number(raw.get("r", raw.get("radius")), 4.0) or 4.0
        return {"type": "circle", "cx": cx, "cy": cy, "r": _clamp(radius, 0.8, 48.0), "width": _width(raw.get("width"), 2.0)}

    if kind == "ellipse":
        cx, cy = _point([raw.get("cx"), raw.get("cy")], (50.0, 50.0))
        rx = _clamp(_number(raw.get("rx", raw.get("r")), 8.0) or 8.0, 0.8, 48.0)
        ry = _clamp(_number(raw.get("ry"), 5.0) or 5.0, 0.8, 48.0)
        return {"type": "polygon", "points": _ellipse_points(cx, cy, rx, ry), "width": _width(raw.get("width"), 2.0)}

    if kind == "arc":
        cx, cy = _point([raw.get("cx"), raw.get("cy")], (50.0, 50.0))
        radius = _clamp(_number(raw.get("r", raw.get("radius")), 10.0) or 10.0, 0.8, 48.0)
        start = _clamp(_number(raw.get("start_deg"), 0.0) or 0.0, -360.0, 360.0)
        end = _clamp(_number(raw.get("end_deg"), 90.0) or 90.0, -360.0, 360.0)
        return {"type": "arc", "cx": cx, "cy": cy, "r": radius, "start_deg": start, "end_deg": end, "width": _width(raw.get("width"), 2.0)}

    if kind == "point":
        cx, cy = _point([raw.get("cx"), raw.get("cy")], (50.0, 50.0))
        radius = _clamp(_number(raw.get("r"), 1.6) or 1.6, 1.0, 4.0)
        return {"type": "point", "cx": cx, "cy": cy, "r": radius}

    if kind == "text":
        position = _point([raw.get("x"), raw.get("y")])
        text = _ascii(raw.get("text", raw.get("label")), 22)
        if position is None or not text:
            if raw.get("text") or raw.get("label"):
                warnings.append("文字标注含不支持的字符，已省略；中文请放在 label_zh 或 note 中。")
            return None
        return {"type": "text", "x": position[0], "y": position[1], "text": text, "height": _height(raw.get("height"), 4.5)}

    if kind == "braille":
        position = _point([raw.get("x"), raw.get("y")])
        text = _ascii(raw.get("text"), 22)
        if position is None or not text:
            return None
        return {"type": "braille", "x": position[0], "y": position[1], "text": text}

    return None


def normalize_scene(raw: Any, *, fallback_title: str = "") -> tuple[dict[str, Any], list[str]]:
    if not isinstance(raw, dict):
        raise SceneError("场景必须是 JSON 对象。")
    warnings: list[str] = []
    title = _ascii(raw.get("title"), 32) or _ascii(fallback_title, 32) or "TACTILE DIAGRAM"
    title_zh = _chinese(raw.get("title_zh"), 40) or _chinese(fallback_title, 40) or title
    subtitle = _ascii(raw.get("subtitle"), 48)
    blocks_raw = raw.get("blocks")
    if not isinstance(blocks_raw, list) or not blocks_raw:
        raise SceneError("场景至少需要一个 blocks 图块。")

    blocks: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(blocks_raw[:MAX_BLOCKS]):
        if not isinstance(item, dict):
            continue
        block_id = _slug(item.get("id"), f"b{index + 1}")
        while block_id in seen_ids:
            block_id = f"{block_id}_{index + 1}"
        seen_ids.add(block_id)
        primitives: list[dict[str, Any]] = []
        for primitive_raw in (item.get("primitives") or [])[:MAX_PRIMITIVES]:
            primitive = _normalize_primitive(primitive_raw, warnings)
            if primitive is not None:
                primitives.append(primitive)
        if not primitives:
            warnings.append(f"图块 {block_id} 没有可绘制的图元，已跳过。")
            continue
        label = _ascii(item.get("label"), 18)
        label_zh = _chinese(item.get("label_zh"), 24)
        note = _chinese(item.get("note"), 180)
        marker = _point(item.get("marker")) if item.get("marker") is not None else None
        blocks.append(
            {
                "id": block_id,
                "label": label,
                "label_zh": label_zh or label or block_id.upper(),
                "note": note,
                "primitives": primitives,
                "marker": list(marker) if marker else None,
                "show_label": bool(item.get("show_label", True)),
                "kind": _slug(item.get("kind"), "scene"),
            }
        )
    if not blocks:
        raise SceneError("没有可绘制的图块；请检查坐标系与图元类型。")

    ids = {block["id"] for block in blocks}
    order_raw = raw.get("reading_order")
    order: list[str] = []
    if isinstance(order_raw, list):
        for item in order_raw:
            block_id = _slug(item, "")
            if block_id in ids and block_id not in order:
                order.append(block_id)
    for block in blocks:
        if block["id"] not in order:
            order.append(block["id"])
    by_id = {block["id"]: block for block in blocks}
    blocks = [by_id[block_id] for block_id in order]

    footer_lines: list[str] = []
    raw_footer = raw.get("footer_lines")
    if isinstance(raw_footer, list):
        for item in raw_footer:
            value = _ascii(item, 42)
            if value:
                footer_lines.append(value)
    if not footer_lines:
        footer_lines = ["SCHEMATIC / NOT TO SCALE", "SCAN QR FOR AUDIO"]
    footer_lines = footer_lines[:2]

    notes: list[str] = []
    if isinstance(raw.get("notes"), list):
        for item in raw.get("notes")[:6]:
            value = _chinese(item, 180)
            if value:
                notes.append(value)

    scene = {
        "version": SCENE_VERSION,
        "title": title,
        "title_zh": title_zh,
        "subtitle": subtitle,
        "blocks": blocks,
        "reading_order": order,
        "footer_lines": footer_lines,
        "notes": notes,
        "scale_note": _ascii(raw.get("scale_note"), 42) or "SCHEMATIC / NOT TO SCALE",
    }
    return scene, warnings


def scene_summary(scene: dict[str, Any]) -> dict[str, Any]:
    blocks = scene.get("blocks") or []
    primitive_count = sum(len(block.get("primitives") or []) for block in blocks)
    return {
        "title": scene.get("title"),
        "title_zh": scene.get("title_zh"),
        "subtitle": scene.get("subtitle"),
        "block_count": len(blocks),
        "primitive_count": primitive_count,
        "reading_order": [
            {
                "index": index,
                "id": block.get("id"),
                "label": block.get("label"),
                "label_zh": block.get("label_zh"),
                "note": block.get("note"),
            }
            for index, block in enumerate(blocks, start=1)
        ],
        "footer_lines": scene.get("footer_lines"),
        "notes": scene.get("notes"),
        "scale_note": scene.get("scale_note"),
    }


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------


def _mapper() -> tuple[float, float, float, float, Any]:
    left, top, width, height = CONTENT_BOX

    def to_mm(point: Point) -> Point:
        return (left + point[0] / 100.0 * width, top + point[1] / 100.0 * height)

    return left, top, width, height, to_mm


def _add_primitive(builder: _Builder, feature: Feature, primitive: dict[str, Any], to_mm: Any, box: tuple[float, float, float, float]) -> None:
    left, top, width, height = box
    kind = primitive["type"]
    if kind == "polyline":
        builder.line(feature, [to_mm(point) for point in primitive["points"]], primitive["width"])
    elif kind == "polygon":
        builder.line(feature, [to_mm(point) for point in primitive["points"]], primitive["width"], closed=True)
    elif kind == "rect":
        x = left + primitive["x"] / 100.0 * width
        y = top + primitive["y"] / 100.0 * height
        w = primitive["w"] / 100.0 * width
        h = primitive["h"] / 100.0 * height
        builder.line(feature, rect_polygon(x, y, w, h), primitive["width"], closed=True)
    elif kind == "circle":
        cx, cy = to_mm((primitive["cx"], primitive["cy"]))
        radius = primitive["r"] / 100.0 * min(width, height)
        builder.circle(feature, cx, cy, radius)
    elif kind == "point":
        cx, cy = to_mm((primitive["cx"], primitive["cy"]))
        radius = primitive["r"] / 100.0 * min(width, height)
        builder.circle(feature, cx, cy, max(0.8, radius))
    elif kind == "arc":
        cx, cy = to_mm((primitive["cx"], primitive["cy"]))
        radius = primitive["r"] / 100.0 * min(width, height)
        points = arc_points(cx, cy, radius, primitive["start_deg"], primitive["end_deg"], segments=24)
        builder.line(feature, points, primitive["width"])
    elif kind == "arrow":
        points = [to_mm(point) for point in primitive["points"]]
        builder.line(feature, points, primitive["width"])
        tip = points[-1]
        previous = points[-2]
        length = min(6.0, max(2.5, math.dist(tip, previous) * 0.35))
        _axis_arrow(builder, feature, tip, (tip[0] - previous[0], tip[1] - previous[1]), length=length, half=length * 0.55)
    elif kind == "text":
        x, y = to_mm((primitive["x"], primitive["y"]))
        builder.text(feature, x, y, primitive["text"], primitive["height"])
    elif kind == "braille":
        x, y = to_mm((primitive["x"], primitive["y"]))
        builder.braille(feature, x, y, primitive["text"].lower())
    else:
        raise SceneError(f"未知图元：{kind}")


def build_scene_layout(diagram: Diagram) -> LayoutResult:
    options = diagram.layout.get("options") or {}
    scene, warnings = normalize_scene(options.get("scene"), fallback_title=diagram.title)
    builder = _Builder(diagram)
    width = diagram.canvas.width_mm
    height = diagram.canvas.height_mm
    margin = float(diagram.canvas.margin_mm)
    left, top, box_width, box_height = CONTENT_BOX

    title_feature = builder.feature("title", "title")
    title_text, title_height = _fit_text(scene["title"], width - 2 * margin - 4.0, 5.4, 4.2)
    builder.text(title_feature, (width - text_width(title_text, title_height)) / 2.0, 8.5, title_text, title_height)
    if scene.get("subtitle"):
        subtitle_feature = builder.feature("subtitle", "label")
        subtitle_text, subtitle_height = _fit_text(scene["subtitle"], width - 2 * margin - 8.0, 3.4, 2.8)
        builder.text(subtitle_feature, (width - text_width(subtitle_text, subtitle_height)) / 2.0, 17.4, subtitle_text, subtitle_height)

    builder.trace.append(
        {
            "stage": "layout",
            "action": "choose_engine",
            "decision": "scene / normalized 0-100 coordinates",
            "reason": "The request is not a function or a serial robot; a generic scene preserves the described structure without inventing measurements.",
        }
    )
    builder.trace.append(
        {
            "stage": "layout",
            "action": "map_content_box",
            "decision": f"x {left:.0f}-{left + box_width:.0f} mm, y {top:.0f}-{top + box_height:.0f} mm",
            "reason": "Reserves the title, footer and 30x30 mm QR corner on the A5 sheet.",
        }
    )

    obstacles: list[Feature] = []
    block_features: list[tuple[int, dict[str, Any], Feature]] = []
    to_mm = _mapper()[4]
    for index, block in enumerate(scene["blocks"], start=1):
        feature = builder.feature(f"block_{block['id']}", block.get("kind") or "scene", label=block.get("label") or "", semantic_id=block["id"], index=index)
        for primitive in block["primitives"]:
            _add_primitive(builder, feature, primitive, to_mm, CONTENT_BOX)
        obstacles.append(feature)
        block_features.append((index, block, feature))
        builder.trace.append(
            {
                "stage": "layout",
                "action": "place_block",
                "decision": f"{index}. {block.get('label') or block['id']} / {len(block['primitives'])} primitives",
                "reason": block.get("note") or "Reading order follows the confirmed scene specification.",
            }
        )

    # Labels are placed after every outline exists, so a label cannot land on
    # a later block.  The title band and the footer/QR band are reserved.
    reserved = [(0.0, 0.0, width, 23.0), (0.0, height - 38.0, width, 38.0)]
    for index, block, feature in block_features:
        label_text = f"{index} {block.get('label') or ''}".strip() if block.get("show_label", True) else str(index)
        anchor = block.get("marker")
        if anchor is None:
            box = feature.bbox()
            anchor = (box[0], box[1])
        else:
            anchor = to_mm(tuple(anchor))
        _place_label(
            builder,
            f"label_{block['id']}",
            label_text,
            anchor,
            obstacles,
            reserved,
            margin,
            semantic_id=block["id"],
            index=index,
            text_height=3.8,
            preferred=(1.0, -1.0),
        )

    qr_rect = _qr_zone(builder, width, height, margin)
    footer_lines = list(scene.get("footer_lines") or [])
    if scene.get("scale_note") and scene["scale_note"] not in footer_lines:
        footer_lines.insert(0, scene["scale_note"])
    _footer(builder, width, height, margin, qr_rect, lines=footer_lines[:2])

    builder.warnings.extend(warnings)
    return LayoutResult(
        diagram=diagram,
        width=width,
        height=height,
        base_polygon=base_plate_polygon(width, height),
        features=builder.features,
        trace=builder.trace,
        warnings=builder.warnings,
    )


# ---------------------------------------------------------------------------
# Narration
# ---------------------------------------------------------------------------


def scene_narration(scene: dict[str, Any]) -> dict[str, Any]:
    title = scene.get("title_zh") or scene.get("title") or "触觉示意图"
    intro = f"这是一张{title}的触觉示意图。图幅左上角有缺口，缺口方向是上方。"
    steps = ["先摸左上角的缺口，确认图的方向。"]
    for index, block in enumerate(scene.get("blocks") or [], start=1):
        label = block.get("label_zh") or block.get("label") or block.get("id")
        step = f"编号 {index} 是{label}。"
        if block.get("note"):
            step += str(block["note"]).rstrip("。") + "。"
        steps.append(step)
    steps.append("按编号顺序把各部分连起来，先整体摸一遍，再回到编号 1 逐步细读。")
    if scene.get("notes"):
        steps.extend(str(note).rstrip("。") + "。" for note in scene["notes"][:3])
    outro = "这是一张结构示意图，用来表达部件位置和连接关系；具体尺寸以说明和打印设置为准。音频只描述触摸顺序，不替代课程讲解。"
    return {"intro": intro, "steps": steps, "outro": outro}


# ---------------------------------------------------------------------------
# LLM generation
# ---------------------------------------------------------------------------

SCENE_SYSTEM = (
    "你是触觉教材的结构设计助手。你把用户的二维示意图需求转成严格 JSON，供确定性编译器排成 A5 触觉板。"
    "只输出 JSON 对象，不要 Markdown，不要解释。不要输出毫米坐标；所有 x/y 都用内容区的 0-100 归一化坐标，"
    "原点在左上角，x 向右，y 向下。线条宽度用毫米，范围 1.2-3.0。"
    "不要编造用户没有提供的事实、数字或标签；不确定的内容放到 questions 或 notes。"
)

SCENE_PROMPT = """请把下面的需求转成触觉场景 JSON。

用户需求：
{request}

已有提示：
{hint}

输出结构：
{{
  "supported": true,
  "title": "不超过 28 个字符的英文标题",
  "title_zh": "中文标题",
  "subtitle": "不超过 40 个字符的英文副标题，可空",
  "confidence": 0.0,
  "blocks": [
    {{
      "id": "battery",
      "label": "BATTERY",
      "label_zh": "电池",
      "note": "一句中文触摸说明",
      "marker": [50, 85],
      "show_label": true,
      "primitives": [
        {{"type": "rect", "x": 35, "y": 40, "w": 30, "h": 14, "width": 2.2}},
        {{"type": "line", "points": [[50, 54], [50, 70]], "width": 2.0}},
        {{"type": "text", "x": 42, "y": 44, "text": "BATTERY", "height": 4.0}}
      ]
    }}
  ],
  "reading_order": ["battery"],
  "footer_lines": ["SCHEMATIC / NOT TO SCALE", "SCAN QR FOR AUDIO"],
  "notes": ["这张板只表达连接关系，不表示真实尺寸。"],
  "questions": ["请确认开关位置是否正确。"],
  "unsupported_reason": ""
}}

规则：
1. 可用图元：line（points）、polygon（points）、rect（x,y,w,h）、circle（cx,cy,r）、ellipse（cx,cy,rx,ry）、arrow（points）、arc（cx,cy,r,start_deg,end_deg）、point（cx,cy,r）、text（x,y,text,height）、braille（x,y,text）。
2. 所有坐标在 0-100；矩形和文字不要越界；图块之间尽量留出 2-3 个单位的空隙。
3. 每个场景 1-10 个图块；每个图块对应一个可触摸的学习单元，按阅读顺序编号。
4. label、text 只能用英文字母、数字和 ()+-./:=?^_；中文放在 label_zh 和 note 中。
5. 只画轮廓和必要的实心小元素（箭头、圆点）；不要画大面积实心色块。
6. 如果需求无法用二维线条/形状可靠表达（例如照片美化、写文章、生成任意三维物体），返回 {{"supported": false, "unsupported_reason": "原因", "confidence": 0.0}}。
7. 如果用户给了尺寸，只能把它转换成相对比例，并在 notes 中说明实际尺寸需在打印设置中确认。
8. 最多 10 个图块；不要为了凑数拆散一个学习单元。
"""

SCENE_IMAGE_HINT = (
    "用户上传了图片。请把图片重建成简化的触觉示意图：只保留可辨认的主要轮廓、部件和标注，"
    "不要逐像素描摹，不要还原照片纹理；看不清的文字不要猜，放到 questions 中。"
)


def generate_scene(
    text: str,
    *,
    images: Sequence[str | Path] = (),
    hint: str = "",
    use_vision: bool = True,
) -> SceneGeneration:
    request = (text or "").strip() or "一张二维示意图"
    prompt = SCENE_PROMPT.format(request=request[:4000], hint=(hint or "无")[:800])
    if images and use_vision:
        prompt += "\n\n" + SCENE_IMAGE_HINT
    result = chat_json(
        SCENE_SYSTEM,
        prompt,
        images=images if use_vision else (),
        kind="vision" if images and use_vision else "text",
        max_tokens=3200,
    )
    if not result.ok:
        return SceneGeneration(False, provider=result.provider, model=result.model, message=result.message)
    data = result.data if isinstance(result.data, dict) else None
    if data is None:
        return SceneGeneration(False, provider=result.provider, model=result.model, message="模型返回的不是 JSON 对象。")

    if data.get("supported") is False:
        reason = _chinese(data.get("unsupported_reason"), 240) or "模型判断该需求不适合转成二维触觉图。"
        return SceneGeneration(False, provider=result.provider, model=result.model, message=reason, raw=data)

    raw_scene = data.get("scene") if isinstance(data.get("scene"), dict) else data
    try:
        scene, warnings = normalize_scene(raw_scene, fallback_title=request[:28])
    except SceneError as error:
        return SceneGeneration(False, provider=result.provider, model=result.model, message=str(error), raw=data)

    confidence = data.get("confidence", 0.55)
    try:
        confidence = max(0.0, min(1.0, float(confidence)))
    except (TypeError, ValueError):
        confidence = 0.55
    notes = [str(item).strip()[:180] for item in (data.get("notes") or []) if isinstance(item, str) and item.strip()][:5]
    questions = [str(item).strip()[:180] for item in (data.get("questions") or []) if isinstance(item, str) and item.strip()][:5]
    notes.extend(warnings[:4])
    return SceneGeneration(
        True,
        scene=scene,
        confidence=confidence,
        provider=result.provider,
        model=result.model,
        message="已生成场景候选。",
        notes=notes,
        questions=questions,
        raw=data,
    )


def generate_scene_from_image(
    path: str | Path,
    *,
    text: str = "",
    hint: str = "",
) -> SceneGeneration:
    return generate_scene(text, images=[path], hint=hint)


# ---------------------------------------------------------------------------
# Offline templates
# ---------------------------------------------------------------------------


def _template_scene(
    title: str,
    title_zh: str,
    subtitle: str,
    blocks: list[dict[str, Any]],
    order: list[str],
    notes: list[str],
    footer: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "version": SCENE_VERSION,
        "title": title,
        "title_zh": title_zh,
        "subtitle": subtitle,
        "blocks": blocks,
        "reading_order": order,
        "footer_lines": footer or ["SCHEMATIC / NOT TO SCALE", "SCAN QR FOR AUDIO"],
        "notes": notes,
        "scale_note": "SCHEMATIC / NOT TO SCALE",
    }


def _triangle_scene() -> dict[str, Any]:
    return _template_scene(
        "TRIANGLE",
        "三角形与高",
        "GEOMETRY / VERTICES A B C",
        [
            {
                "id": "triangle",
                "label": "TRIANGLE",
                "label_zh": "三角形",
                "note": "三条边围成三角形，中间细线是从顶点 A 落到底边的高。",
                "marker": [50, 45],
                "primitives": [
                    {"type": "polygon", "points": [[50, 6], [10, 88], [90, 88]], "width": 2.6},
                    {"type": "line", "points": [[50, 6], [50, 88]], "width": 1.2},
                    {"type": "line", "points": [[50, 82], [56, 82], [56, 88]], "width": 1.2},
                    {"type": "text", "x": 50, "y": 0.5, "text": "A", "height": 4.5},
                    {"type": "text", "x": 4, "y": 90, "text": "B", "height": 4.5},
                    {"type": "text", "x": 92, "y": 90, "text": "C", "height": 4.5},
                ],
            }
        ],
        ["triangle"],
        ["底边 BC 水平放置，顶点 A 在上方；高与底边垂直。"],
    )


def _pythagorean_scene() -> dict[str, Any]:
    return _template_scene(
        "RIGHT TRIANGLE",
        "直角三角形与边长",
        "GEOMETRY / SIDES 3 4 5",
        [
            {
                "id": "triangle",
                "label": "RIGHT TRIANGLE",
                "label_zh": "直角三角形",
                "note": "直角在 B。竖直直角边 AB 标 3，水平直角边 BC 标 4，斜边 AC 标 5。",
                "marker": [2, 5],
                "show_label": False,
                "primitives": [
                    {"type": "polygon", "points": [[20, 15], [20, 75], [80, 75]], "width": 2.8},
                    {"type": "line", "points": [[20, 67], [28, 67], [28, 75]], "width": 1.2},
                    {"type": "text", "x": 14, "y": 8, "text": "A", "height": 4.5},
                    {"type": "text", "x": 14, "y": 78, "text": "B", "height": 4.5},
                    {"type": "text", "x": 84, "y": 78, "text": "C", "height": 4.5},
                    {"type": "text", "x": 10, "y": 44, "text": "3", "height": 5.0},
                    {"type": "text", "x": 48, "y": 80, "text": "4", "height": 5.0},
                    {"type": "text", "x": 44, "y": 40, "text": "5", "height": 5.0},
                ],
            }
        ],
        ["triangle"],
        ["三个数字是边长比例，不代表毫米；打印尺寸以板面为准。"],
    )


def _circuit_scene() -> dict[str, Any]:
    return _template_scene(
        "ELECTRIC CIRCUIT",
        "串联电路",
        "PHYSICS / BATTERY SWITCH RESISTOR LAMP",
        [
            {
                "id": "wires",
                "label": "WIRES",
                "label_zh": "导线",
                "note": "其余线条是把电池、开关、电阻和灯泡连成一圈的导线。",
                "marker": [30, 68],
                "primitives": [
                    {"type": "polyline", "points": [[15, 25], [85, 25], [85, 85], [15, 85], [15, 25]], "width": 2.0},
                ],
            },
            {
                "id": "battery",
                "label": "BATTERY",
                "label_zh": "电池",
                "note": "长线是正极，短线是负极。",
                "marker": [50, 92],
                "primitives": [
                    {"type": "line", "points": [[44, 80], [44, 90]], "width": 2.6},
                    {"type": "line", "points": [[49, 82], [49, 88]], "width": 2.6},
                    {"type": "line", "points": [[55, 80], [55, 90]], "width": 2.6},
                    {"type": "line", "points": [[60, 82], [60, 88]], "width": 2.6},
                ],
            },
            {
                "id": "switch",
                "label": "SWITCH",
                "label_zh": "开关",
                "note": "斜线表示开关处于断开位置；合上后电路才接通。",
                "marker": [6, 50],
                "primitives": [
                    {"type": "line", "points": [[15, 38], [15, 46]], "width": 2.4},
                    {"type": "line", "points": [[15, 54], [15, 62]], "width": 2.4},
                    {"type": "line", "points": [[15, 46], [24, 42]], "width": 2.2},
                ],
            },
            {
                "id": "resistor",
                "label": "RESISTOR",
                "label_zh": "电阻",
                "note": "折线表示电阻。",
                "marker": [50, 12],
                "primitives": [
                    {"type": "polyline", "points": [[34, 25], [38, 20], [42, 30], [46, 20], [50, 30], [54, 20], [58, 30], [62, 20], [66, 25]], "width": 2.2},
                ],
            },
            {
                "id": "lamp",
                "label": "LAMP",
                "label_zh": "灯泡",
                "note": "圆内画叉表示灯泡。",
                "marker": [92, 55],
                "primitives": [
                    {"type": "circle", "cx": 85, "cy": 55, "r": 7.5, "width": 2.2},
                    {"type": "line", "points": [[80, 50], [90, 60]], "width": 1.8},
                    {"type": "line", "points": [[90, 50], [80, 60]], "width": 1.8},
                ],
            },
        ],
        ["battery", "switch", "resistor", "lamp", "wires"],
        ["电流从电池正极出发，依次经过开关、电阻和灯泡，再回到负极。"],
        ["PHYSICS / SERIES CIRCUIT", "SCHEMATIC / NOT TO SCALE"],
    )


def _flowchart_scene() -> dict[str, Any]:
    return _template_scene(
        "FLOWCHART",
        "流程图",
        "LOGIC / START PROCESS DECISION OUTPUT",
        [
            {
                "id": "start",
                "label": "START",
                "label_zh": "开始",
                "note": "第一步是开始。",
                "marker": [28, 10],
                "show_label": False,
                "primitives": [
                    {"type": "rect", "x": 35, "y": 4, "w": 30, "h": 12, "width": 2.3},
                    {"type": "text", "x": 41, "y": 8, "text": "START", "height": 4.0},
                ],
            },
            {
                "id": "process",
                "label": "READ INPUT",
                "label_zh": "读取输入",
                "note": "第二步读取输入数据。",
                "marker": [26, 31],
                "show_label": False,
                "primitives": [
                    {"type": "rect", "x": 33, "y": 24, "w": 34, "h": 14, "width": 2.3},
                    {"type": "text", "x": 37, "y": 28, "text": "READ INPUT", "height": 3.8},
                ],
            },
            {
                "id": "decision",
                "label": "CHECK",
                "label_zh": "判断",
                "note": "第三步是菱形判断框。",
                "marker": [16, 60],
                "show_label": False,
                "primitives": [
                    {"type": "polygon", "points": [[50, 44], [78, 59], [50, 74], [22, 59]], "width": 2.3},
                    {"type": "text", "x": 43, "y": 56, "text": "CHECK", "height": 4.0},
                ],
            },
            {
                "id": "output",
                "label": "OUTPUT",
                "label_zh": "输出",
                "note": "最后输出结果。",
                "marker": [26, 88],
                "show_label": False,
                "primitives": [
                    {"type": "rect", "x": 33, "y": 81, "w": 34, "h": 13, "width": 2.3},
                    {"type": "text", "x": 39, "y": 85, "text": "OUTPUT", "height": 4.0},
                ],
            },
            {
                "id": "arrows",
                "label": "FLOW",
                "label_zh": "连接箭头",
                "note": "箭头表示执行顺序，从上往下。",
                "marker": [62, 41],
                "show_label": False,
                "primitives": [
                    {"type": "arrow", "points": [[50, 16], [50, 24]], "width": 2.0},
                    {"type": "arrow", "points": [[50, 38], [50, 44]], "width": 2.0},
                    {"type": "arrow", "points": [[50, 74], [50, 81]], "width": 2.0},
                ],
            },
        ],
        ["start", "process", "decision", "output", "arrows"],
        ["矩形是步骤，菱形是判断，箭头表示执行顺序。"],
        ["LOGIC / TOP TO BOTTOM", "SCHEMATIC / NOT TO SCALE"],
    )


def _coordinate_scene() -> dict[str, Any]:
    return _template_scene(
        "COORDINATE PLANE",
        "平面直角坐标系",
        "MATH / FOUR QUADRANTS",
        [
            {
                "id": "axes",
                "label": "AXES",
                "label_zh": "坐标轴",
                "note": "横轴向右，纵轴向上，交点是原点。",
                "marker": [94, 94],
                "show_label": False,
                "primitives": [
                    {"type": "arrow", "points": [[8, 55], [92, 55]], "width": 2.4},
                    {"type": "arrow", "points": [[50, 92], [50, 8]], "width": 2.4},
                    {"type": "point", "cx": 50, "cy": 55, "r": 1.4},
                    {"type": "text", "x": 92, "y": 50, "text": "X", "height": 4.5},
                    {"type": "text", "x": 53, "y": 5, "text": "Y", "height": 4.5},
                    {"type": "text", "x": 53, "y": 58, "text": "O", "height": 4.0},
                ],
            },
            {
                "id": "quadrants",
                "label": "QUADRANTS",
                "label_zh": "四个象限",
                "note": "从右上角开始逆时针依次是 I、II、III、IV 象限。",
                "marker": [78, 18],
                "show_label": False,
                "primitives": [
                    {"type": "text", "x": 72, "y": 24, "text": "I", "height": 4.5},
                    {"type": "text", "x": 22, "y": 24, "text": "II", "height": 4.5},
                    {"type": "text", "x": 20, "y": 78, "text": "III", "height": 4.5},
                    {"type": "text", "x": 72, "y": 78, "text": "IV", "height": 4.5},
                ],
            },
        ],
        ["axes", "quadrants"],
        ["坐标轴只表示方向和象限，不表示具体刻度。"],
        ["MATH / QUADRANTS I-IV", "SCHEMATIC / NOT TO SCALE"],
    )


def _route_scene() -> dict[str, Any]:
    return _template_scene(
        "ROUTE MAP",
        "路线示意图",
        "ORIENTATION / HOME CROSSING SCHOOL PARK",
        [
            {
                "id": "home",
                "label": "HOME",
                "label_zh": "家",
                "note": "路线从家出发。",
                "marker": [8, 78],
                "show_label": False,
                "primitives": [
                    {"type": "rect", "x": 5, "y": 82, "w": 14, "h": 12, "width": 2.3},
                    {"type": "text", "x": 7, "y": 86, "text": "HOME", "height": 3.4},
                ],
            },
            {
                "id": "route",
                "label": "ROUTE",
                "label_zh": "路线",
                "note": "粗线是行走路线，转弯处要放慢速度。",
                "marker": [40, 62],
                "show_label": False,
                "primitives": [
                    {"type": "polyline", "points": [[12, 88], [28, 70], [28, 50], [48, 50], [48, 28], [72, 28], [72, 55], [88, 55]], "width": 3.0},
                ],
            },
            {
                "id": "crossing",
                "label": "CROSSING",
                "label_zh": "路口",
                "note": "横向短线是路口，通过前先停下确认。",
                "marker": [54, 38],
                "show_label": False,
                "primitives": [
                    {"type": "line", "points": [[44, 44], [52, 44]], "width": 1.8},
                    {"type": "line", "points": [[44, 50], [52, 50]], "width": 1.8},
                    {"type": "line", "points": [[44, 56], [52, 56]], "width": 1.8},
                ],
            },
            {
                "id": "school",
                "label": "SCHOOL",
                "label_zh": "学校",
                "note": "路线先到学校。",
                "marker": [60, 12],
                "show_label": False,
                "primitives": [
                    {"type": "rect", "x": 65, "y": 16, "w": 20, "h": 12, "width": 2.3},
                    {"type": "text", "x": 68, "y": 20, "text": "SCHOOL", "height": 3.2},
                ],
            },
            {
                "id": "park",
                "label": "PARK",
                "label_zh": "公园",
                "note": "最后到公园。",
                "marker": [94, 64],
                "show_label": False,
                "primitives": [
                    {"type": "circle", "cx": 82, "cy": 55, "r": 8, "width": 2.3},
                    {"type": "text", "x": 78, "y": 53, "text": "PARK", "height": 3.4},
                ],
            },
        ],
        ["home", "route", "crossing", "school", "park"],
        ["路线图只表达先后和转弯方向，不代表真实距离与比例。"],
        ["ORIENTATION / NOT TO SCALE", "SCAN QR FOR AUDIO"],
    )


def _bar_chart_scene() -> dict[str, Any]:
    return _template_scene(
        "BAR CHART",
        "柱状图",
        "DATA / FOUR VALUES",
        [
            {
                "id": "axes",
                "label": "AXES",
                "label_zh": "坐标轴",
                "note": "竖轴表示数值，横轴表示类别。",
                "marker": [4, 10],
                "show_label": False,
                "primitives": [
                    {"type": "arrow", "points": [[10, 85], [92, 85]], "width": 2.2},
                    {"type": "arrow", "points": [[10, 85], [10, 12]], "width": 2.2},
                ],
            },
            {
                "id": "bars",
                "label": "BARS",
                "label_zh": "四根柱子",
                "note": "四根柱子的高度分别表示 40、65、30、55。",
                "marker": [80, 40],
                "show_label": False,
                "primitives": [
                    {"type": "polygon", "points": [[20, 85], [30, 85], [30, 45], [20, 45]], "width": 1.8},
                    {"type": "polygon", "points": [[35, 85], [45, 85], [45, 20], [35, 20]], "width": 1.8},
                    {"type": "polygon", "points": [[50, 85], [60, 85], [60, 55], [50, 55]], "width": 1.8},
                    {"type": "polygon", "points": [[65, 85], [75, 85], [75, 30], [65, 30]], "width": 1.8},
                ],
            },
            {
                "id": "labels",
                "label": "LABELS",
                "label_zh": "类别编号",
                "note": "横轴下从左到右是类别 1 到 4。",
                "marker": [8, 92],
                "show_label": False,
                "primitives": [
                    {"type": "text", "x": 23, "y": 88, "text": "1", "height": 4.0},
                    {"type": "text", "x": 38, "y": 88, "text": "2", "height": 4.0},
                    {"type": "text", "x": 53, "y": 88, "text": "3", "height": 4.0},
                    {"type": "text", "x": 68, "y": 88, "text": "4", "height": 4.0},
                ],
            },
        ],
        ["axes", "bars", "labels"],
        ["柱子的高度是相对比例；具体数值以音频和说明中的 40、65、30、55 为准。"],
        ["DATA / RELATIVE HEIGHT", "SCHEMATIC / NOT TO SCALE"],
    )


def _cell_scene() -> dict[str, Any]:
    membrane = _ellipse_points(50.0, 50.0, 38.0, 30.0, 44)
    mitochondria = _ellipse_points(30.0, 66.0, 10.0, 5.0, 24)
    return _template_scene(
        "PLANT CELL",
        "植物细胞结构",
        "BIOLOGY / MEMBRANE NUCLEUS MITOCHONDRIA",
        [
            {
                "id": "membrane",
                "label": "CELL",
                "label_zh": "细胞膜",
                "note": "最外面的大椭圆是细胞膜。",
                "marker": [50, 10],
                "show_label": False,
                "primitives": [
                    {"type": "polygon", "points": membrane, "width": 2.6},
                ],
            },
            {
                "id": "nucleus",
                "label": "NUCLEUS",
                "label_zh": "细胞核",
                "note": "右侧的大圆是细胞核，里面小圆是核仁。",
                "marker": [78, 30],
                "show_label": False,
                "primitives": [
                    {"type": "circle", "cx": 62, "cy": 38, "r": 10, "width": 2.2},
                    {"type": "point", "cx": 62, "cy": 38, "r": 3.2},
                ],
            },
            {
                "id": "mitochondria",
                "label": "MITO",
                "label_zh": "线粒体",
                "note": "左下方的椭圆是线粒体，内部折线表示膜褶。",
                "marker": [16, 74],
                "show_label": False,
                "primitives": [
                    {"type": "polygon", "points": mitochondria, "width": 2.2},
                    {"type": "polyline", "points": [[24, 66], [27, 63], [30, 69], [33, 63], [36, 66]], "width": 1.4},
                ],
            },
            {
                "id": "vacuole",
                "label": "VACUOLE",
                "label_zh": "液泡",
                "note": "左上方的圆是液泡。",
                "marker": [46, 22],
                "show_label": False,
                "primitives": [
                    {"type": "circle", "cx": 34, "cy": 34, "r": 7, "width": 2.2},
                ],
            },
        ],
        ["membrane", "nucleus", "mitochondria", "vacuole"],
        ["这是示意图，细胞器的数量和比例不是真实细胞的比例。"],
        ["BIOLOGY / PLANT CELL", "SCHEMATIC / NOT TO SCALE"],
    )


TEMPLATES: dict[str, dict[str, Any]] = {
    "triangle": {
        "label": "三角形与高",
        "description": "三条边、三个顶点和一条高。",
        "keywords": ["三角形", "三边", "几何", "高线", "triangle", "geometry"],
        "scene": _triangle_scene,
    },
    "pythagorean": {
        "label": "直角三角形 3-4-5",
        "description": "直角边 3、4，斜边 5 的直角三角形。",
        "keywords": ["勾股", "直角三角形", "直角", "斜边", "pythagorean", "right triangle"],
        "scene": _pythagorean_scene,
    },
    "circuit": {
        "label": "串联电路",
        "description": "电池、开关、电阻和灯泡组成的回路。",
        "keywords": ["电路", "电池", "电阻", "开关", "灯泡", "串联", "并联", "circuit", "battery", "resistor", "lamp"],
        "scene": _circuit_scene,
    },
    "flowchart": {
        "label": "流程图",
        "description": "开始、处理、判断、输出的纵向流程。",
        "keywords": ["流程", "步骤", "判断", "框图", "算法", "flowchart", "process", "decision"],
        "scene": _flowchart_scene,
    },
    "coordinate": {
        "label": "平面直角坐标系",
        "description": "横轴、纵轴、原点和四个象限。",
        "keywords": ["坐标系", "象限", "坐标轴", "平面直角", "coordinate", "quadrant"],
        "scene": _coordinate_scene,
    },
    "route": {
        "label": "路线示意图",
        "description": "从家出发，经路口到学校和公园。",
        "keywords": ["地图", "路线", "导航", "校园", "路口", "盲道", "map", "route", "orientation"],
        "scene": _route_scene,
    },
    "bar_chart": {
        "label": "柱状图",
        "description": "四根不同高度的柱子。",
        "keywords": ["柱状图", "条形图", "统计图", "数据图", "bar chart", "histogram"],
        "scene": _bar_chart_scene,
    },
    "cell": {
        "label": "植物细胞",
        "description": "细胞膜、细胞核、线粒体和液泡。",
        "keywords": ["细胞", "细胞核", "线粒体", "液泡", "生物", "cell", "nucleus", "mitochondria"],
        "scene": _cell_scene,
    },
}


def scene_templates() -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for template_id, entry in TEMPLATES.items():
        scene = entry["scene"]()
        result.append(
            {
                "id": template_id,
                "label": entry["label"],
                "description": entry["description"],
                "scene": scene,
                "summary": scene_summary(scene),
            }
        )
    return result


def match_template(text: str) -> tuple[str, dict[str, Any], int] | None:
    haystack = (text or "").lower()
    if not haystack:
        return None
    best: tuple[str, dict[str, Any], int] | None = None
    for template_id, entry in TEMPLATES.items():
        score = sum(1 for keyword in entry["keywords"] if keyword.lower() in haystack)
        if score and (best is None or score > best[2]):
            best = (template_id, copy.deepcopy(entry["scene"]()), score)
    return best
