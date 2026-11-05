"""Deterministic 3D model compiler for the tactile-model mode.

The language model only describes *what* the model contains: parts, shapes,
relative sizes and relative positions.  This module owns *how* it is built:
millimetres, supports, orbit rings, base, braille plaques and printable shells.
That split keeps the result reproducible and stops a model from inventing a
millimetre dimension it cannot know.

Two entry points matter:

* :func:`plan_model3d` turns a request into a validated spec.  The solar-system
  case is compiled offline from a fixed catalogue, so the demo works without a
  network or an API key.
* :func:`build_model3d` turns the spec into a watertight STL.  Overlapping
  shells are intentional (the slicer unions them), exactly like the robot
  volumetric path.

Chinese labels use GF 0019-2018 encoding from :mod:`touchlearn.cnbraille`.
The solar-system model is deliberately *not* to scale: planet radii keep their
real order but are compressed so Mercury and Jupiter can both be touched.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from . import cnbraille
from .agent.llm import chat_json
from .agent.volumetric import (
    _add,
    _add_quad,
    _add_triangle,
    _box_xy,
    _cylinder,
    _mul,
    _prism_xy,
    _sphere,
    _sub,
    _unit,
)
from .geometry import Mesh, Primitive, primitive_polygons, triangulate
from .layout import rect_polygon

Vec3 = tuple[float, float, float]
Point2 = tuple[float, float]

DEFAULT_BASE_RADIUS_MM = 102.0
BASE_THICKNESS_MM = 5.0
ORBIT_HEIGHT_MM = 1.2
BRAILLE_DOT_HEIGHT_MM = 0.8
SUPPORT_RADIUS_MM = 1.8
BUILD_VOLUME_MM = (220.0, 220.0, 250.0)
SHAPE_NAMES = {
    "sphere": "球体",
    "cylinder": "圆柱",
    "box": "长方体",
    "cone": "圆锥",
    "frustum": "圆台",
    "tube": "空心圆管",
    "torus": "圆环",
    "plate": "薄板",
    "profile": "轮廓拉伸体",
}
SUPPORTED_SHAPES = frozenset(SHAPE_NAMES)
MAX_PARTS = 12
# A braille line is 6.8 mm high; a legend row needs one line plus clearance.
BRAILLE_LINE_MM = 6.8
LEGEND_ROW_MM = 8.0

# Real equatorial radii relative to Earth.  They are used only for ordering and
# for the compression formula; the printed radii are in the catalogue below.
PLANETS: tuple[dict[str, Any], ...] = (
    {
        "id": "mercury",
        "name_zh": "水星",
        "name_en": "MERCURY",
        "relative_radius": 0.383,
        "orbit_mm": 38.0,
        "angle_deg": 200.0,
        "note": "离太阳最近，体积最小。",
    },
    {
        "id": "venus",
        "name_zh": "金星",
        "name_en": "VENUS",
        "relative_radius": 0.949,
        "orbit_mm": 46.0,
        "angle_deg": 248.0,
        "note": "大小和地球接近，表面被厚云层包住。",
    },
    {
        "id": "earth",
        "name_zh": "地球",
        "name_en": "EARTH",
        "relative_radius": 1.0,
        "orbit_mm": 54.0,
        "angle_deg": 296.0,
        "note": "目前已知有生命的行星。",
    },
    {
        "id": "mars",
        "name_zh": "火星",
        "name_en": "MARS",
        "relative_radius": 0.532,
        "orbit_mm": 62.0,
        "angle_deg": 344.0,
        "note": "比地球小，表面呈红色。",
    },
    {
        "id": "jupiter",
        "name_zh": "木星",
        "name_en": "JUPITER",
        "relative_radius": 11.21,
        "orbit_mm": 70.0,
        "angle_deg": 32.0,
        "note": "太阳系里最大的行星。",
    },
    {
        "id": "saturn",
        "name_zh": "土星",
        "name_en": "SATURN",
        "relative_radius": 9.45,
        "orbit_mm": 78.0,
        "angle_deg": 80.0,
        "note": "外面有一圈明显的光环。",
    },
    {
        "id": "uranus",
        "name_zh": "天王星",
        "name_en": "URANUS",
        "relative_radius": 4.01,
        "orbit_mm": 86.0,
        "angle_deg": 128.0,
        "note": "体积较大，自转轴倾斜得很厉害。",
    },
    {
        "id": "neptune",
        "name_zh": "海王星",
        "name_en": "NEPTUNE",
        "relative_radius": 3.88,
        "orbit_mm": 94.0,
        "angle_deg": 176.0,
        "note": "离太阳最远的行星。",
    },
)

SOLAR_KEYWORDS = ("太阳系", "行星", "轨道", "solar system", "planet", "orbit", "八大行星", "九大行星")


class Model3DError(ValueError):
    """Raised when a 3D spec cannot be compiled without inventing geometry."""


@dataclass
class Model3DPlan:
    ok: bool
    spec: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    provider: str = ""
    model: str = ""
    message: str = ""
    notes: list[str] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "kind": self.spec.get("kind"),
            "title": self.spec.get("title"),
            "title_zh": self.spec.get("title_zh"),
            "part_count": len(self.spec.get("parts") or []),
            "confidence": round(self.confidence, 3),
            "provider": self.provider,
            "model": self.model,
            "message": self.message,
            "notes": self.notes,
            "questions": self.questions,
        }


@dataclass
class Model3DResult:
    mesh: Mesh
    meta: dict[str, Any]


# ---------------------------------------------------------------------------
# Spec planning
# ---------------------------------------------------------------------------


def _wants_solar(text: str) -> bool:
    lowered = (text or "").lower()
    return any(keyword in lowered for keyword in SOLAR_KEYWORDS)


def _planet_count(text: str) -> int:
    if "八" in text or "8" in text:
        return 8
    if "七" in text or "7" in text:
        return 7
    return 7


def solar_system_spec(text: str = "") -> dict[str, Any]:
    count = max(1, min(len(PLANETS), _planet_count(text)))
    selected = PLANETS[:count]
    planets: list[dict[str, Any]] = []
    for index, entry in enumerate(selected):
        radius = compress_planet_radius(entry["relative_radius"])
        planets.append(
            {
                "id": entry["id"],
                "name_zh": entry["name_zh"],
                "name_en": entry["name_en"],
                "shape": "sphere",
                "relative_radius": entry["relative_radius"],
                "radius_mm": round(radius, 3),
                "orbit_mm": entry["orbit_mm"],
                "angle_deg": entry["angle_deg"],
                "order": index + 1,
                "note": entry["note"],
            }
        )
    return {
        "version": 1,
        "kind": "solar_system",
        "title": "SOLAR SYSTEM",
        "title_zh": "太阳系",
        "summary": f"太阳和 {count} 颗行星，行星位于同心类圆形轨道上，中文盲文名称刻在底座外缘。",
        "base": {
            "shape": "disc",
            "radius_mm": DEFAULT_BASE_RADIUS_MM,
            "thickness_mm": BASE_THICKNESS_MM,
        },
        "sun": {"radius_mm": 20.0, "pedestal_mm": 6.0},
        "planets": planets,
        "planet_count": count,
        "scale_note": "行星半径按真实相对大小排序后压缩，保持大小顺序，不是等比例模型。",
        "source": "catalogue",
    }


def compress_planet_radius(relative: float) -> float:
    """Map a real relative radius into a printable 4.2-12.6 mm band.

    A power compression keeps the ordering (Jupiter > Saturn > Uranus > Earth >
    Venus > Mars > Mercury) while making every planet large enough for a
    support post and a readable silhouette.
    """
    maximum = max(entry["relative_radius"] for entry in PLANETS)
    ratio = max(0.0, relative) / maximum
    return 4.2 + (ratio ** 0.42) * 8.4


def _number(value: Any, *, low: float, high: float, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise Model3DError(f"{label} 必须是数字。")
    number = float(value)
    if not math.isfinite(number):
        raise Model3DError(f"{label} 不是有限数字。")
    return max(low, min(high, number))


PROFILE_PRESETS = {"airfoil", "fin", "ellipse", "rounded_rect"}


def _normalize_outline(
    raw: Any,
    *,
    shape: str,
    label_zh: str,
    warnings: list[str],
    index: int,
) -> Any:
    """Return a profile preset name or a validated list of 0-100 points.

    A ``profile`` part is an extruded 2-D silhouette.  The model may supply its
    own 8-16 points, or name one of the deterministic presets.  When it supplies
    neither, the name decides: wings become an airfoil, fins become a swept
    trapezoid, everything else becomes an ellipse.  That fallback is what stops
    a wing request from collapsing into a handful of unrelated plates.
    """
    if shape != "profile":
        return ""
    if isinstance(raw, str):
        preset = raw.strip().lower()
        if preset in PROFILE_PRESETS:
            return preset
    points: list[list[float]] = []
    if isinstance(raw, list):
        for point in raw[:32]:
            if not isinstance(point, (list, tuple)) or len(point) < 2:
                continue
            try:
                u = _number(point[0], low=0.0, high=100.0, label="outline")
                v = _number(point[1], low=0.0, high=100.0, label="outline")
            except Model3DError:
                continue
            if points and abs(points[-1][0] - u) < 0.5 and abs(points[-1][1] - v) < 0.5:
                continue
            points.append([round(u, 3), round(v, 3)])
    if len(points) >= 3:
        return points
    if points:
        warnings.append(f"部件 {index + 1} 的轮廓点不足 3 个，已改用内置轮廓。")
    name = label_zh or ""
    if any(word in name for word in ("机翼", "尾翼", "翼型", "叶片", "桨叶")):
        return "airfoil"
    if any(word in name for word in ("鳍", "尾鳍", "帆", "靠背")):
        return "fin"
    return "ellipse"


def normalize_model_spec(raw: Any) -> tuple[dict[str, Any], list[str]]:
    """Validate an LLM or catalogue spec without inventing missing parts."""
    if not isinstance(raw, dict):
        raise Model3DError("三维模型规格必须是 JSON 对象。")
    kind = str(raw.get("kind") or "assembly").strip().lower()
    if kind == "solar_system":
        text = str(raw.get("request") or raw.get("summary") or "")
        spec = solar_system_spec(text)
        return spec, []
    title = str(raw.get("title") or "TACTILE 3D MODEL").strip()[:48]
    title_zh = str(raw.get("title_zh") or title).strip()[:48]
    parts_raw = raw.get("parts")
    if not isinstance(parts_raw, list) or not parts_raw:
        raise Model3DError("三维模型至少需要一个 parts 部件。")
    parts: list[dict[str, Any]] = []
    warnings: list[str] = []
    if len(parts_raw) > MAX_PARTS:
        warnings.append(f"部件过多（{len(parts_raw)} 个），只保留前 {MAX_PARTS} 个主要部件。")
    for index, item in enumerate(parts_raw[:MAX_PARTS]):
        if not isinstance(item, dict):
            continue
        shape = str(item.get("shape") or "").strip().lower()
        if shape not in SUPPORTED_SHAPES:
            warnings.append(f"部件 {index + 1} 的形状 {shape or '空'} 不支持，已跳过。")
            continue
        label_zh = str(item.get("name_zh") or item.get("label_zh") or item.get("label") or f"部件{index + 1}").strip()[:24]
        size = item.get("size", 20)
        if isinstance(size, (list, tuple)):
            size_value = [_number(value, low=1.0, high=100.0, label="size") for value in size[:3]]
        else:
            scalar = _number(size, low=1.0, high=100.0, label="size")
            size_value = [scalar, scalar, scalar]
        position = item.get("position", [50, 50, 0])
        if not isinstance(position, (list, tuple)) or len(position) < 2:
            position = [50, 50, 0]
        pos = [
            _number(position[0], low=0.0, high=100.0, label="position"),
            _number(position[1], low=0.0, high=100.0, label="position"),
            _number(position[2] if len(position) > 2 else 0.0, low=0.0, high=100.0, label="position"),
        ]
        outline = _normalize_outline(item.get("outline"), shape=shape, label_zh=label_zh, warnings=warnings, index=index)
        parts.append(
            {
                "id": str(item.get("id") or f"part_{index + 1}").strip()[:32],
                "name_zh": label_zh,
                "name_en": str(item.get("label") or item.get("name_en") or "").strip()[:32],
                "shape": shape,
                "size": size_value,
                "position": pos,
                "rotation_deg": _number(item.get("rotation_deg", 0), low=-180.0, high=180.0, label="rotation_deg"),
                "tilt_deg": _number(item.get("tilt_deg", 0), low=-90.0, high=90.0, label="tilt_deg"),
                "outline": outline,
                "note": str(item.get("note") or "").strip()[:160],
            }
        )
    if not parts:
        raise Model3DError("没有可编译的三维部件。")
    spec = {
        "version": 1,
        "kind": "assembly",
        "title": title,
        "title_zh": title_zh,
        "summary": str(raw.get("summary") or "").strip()[:240],
        "base": {
            "shape": "disc",
            "radius_mm": _number((raw.get("base") or {}).get("radius_mm", 90), low=30, high=110, label="base.radius_mm"),
            "thickness_mm": _number((raw.get("base") or {}).get("thickness_mm", 4), low=2, high=10, label="base.thickness_mm"),
        },
        "parts": parts,
        "scale_note": str(raw.get("scale_note") or "部件大小为相对比例，不是工程尺寸。").strip()[:160],
        "source": str(raw.get("source") or "llm")[:32],
    }
    return spec, warnings


MODEL3D_SYSTEM = (
    "你是触觉三维模型的结构设计助手。你把用户的建模需求转成严格 JSON，供确定性几何编译器生成可打印 STL。"
    "只输出 JSON 对象，不要 Markdown，不要解释。不要输出毫米绝对值；部件大小用 1-100 的相对值，"
    "位置用 0-100 的相对坐标（x 向右，y 向后，z 向上）。宁可少拆几个大部件，也不要用几十个小块拼一个曲面。"
    "不要编造用户没有提供的事实；无法可靠建模时返回 supported=false 并说明原因。"
)

MODEL3D_PROMPT = """请把下面的需求转成触觉三维模型 JSON。

用户需求：
{request}

已有提示：
{hint}

输出结构：
{{
  "supported": true,
  "kind": "assembly",
  "title": "英文标题",
  "title_zh": "中文标题",
  "summary": "一句中文说明",
  "base": {{"shape": "disc", "radius_mm": 90, "thickness_mm": 4}},
  "parts": [
    {{
      "id": "part_1",
      "name_zh": "部件中文名",
      "label": "PART 1",
      "shape": "sphere|cylinder|box|cone|frustum|tube|torus|plate|profile",
      "size": [20, 20, 20],
      "position": [50, 50, 20],
      "rotation_deg": 0,
      "tilt_deg": 0,
      "outline": "airfoil|fin|ellipse|rounded_rect，或 8-16 个 [u,v] 轮廓点（仅 profile 用）",
      "note": "触摸说明"
    }}
  ],
  "scale_note": "相对比例说明",
  "notes": ["制作说明"],
  "questions": ["需要用户确认的点"]
}}

形状与 size 的含义（必须遵守）：
- sphere：size=[直径, 直径, 直径]。
- cylinder：size=[直径, 直径, 高度]，轴竖直。
- box：size=[长, 宽, 高]。
- cone：size=[底直径, 底直径, 高度]，尖端朝上。
- frustum：size=[底直径, 顶直径, 高度]；灯罩、花盆、锥台用它。
- tube：size=[外径, 内径, 高度]；杯壁、灯罩、空心圆筒用它，上下开口，需要杯底时再加一个 cylinder。
- torus：size=[外径, 管径, 管径]；提手、地球仪子午环、圆环用它。
- plate：size=[长, 宽, 厚度]，薄片。
- profile：size=[弦长, 拉伸厚度, 截面高度]；outline 是截面轮廓，u 沿弦长、v 沿截面高度，0-100，沿 y 方向拉伸。机翼、尾翼、鳍片、椅子靠背用它。

常用对象的正确拆法（优先照此，不要自创几十个零件）：
- 尾翼/机翼：1 个 profile 机翼（outline="airfoil" 或给 8-16 点翼型）+ 1 个 cylinder 或 box 机身/连接件。绝不要用一堆小球、小薄板拼机翼。
- 水杯：1 个 tube 杯壁 + 1 个 cylinder 杯底 + 1 个 torus 提手（tilt_deg=90）。
- 地球仪：1 个 sphere 球体 + 1 个 cylinder 支架 + 1 个 torus 子午环（tilt_deg=90）+ 1 个 cylinder 底座。
- 台灯：1 个 box 或 cylinder 底座 + 1 个 cylinder 灯杆 + 1 个 frustum 灯罩 + 1 个 sphere 灯泡。
- 火箭：1 个 cylinder 箭体 + 1 个 cone 头锥 + 3-4 个 profile 或 plate 尾翼。
- 椅子：1 个 box 坐面 + 1 个 box 靠背 + 4 个 cylinder 腿。
- 花瓶：1 个 frustum 或 tube 主体 + 1 个 cylinder 底；需要曲线时用 2-3 段 frustum 叠起来。
- 人物/动物：只做 5-8 个主要体块（头、躯干、四肢），不要做手指、毛发等细节。

规则：
1. 每个部件必须有形状、相对大小和相对位置；不要给出真实毫米尺寸。
2. 只拆 1-8 个主要部件，最多 12 个；只表达能可靠触摸的主要结构，不要逐像素还原照片。
3. position 的 x/y 是俯视位置，z 是离地高度；rotation_deg 绕 Z 轴，tilt_deg 绕 X 轴倾斜（-90 到 90）。
4. 需要底座时在 base 中说明；没有底座时仍会生成一个薄底板以保证可打印。
5. 中文名称会转成 GF 0019-2018 中文盲文；名称要短（建议 2-4 个字）。
6. 如果需求是太阳系，kind 写 solar_system，并让编译器使用内置行星目录。
7. 无法可靠建模时返回 {{"supported": false, "reason": "原因"}}。
"""


def plan_model3d(
    text: str,
    *,
    images: Sequence[str] | None = None,
    hint: str = "",
    model: str | None = None,
    effort: str | None = None,
) -> Model3DPlan:
    request = (text or "").strip()
    if _wants_solar(request):
        spec = solar_system_spec(request)
        return Model3DPlan(
            True,
            spec=spec,
            confidence=0.92,
            provider="catalogue",
            message="已用内置太阳系目录生成三维规格；行星大小按真实顺序压缩。",
            notes=[
                "行星轨道是底座上的同心圆环，行星由立柱支撑在轨道上方。",
                "行星直径只有 9-25 mm，放不下中文盲文名称；名称按轨道顺序刻在底座外缘的圆柱面上，不生成竖立名牌。",
                "底座正面有标题盲文和二维码平台，从标题牌左侧起绕底座摸读行星名称。",
                "行星半径保持真实大小顺序，但经过压缩；不是等比例模型。",
            ],
            questions=["请确认行星数量、中文名称和底座外缘的阅读方向。"],
        )

    if not request and not images:
        return Model3DPlan(False, message="请描述要生成的三维模型。", questions=["例如：太阳系、地球仪、分子结构、火箭。"])
    prompt = MODEL3D_PROMPT.format(request=request[:4000], hint=(hint or "无")[:600])
    result = chat_json(
        MODEL3D_SYSTEM,
        prompt,
        images=images or (),
        kind="vision" if images else "text",
        model=model,
        effort=effort,
    )
    retried = False
    if not result.ok and result.finish_reason == "length" and (effort or "").strip().lower() not in {
        "off",
        "none",
        "disabled",
    }:
        # A high thinking budget can consume the whole output window before the
        # model writes its JSON.  Retry once without hidden reasoning so the
        # user still gets a model instead of a budget error.
        result = chat_json(
            MODEL3D_SYSTEM,
            prompt,
            images=images or (),
            kind="vision" if images else "text",
            model=model,
            effort="off",
        )
        retried = True
    if not result.ok:
        return Model3DPlan(
            False,
            provider=result.provider,
            model=result.model,
            message=result.message,
            questions=["当前没有可用的大模型来理解这个三维需求；可以换成“太阳系”示例，或配置模型后重试。"],
        )
    data = result.data if isinstance(result.data, dict) else None
    if data is None:
        return Model3DPlan(False, provider=result.provider, model=result.model, message="模型返回的不是 JSON 对象。")
    if data.get("supported") is False:
        return Model3DPlan(
            False,
            provider=result.provider,
            model=result.model,
            message=str(data.get("reason") or "模型判断该需求不适合三维建模。"),
        )
    try:
        spec, warnings = normalize_model_spec(data)
    except Model3DError as error:
        return Model3DPlan(False, provider=result.provider, model=result.model, message=str(error))
    notes = [str(item).strip()[:180] for item in (data.get("notes") or []) if isinstance(item, str) and item.strip()][:5]
    if retried:
        notes.insert(0, "思考过程占满了本次输出额度，已自动降低思考强度重试。")
    questions = [str(item).strip()[:180] for item in (data.get("questions") or []) if isinstance(item, str) and item.strip()][:5]
    confidence = data.get("confidence", 0.65)
    try:
        confidence = max(0.0, min(1.0, float(confidence)))
    except (TypeError, ValueError):
        confidence = 0.65
    return Model3DPlan(
        True,
        spec=spec,
        confidence=confidence,
        provider=result.provider,
        model=result.model,
        message="大模型已生成三维结构候选；几何由本地编译器确定。",
        notes=notes + warnings,
        questions=questions,
    )


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------


def _ring(mesh: Mesh, centre: Point2, outer: float, inner: float, z0: float, z1: float, segments: int = 64) -> None:
    if outer <= inner or outer <= 0:
        return
    top = z1
    bottom = z0
    outer_bottom: list[Vec3] = []
    outer_top: list[Vec3] = []
    inner_bottom: list[Vec3] = []
    inner_top: list[Vec3] = []
    for index in range(segments):
        angle = 2.0 * math.pi * index / segments
        cos_a, sin_a = math.cos(angle), math.sin(angle)
        outer_bottom.append((centre[0] + outer * cos_a, centre[1] + outer * sin_a, bottom))
        outer_top.append((centre[0] + outer * cos_a, centre[1] + outer * sin_a, top))
        inner_bottom.append((centre[0] + inner * cos_a, centre[1] + inner * sin_a, bottom))
        inner_top.append((centre[0] + inner * cos_a, centre[1] + inner * sin_a, top))
    centre_top = (centre[0], centre[1], top + 1.0)
    centre_bottom = (centre[0], centre[1], bottom - 1.0)
    for index in range(segments):
        nxt = (index + 1) % segments
        _add_quad(mesh, outer_top[index], outer_top[nxt], inner_top[nxt], inner_top[index], centre_top)
        _add_quad(mesh, outer_bottom[nxt], outer_bottom[index], inner_bottom[index], inner_bottom[nxt], centre_bottom)
        _add_quad(
            mesh,
            outer_bottom[index],
            outer_bottom[nxt],
            outer_top[nxt],
            outer_top[index],
            ((outer_bottom[index][0] + outer_bottom[nxt][0]) / 2 - centre[0], (outer_bottom[index][1] + outer_bottom[nxt][1]) / 2 - centre[1], 0.0),
        )
        _add_quad(
            mesh,
            inner_bottom[nxt],
            inner_bottom[index],
            inner_top[index],
            inner_top[nxt],
            (centre[0] - (inner_bottom[index][0] + inner_bottom[nxt][0]) / 2, centre[1] - (inner_bottom[index][1] + inner_bottom[nxt][1]) / 2, 0.0),
        )


def _base_outline(radius: float, tab_front: float) -> list[Point2]:
    """Round display base with a front tab for the title and QR pads.

    A pure disc has no flat land outside the outer orbit, so the pads would
    overhang the rim.  The tab keeps both pads fully supported without making
    the whole model a square block.
    """
    join_y = -0.68 * radius
    join_x = math.sqrt(max(radius * radius - join_y * join_y, 1.0))
    half = max(20.0, join_x - 1.5)
    start = math.atan2(join_y, join_x)
    end = math.pi - start
    steps = 72
    points: list[Point2] = []
    for index in range(steps + 1):
        angle = start + (end - start) * index / steps
        points.append((radius * math.cos(angle), radius * math.sin(angle)))
    points.append((-half, tab_front))
    points.append((half, tab_front))
    return points


def _frustum(mesh: Mesh, p0: Vec3, p1: Vec3, r0: float, r1: float, segments: int = 28) -> None:
    if r0 <= 0 and r1 <= 0:
        return
    axis = _sub(p1, p0)
    length = math.sqrt(axis[0] ** 2 + axis[1] ** 2 + axis[2] ** 2)
    if length <= 1e-9:
        return
    w = _unit(axis)
    helper = (0.0, 0.0, 1.0) if abs(w[2]) < 0.9 else (1.0, 0.0, 0.0)
    u = _unit((w[1] * helper[2] - w[2] * helper[1], w[2] * helper[0] - w[0] * helper[2], w[0] * helper[1] - w[1] * helper[0]))
    v = (w[1] * u[2] - w[2] * u[1], w[2] * u[0] - w[0] * u[2], w[0] * u[1] - w[1] * u[0])
    ring0: list[Vec3] = []
    ring1: list[Vec3] = []
    for index in range(segments):
        angle = 2.0 * math.pi * index / segments
        offset0 = _add(_mul(u, r0 * math.cos(angle)), _mul(v, r0 * math.sin(angle)))
        offset1 = _add(_mul(u, r1 * math.cos(angle)), _mul(v, r1 * math.sin(angle)))
        ring0.append(_add(p0, offset0))
        ring1.append(_add(p1, offset1))
    centre = _mul(_add(p0, p1), 0.5)
    for index in range(segments):
        nxt = (index + 1) % segments
        if r0 > 0 and r1 > 0:
            _add_quad(mesh, ring0[index], ring0[nxt], ring1[nxt], ring1[index], centre)
        elif r1 == 0:
            _add_triangle(mesh, ring0[index], ring0[nxt], p1, _sub(p1, centre))
        else:
            _add_triangle(mesh, ring1[nxt], ring1[index], p0, _sub(p0, centre))
    if r0 > 0:
        for index in range(segments):
            nxt = (index + 1) % segments
            _add_triangle(mesh, p0, ring0[nxt], ring0[index], _sub(p0, centre))
    if r1 > 0:
        for index in range(segments):
            nxt = (index + 1) % segments
            _add_triangle(mesh, p1, ring1[index], ring1[nxt], _sub(p1, centre))


@dataclass(frozen=True)
class Frame:
    origin: Vec3
    u: Vec3
    v: Vec3
    w: Vec3

    def point(self, du: float, dv: float, dw: float) -> Vec3:
        return (
            self.origin[0] + self.u[0] * du + self.v[0] * dv + self.w[0] * dw,
            self.origin[1] + self.u[1] * du + self.v[1] * dv + self.w[1] * dw,
            self.origin[2] + self.u[2] * du + self.v[2] * dv + self.w[2] * dw,
        )


def _oriented_plate(mesh: Mesh, frame: Frame, width: float, height: float, thickness: float) -> None:
    hw, hh, ht = width / 2.0, height / 2.0, thickness / 2.0
    corners = [
        frame.point(-hw, -hh, -ht),
        frame.point(hw, -hh, -ht),
        frame.point(hw, hh, -ht),
        frame.point(-hw, hh, -ht),
        frame.point(-hw, -hh, ht),
        frame.point(hw, -hh, ht),
        frame.point(hw, hh, ht),
        frame.point(-hw, hh, ht),
    ]
    centre = frame.origin
    for indices, hint in (
        ((4, 5, 6, 7), frame.w),
        ((1, 0, 3, 2), _mul(frame.w, -1.0)),
        ((0, 1, 5, 4), _mul(frame.v, -1.0)),
        ((2, 3, 7, 6), frame.v),
        ((0, 4, 7, 3), _mul(frame.u, -1.0)),
        ((1, 2, 6, 5), frame.u),
    ):
        a, b, c, d = (corners[index] for index in indices)
        _add_quad(mesh, a, b, c, d, hint)


def _braille_on_plate(
    mesh: Mesh,
    frame: Frame,
    text: str,
    width: float,
    height: float,
    thickness: float,
    *,
    dot_height: float = BRAILLE_DOT_HEIGHT_MM,
) -> list[tuple[Vec3, float]]:
    text_width = cnbraille.width(text, syllable_gap_mm=cnbraille.SYLLABLE_GAP_MM)
    text_height = cnbraille.height()
    if text_width > width - 2.0:
        raise Model3DError(f"盲文 {text!r} 宽 {text_width:.1f} mm，放不进 {width:.1f} mm 的名牌。")
    origin_u = -width / 2.0 + (width - text_width) / 2.0
    origin_v = height / 2.0 - (height - text_height) / 2.0
    dots = cnbraille.dot_centers(text, 0.0, 0.0)
    placed: list[tuple[Vec3, float]] = []
    for x, y in dots:
        du = origin_u + x
        dv = origin_v - y
        start = frame.point(du, dv, thickness / 2.0 - 0.12)
        end = frame.point(du, dv, thickness / 2.0 + dot_height)
        _cylinder(mesh, start, end, cnbraille.DOT_DIAMETER_MM / 2.0, segments=12)
        placed.append((start, cnbraille.DOT_DIAMETER_MM / 2.0))
    return placed


def _braille_on_top(mesh: Mesh, text: str, x: float, y: float, z0: float, z1: float) -> None:
    for dot_x, dot_y in cnbraille.dot_centers(text, x, y):
        _cylinder(mesh, (dot_x, dot_y, z0 - 0.1), (dot_x, dot_y, z1), cnbraille.DOT_DIAMETER_MM / 2.0, segments=12)


def _emboss_text_on_top(mesh: Mesh, text: str, x: float, y: float, z0: float, z1: float, height: float) -> None:
    primitive = Primitive("text", ((x, y),), text=text, height=height)
    for polygon in primitive_polygons(primitive):
        _prism_xy(mesh, polygon, z0, z1)


# ---------------------------------------------------------------------------
# Generalised primitives and surface labels
# ---------------------------------------------------------------------------


def _clean_text(text: str, *, max_chars: int = 4) -> str:
    """Keep a readable label intact instead of silently shortening a part name.

    A name that does not fit a surface is not trimmed: the caller falls back to
    the numbered base legend, where the full name can be printed.
    """
    value = "".join(ch for ch in (text or "") if "\u4e00" <= ch <= "\u9fff" or ch.isalnum())
    return value[:max_chars]


def _fit_text(text: str, max_width: float, *, max_chars: int = 4) -> str:
    """Fit the *title* into a plaque, trimming characters only as a last resort."""
    value = _clean_text(text, max_chars=max_chars) or "模型"
    while len(value) > 1:
        try:
            if cnbraille.width(value, syllable_gap_mm=cnbraille.SYLLABLE_GAP_MM) <= max_width:
                break
        except cnbraille.BrailleError:
            pass
        value = value[:-1]
    return value


def _profile_normalized(kind: str) -> list[Point2]:
    """Deterministic 0-100 outlines used when the model gives no points."""
    if kind == "airfoil":
        # A symmetric NACA 00xx-style section.  It keeps the leading edge round
        # and the trailing edge thin, which is what makes a wing read as a wing
        # under a fingertip instead of a stack of plates.
        upper: list[Point2] = []
        lower: list[Point2] = []
        for index in range(13):
            x = 0.5 * (1.0 - math.cos(math.pi * index / 12.0))
            yt = 5.0 * 0.12 * (
                0.2969 * math.sqrt(max(x, 0.0))
                - 0.1260 * x
                - 0.3516 * x * x
                + 0.2843 * x ** 3
                - 0.1015 * x ** 4
            )
            upper.append((x, yt))
            lower.append((x, -yt))
        points = upper + list(reversed(lower[1:-1]))
        max_y = max(abs(point[1]) for point in points) or 1.0
        return [
            (round(point[0] * 100.0, 3), round((point[1] + max_y) / (2.0 * max_y) * 100.0, 3))
            for point in points
        ]
    if kind == "fin":
        return [(0, 0), (55, 0), (85, 80), (100, 100), (60, 100), (0, 25)]
    if kind == "rounded_rect":
        return [(6, 0), (94, 0), (100, 6), (100, 94), (94, 100), (6, 100), (0, 94), (0, 6)]
    # ellipse
    return [
        (
            round(50.0 + 50.0 * math.cos(2.0 * math.pi * index / 24.0), 3),
            round(50.0 + 50.0 * math.sin(2.0 * math.pi * index / 24.0), 3),
        )
        for index in range(24)
    ]


def _profile_local_points(outline: Any, sx: float, sz: float) -> list[Point2]:
    if isinstance(outline, list) and len(outline) >= 3:
        raw = [(float(point[0]), float(point[1])) for point in outline]
    else:
        raw = _profile_normalized(str(outline or "ellipse"))
    return [((u / 100.0 - 0.5) * sx, (v / 100.0) * sz) for u, v in raw]


def _profile_prism(mesh: Mesh, points: Sequence[Point2], depth: float) -> None:
    """Extrude an X-Z polygon along Y with flat caps."""
    if len(points) < 3 or depth <= 0:
        return
    polygon = [(float(x), float(z)) for x, z in points]
    area = 0.0
    for index, point in enumerate(polygon):
        nxt = polygon[(index + 1) % len(polygon)]
        area += point[0] * nxt[1] - nxt[0] * point[1]
    if abs(area) < 1e-6:
        return
    if area < 0:
        polygon.reverse()
    y0, y1 = -depth / 2.0, depth / 2.0
    for a, b, c in triangulate(polygon):
        _add_triangle(mesh, (a[0], y0, a[1]), (b[0], y0, b[1]), (c[0], y0, c[1]), (0.0, -1.0, 0.0))
        _add_triangle(mesh, (a[0], y1, a[1]), (b[0], y1, b[1]), (c[0], y1, c[1]), (0.0, 1.0, 0.0))
    centre_x = sum(point[0] for point in polygon) / len(polygon)
    centre_z = sum(point[1] for point in polygon) / len(polygon)
    for index, point in enumerate(polygon):
        nxt = polygon[(index + 1) % len(polygon)]
        hint = (
            (point[0] + nxt[0]) / 2.0 - centre_x,
            0.0,
            (point[1] + nxt[1]) / 2.0 - centre_z,
        )
        _add_quad(
            mesh,
            (point[0], y0, point[1]),
            (nxt[0], y0, nxt[1]),
            (nxt[0], y1, nxt[1]),
            (point[0], y1, point[1]),
            hint,
        )


def _torus(
    mesh: Mesh,
    centre: Vec3,
    major_r: float,
    minor_r: float,
    *,
    major_segments: int = 36,
    minor_segments: int = 12,
) -> None:
    if major_r <= 0 or minor_r <= 0:
        return
    rings: list[tuple[list[Vec3], Vec3]] = []
    for index in range(major_segments):
        angle = 2.0 * math.pi * index / major_segments
        cos_a, sin_a = math.cos(angle), math.sin(angle)
        tube_centre = (centre[0] + major_r * cos_a, centre[1] + major_r * sin_a, centre[2])
        ring: list[Vec3] = []
        for minor in range(minor_segments):
            phi = 2.0 * math.pi * minor / minor_segments
            radius = major_r + minor_r * math.cos(phi)
            ring.append(
                (
                    centre[0] + radius * cos_a,
                    centre[1] + radius * sin_a,
                    centre[2] + minor_r * math.sin(phi),
                )
            )
        rings.append((ring, tube_centre))
    for index in range(major_segments):
        ring, tube_centre = rings[index]
        nxt_ring, _ = rings[(index + 1) % major_segments]
        for minor in range(minor_segments):
            nxt = (minor + 1) % minor_segments
            point = ring[minor]
            hint = (point[0] - tube_centre[0], point[1] - tube_centre[1], point[2] - tube_centre[2])
            _add_quad(mesh, ring[minor], ring[nxt], nxt_ring[nxt], nxt_ring[minor], hint)


def _transform_part(local: Mesh, *, tilt_deg: float, rotation_deg: float, x: float, y: float, z: float) -> Mesh:
    """Tilt about X, rotate about Z, drop onto the base and translate."""
    if not local.triangles:
        return Mesh([])
    tilt = math.radians(float(tilt_deg or 0.0))
    spin = math.radians(float(rotation_deg or 0.0))
    ct, st = math.cos(tilt), math.sin(tilt)
    cs, ss = math.cos(spin), math.sin(spin)
    rotated: list[tuple[Vec3, Vec3, Vec3]] = []
    min_z = float("inf")
    for a, b, c in local.triangles:
        triangle: list[Vec3] = []
        for px, py, pz in (a, b, c):
            ty = py * ct - pz * st
            tz = py * st + pz * ct
            rx = px * cs - ty * ss
            ry = px * ss + ty * cs
            triangle.append((rx, ry, tz))
            min_z = min(min_z, tz)
        rotated.append((triangle[0], triangle[1], triangle[2]))
    if min_z == float("inf"):
        return Mesh([])
    lift = -min_z
    return Mesh(
        [
            (
                (a[0] + x, a[1] + y, a[2] + lift + z),
                (b[0] + x, b[1] + y, b[2] + lift + z),
                (c[0] + x, c[1] + y, c[2] + lift + z),
            )
            for a, b, c in rotated
        ]
    )


def _braille_on_flat(
    mesh: Mesh,
    origin: Vec3,
    u: Vec3,
    v: Vec3,
    w: Vec3,
    text: str,
    width: float,
    height: float,
) -> int:
    """Emboss braille directly on a flat face; dots protrude along ``w``."""
    text_width = cnbraille.width(text, syllable_gap_mm=cnbraille.SYLLABLE_GAP_MM)
    if text_width > width - 3.0 or cnbraille.height() > height - 1.2:
        raise Model3DError(f"盲文 {text!r} 放不进 {width:.1f}×{height:.1f} mm 的平面。")
    frame = Frame(origin=origin, u=_unit(u), v=_unit(v), w=_unit(w))
    return len(_braille_on_plate(mesh, frame, text, width, height, 0.0))


def _braille_on_cylinder_wall(
    mesh: Mesh,
    centre: Point2,
    radius_bottom: float,
    radius_top: float,
    z0: float,
    z1: float,
    text: str,
    *,
    face_angle_deg: float = -90.0,
    max_arc_deg: float = 160.0,
) -> int:
    """Emboss braille around the outside of a cylinder or a tapered wall."""
    text_width = cnbraille.width(text, syllable_gap_mm=cnbraille.SYLLABLE_GAP_MM)
    text_height = cnbraille.height()
    if z1 - z0 < text_height + 1.0:
        raise Model3DError(f"盲文 {text!r} 放不进 {z1 - z0:.1f} mm 高的侧壁。")
    radius = max(1.0, (radius_bottom + radius_top) / 2.0)
    if text_width > 2.0 * math.pi * radius * max_arc_deg / 360.0 - 2.0:
        raise Model3DError(f"盲文 {text!r} 太长，放不进圆柱侧壁。")
    face = math.radians(face_angle_deg)
    z_centre = (z0 + z1) / 2.0
    placed = 0
    for dot_x, dot_y in cnbraille.dot_centers(text, 0.0, 0.0):
        u = dot_x - text_width / 2.0
        angle = face + u / radius
        z = z_centre + (text_height / 2.0 - dot_y)
        t = 0.0 if z1 <= z0 else (z - z0) / (z1 - z0)
        local_radius = radius_bottom + (radius_top - radius_bottom) * max(0.0, min(1.0, t))
        cos_a, sin_a = math.cos(angle), math.sin(angle)
        start = (
            centre[0] + cos_a * (local_radius - 0.15),
            centre[1] + sin_a * (local_radius - 0.15),
            z,
        )
        end = (
            centre[0] + cos_a * (local_radius + BRAILLE_DOT_HEIGHT_MM),
            centre[1] + sin_a * (local_radius + BRAILLE_DOT_HEIGHT_MM),
            z,
        )
        _cylinder(mesh, start, end, cnbraille.DOT_DIAMETER_MM / 2.0, segments=10)
        placed += 1
    return placed


def _braille_on_sphere_cap(mesh: Mesh, centre: Vec3, radius: float, text: str) -> int:
    """Emboss braille on the top cap of a sphere, following its curvature."""
    text_width = cnbraille.width(text, syllable_gap_mm=cnbraille.SYLLABLE_GAP_MM)
    text_height = cnbraille.height()
    if max(text_width, text_height) > radius * 1.35:
        raise Model3DError(f"盲文 {text!r} 放不进半径 {radius:.1f} mm 的球冠。")
    placed = 0
    for dot_x, dot_y in cnbraille.dot_centers(text, 0.0, 0.0):
        u = dot_x - text_width / 2.0
        v = text_height / 2.0 - dot_y
        distance = math.hypot(u, v)
        if distance > radius * 0.86:
            raise Model3DError(f"盲文 {text!r} 超出球冠中心区域。")
        z = math.sqrt(max(radius * radius - distance * distance, 0.0))
        normal = (u / radius, v / radius, z / radius)
        surface = (centre[0] + u, centre[1] + v, centre[2] + z)
        start = (
            surface[0] - normal[0] * 0.15,
            surface[1] - normal[1] * 0.15,
            surface[2] - normal[2] * 0.15,
        )
        end = (
            surface[0] + normal[0] * BRAILLE_DOT_HEIGHT_MM,
            surface[1] + normal[1] * BRAILLE_DOT_HEIGHT_MM,
            surface[2] + normal[2] * BRAILLE_DOT_HEIGHT_MM,
        )
        _cylinder(mesh, start, end, cnbraille.DOT_DIAMETER_MM / 2.0, segments=10)
        placed += 1
    return placed


def _point_in_polygon(x: float, z: float, polygon: Sequence[Point2]) -> bool:
    inside = False
    count = len(polygon)
    for index in range(count):
        x1, z1 = polygon[index]
        x2, z2 = polygon[(index + 1) % count]
        if (z1 > z) != (z2 > z):
            cross_x = x1 + (z - z1) * (x2 - x1) / (z2 - z1)
            if cross_x > x:
                inside = not inside
    return inside


def _braille_on_profile_face(
    mesh: Mesh,
    points: Sequence[Point2],
    depth: float,
    text: str,
    *,
    side: float = -1.0,
) -> int:
    """Emboss braille on a flat profile end cap, keeping every dot inside it."""
    text_width = cnbraille.width(text, syllable_gap_mm=cnbraille.SYLLABLE_GAP_MM)
    text_height = cnbraille.height()
    xs = [point[0] for point in points]
    zs = [point[1] for point in points]
    centre_x = (min(xs) + max(xs)) / 2.0
    centre_z = (min(zs) + max(zs)) / 2.0
    dots = cnbraille.dot_centers(text, 0.0, 0.0)
    candidates: list[tuple[float, float]] = []
    for dot_x, dot_y in dots:
        x = centre_x + dot_x - text_width / 2.0
        z = centre_z + (text_height / 2.0 - dot_y)
        if not _point_in_polygon(x, z, points):
            raise Model3DError(f"盲文 {text!r} 会落到轮廓外面。")
        candidates.append((x, z))
    face_y = side * depth / 2.0
    for x, z in candidates:
        _cylinder(
            mesh,
            (x, face_y - side * 0.15, z),
            (x, face_y + side * BRAILLE_DOT_HEIGHT_MM, z),
            cnbraille.DOT_DIAMETER_MM / 2.0,
            segments=10,
        )
    return len(candidates)


def _tab_metrics(radius: float, extra: float) -> tuple[float, float, float]:
    join_y = -0.68 * radius
    join_x = math.sqrt(max(radius * radius - join_y * join_y, 1.0))
    return join_y, max(20.0, join_x - 1.5), -(radius + extra)


def _legend_panel(
    mesh: Mesh,
    lines: Sequence[str],
    *,
    left: float,
    front: float,
    width: float,
    max_depth: float,
    top: float,
) -> tuple[float, int]:
    """Draw a raised braille legend panel and return its depth and column count."""
    if not lines or width < 24.0 or max_depth < LEGEND_ROW_MM:
        return 0.0, 0
    widths = []
    for line in lines:
        try:
            widths.append(cnbraille.width(line, syllable_gap_mm=cnbraille.SYLLABLE_GAP_MM))
        except cnbraille.BrailleError:
            widths.append(0.0)
    max_width = max(widths) if widths else 0.0
    column_width = max_width + 5.0
    columns = max(1, min(len(lines), int(max(1.0, (width - 4.0) // column_width))))
    rows = int(math.ceil(len(lines) / columns))
    row_height = LEGEND_ROW_MM
    depth = rows * row_height + 4.0
    if depth > max_depth:
        # Add columns rather than shrinking a standard braille cell.
        columns = min(len(lines), max(columns, int(math.ceil(len(lines) * row_height / max(1.0, max_depth - 4.0)))))
        column_width = max(1.0, (width - 4.0) / columns)
        rows = int(math.ceil(len(lines) / columns))
        depth = min(max_depth, rows * row_height + 4.0)
    _box_xy(mesh, left, front, width, depth, top - 0.1, top + 1.2, chamfer=1.5)
    for index, line in enumerate(lines):
        column = index // rows
        row = index % rows
        x = left + 2.0 + column * column_width
        y = front + 2.0 + row * row_height
        try:
            _braille_on_top(mesh, line, x, y, top + 1.1, top + 1.1 + BRAILLE_DOT_HEIGHT_MM)
        except cnbraille.BrailleError:
            continue
    return depth, columns


# ---------------------------------------------------------------------------
# Solar-system compiler
# ---------------------------------------------------------------------------


def _solar_system_mesh(spec: dict[str, Any]) -> Model3DResult:
    mesh = Mesh([])
    base = spec.get("base") or {}
    base_radius = float(base.get("radius_mm") or DEFAULT_BASE_RADIUS_MM)
    # A braille line is 6.8 mm high.  The round wall of the base is the only
    # continuous surface large enough to hold every planet name, so the base is
    # at least thick enough for one line of braille on its rim.
    base_thickness = max(10.0, float(base.get("thickness_mm") or BASE_THICKNESS_MM))
    top = base_thickness
    # The braille dots on the far rim add 0.8 mm, so the tab is 15 mm rather
    # than 16 mm deep to keep the whole model inside a 220 mm build volume.
    tab_front = -(base_radius + 15.0)

    # Round body plus a front tab.  The tab is what lets the title plaque and
    # the QR pad sit on solid material instead of overhanging the rim.
    _prism_xy(mesh, _base_outline(base_radius, tab_front), 0.0, top)
    _cylinder(mesh, (0.0, 0.0, top - 0.1), (0.0, 0.0, top + 0.5), base_radius - 1.5, segments=96)

    # Sun pedestal and sun.
    sun_radius = float((spec.get("sun") or {}).get("radius_mm") or 20.0)
    pedestal = float((spec.get("sun") or {}).get("pedestal_mm") or 6.0)
    _cylinder(mesh, (0.0, 0.0, top), (0.0, 0.0, top + pedestal), 10.0, segments=40)
    sun_centre = (0.0, 0.0, top + pedestal + sun_radius)
    _sphere(mesh, sun_centre, sun_radius, slices=40, stacks=24)

    # Orbit rings.
    for planet in spec.get("planets") or []:
        orbit = float(planet["orbit_mm"])
        _ring(mesh, (0.0, 0.0), orbit + 1.3, orbit - 1.3, top, top + ORBIT_HEIGHT_MM, segments=96)

    labels: list[dict[str, Any]] = []
    for planet in spec.get("planets") or []:
        orbit = float(planet["orbit_mm"])
        angle = math.radians(float(planet["angle_deg"]))
        radius = float(planet["radius_mm"])
        x = math.cos(angle) * orbit
        y = math.sin(angle) * orbit
        centre_z = top + 34.0 + radius
        centre = (x, y, centre_z)

        # Support post from the base to the planet's lower surface.
        _cylinder(mesh, (x, y, top - 0.1), (x, y, centre_z - radius + 0.2), SUPPORT_RADIUS_MM, segments=20)
        _sphere(mesh, centre, radius, slices=32, stacks=20)

        if planet.get("id") == "saturn":
            _ring(mesh, (x, y), radius * 1.85, radius * 1.28, centre_z - 0.6, centre_z + 0.6, segments=48)

    # Planet names are engraved on the outer wall of the round base, in orbit
    # order starting at the left edge of the front tab.  A 9-25 mm planet can
    # not carry a 25-53 mm braille name, so this is the honest base-surface
    # fallback: no standing flags, no bridges, just one continuous legend that
    # the reader can follow with a fingertip while turning the base.
    planets = list(spec.get("planets") or [])
    legend_lines: list[str] = []
    legend_angles: list[float] = []
    if planets:
        widths = [
            cnbraille.width(str(planet.get("name_zh") or ""), syllable_gap_mm=cnbraille.SYLLABLE_GAP_MM)
            for planet in planets
        ]
        arcs = [math.degrees(width / base_radius) for width in widths]
        join_y = -0.68 * base_radius
        join_x = math.sqrt(max(base_radius * base_radius - join_y * join_y, 1.0))
        start_deg = math.degrees(math.atan2(join_y, join_x)) + 8.0
        end_deg = 180.0 - (start_deg - 8.0) - 8.0
        available = max(0.0, end_deg - start_deg)
        needed = sum(arcs)
        gap = 6.0 if len(arcs) == 1 else max(2.0, min(10.0, (available - needed) / (len(arcs) - 1)))
        cursor = start_deg + max(0.0, (available - needed - gap * (len(arcs) - 1)) / 2.0)
        for index, planet in enumerate(planets, start=1):
            label = str(planet.get("name_zh") or "")
            arc = arcs[index - 1]
            centre_deg = cursor + arc / 2.0
            dots = _braille_on_cylinder_wall(
                mesh,
                (0.0, 0.0),
                base_radius,
                base_radius,
                0.0,
                top,
                label,
                face_angle_deg=centre_deg,
                max_arc_deg=arc + 2.0,
            )
            legend_lines.append(label)
            legend_angles.append(round(centre_deg, 2))
            labels.append(
                {
                    "planet": planet.get("id"),
                    "text": label,
                    "cells": [list(cell) for cell in cnbraille.encode_text(label)],
                    "cell_count": len(cnbraille.encode_text(label)),
                    "dots": dots,
                    "placement": "base_wall_legend",
                    "legend_index": index,
                    "legend_line": label,
                    "angle_deg": round(centre_deg, 2),
                }
            )
            cursor += arc + gap

    # Title plaque and QR pad sit side by side on the front tab.  Both tops are
    # above the orbit rings, so a ring that passes underneath cannot break the
    # flat reading or scanning surface.
    plaque_width = 72.0
    plaque_depth = 20.0
    plaque_left = -56.0
    plaque_front = tab_front + 4.0
    _box_xy(mesh, plaque_left, plaque_front, plaque_width, plaque_depth, top - 0.1, top + 2.2, chamfer=2.0)
    _emboss_text_on_top(mesh, "SOLAR SYSTEM", plaque_left + 4.0, plaque_front + 5.0, top + 2.0, top + 2.9, 4.2)
    _braille_on_top(mesh, "太阳系", plaque_left + 6.0, plaque_front + 11.0, top + 2.0, top + 2.75)

    qr_size = 32.0
    qr_left = 24.0
    qr_front = tab_front + 4.0
    _box_xy(mesh, qr_left, qr_front, qr_size, qr_size, top - 0.1, top + 1.8, chamfer=2.0)
    _emboss_text_on_top(mesh, "QR", qr_left + 4.0, qr_front + 10.0, top + 1.7, top + 2.4, 4.0)

    bounds = mesh.bounds()
    size = [bounds[3] - bounds[0], bounds[4] - bounds[1], bounds[5] - bounds[2]]
    meta = {
        "kind": "solar_system",
        "title": spec.get("title"),
        "title_zh": spec.get("title_zh"),
        "planet_count": len(spec.get("planets") or []),
        "base_mm": [round(base_radius * 2.0, 2), round(base_radius - tab_front, 2), round(base_thickness, 2)],
        "base_tab_mm": round(abs(tab_front) - base_radius, 2),
        "orbit_radii_mm": [round(float(planet["orbit_mm"]), 2) for planet in spec.get("planets") or []],
        "planets": [
            {
                "id": planet.get("id"),
                "name_zh": planet.get("name_zh"),
                "name_en": planet.get("name_en"),
                "radius_mm": round(float(planet["radius_mm"]), 3),
                "relative_radius": planet.get("relative_radius"),
                "orbit_mm": planet.get("orbit_mm"),
                "angle_deg": planet.get("angle_deg"),
            }
            for planet in spec.get("planets") or []
        ],
        "braille": {
            "standard": "GF 0019-2018",
            "labels": labels,
            "dot_diameter_mm": cnbraille.DOT_DIAMETER_MM,
            "dot_height_mm": BRAILLE_DOT_HEIGHT_MM,
            "label_strategy": "base_wall_legend_in_orbit_order",
            "note": "行星直径只有 9-25 mm，放不下 25-53 mm 的盲文名称，所以名称按离太阳由近到远的顺序刻在底座外缘的圆柱面上；从正面标签牌左侧起，绕底座一周摸读。不生成竖立名牌。",
        },
        "legend": {
            "placement": "base_outer_wall",
            "lines": legend_lines,
            "columns": 1,
            "angles_deg": legend_angles,
            "note": "底座外缘连续盲文，顺序与轨道由内到外一致。",
        },
        "standing_flags": 0,
        "qr_pad_mm": qr_size,
        "scale_note": spec.get("scale_note"),
        "size_mm": [round(value, 3) for value in size],
        "watertight": mesh.is_watertight(),
        "triangles": mesh.triangle_count(),
        "volume_mm3": round(mesh.volume(), 3),
    }
    return Model3DResult(mesh, meta)


# ---------------------------------------------------------------------------
# Generic assembly compiler
# ---------------------------------------------------------------------------


def _emboss_fit(
    mesh: Mesh,
    text: str,
    x: float,
    y: float,
    z0: float,
    z1: float,
    max_width: float,
) -> None:
    """Emboss a short Latin title, shrinking it until it fits the plaque."""
    value = (text or "").strip()[:16]
    if not value:
        return
    height = max(1.8, min(4.2, (max_width - 6.0) / (max(1, len(value)) * 0.62)))
    try:
        _emboss_text_on_top(mesh, value, x, y, z0, z1, height)
    except Exception:
        pass


def _local_part_mesh(part: dict[str, Any], sx: float, sy: float, sz: float) -> tuple[Mesh, dict[str, Any] | None]:
    """Build one part in local coordinates and try to emboss its name on it.

    Local coordinates keep the label glued to the part: the whole shell, dots
    included, is rotated and translated afterwards.  A part that has no usable
    flat or gently curved surface returns no label and is listed in the base
    legend instead.
    """
    shape = part["shape"]
    local = Mesh([])
    name = str(part.get("name_zh") or part.get("id") or "").strip()
    label_meta: dict[str, Any] | None = None

    def finish(placement: str, text: str, dots: int) -> None:
        nonlocal label_meta
        try:
            cells = [list(cell) for cell in cnbraille.encode_text(text)]
        except cnbraille.BrailleError:
            return
        label_meta = {
            "kind": "part",
            "part": part.get("id"),
            "text": text,
            "cells": cells,
            "cell_count": len(cells),
            "dots": dots,
            "placement": placement,
        }

    if shape == "sphere":
        radius = max(sx, sy, sz) / 2.0
        _sphere(local, (0.0, 0.0, radius), radius, slices=32, stacks=20)
        if name and radius >= 7.0:
            text = _clean_text(name)
            try:
                finish("sphere_cap", text, _braille_on_sphere_cap(local, (0.0, 0.0, radius), radius, text))
            except (Model3DError, cnbraille.BrailleError):
                pass
        return local, label_meta

    if shape == "cone":
        radius = max(sx, sy) / 2.0
        _frustum(local, (0.0, 0.0, 0.0), (0.0, 0.0, sz), radius, 0.0, segments=40)
        if name and sz >= BRAILLE_LINE_MM + 2.0 and radius >= 9.0:
            text = _clean_text(name)
            try:
                finish(
                    "wall",
                    text,
                    _braille_on_cylinder_wall(local, (0.0, 0.0), radius, radius * 0.12, 0.0, sz, text, max_arc_deg=220.0),
                )
            except (Model3DError, cnbraille.BrailleError):
                pass
        return local, label_meta

    if shape == "torus":
        outer = max(sx, sy)
        tube = max(1.8, min(sz, outer * 0.35))
        major = max(1.0, (outer - tube) / 2.0)
        _torus(local, (0.0, 0.0, tube / 2.0), major, tube / 2.0)
        return local, label_meta

    if shape == "profile":
        points = _profile_local_points(part.get("outline"), sx, sz)
        _profile_prism(local, points, sy)
        if name and sy >= 1.2:
            text = _clean_text(name)
            try:
                finish("profile_face", text, _braille_on_profile_face(local, points, sy, text, side=-1.0))
            except (Model3DError, cnbraille.BrailleError):
                pass
        return local, label_meta

    if shape == "tube":
        outer_r = max(1.0, sx / 2.0)
        inner_r = min(max(0.0, sy / 2.0), outer_r - 0.8)
        if inner_r < 0.5:
            inner_r = outer_r * 0.72
        _ring(local, (0.0, 0.0), outer_r, inner_r, 0.0, sz, segments=48)
        if name and sz >= BRAILLE_LINE_MM + 2.0:
            text = _clean_text(name)
            try:
                finish("wall", text, _braille_on_cylinder_wall(local, (0.0, 0.0), outer_r, outer_r, 0.0, sz, text))
            except (Model3DError, cnbraille.BrailleError):
                pass
        return local, label_meta

    if shape == "frustum":
        bottom_r = max(1.0, sx / 2.0)
        top_r = max(1.0, sy / 2.0)
        _frustum(local, (0.0, 0.0, 0.0), (0.0, 0.0, sz), bottom_r, top_r, segments=48)
        if name and sz >= BRAILLE_LINE_MM + 2.0:
            text = _clean_text(name)
            try:
                finish("wall", text, _braille_on_cylinder_wall(local, (0.0, 0.0), bottom_r, top_r, 0.0, sz, text))
            except (Model3DError, cnbraille.BrailleError):
                pass
        if label_meta is None and name and top_r >= 9.0:
            text = _clean_text(name)
            try:
                finish(
                    "top",
                    text,
                    _braille_on_flat(local, (0.0, 0.0, sz), (1, 0, 0), (0, 1, 0), (0, 0, 1), text, top_r * 2.0, top_r * 2.0),
                )
            except (Model3DError, cnbraille.BrailleError):
                pass
        return local, label_meta

    if shape == "plate":
        _box_xy(local, -sx / 2.0, -sy / 2.0, sx, sy, 0.0, max(1.0, sz), chamfer=1.0)
        if name:
            text = _clean_text(name)
            try:
                finish("top", text, _braille_on_flat(local, (0.0, 0.0, max(1.0, sz)), (1, 0, 0), (0, 1, 0), (0, 0, 1), text, sx, sy))
            except (Model3DError, cnbraille.BrailleError):
                pass
        return local, label_meta

    if shape == "box":
        _box_xy(local, -sx / 2.0, -sy / 2.0, sx, sy, 0.0, sz, chamfer=0.8)
        if name:
            text = _clean_text(name)
            try:
                finish("top", text, _braille_on_flat(local, (0.0, 0.0, sz), (1, 0, 0), (0, 1, 0), (0, 0, 1), text, sx, sy))
            except (Model3DError, cnbraille.BrailleError):
                try:
                    finish(
                        "front",
                        text,
                        _braille_on_flat(local, (0.0, -sy / 2.0, sz / 2.0), (1, 0, 0), (0, 0, 1), (0, -1, 0), text, sx, sz),
                    )
                except (Model3DError, cnbraille.BrailleError):
                    pass
        return local, label_meta

    # cylinder (the default shape)
    radius = max(1.0, (sx + sy) / 4.0)
    _frustum(local, (0.0, 0.0, 0.0), (0.0, 0.0, sz), radius, radius, segments=48)
    if name and sz >= BRAILLE_LINE_MM + 2.0:
        text = _clean_text(name)
        try:
            finish("wall", text, _braille_on_cylinder_wall(local, (0.0, 0.0), radius, radius, 0.0, sz, text))
        except (Model3DError, cnbraille.BrailleError):
            pass
    if label_meta is None and name and radius >= 9.0:
        text = _clean_text(name)
        try:
            finish(
                "top",
                text,
                _braille_on_flat(local, (0.0, 0.0, sz), (1, 0, 0), (0, 1, 0), (0, 0, 1), text, radius * 2.0, radius * 2.0),
            )
        except (Model3DError, cnbraille.BrailleError):
            pass
    return local, label_meta


def _assembly_mesh(spec: dict[str, Any], *, max_height_mm: float = 180.0) -> Model3DResult:
    mesh = Mesh([])
    base = spec.get("base") or {}
    # The display base carries a front tab for the title, QR and legend.  Keep
    # the overall depth inside the 220 mm build volume even with a long legend.
    base_radius = max(50.0, min(float(base.get("radius_mm") or 88.0), 88.0))
    base_thickness = float(base.get("thickness_mm") or 4.0)
    top = base_thickness
    join_y, tab_half, tab_front = _tab_metrics(base_radius, 40.0)
    _prism_xy(mesh, _base_outline(base_radius, tab_front), 0.0, top)
    _cylinder(mesh, (0.0, 0.0, top - 0.1), (0.0, 0.0, top + 0.5), base_radius - 1.5, segments=96)

    usable = base_radius * 0.74
    size_scale = base_radius * 1.35
    parts = spec.get("parts") or []
    placed: list[dict[str, Any]] = []
    label_by_part: dict[str, dict[str, Any]] = {}
    unlabeled: list[tuple[int, dict[str, Any]]] = []
    for index, part in enumerate(parts):
        size = part.get("size") or [20, 20, 20]
        position = part.get("position") or [50, 50, 0]
        # 0-100 -> centred coordinates; z 0-100 -> above the base.
        x = (float(position[0]) - 50.0) / 50.0 * usable
        y = (float(position[1]) - 50.0) / 50.0 * usable
        z = top + max(0.0, float(position[2]) / 100.0 * max_height_mm)
        sx = max(1.0, float(size[0]) / 100.0 * size_scale)
        sy = max(1.0, float(size[1]) / 100.0 * size_scale)
        sz = max(1.0, float(size[2]) / 100.0 * max_height_mm)
        tilt = float(part.get("tilt_deg") or 0.0)
        rotation = float(part.get("rotation_deg") or 0.0)
        # Keep every part inside the round base; a tilted part needs extra room.
        radial_extent = max(sx, sy) / 2.0 + abs(math.sin(math.radians(tilt))) * sz / 2.0
        radial_limit = max(0.0, base_radius - radial_extent - 2.0)
        raw_radius = math.hypot(x, y)
        if raw_radius > radial_limit > 0.0:
            scale = radial_limit / raw_radius
            x *= scale
            y *= scale
        local, label_meta = _local_part_mesh(part, sx, sy, sz)
        world = _transform_part(local, tilt_deg=tilt, rotation_deg=rotation, x=x, y=y, z=z)
        mesh.triangles.extend(world.triangles)
        bounds = world.bounds()
        bottom, top_z = bounds[2], bounds[5]
        if bottom > top + 0.5:
            _cylinder(mesh, (x, y, top - 0.1), (x, y, bottom + 0.2), 1.5, segments=16)
        placed.append(
            {
                "id": part.get("id"),
                "name_zh": part.get("name_zh"),
                "shape": part["shape"],
                "centre_mm": [round(x, 2), round(y, 2), round((bottom + top_z) / 2.0, 2)],
                "size_mm": [round(sx, 2), round(sy, 2), round(top_z - bottom, 2)],
                "rotation_deg": rotation,
                "tilt_deg": tilt,
                "label_placement": (label_meta or {}).get("placement") or "base_legend",
            }
        )
        part_id = str(part.get("id") or f"part_{index + 1}")
        if label_meta:
            label_by_part[part_id] = label_meta
        else:
            unlabeled.append((index + 1, part, part_id))

    # Title plaque and QR pad sit on the front tab.  The plaque is a flat top
    # surface, so the title is genuine surface braille rather than a flag.
    title_text = _fit_text(str(spec.get("title_zh") or "模型"), 58.0, max_chars=8)
    title_left = -tab_half + 4.0
    qr_size = 28.0 if tab_half * 2.0 >= 104.0 else 24.0
    qr_left = tab_half - 4.0 - qr_size
    title_width = max(28.0, min(72.0, qr_left - 4.0 - title_left))
    plaque_front = tab_front + 4.0
    _box_xy(mesh, title_left, plaque_front, title_width, 18.0, top - 0.1, top + 2.2, chamfer=2.0)
    _emboss_fit(mesh, str(spec.get("title") or "MODEL"), title_left + 4.0, plaque_front + 5.0, top + 2.0, top + 2.9, title_width - 6.0)
    _braille_on_top(mesh, title_text, title_left + 5.0, plaque_front + 10.5, top + 2.0, top + 2.75)
    _box_xy(mesh, qr_left, plaque_front, qr_size, qr_size, top - 0.1, top + 1.8, chamfer=2.0)
    _emboss_text_on_top(mesh, "QR", qr_left + qr_size * 0.22, plaque_front + qr_size * 0.40, top + 1.7, top + 2.4, 4.0)

    # Anything that could not take a direct surface label goes into one compact
    # braille legend on the same tab.  No standing flags are generated.
    legend_lines: list[str] = []
    legend_meta: list[dict[str, Any]] = []
    for index, part, part_id in unlabeled:
        name = _clean_text(str(part.get("name_zh") or part.get("id") or "部件")) or "部件"
        line = f"{index}{name}"
        try:
            cells = [list(cell) for cell in cnbraille.encode_text(line)]
        except cnbraille.BrailleError:
            continue
        legend_lines.append(line)
        legend_meta.append(
            {
                "kind": "part",
                "part": part_id,
                "text": name,
                "legend_line": line,
                "cells": cells,
                "cell_count": len(cells),
                "dots": 0,
                "placement": "base_legend",
                "legend_index": index,
            }
        )
    legend_front = plaque_front + 22.0
    legend_width = tab_half * 2.0 - 8.0
    legend_depth, legend_columns = _legend_panel(
        mesh,
        legend_lines,
        left=-tab_half + 4.0,
        front=legend_front,
        width=legend_width,
        max_depth=join_y - legend_front - 2.0,
        top=top,
    )

    # Preserve the spec's part order in the label list so the UI and tests see
    # title -> part 1 -> part 2, whether a part was labelled in place or in the
    # legend.
    labels_meta: list[dict[str, Any]] = [
        {
            "kind": "title",
            "text": title_text,
            "cells": [list(cell) for cell in cnbraille.encode_text(title_text)],
            "cell_count": len(cnbraille.encode_text(title_text)),
            "placement": "base_plaque",
        }
    ]
    legend_by_part = {str(item["part"]): item for item in legend_meta}
    for index, part in enumerate(parts):
        part_id = str(part.get("id") or f"part_{index + 1}")
        if part_id in label_by_part:
            labels_meta.append(label_by_part[part_id])
        elif part_id in legend_by_part:
            labels_meta.append(legend_by_part[part_id])

    bounds = mesh.bounds()
    size = [bounds[3] - bounds[0], bounds[4] - bounds[1], bounds[5] - bounds[2]]
    meta = {
        "kind": "assembly",
        "title": spec.get("title"),
        "title_zh": spec.get("title_zh"),
        "base_mm": [round(base_radius * 2.0, 2), round(base_radius * 2.0, 2), round(base_thickness, 2)],
        "base_tab_mm": round(abs(tab_front) - base_radius, 2),
        "parts": placed,
        "braille": {
            "standard": "GF 0019-2018",
            "labels": labels_meta,
            "dot_diameter_mm": cnbraille.DOT_DIAMETER_MM,
            "dot_height_mm": BRAILLE_DOT_HEIGHT_MM,
            "label_strategy": "surface_first_then_base_legend",
            "note": "部件名称优先直接刻在可触摸表面（顶面、侧壁、球冠或轮廓侧面）；放不下的名称按编号集中刻在底座前缘的盲文图例上，不生成竖立名牌。",
        },
        "legend": {
            "placement": "base_front_tab",
            "lines": legend_lines,
            "columns": legend_columns,
            "depth_mm": round(legend_depth, 2),
        },
        "standing_flags": 0,
        "qr_pad_mm": qr_size,
        "scale_note": spec.get("scale_note"),
        "size_mm": [round(value, 3) for value in size],
        "watertight": mesh.is_watertight(),
        "triangles": mesh.triangle_count(),
        "volume_mm3": round(mesh.volume(), 3),
    }
    return Model3DResult(mesh, meta)


def build_model3d(spec: dict[str, Any], *, target_height_mm: float | None = None) -> Model3DResult:
    if not isinstance(spec, dict):
        raise Model3DError("三维模型规格必须是对象。")
    kind = str(spec.get("kind") or "assembly")
    if kind == "solar_system":
        return _solar_system_mesh(spec)
    result = _assembly_mesh(spec)
    if target_height_mm:
        bounds = result.mesh.bounds()
        height = bounds[5] - bounds[2]
        if height > 1e-6:
            scale = min(1.0, float(target_height_mm) / height)
            if scale < 1.0:
                result.mesh = Mesh(
                    [
                        (
                            (a[0] * scale, a[1] * scale, a[2] * scale),
                            (b[0] * scale, b[1] * scale, b[2] * scale),
                            (c[0] * scale, c[1] * scale, c[2] * scale),
                        )
                        for a, b, c in result.mesh.triangles
                    ]
                )
                result.meta["scale"] = scale
                result.meta["size_mm"] = [round(value * scale, 3) for value in result.meta["size_mm"]]
    return result


def model3d_narration(spec: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
    title = spec.get("title_zh") or "三维触觉模型"
    kind = spec.get("kind")
    if kind == "solar_system":
        intro = f"这是一个{title}触觉模型。底座是圆形，中央最大的球体是太阳。"
        steps = [
            "先摸圆形底座的外缘，确认模型的方向；底座前方有一块标题盲文牌，标题牌左侧是行星名称图例的起点。",
        ]
        for index, planet in enumerate(spec.get("planets") or [], start=1):
            steps.append(
                f"从中心向外数第 {index} 条轨道上是{planet.get('name_zh')}，"
                f"球体半径约 {float(planet.get('radius_mm') or 0):.1f} 毫米；"
                f"沿底座外缘按同一顺序可以摸到它的中文盲文名称。"
            )
        steps.append("土星外面还有一圈光环，可以沿着球体赤道方向摸到。")
        outro = (
            "行星大小经过压缩，只保持真实大小的顺序，不是等比例模型；"
            "轨道表达相对位置，不表示真实距离。行星名称刻在底座外缘，"
            "不占用行星本体；中文盲文按 GF 0019-2018 编码。"
        )
        return {"intro": intro, "steps": steps, "outro": outro}
    intro = f"这是一个{title}触觉模型。先摸底板，再按部件顺序触摸。"
    steps = ["先确认底板方向，再从底座前缘的盲文标题开始。"]
    for index, part in enumerate(spec.get("parts") or [], start=1):
        name = part.get("name_zh") or part.get("id")
        shape = SHAPE_NAMES.get(str(part.get("shape") or ""), str(part.get("shape") or "部件"))
        steps.append(f"第 {index} 个部件是{name}，形状是{shape}。")
    outro = (
        "部件大小是相对比例，不是工程尺寸；需要用支撑的地方已由编译器加立柱。"
        "名称优先直接刻在部件表面；部件太小放不下时，按编号刻在底座前缘的盲文图例上。"
    )
    return {"intro": intro, "steps": steps, "outro": outro}


def braille_summary(text: str) -> dict[str, Any]:
    """Small helper for the UI: show readings and cells without a printer."""
    try:
        return {"ok": True, "readings": cnbraille.readings(text), "rows": cnbraille.describe(text)}
    except (cnbraille.BrailleError, ValueError) as error:
        return {"ok": False, "message": str(error)}


def companion_scene(spec: dict[str, Any], meta: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a 2-D overview plate for the same request.

    The plate is deliberately a schematic: it shows position, order and names,
    not a projection of the printed model.  That keeps the tactile-book
    promise honest and gives a reader who cannot see the model a way to study
    the layout before touching the object.
    """
    kind = str(spec.get("kind") or "assembly")
    title = str(spec.get("title") or "TACTILE 3D MODEL")[:28]
    title_zh = str(spec.get("title_zh") or "三维模型")[:28]
    blocks: list[dict[str, Any]] = []
    order: list[str] = []

    if kind == "solar_system":
        planets = spec.get("planets") or []
        orbit_values = [float(item.get("orbit_mm") or 0.0) for item in planets]
        outer = max(orbit_values) if orbit_values else 94.0
        scale = 40.0 / max(outer, 1.0)
        centre = (50.0, 50.0)
        rings = []
        for planet in planets:
            radius = float(planet.get("orbit_mm") or 0.0) * scale
            rings.append({"type": "circle", "cx": 50.0, "cy": 50.0, "r": round(radius, 3), "width": 1.2})
        blocks.append(
            {
                "id": "orbits",
                "label": "ORBITS",
                "label_zh": "轨道",
                "note": "七条同心类圆形轨道，从内到外表示由近到远的顺序。",
                "marker": [50.0, 8.0],
                "show_label": False,
                "primitives": rings,
            }
        )
        blocks.append(
            {
                "id": "sun",
                "label": "SUN",
                "label_zh": "太阳",
                "note": "中心最大的圆是太阳。",
                "marker": [50.0, 50.0],
                "show_label": True,
                "primitives": [
                    {"type": "circle", "cx": 50.0, "cy": 50.0, "r": 7.0, "width": 2.0},
                    {"type": "text", "x": 45.0, "y": 48.0, "text": "SUN", "height": 3.2},
                    {"type": "braille", "x": 47.0, "y": 54.0, "text": "SUN"},
                ],
            }
        )
        order.append("sun")
        for index, planet in enumerate(planets, start=1):
            angle = math.radians(float(planet.get("angle_deg") or 0.0))
            orbit = float(planet.get("orbit_mm") or 0.0) * scale
            radius = max(1.6, min(7.0, float(planet.get("radius_mm") or 4.0) * 0.52))
            x = centre[0] + math.cos(angle) * orbit
            y = centre[1] + math.sin(angle) * orbit
            name = str(planet.get("name_en") or planet.get("id") or f"P{index}").upper()[:8]
            primitives = [
                {"type": "circle", "cx": round(x, 3), "cy": round(y, 3), "r": round(radius, 3), "width": 1.8},
                {"type": "braille", "x": round(x - 2.0, 3), "y": round(y + radius + 1.0, 3), "text": str(index)},
            ]
            blocks.append(
                {
                    "id": str(planet.get("id") or f"planet_{index}"),
                    "label": name,
                    "label_zh": str(planet.get("name_zh") or name),
                    "note": str(planet.get("note") or ""),
                    "marker": [round(x, 3), round(y, 3)],
                    "show_label": True,
                    "primitives": primitives,
                }
            )
            order.append(str(planet.get("id") or f"planet_{index}"))
        subtitle = f"TACTILE OVERVIEW / {len(planets)} PLANETS / NOT TO SCALE"
        footer = ["ORBIT ORDER, NOT TRUE DISTANCE", "SCAN QR FOR AUDIO"]
        notes = [
            "这张板是太阳系的结构总览：同心圆表示轨道顺序，圆点表示行星位置，不表示真实距离和大小。",
            "行星名称用拉丁字母，数字盲文标出阅读顺序；完整中文盲文名称按轨道顺序刻在三维模型底座外缘。",
        ]
    else:
        parts = spec.get("parts") or []
        for index, part in enumerate(parts[:12], start=1):
            position = part.get("position") or [50.0, 50.0, 0.0]
            x = max(10.0, min(90.0, float(position[0])))
            y = max(10.0, min(88.0, float(position[1])))
            size = part.get("size") or [20.0, 20.0, 20.0]
            span = max(6.0, min(22.0, float(size[0]) * 0.30))
            shape = str(part.get("shape") or "box")
            if shape in {"sphere", "torus"}:
                primitive = {"type": "circle", "cx": round(x, 3), "cy": round(y, 3), "r": round(span / 2.0, 3), "width": 1.8}
            elif shape == "cone":
                primitive = {
                    "type": "polygon",
                    "points": [[round(x, 3), round(y - span / 2.0, 3)], [round(x + span / 2.0, 3), round(y + span / 2.0, 3)], [round(x - span / 2.0, 3), round(y + span / 2.0, 3)]],
                    "width": 1.8,
                }
            else:
                primitive = {
                    "type": "rect",
                    "x": round(x - span / 2.0, 3),
                    "y": round(y - span / 2.0, 3),
                    "w": round(span, 3),
                    "h": round(span, 3),
                    "width": 1.8,
                }
            label = str(part.get("name_en") or part.get("id") or f"PART {index}").upper()[:10]
            blocks.append(
                {
                    "id": str(part.get("id") or f"part_{index}"),
                    "label": label,
                    "label_zh": str(part.get("name_zh") or label),
                    "note": str(part.get("note") or ""),
                    "marker": [round(x, 3), round(y, 3)],
                    "show_label": True,
                    "primitives": [
                        primitive,
                        {"type": "braille", "x": round(x - 2.0, 3), "y": round(y + span / 2.0 + 1.0, 3), "text": str(index)},
                    ],
                }
            )
            order.append(str(part.get("id") or f"part_{index}"))
        subtitle = f"STRUCTURE OVERVIEW / {len(blocks)} PARTS / NOT TO SCALE"
        footer = ["PART ORDER, NOT TRUE SIZE", "SCAN QR FOR AUDIO"]
        notes = [
            "这张板是三维模型的结构总览：只标出部件位置和阅读顺序，不还原立体表面。",
            "部件的实际形状、相对大小和中文盲文名称以三维模型为准。",
        ]

    return {
        "version": 1,
        "title": title,
        "title_zh": title_zh,
        "subtitle": subtitle,
        "blocks": blocks,
        "reading_order": order,
        "footer_lines": footer,
        "notes": notes,
        "scale_note": "SCHEMATIC / NOT TO SCALE",
    }
