"""Semantic series-circuit model and deterministic scene compiler.

A circuit is not a bag of line segments.  It has components, a loop of wire,
and a reading order.  Keeping those separate means the visual editor can move a
component and re-route the wire without ever producing a dangling connection.

The model deliberately stays small: one closed loop (series circuit), standard
symbols, normalized 0-100 coordinates.  The compiler turns it into the same
scene JSON used by the tactile layout engine, so the geometry kernel remains
the only thing that decides millimetres.
"""

from __future__ import annotations

import copy
import math
import re
from typing import Any, Iterable

from ..font import text_width

CIRCUIT_VERSION = 1
LOOP_DEFAULT = (15.0, 25.0, 85.0, 85.0)
MAX_COMPONENTS = 8
SIDES = ("top", "right", "bottom", "left")
SIDE_ZH = {"top": "上方", "right": "右侧", "bottom": "下方", "left": "左侧"}
SIDE_ORDER = {"top": 0, "right": 1, "bottom": 2, "left": 3}

COMPONENT_LIBRARY: dict[str, dict[str, Any]] = {
    "battery": {
        "type": "battery",
        "label": "BATTERY",
        "label_zh": "电池",
        "note": "长线是正极，短线是负极。",
        "gap": 11.0,
        "extent": 12.0,
    },
    "switch": {
        "type": "switch",
        "label": "SWITCH",
        "label_zh": "开关",
        "note": "斜线表示开关处于断开位置；合上后电路才接通。",
        "gap": 12.0,
        "extent": 10.0,
        "state": "open",
    },
    "resistor": {
        "type": "resistor",
        "label": "RESISTOR",
        "label_zh": "电阻",
        "note": "折线表示电阻。",
        "gap": 14.0,
        "extent": 8.0,
    },
    "lamp": {
        "type": "lamp",
        "label": "LAMP",
        "label_zh": "灯泡",
        "note": "圆内画叉表示灯泡。",
        "gap": 12.0,
        "extent": 12.0,
    },
    "ammeter": {
        "type": "ammeter",
        "label": "AMMETER",
        "label_zh": "电流表",
        "note": "圆内写 A 表示电流表，串联在回路中测电流。",
        "gap": 11.0,
        "extent": 11.0,
    },
    "voltmeter": {
        "type": "voltmeter",
        "label": "VOLTMETER",
        "label_zh": "电压表",
        "note": "圆内写 V 表示电压表；本示意图只表达元件位置。",
        "gap": 11.0,
        "extent": 11.0,
    },
    "motor": {
        "type": "motor",
        "label": "MOTOR",
        "label_zh": "电动机",
        "note": "圆内写 M 表示电动机。",
        "gap": 12.0,
        "extent": 12.0,
    },
}

TYPE_ALIASES: dict[str, str] = {
    "battery": "battery",
    "cell": "battery",
    "电池": "battery",
    "电源": "battery",
    "蓄电池": "battery",
    "switch": "switch",
    "开关": "switch",
    "闸刀": "switch",
    "电键": "switch",
    "resistor": "resistor",
    "resistance": "resistor",
    "电阻": "resistor",
    "定值电阻": "resistor",
    "lamp": "lamp",
    "bulb": "lamp",
    "light": "lamp",
    "灯泡": "lamp",
    "小灯泡": "lamp",
    "电灯": "lamp",
    "ammeter": "ammeter",
    "电流表": "ammeter",
    "安培表": "ammeter",
    "voltmeter": "voltmeter",
    "电压表": "voltmeter",
    "伏特表": "voltmeter",
    "motor": "motor",
    "马达": "motor",
    "电动机": "motor",
}

SIDE_ALIASES: dict[str, str] = {
    "top": "top",
    "upper": "top",
    "上": "top",
    "上面": "top",
    "上方": "top",
    "上边": "top",
    "顶部": "top",
    "顶端": "top",
    "right": "right",
    "右": "right",
    "右面": "right",
    "右方": "right",
    "右边": "right",
    "右侧": "right",
    "bottom": "bottom",
    "lower": "bottom",
    "下": "bottom",
    "下面": "bottom",
    "下方": "bottom",
    "下边": "bottom",
    "底部": "bottom",
    "底端": "bottom",
    "left": "left",
    "左": "left",
    "左面": "left",
    "左方": "left",
    "左边": "left",
    "左侧": "left",
}


class CircuitError(ValueError):
    """Raised when a circuit model or edit cannot be applied safely."""


class CircuitInstructionError(CircuitError):
    """Raised when no deterministic edit matches a natural-language command."""


def _number(value: Any, default: float | None = None) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    if not math.isfinite(float(value)):
        return default
    return float(value)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _ascii(text: Any, limit: int = 24) -> str:
    if not isinstance(text, str):
        return ""
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 ()+-./:=?^_")
    cleaned = [char if char.upper() in allowed else " " for char in text.upper()]
    value = " ".join("".join(cleaned).split())
    return value[:limit].strip()


def _chinese(text: Any, limit: int = 120) -> str:
    if not isinstance(text, str):
        return ""
    return " ".join(text.split())[:limit]


def _slug(text: Any, fallback: str) -> str:
    cleaned = []
    for character in str(text or "").lower():
        if character.isalnum() and character.isascii():
            cleaned.append(character)
        elif character in {"-", "_"}:
            cleaned.append("_")
    value = "".join(cleaned).strip("_")
    return (value or fallback)[:24]


def component_type(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return TYPE_ALIASES.get(value.lower().strip())


def component_payload() -> list[dict[str, Any]]:
    return [
        {
            "type": spec["type"],
            "label": spec["label"],
            "label_zh": spec["label_zh"],
            "note": spec["note"],
            "has_state": spec["type"] == "switch",
        }
        for spec in COMPONENT_LIBRARY.values()
    ]


def default_circuit() -> dict[str, Any]:
    return {
        "version": CIRCUIT_VERSION,
        "title": "ELECTRIC CIRCUIT",
        "title_zh": "串联电路",
        "subtitle": "PHYSICS / BATTERY SWITCH RESISTOR LAMP",
        "loop": list(LOOP_DEFAULT),
        "components": [
            {
                "id": "battery",
                "type": "battery",
                "side": "bottom",
                "offset": 0.5,
                "label": "BATTERY",
                "label_zh": "电池",
                "note": COMPONENT_LIBRARY["battery"]["note"],
            },
            {
                "id": "switch",
                "type": "switch",
                "side": "left",
                "offset": 0.5,
                "state": "open",
                "label": "SWITCH",
                "label_zh": "开关",
                "note": COMPONENT_LIBRARY["switch"]["note"],
            },
            {
                "id": "resistor",
                "type": "resistor",
                "side": "top",
                "offset": 0.5,
                "label": "RESISTOR",
                "label_zh": "电阻",
                "note": COMPONENT_LIBRARY["resistor"]["note"],
            },
            {
                "id": "lamp",
                "type": "lamp",
                "side": "right",
                "offset": 0.5,
                "label": "LAMP",
                "label_zh": "灯泡",
                "note": COMPONENT_LIBRARY["lamp"]["note"],
            },
        ],
        "notes": ["电流从电池正极出发，依次经过开关、电阻和灯泡，再回到负极。"],
    }


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def _normalize_loop(raw: Any) -> tuple[float, float, float, float]:
    if isinstance(raw, dict):
        values = [raw.get("x0"), raw.get("y0"), raw.get("x1"), raw.get("y1")]
    elif isinstance(raw, (list, tuple)) and len(raw) >= 4:
        values = list(raw[:4])
    else:
        values = list(LOOP_DEFAULT)
    numbers = [_number(value) for value in values]
    if any(value is None for value in numbers):
        numbers = list(LOOP_DEFAULT)
    x0, y0, x1, y1 = (float(value) for value in numbers)  # type: ignore[arg-type]
    if x1 < x0:
        x0, x1 = x1, x0
    if y1 < y0:
        y0, y1 = y1, y0
    x0 = _clamp(x0, 4.0, 55.0)
    y0 = _clamp(y0, 6.0, 55.0)
    x1 = _clamp(x1, 45.0, 96.0)
    y1 = _clamp(y1, 45.0, 94.0)
    if x1 - x0 < 40.0:
        center = (x0 + x1) / 2.0
        x0, x1 = center - 20.0, center + 20.0
    if y1 - y0 < 40.0:
        center = (y0 + y1) / 2.0
        y0, y1 = center - 20.0, center + 20.0
    return round(x0, 3), round(y0, 3), round(x1, 3), round(y1, 3)


def _normalize_component(raw: Any, index: int, seen: set[str]) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    type_name = component_type(raw.get("type")) or component_type(raw.get("kind"))
    if type_name is None:
        type_name = component_type(raw.get("label_zh")) or component_type(raw.get("label"))
    if type_name is None:
        return None
    spec = COMPONENT_LIBRARY[type_name]
    side = SIDE_ALIASES.get(str(raw.get("side") or "").lower().strip())
    if side is None:
        side = "bottom"
    offset = _number(raw.get("offset"), 0.5)
    offset = _clamp(float(offset), 0.06, 0.94)
    block_id = _slug(raw.get("id"), type_name)
    while block_id in seen:
        block_id = f"{block_id}_{index + 1}"
    seen.add(block_id)
    label = _ascii(raw.get("label"), 18) or spec["label"]
    label_zh = _chinese(raw.get("label_zh"), 24) or spec["label_zh"]
    note = _chinese(raw.get("note"), 180) or spec["note"]
    state = str(raw.get("state") or spec.get("state") or "").lower()
    if type_name == "switch":
        state = "closed" if state in {"closed", "on", "闭合", "合上"} else "open"
    else:
        state = ""
    return {
        "id": block_id,
        "type": type_name,
        "side": side,
        "offset": round(offset, 4),
        "label": label,
        "label_zh": label_zh,
        "note": note,
        "state": state,
    }


def _side_length(side: str, loop: tuple[float, float, float, float]) -> float:
    x0, y0, x1, y1 = loop
    return (x1 - x0) if side in {"top", "bottom"} else (y1 - y0)


def _enforce_spacing(components: list[dict[str, Any]], loop: tuple[float, float, float, float]) -> list[str]:
    warnings: list[str] = []
    for side in SIDES:
        items = sorted(
            [item for item in components if item["side"] == side],
            key=lambda item: item["offset"],
        )
        if len(items) < 2:
            continue
        length = max(1.0, _side_length(side, loop))
        cursor = 0.06
        for index, item in enumerate(items):
            item["offset"] = max(item["offset"], cursor)
            gap = float(COMPONENT_LIBRARY[item["type"]]["gap"])
            cursor = item["offset"] + gap / length + 0.025
        overflow = items[-1]["offset"] - 0.94
        if overflow > 0:
            for item in items:
                item["offset"] = round(_clamp(item["offset"] - overflow, 0.06, 0.94), 4)
        for previous, current in zip(items, items[1:]):
            previous_gap = float(COMPONENT_LIBRARY[previous["type"]]["gap"])
            current_gap = float(COMPONENT_LIBRARY[current["type"]]["gap"])
            minimum = (previous_gap + current_gap) / 2.0 / length + 0.01
            if current["offset"] - previous["offset"] < minimum - 1e-6:
                warnings.append(f"{SIDE_ZH[side]}的元件间距过小，已尽量错开；请手动调整位置。")
                break
    for item in components:
        item["offset"] = round(_clamp(float(item["offset"]), 0.06, 0.94), 4)
    return warnings


def normalize_circuit(raw: Any) -> tuple[dict[str, Any], list[str]]:
    if not isinstance(raw, dict):
        raise CircuitError("电路模型必须是 JSON 对象。")
    source = raw.get("circuit") if isinstance(raw.get("circuit"), dict) else raw
    warnings: list[str] = []
    loop = _normalize_loop(source.get("loop"))
    components_raw = source.get("components")
    if not isinstance(components_raw, list) or not components_raw:
        if isinstance(source.get("blocks"), list):
            converted = circuit_from_scene(source)
            if converted is not None:
                converted["loop"] = list(loop)
                return normalize_circuit(converted)
        components_raw = default_circuit()["components"]
        warnings.append("电路模型没有元件，已载入默认串联电路。")
    components: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(components_raw[:MAX_COMPONENTS]):
        component = _normalize_component(item, index, seen)
        if component is not None:
            components.append(component)
    if not components:
        raise CircuitError("电路模型没有可识别的元件；支持电池、开关、电阻、灯泡、电流表、电压表和电动机。")
    warnings.extend(_enforce_spacing(components, loop))
    title = _ascii(source.get("title"), 32) or "ELECTRIC CIRCUIT"
    title_zh = _chinese(source.get("title_zh"), 40) or "串联电路"
    subtitle = _ascii(source.get("subtitle"), 48) or "PHYSICS / SERIES CIRCUIT"
    notes: list[str] = []
    if isinstance(source.get("notes"), list):
        for item in source["notes"][:6]:
            value = _chinese(item, 180)
            if value:
                notes.append(value)
    if not notes:
        notes = list(default_circuit()["notes"])
    return (
        {
            "version": CIRCUIT_VERSION,
            "title": title,
            "title_zh": title_zh,
            "subtitle": subtitle,
            "loop": list(loop),
            "components": components,
            "notes": notes,
        },
        warnings,
    )


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------


def _point(side: str, offset: float, loop: tuple[float, float, float, float]) -> tuple[float, float]:
    x0, y0, x1, y1 = loop
    offset = _clamp(offset, 0.0, 1.0)
    if side == "top":
        return (x0 + offset * (x1 - x0), y0)
    if side == "right":
        return (x1, y0 + offset * (y1 - y0))
    if side == "bottom":
        return (x0 + offset * (x1 - x0), y1)
    return (x0, y0 + offset * (y1 - y0))


def _walk_start(side: str, loop: tuple[float, float, float, float]) -> tuple[float, float]:
    x0, y0, x1, y1 = loop
    if side == "top":
        return (x0, y0)
    if side == "right":
        return (x1, y0)
    if side == "bottom":
        return (x1, y1)
    return (x0, y1)


def _walk_point(side: str, distance: float, loop: tuple[float, float, float, float]) -> tuple[float, float]:
    x0, y0, x1, y1 = loop
    if side == "top":
        return (x0 + distance, y0)
    if side == "right":
        return (x1, y0 + distance)
    if side == "bottom":
        return (x1 - distance, y1)
    return (x0, y1 - distance)


def _walk_distance(side: str, offset: float, loop: tuple[float, float, float, float]) -> float:
    length = _side_length(side, loop)
    return offset * length if side in {"top", "right"} else (1.0 - offset) * length


def _dedupe(points: Iterable[tuple[float, float]]) -> list[list[float]]:
    result: list[list[float]] = []
    for point in points:
        rounded = [round(float(point[0]), 3), round(float(point[1]), 3)]
        if not result or result[-1] != rounded:
            result.append(rounded)
    return result


def _wire_segments(components: list[dict[str, Any]], loop: tuple[float, float, float, float]) -> list[list[list[float]]]:
    segments: list[list[list[float]]] = []
    current: list[tuple[float, float]] = [_walk_start("top", loop)]
    for side in SIDES:
        side_length = _side_length(side, loop)
        items = sorted(
            [item for item in components if item["side"] == side],
            key=lambda item: _walk_distance(side, item["offset"], loop),
        )
        for item in items:
            gap = float(COMPONENT_LIBRARY[item["type"]]["gap"])
            distance = _walk_distance(side, item["offset"], loop)
            entry = _walk_point(side, distance - gap / 2.0, loop)
            exit_point = _walk_point(side, distance + gap / 2.0, loop)
            if current[-1] != entry:
                current.append(entry)
            if len(current) >= 2:
                segments.append(_dedupe(current))
            current = [exit_point]
        corner = _walk_point(side, side_length, loop)
        if current[-1] != corner:
            current.append(corner)
    start = _walk_start("top", loop)
    if current[-1] != start:
        current.append(start)
    if len(current) >= 2:
        segments.append(_dedupe(current))
    return [segment for segment in segments if len(segment) >= 2 and _polyline_length(segment) > 0.6]


def _polyline_length(points: list[list[float]]) -> float:
    return sum(math.dist(points[index], points[index - 1]) for index in range(1, len(points)))


def _transform(u: float, v: float, cx: float, cy: float, vertical: bool) -> tuple[float, float]:
    if vertical:
        return (cx + v, cy + u)
    return (cx + u, cy + v)


def _line(points: list[tuple[float, float]], width: float = 2.2) -> dict[str, Any]:
    return {"type": "line", "points": [[round(x, 3), round(y, 3)] for x, y in points], "width": width}


def _component_primitives(component: dict[str, Any], center: tuple[float, float], vertical: bool) -> list[dict[str, Any]]:
    type_name = component["type"]
    state = component.get("state") or "open"
    cx, cy = center

    def p(u: float, v: float) -> tuple[float, float]:
        return _transform(u, v, cx, cy, vertical)

    primitives: list[dict[str, Any]] = []
    if type_name == "battery":
        primitives.extend(
            [
                _line([p(-5.5, 0), p(-4.0, 0)], 2.0),
                _line([p(-4.0, -6.0), p(-4.0, 6.0)], 2.8),
                _line([p(-1.6, -3.4), p(-1.6, 3.4)], 2.6),
                _line([p(1.6, -6.0), p(1.6, 6.0)], 2.8),
                _line([p(4.0, -3.4), p(4.0, 3.4)], 2.6),
                _line([p(4.0, 0), p(5.5, 0)], 2.0),
            ]
        )
    elif type_name == "switch":
        primitives.extend(
            [
                _line([p(-6.0, 0), p(-2.4, 0)], 2.2),
                _line([p(2.4, 0), p(6.0, 0)], 2.2),
                {"type": "point", "cx": round(p(-2.4, 0)[0], 3), "cy": round(p(-2.4, 0)[1], 3), "r": 0.9},
                {"type": "point", "cx": round(p(2.4, 0)[0], 3), "cy": round(p(2.4, 0)[1], 3), "r": 0.9},
            ]
        )
        if state == "closed":
            primitives.append(_line([p(-2.4, 0), p(2.4, 0)], 2.2))
        else:
            primitives.append(_line([p(-2.4, 0), p(3.2, -3.4)], 2.2))
    elif type_name == "resistor":
        points = [p(-7.0, 0)]
        for index in range(8):
            u = -6.0 + index * 1.5
            points.append(p(u, -3.0 if index % 2 == 0 else 3.0))
        points.append(p(7.0, 0))
        primitives.append(_line(points, 2.2))
    elif type_name in {"lamp", "ammeter", "voltmeter", "motor"}:
        radius = 5.5 if type_name == "lamp" else 5.2
        primitives.append(
            {
                "type": "circle",
                "cx": round(cx, 3),
                "cy": round(cy, 3),
                "r": radius,
                "width": 2.2,
            }
        )
        if type_name == "lamp":
            primitives.extend(
                [
                    _line([p(-3.9, -3.9), p(3.9, 3.9)], 1.8),
                    _line([p(-3.9, 3.9), p(3.9, -3.9)], 1.8),
                ]
            )
        else:
            letter = {"ammeter": "A", "voltmeter": "V", "motor": "M"}[type_name]
            height = 5.6
            width_units = text_width(letter, height) / 1.32
            primitives.append(
                {
                    "type": "text",
                    "x": round(cx - width_units / 2.0, 3),
                    "y": round(cy - height / 1.42 / 2.0, 3),
                    "text": letter,
                    "height": height,
                }
            )
    return primitives


def _marker(component: dict[str, Any], center: tuple[float, float], loop: tuple[float, float, float, float]) -> list[float]:
    x0, y0, x1, y1 = loop
    cx, cy = center
    extent = float(COMPONENT_LIBRARY[component["type"]]["extent"])
    side = component["side"]
    if side == "top":
        return [round(cx, 3), round(_clamp(y0 - extent / 2.0 - 2.0, 1.0, 99.0), 3)]
    if side == "bottom":
        return [round(cx, 3), round(_clamp(y1 + extent / 2.0 + 2.0, 1.0, 99.0), 3)]
    if side == "left":
        return [round(_clamp(x0 - extent / 2.0 - 2.0, 1.0, 99.0), 3), round(cy, 3)]
    return [round(_clamp(x1 + extent / 2.0 + 2.0, 1.0, 99.0), 3), round(cy, 3)]


def circuit_to_scene(raw: Any) -> dict[str, Any]:
    """Compile a circuit model into a normalized scene spec (not yet normalized)."""
    circuit, warnings = normalize_circuit(raw)
    loop = tuple(circuit["loop"])  # type: ignore[assignment]
    x0, y0, x1, y1 = loop
    blocks: list[dict[str, Any]] = []
    for component in circuit["components"]:
        center = _point(component["side"], component["offset"], loop)
        vertical = component["side"] in {"left", "right"}
        primitives = _component_primitives(component, center, vertical)
        blocks.append(
            {
                "id": component["id"],
                "label": component["label"],
                "label_zh": component["label_zh"],
                "note": component["note"],
                "marker": _marker(component, center, loop),
                "show_label": True,
                "kind": component["type"],
                "primitives": primitives,
            }
        )
    wire_segments = _wire_segments(circuit["components"], loop)
    blocks.append(
        {
            "id": "wires",
            "label": "WIRES",
            "label_zh": "导线",
            "note": "导线把电池、开关、电阻和灯泡连成一个闭合回路。",
            "marker": [round(_clamp(x0 - 4.0, 1.0, 99.0), 3), round((y0 + y1) / 2.0, 3)],
            "show_label": False,
            "kind": "wire",
            "primitives": [
                {"type": "polyline", "points": segment, "width": 2.0}
                for segment in wire_segments
            ],
        }
    )
    notes = list(circuit.get("notes") or [])
    notes.append("导线会根据元件位置自动重画；改动元件后不需要手动补线。")
    if warnings:
        notes.extend(warnings)
    return {
        "version": 1,
        "title": circuit["title"],
        "title_zh": circuit["title_zh"],
        "subtitle": circuit["subtitle"],
        "blocks": blocks,
        "reading_order": [component["id"] for component in circuit["components"]] + ["wires"],
        "footer_lines": ["PHYSICS / SERIES CIRCUIT", "SCHEMATIC / NOT TO SCALE"],
        "notes": notes,
        "scale_note": "SCHEMATIC / NOT TO SCALE",
    }


# ---------------------------------------------------------------------------
# Scene -> circuit (best effort, for vision output and existing JSON)
# ---------------------------------------------------------------------------


def _primitive_bbox(primitive: dict[str, Any]) -> tuple[float, float, float, float] | None:
    kind = primitive.get("type")
    if kind in {"line", "polyline", "polygon", "arrow"}:
        points = primitive.get("points") or []
        xs = [float(point[0]) for point in points if isinstance(point, (list, tuple)) and len(point) >= 2]
        ys = [float(point[1]) for point in points if isinstance(point, (list, tuple)) and len(point) >= 2]
        return (min(xs), min(ys), max(xs), max(ys)) if xs and ys else None
    if kind == "rect":
        x, y = float(primitive.get("x", 0)), float(primitive.get("y", 0))
        return (x, y, x + float(primitive.get("w", 0)), y + float(primitive.get("h", 0)))
    if kind in {"circle", "point"}:
        cx, cy, radius = float(primitive.get("cx", 0)), float(primitive.get("cy", 0)), float(primitive.get("r", 0))
        return (cx - radius, cy - radius, cx + radius, cy + radius)
    if kind == "arc":
        cx, cy, radius = float(primitive.get("cx", 0)), float(primitive.get("cy", 0)), float(primitive.get("r", 0))
        return (cx - radius, cy - radius, cx + radius, cy + radius)
    if kind == "text":
        x, y = float(primitive.get("x", 0)), float(primitive.get("y", 0))
        height = float(primitive.get("height", 4.5))
        width = text_width(str(primitive.get("text") or ""), height) / 1.32
        return (x, y, x + width, y + height / 1.42)
    return None


def _block_bbox(block: dict[str, Any]) -> tuple[float, float, float, float] | None:
    boxes = [box for primitive in block.get("primitives") or [] if (box := _primitive_bbox(primitive))]
    if not boxes:
        return None
    return (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )


def _block_type(block: dict[str, Any]) -> str | None:
    for value in (block.get("kind"), block.get("id"), block.get("label"), block.get("label_zh")):
        found = component_type(value)
        if found:
            return found
    return None


def _nearest_side(center: tuple[float, float], loop: tuple[float, float, float, float]) -> str:
    x0, y0, x1, y1 = loop
    cx, cy = center
    distances = {
        "top": abs(cy - y0),
        "bottom": abs(cy - y1),
        "left": abs(cx - x0),
        "right": abs(cx - x1),
    }
    return min(distances, key=distances.get)


def _offset_for_side(point: tuple[float, float], side: str, loop: tuple[float, float, float, float]) -> float:
    x0, y0, x1, y1 = loop
    cx, cy = point
    if side in {"top", "bottom"}:
        return _clamp((cx - x0) / max(1e-6, x1 - x0), 0.06, 0.94)
    return _clamp((cy - y0) / max(1e-6, y1 - y0), 0.06, 0.94)


def circuit_from_scene(scene: Any) -> dict[str, Any] | None:
    """Recover an editable circuit from a scene that contains standard symbols."""
    if not isinstance(scene, dict):
        return None
    blocks = scene.get("blocks")
    if not isinstance(blocks, list):
        return None
    found: list[tuple[dict[str, Any], str, tuple[float, float, float, float]]] = []
    wire_boxes: list[tuple[float, float, float, float]] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        box = _block_bbox(block)
        if box is None:
            continue
        type_name = _block_type(block)
        if type_name:
            found.append((block, type_name, box))
        elif block.get("id") == "wires" or block.get("kind") in {"wire", "wires"} or block.get("label") == "WIRES":
            wire_boxes.append(box)
    if len(found) < 2:
        return None
    if wire_boxes:
        x0 = min(box[0] for box in wire_boxes)
        y0 = min(box[1] for box in wire_boxes)
        x1 = max(box[2] for box in wire_boxes)
        y1 = max(box[3] for box in wire_boxes)
    else:
        x0 = min(box[0] for _, _, box in found) - 8.0
        y0 = min(box[1] for _, _, box in found) - 8.0
        x1 = max(box[2] for _, _, box in found) + 8.0
        y1 = max(box[3] for _, _, box in found) + 8.0
    loop = _normalize_loop([x0, y0, x1, y1])
    components: list[dict[str, Any]] = []
    used_ids: set[str] = set()
    for block, type_name, box in found[:MAX_COMPONENTS]:
        center = ((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0)
        side = _nearest_side(center, loop)
        offset = _offset_for_side(center, side, loop)
        spec = COMPONENT_LIBRARY[type_name]
        block_id = _slug(block.get("id"), type_name)
        while block_id in used_ids:
            block_id = f"{block_id}_{len(used_ids) + 1}"
        used_ids.add(block_id)
        components.append(
            {
                "id": block_id,
                "type": type_name,
                "side": side,
                "offset": offset,
                "label": _ascii(block.get("label"), 18) or spec["label"],
                "label_zh": _chinese(block.get("label_zh"), 24) or spec["label_zh"],
                "note": _chinese(block.get("note"), 180) or spec["note"],
                "state": "open",
            }
        )
    return {
        "version": CIRCUIT_VERSION,
        "title": _ascii(scene.get("title"), 32) or "ELECTRIC CIRCUIT",
        "title_zh": _chinese(scene.get("title_zh"), 40) or "串联电路",
        "subtitle": _ascii(scene.get("subtitle"), 48) or "PHYSICS / SERIES CIRCUIT",
        "loop": list(loop),
        "components": components,
        "notes": list(scene.get("notes") or [])[:4] or list(default_circuit()["notes"]),
    }


# ---------------------------------------------------------------------------
# Deterministic edit operations
# ---------------------------------------------------------------------------


def _find(circuit: dict[str, Any], block_id: str) -> dict[str, Any]:
    for component in circuit["components"]:
        if component["id"] == block_id:
            return component
    raise CircuitError(f"没有编号为 {block_id} 的元件。")


def _unique_id(circuit: dict[str, Any], type_name: str) -> str:
    ids = {component["id"] for component in circuit["components"]}
    if type_name not in ids:
        return type_name
    index = 2
    while f"{type_name}_{index}" in ids:
        index += 1
    return f"{type_name}_{index}"


def apply_op(raw: Any, op: Any) -> tuple[dict[str, Any], str, list[str]]:
    circuit, warnings = normalize_circuit(raw)
    if not isinstance(op, dict):
        raise CircuitError("编辑操作必须是 JSON 对象。")
    kind = str(op.get("type") or "").strip()
    change = ""

    if kind == "move_component":
        component = _find(circuit, str(op.get("id") or ""))
        side = SIDE_ALIASES.get(str(op.get("side") or "").lower().strip())
        if side is None:
            raise CircuitError("移动元件时缺少有效的 side。")
        offset = _number(op.get("offset"), component["offset"])
        component["side"] = side
        component["offset"] = round(_clamp(float(offset), 0.06, 0.94), 4)
        change = f"把{component['label_zh']}移到{SIDE_ZH[side]}。"

    elif kind == "add_component":
        type_name = component_type(op.get("component_type") or op.get("component") or op.get("kind"))
        if type_name is None:
            raise CircuitError("无法识别要添加的元件类型。")
        if len(circuit["components"]) >= MAX_COMPONENTS:
            raise CircuitError(f"一个回路最多放 {MAX_COMPONENTS} 个元件。")
        spec = COMPONENT_LIBRARY[type_name]
        side = SIDE_ALIASES.get(str(op.get("side") or "").lower().strip(), "top")
        offset = _number(op.get("offset"), 0.5)
        component = {
            "id": _unique_id(circuit, type_name),
            "type": type_name,
            "side": side,
            "offset": round(_clamp(float(offset), 0.06, 0.94), 4),
            "label": spec["label"],
            "label_zh": spec["label_zh"],
            "note": spec["note"],
            "state": spec.get("state", ""),
        }
        circuit["components"].append(component)
        change = f"添加了{component['label_zh']}，放在{SIDE_ZH[side]}。"

    elif kind == "remove_component":
        component = _find(circuit, str(op.get("id") or ""))
        if len(circuit["components"]) <= 1:
            raise CircuitError("回路至少需要保留一个元件。")
        circuit["components"] = [item for item in circuit["components"] if item["id"] != component["id"]]
        change = f"删除了{component['label_zh']}。"

    elif kind == "set_type":
        component = _find(circuit, str(op.get("id") or ""))
        type_name = component_type(op.get("component_type") or op.get("component") or op.get("kind"))
        if type_name is None:
            raise CircuitError("无法识别要替换成的元件类型。")
        old_spec = COMPONENT_LIBRARY[component["type"]]
        new_spec = COMPONENT_LIBRARY[type_name]
        component["type"] = type_name
        if component["label"] == old_spec["label"]:
            component["label"] = new_spec["label"]
        if component["label_zh"] == old_spec["label_zh"]:
            component["label_zh"] = new_spec["label_zh"]
        if component["note"] == old_spec["note"]:
            component["note"] = new_spec["note"]
        component["state"] = new_spec.get("state", "") if type_name == "switch" else ""
        change = f"把{old_spec['label_zh']}换成了{new_spec['label_zh']}。"

    elif kind == "set_state":
        component = _find(circuit, str(op.get("id") or ""))
        if component["type"] != "switch":
            raise CircuitError("只有开关可以设置断开或闭合。")
        state = str(op.get("state") or "").lower()
        component["state"] = "closed" if state in {"closed", "on", "闭合", "合上"} else "open"
        change = f"开关已{'闭合' if component['state'] == 'closed' else '断开'}。"

    elif kind == "relabel":
        component = _find(circuit, str(op.get("id") or ""))
        if "label" in op:
            component["label"] = _ascii(op.get("label"), 18) or component["label"]
        if "label_zh" in op:
            component["label_zh"] = _chinese(op.get("label_zh"), 24) or component["label_zh"]
        if "note" in op:
            component["note"] = _chinese(op.get("note"), 180) or component["note"]
        change = f"更新了{component['label_zh']}的标签。"

    elif kind == "rotate_component":
        component = _find(circuit, str(op.get("id") or ""))
        direction = str(op.get("direction") or "cw").lower()
        index = SIDES.index(component["side"])
        component["side"] = SIDES[(index + (1 if direction == "cw" else -1)) % len(SIDES)]
        change = f"把{component['label_zh']}沿回路{'顺时针' if direction == 'cw' else '逆时针'}转到了{SIDE_ZH[component['side']]}。"

    elif kind == "set_loop":
        loop = circuit["loop"]
        dx = _number(op.get("dx"), 0.0) or 0.0
        dy = _number(op.get("dy"), 0.0) or 0.0
        if op.get("scale") is not None:
            scale = _clamp(float(_number(op.get("scale"), 1.0) or 1.0), 0.7, 1.3)
            center_x = (loop[0] + loop[2]) / 2.0
            center_y = (loop[1] + loop[3]) / 2.0
            half_w = (loop[2] - loop[0]) / 2.0 * scale
            half_h = (loop[3] - loop[1]) / 2.0 * scale
            circuit["loop"] = _normalize_loop([center_x - half_w, center_y - half_h, center_x + half_w, center_y + half_h])
            change = "回路已" + ("放大。" if scale > 1 else "缩小。")
        else:
            circuit["loop"] = _normalize_loop([loop[0] + dx, loop[1] + dy, loop[2] + dx, loop[3] + dy])
            change = "回路位置已调整。"

    elif kind == "reorder":
        block_id = str(op.get("id") or "")
        _find(circuit, block_id)
        target = int(_number(op.get("index"), 0) or 0)
        items = [item for item in circuit["components"] if item["id"] != block_id]
        target = max(0, min(len(items), target))
        moving = next(item for item in circuit["components"] if item["id"] == block_id)
        items.insert(target, moving)
        circuit["components"] = items
        change = f"调整了{moving['label_zh']}的阅读顺序。"

    else:
        raise CircuitError(f"不支持的编辑操作：{kind or '空'}。")

    normalized, extra = normalize_circuit(circuit)
    return normalized, change, warnings + extra


# ---------------------------------------------------------------------------
# Natural-language parsing
# ---------------------------------------------------------------------------


def _clauses(instruction: str) -> list[str]:
    text = (instruction or "").strip()
    if not text:
        return []
    parts = re.split(r"[，,；;。\n]+|然后|接着", text)
    return [part.strip() for part in parts if part.strip()]


def _component_ref(circuit: dict[str, Any], ref: str) -> dict[str, Any] | None:
    ref = (ref or "").strip()
    if not ref:
        return None
    index_match = re.search(r"第\s*(\d+)\s*(?:个|号)?", ref)
    wanted_index = int(index_match.group(1)) if index_match else 0
    type_name = None
    for alias, resolved in sorted(TYPE_ALIASES.items(), key=lambda item: len(item[0]), reverse=True):
        if alias.lower() in ref.lower():
            type_name = resolved
            break
    if type_name:
        candidates = [item for item in circuit["components"] if item["type"] == type_name]
        if wanted_index and 1 <= wanted_index <= len(candidates):
            return candidates[wanted_index - 1]
        if candidates:
            return candidates[0]
    for item in circuit["components"]:
        if ref.lower() in {item["id"].lower(), item["label"].lower(), item["label_zh"]}:
            return item
    return None


def _parse_destination(destination: str) -> tuple[str | None, float | None]:
    text = (destination or "").strip()
    side = None
    if "左上" in text or "上左" in text:
        return "top", 0.14
    if "右上" in text or "上右" in text:
        return "top", 0.86
    if "左下" in text or "下左" in text:
        return "bottom", 0.14
    if "右下" in text or "下右" in text:
        return "bottom", 0.86
    for alias, resolved in sorted(SIDE_ALIASES.items(), key=lambda item: len(item[0]), reverse=True):
        if alias.lower() in text.lower():
            side = resolved
            break
    if side:
        return side, 0.5
    if any(word in text for word in ("中间", "中央", "正中")):
        return None, 0.5
    return None, None


def _add_types(text: str) -> list[str]:
    found: list[tuple[int, str]] = []
    lower = text.lower()
    for alias, resolved in TYPE_ALIASES.items():
        index = lower.find(alias.lower())
        if index >= 0:
            found.append((index, resolved))
    result: list[str] = []
    for _index, type_name in sorted(found):
        if type_name not in result:
            result.append(type_name)
    return result


def parse_circuit_instruction(raw: Any, instruction: str) -> tuple[dict[str, Any], list[str], str]:
    """Apply common Chinese circuit edits without a model.

    Raises ``CircuitInstructionError`` when nothing matches, so the caller can
    ask the configured model or tell the user what the offline editor supports.
    """
    circuit, warnings = normalize_circuit(raw)
    changes: list[str] = []
    skipped: list[str] = []
    for clause in _clauses(instruction):
        applied = False

        replace = re.search(r"(?:把|将)?\s*(.+?)\s*(?:换成|改成|替换成|变为|换为)\s*(.+)", clause)
        if replace:
            component = _component_ref(circuit, replace.group(1))
            types = _add_types(replace.group(2))
            if component and types:
                circuit, change, extra = apply_op(circuit, {"type": "set_type", "id": component["id"], "component_type": types[0]})
                changes.append(change)
                warnings.extend(extra)
                applied = True

        if not applied and any(word in clause for word in ("闭合", "合上", "断开", "打开")):
            if "开关" in clause:
                switches = [item for item in circuit["components"] if item["type"] == "switch"]
                target = _component_ref(circuit, clause) or (switches[0] if switches else None)
                if target is not None:
                    state = "closed" if any(word in clause for word in ("闭合", "合上")) else "open"
                    circuit, change, extra = apply_op(circuit, {"type": "set_state", "id": target["id"], "state": state})
                    changes.append(change)
                    warnings.extend(extra)
                    applied = True

        if not applied:
            remove = re.search(r"(?:删除|去掉|移除|拿掉|不要)\s*(.+)", clause)
            if remove:
                component = _component_ref(circuit, remove.group(1))
                if component:
                    circuit, change, extra = apply_op(circuit, {"type": "remove_component", "id": component["id"]})
                    changes.append(change)
                    warnings.extend(extra)
                    applied = True

        if not applied:
            add = re.search(r"(?:添加|增加|加上|加一个|再加|加)\s*(?:一个|一只|一段|一个|个)?\s*(.+)", clause)
            if add:
                types = _add_types(add.group(1))
                if types:
                    for type_name in types[:2]:
                        if len(circuit["components"]) >= MAX_COMPONENTS:
                            warnings.append(f"一个回路最多放 {MAX_COMPONENTS} 个元件，部分添加已跳过。")
                            break
                        circuit, change, extra = apply_op(circuit, {"type": "add_component", "component_type": type_name, "side": "top", "offset": 0.5})
                        changes.append(change)
                        warnings.extend(extra)
                    applied = True

        if not applied:
            move = re.search(r"(?:把|将)?\s*(.+?)\s*(?:移到|挪到|放到|放在|移动到|移至|换到)\s*(.+)", clause)
            if move:
                component = _component_ref(circuit, move.group(1))
                side, offset = _parse_destination(move.group(2))
                if component and (side or offset is not None):
                    payload = {
                        "type": "move_component",
                        "id": component["id"],
                        "side": side or component["side"],
                        "offset": offset if offset is not None else component["offset"],
                    }
                    circuit, change, extra = apply_op(circuit, payload)
                    changes.append(change)
                    warnings.extend(extra)
                    applied = True

        if not applied:
            nudge = re.search(r"(?:把|将)?\s*(.+?)\s*(?:往|向|朝)\s*(左|右|上|下)(?:边|面)?\s*(?:移|挪|走|偏)?\s*(一点|一些|少少)?", clause)
            if nudge:
                component = _component_ref(circuit, nudge.group(1))
                direction = SIDE_ALIASES[nudge.group(2)]
                small = bool(nudge.group(3))
                if component:
                    payload = {"type": "move_component", "id": component["id"], "side": component["side"], "offset": component["offset"]}
                    if small and component["side"] in {"top", "bottom"} and direction in {"left", "right"}:
                        payload["offset"] = component["offset"] + (-0.08 if direction == "left" else 0.08)
                    elif small and component["side"] in {"left", "right"} and direction in {"top", "bottom"}:
                        payload["offset"] = component["offset"] + (-0.08 if direction == "top" else 0.08)
                    else:
                        payload["side"] = direction
                        payload["offset"] = 0.5
                    circuit, change, extra = apply_op(circuit, payload)
                    changes.append(change)
                    warnings.extend(extra)
                    applied = True

        if not applied:
            rotate = re.search(r"(?:把|将)?\s*(.+?)\s*(?:顺时针|逆时针)", clause)
            if rotate:
                component = _component_ref(circuit, rotate.group(1))
                if component:
                    direction = "cw" if "顺时针" in clause else "ccw"
                    circuit, change, extra = apply_op(circuit, {"type": "rotate_component", "id": component["id"], "direction": direction})
                    changes.append(change)
                    warnings.extend(extra)
                    applied = True

        if not applied and any(word in clause for word in ("放大", "大一点", "扩大", "缩小", "小一点")):
            scale = 1.12 if any(word in clause for word in ("放大", "大一点", "扩大")) else 0.9
            circuit, change, extra = apply_op(circuit, {"type": "set_loop", "scale": scale})
            changes.append(change)
            warnings.extend(extra)
            applied = True

        if not applied:
            title = re.search(r"(?:标题|题目)(?:改成|改为|换成)\s*(.+)", clause)
            if title:
                circuit["title_zh"] = _chinese(title.group(1), 40)
                circuit, _ = normalize_circuit(circuit)
                changes.append("已更新中文标题。")
                applied = True

        if not applied:
            skipped.append(clause)

    if not changes:
        raise CircuitInstructionError(
            "这句话没有匹配到可执行的电路修改。可以试试：把开关移到左上角、开关闭合、"
            "把灯泡换成电阻、加一个电流表、删除电阻、把回路放大。"
        )
    message = "；".join(changes)
    if skipped:
        message += "。未执行：" + "；".join(skipped[:3])
    return circuit, warnings, message
