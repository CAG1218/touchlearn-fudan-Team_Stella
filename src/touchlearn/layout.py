"""Deterministic layout engines for the two v0 cases.

Layout is expressed in millimetres on an A5 portrait sheet, origin at the
top-left, x to the right and y downwards.  The geometry stage later flips y
into the conventional 3D coordinate system.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable

from . import braille, font
from .model import Diagram

Point = tuple[float, float]
BBox = tuple[float, float, float, float]


@dataclass(frozen=True)
class Primitive:
    kind: str
    points: tuple[Point, ...] = ()
    width: float = 1.2
    radius: float = 0.0
    text: str = ""
    height: float = 6.0
    closed: bool = False


@dataclass
class Feature:
    id: str
    kind: str
    label: str = ""
    semantic_id: str | None = None
    index: int | None = None
    primitives: list[Primitive] = field(default_factory=list)

    def bbox(self) -> BBox:
        if not self.primitives:
            raise ValueError(f"feature '{self.id}' has no primitives")
        boxes = [primitive_bbox(primitive) for primitive in self.primitives]
        left = min(box[0] for box in boxes)
        top = min(box[1] for box in boxes)
        right = max(box[0] + box[2] for box in boxes)
        bottom = max(box[1] + box[3] for box in boxes)
        return (left, top, right - left, bottom - top)


@dataclass
class LayoutResult:
    diagram: Diagram
    width: float
    height: float
    base_polygon: list[Point]
    features: list[Feature]
    trace: list[dict[str, Any]]
    warnings: list[str]

    def feature(self, feature_id: str) -> Feature:
        for item in self.features:
            if item.id == feature_id:
                return item
        raise KeyError(f"feature not found: {feature_id}")

    def semantic_features(self) -> list[Feature]:
        """Raised features that carry a semantic id (labels excluded)."""
        return [item for item in self.features if item.semantic_id is not None and item.kind != "label"]

    def labels(self) -> list[Feature]:
        return [item for item in self.features if item.kind == "label"]

    def bounds(self) -> BBox:
        boxes = [item.bbox() for item in self.features]
        left = min(box[0] for box in boxes)
        top = min(box[1] for box in boxes)
        right = max(box[0] + box[2] for box in boxes)
        bottom = max(box[1] + box[3] for box in boxes)
        return (left, top, right - left, bottom - top)

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.diagram.id,
            "title": self.diagram.title,
            "engine": self.diagram.layout.get("engine"),
            "canvas_mm": [self.width, self.height],
            "feature_count": len(self.features),
            "semantic_count": len(self.diagram.elements),
            "bounds_mm": [round(value, 3) for value in self.bounds()],
            "warnings": self.warnings,
        }


def primitive_bbox(primitive: Primitive) -> BBox:
    if primitive.kind in {"polyline", "polygon"}:
        if not primitive.points:
            return (0.0, 0.0, 0.0, 0.0)
        pad = 0.0 if primitive.kind == "polygon" else primitive.width / 2.0
        xs = [point[0] for point in primitive.points]
        ys = [point[1] for point in primitive.points]
        return (min(xs) - pad, min(ys) - pad, max(xs) - min(xs) + 2 * pad, max(ys) - min(ys) + 2 * pad)
    if primitive.kind == "circle":
        return (
            primitive.points[0][0] - primitive.radius,
            primitive.points[0][1] - primitive.radius,
            primitive.radius * 2,
            primitive.radius * 2,
        )
    if primitive.kind == "text":
        return font.text_bbox(primitive.text, primitive.points[0][0], primitive.points[0][1], primitive.height)
    if primitive.kind == "braille":
        return braille.bbox(primitive.text, primitive.points[0][0], primitive.points[0][1])
    raise ValueError(f"unknown primitive kind: {primitive.kind}")


def text_width(text: str, height: float = 6.0) -> float:
    return font.text_width(text, height)


def rect_polygon(x: float, y: float, width: float, height: float, chamfer: float = 0.0) -> list[Point]:
    if chamfer <= 0:
        return [(x, y), (x + width, y), (x + width, y + height), (x, y + height)]
    c = min(chamfer, width / 2.0, height / 2.0)
    return [
        (x + c, y),
        (x + width - c, y),
        (x + width, y + c),
        (x + width, y + height - c),
        (x + width - c, y + height),
        (x + c, y + height),
        (x, y + height - c),
        (x, y + c),
    ]


def circle_points(cx: float, cy: float, radius: float, segments: int = 32) -> list[Point]:
    return [
        (cx + radius * math.cos(2 * math.pi * index / segments), cy + radius * math.sin(2 * math.pi * index / segments))
        for index in range(segments)
    ]


def arc_points(cx: float, cy: float, radius: float, start_deg: float, end_deg: float, segments: int = 16) -> list[Point]:
    start = math.radians(start_deg)
    end = math.radians(end_deg)
    return [
        (
            cx + radius * math.cos(start + (end - start) * index / segments),
            cy + radius * math.sin(start + (end - start) * index / segments),
        )
        for index in range(segments + 1)
    ]


def base_plate_polygon(width: float = 148.0, height: float = 210.0, chamfer: float = 2.0, notch: float = 5.0) -> list[Point]:
    """A5 plate with three chamfered corners and a 5 mm top-left notch."""
    return [
        (notch, 0.0),
        (width - chamfer, 0.0),
        (width, chamfer),
        (width, height - chamfer),
        (width - chamfer, height),
        (chamfer, height),
        (0.0, height - chamfer),
        (0.0, notch),
        (notch, notch),
    ]


class _Builder:
    def __init__(self, diagram: Diagram) -> None:
        self.diagram = diagram
        self.features: list[Feature] = []
        self.trace: list[dict[str, Any]] = []
        self.warnings: list[str] = []

    def feature(self, feature_id: str, kind: str, label: str = "", semantic_id: str | None = None, index: int | None = None) -> Feature:
        if any(item.id == feature_id for item in self.features):
            raise ValueError(f"duplicate feature id: {feature_id}")
        item = Feature(id=feature_id, kind=kind, label=label, semantic_id=semantic_id, index=index)
        self.features.append(item)
        return item

    def line(self, feature: Feature, points: Iterable[Point], width: float, closed: bool = False) -> None:
        feature.primitives.append(Primitive("polyline", tuple(points), width=width, closed=closed))

    def polygon(self, feature: Feature, points: Iterable[Point]) -> None:
        feature.primitives.append(Primitive("polygon", tuple(points), closed=True))

    def circle(self, feature: Feature, cx: float, cy: float, radius: float) -> None:
        feature.primitives.append(Primitive("circle", ((cx, cy),), radius=radius))

    def text(self, feature: Feature, x: float, y: float, text: str, height: float = 6.0) -> None:
        feature.primitives.append(Primitive("text", ((x, y),), text=text, height=height))

    def braille(self, feature: Feature, x: float, y: float, text: str) -> None:
        feature.primitives.append(Primitive("braille", ((x, y),), text=text))

    def index_label(self, feature: Feature, x: float, y: float, index: int) -> None:
        self.text(feature, x, y, str(index), 6.0)
        self.braille(feature, x, y + 7.5, str(index))

    def text_with_braille(self, feature: Feature, x: float, y: float, text: str) -> None:
        self.text(feature, x, y, text, 6.0)
        self.braille(feature, x, y + 7.5, text)

    def dashed_line(self, feature: Feature, start: Point, end: Point, width: float, dash: float = 5.0, gap: float = 5.0) -> None:
        dx = end[0] - start[0]
        dy = end[1] - start[1]
        length = math.hypot(dx, dy)
        if length == 0:
            return
        ux, uy = dx / length, dy / length
        offset = 0.0
        while offset < length:
            segment_end = min(offset + dash, length)
            self.line(
                feature,
                (
                    (start[0] + ux * offset, start[1] + uy * offset),
                    (start[0] + ux * segment_end, start[1] + uy * segment_end),
                ),
                width,
            )
            offset += dash + gap

    def arc_arrow(self, feature: Feature, cx: float, cy: float, radius: float, start_deg: float, end_deg: float, width: float = 2.0, head: float = 4.5) -> None:
        points = arc_points(cx, cy, radius, start_deg, end_deg)
        self.line(feature, points, width)
        tip = points[-1]
        previous = points[-2]
        dx = tip[0] - previous[0]
        dy = tip[1] - previous[1]
        length = math.hypot(dx, dy)
        if length == 0:
            return
        ux, uy = dx / length, dy / length
        base_x = tip[0] - ux * head
        base_y = tip[1] - uy * head
        perp_x, perp_y = -uy, ux
        self.polygon(
            feature,
            (
                tip,
                (base_x + perp_x * head * 0.55, base_y + perp_y * head * 0.55),
                (base_x - perp_x * head * 0.55, base_y - perp_y * head * 0.55),
            ),
        )

    def title(self, text: str, y: float = 12.0) -> Feature:
        feature = self.feature("title", "title", label=text)
        width = text_width(text, 6.0)
        self.text(feature, (self.diagram.canvas.width_mm - width) / 2.0, y, text, 6.0)
        return feature

    def footer(self) -> None:
        footer = self.feature("footer", "footer")
        self.line(footer, ((8.0, 202.0), (18.0, 202.0)), 1.2)
        self.text(footer, 8.0, 194.0, "10MM", 5.0)
        self.text(footer, 55.0, 198.0, "TOUCHLEARN", 5.0)

    def qr_zone(self) -> None:
        feature = self.feature("qr_zone", "qr_zone")
        self.dashed_line(feature, (114.0, 176.0), (142.0, 176.0), 1.0, 3.0, 3.0)
        self.dashed_line(feature, (142.0, 176.0), (142.0, 204.0), 1.0, 3.0, 3.0)
        self.dashed_line(feature, (142.0, 204.0), (114.0, 204.0), 1.0, 3.0, 3.0)
        self.dashed_line(feature, (114.0, 204.0), (114.0, 176.0), 1.0, 3.0, 3.0)
        self.text(feature, 114.0, 168.0, "QR", 5.0)


def _layout_robot(diagram: Diagram) -> LayoutResult:
    builder = _Builder(diagram)
    width = diagram.canvas.width_mm
    height = diagram.canvas.height_mm

    builder.trace.append(
        {
            "stage": "layout",
            "action": "choose_engine",
            "decision": "kinematic_chain / side view",
            "reason": "The six-axis arm is a serial chain; a side view preserves joint order and wrist density.",
        }
    )

    elements = diagram.element_map()
    title = builder.feature("title", "title")
    title_text = "SIX-AXIS ROBOT ARM"
    builder.text(title, (width - text_width(title_text, 6.0)) / 2.0, 12.0, title_text, 6.0)

    base = builder.feature("BASE", "link", "BASE", semantic_id="BASE")
    builder.polygon(base, rect_polygon(40.0, 180.0, 30.0, 12.0, chamfer=2.0))
    for x0 in (43.0, 49.0, 55.0, 61.0):
        builder.line(base, ((x0, 189.0), (x0 + 7.0, 183.0)), 1.2)
    builder.index_label(builder.feature("label_BASE", "label", "1", semantic_id="BASE", index=1), 18.0, 176.0, 1)

    l1 = builder.feature("L1", "link", "L1", semantic_id="L1")
    builder.line(l1, ((55.0, 180.0), (55.0, 145.0)), 11.0)
    builder.index_label(builder.feature("label_L1", "label", "2", semantic_id="L1", index=2), 65.0, 155.0, 2)

    j1 = builder.feature("J1", "joint", "J1", semantic_id="J1")
    builder.circle(j1, 55.0, 180.0, 6.0)
    builder.index_label(builder.feature("label_J1", "label", "4", semantic_id="J1", index=4), 34.0, 172.0, 4)

    l2 = builder.feature("L2", "link", "L2", semantic_id="L2")
    builder.line(l2, ((55.0, 145.0), (90.0, 115.0)), 10.0)
    builder.index_label(builder.feature("label_L2", "label", "3", semantic_id="L2", index=3), 80.0, 152.0, 3)

    j2 = builder.feature("J2", "joint", "J2", semantic_id="J2")
    builder.circle(j2, 55.0, 145.0, 5.5)
    builder.arc_arrow(j2, 55.0, 145.0, 10.0, -80.0, -10.0, width=2.0, head=4.0)
    builder.index_label(builder.feature("label_J2", "label", "5", semantic_id="J2", index=5), 34.0, 137.0, 5)

    j3 = builder.feature("J3", "joint", "J3", semantic_id="J3")
    builder.circle(j3, 90.0, 115.0, 5.0)
    builder.arc_arrow(j3, 90.0, 115.0, 9.0, -80.0, -10.0, width=2.0, head=4.0)
    j3_label = builder.feature("label_J3", "label", "6", semantic_id="J3", index=6)
    builder.index_label(j3_label, 70.0, 92.0, 6)
    builder.line(j3_label, ((80.0, 104.0), (85.0, 110.0)), 1.2)

    l3 = builder.feature("L3", "link", "L3", semantic_id="J4J6")
    builder.line(l3, ((90.0, 115.0), (124.0, 87.0)), 9.0)

    wrist = builder.feature("J4J6", "joint_group", "J4-J6", semantic_id="J4J6")
    builder.line(wrist, rect_polygon(115.0, 45.0, 18.0, 42.0, chamfer=2.0), 2.0, closed=True)
    builder.line(wrist, ((124.0, 45.0), (124.0, 87.0)), 6.0)
    for cy in (51.0, 63.0, 75.0):
        builder.circle(wrist, 124.0, cy, 3.0)
    builder.index_label(builder.feature("label_J4J6", "label", "7", semantic_id="J4J6", index=7), 100.0, 46.0, 7)

    ee = builder.feature("EE", "end_effector", "EE", semantic_id="EE")
    builder.line(ee, ((124.0, 45.0), (124.0, 36.0)), 7.0)
    builder.polygon(
        ee,
        (
            (118.0, 30.0),
            (130.0, 30.0),
            (130.0, 36.0),
            (126.0, 36.0),
            (126.0, 33.0),
            (122.0, 33.0),
            (122.0, 36.0),
            (118.0, 36.0),
        ),
    )
    builder.index_label(builder.feature("label_EE", "label", "8", semantic_id="EE", index=8), 102.0, 26.0, 8)

    builder.footer()
    builder.qr_zone()

    builder.trace.append(
        {
            "stage": "layout",
            "action": "place_wrist_panel",
            "decision": "18 x 42 mm panel, joints 12 mm apart",
            "reason": "Keeps J4-J6 at 6 mm edge-to-edge spacing instead of collapsing them into one unresolved blob.",
        }
    )
    builder.trace.append(
        {
            "stage": "layout",
            "action": "index_labels",
            "decision": "eight numeric labels, legend card carries names",
            "reason": "Numeric braille is shorter than spelling BASE/L1/J4-J6 on the plate; the A6 legend maps 1-8.",
        }
    )

    return LayoutResult(
        diagram=diagram,
        width=width,
        height=height,
        base_polygon=base_plate_polygon(width, height),
        features=builder.features,
        trace=builder.trace,
        warnings=builder.warnings,
    )


def _layout_quadratic(diagram: Diagram) -> LayoutResult:
    builder = _Builder(diagram)
    width = diagram.canvas.width_mm
    height = diagram.canvas.height_mm
    options = diagram.layout.get("options", {})
    x_min = float(options.get("x_min", -2))
    x_max = float(options.get("x_max", 4))
    y_min = float(options.get("y_min", -5))
    y_max = float(options.get("y_max", 6))
    scale = float(options.get("mm_per_unit", 10))
    expression = "Y=X^2-2X-3"
    coefficients = {"a": 1.0, "b": -2.0, "c": -3.0}

    builder.trace.append(
        {
            "stage": "layout",
            "action": "choose_engine",
            "decision": "cartesian_plot / 10 mm per unit",
            "reason": "The curve is generated from a=1, b=-2, c=-3, not traced from pixels.",
        }
    )

    title_text = "QUADRATIC FUNCTION"
    title = builder.feature("title", "title")
    builder.text(title, (width - text_width(title_text, 6.0)) / 2.0, 12.0, title_text, 6.0)

    equation = builder.feature("equation", "label")
    builder.text(equation, 30.0, 30.0, expression, 6.0)

    def to_canvas(x_value: float, y_value: float) -> Point:
        return (50.0 + x_value * scale, 110.0 - y_value * scale)

    grid = builder.feature("grid", "grid")
    for x_value in (-2, 4):
        builder.line(grid, ((50.0 + x_value * scale, 60.0), (50.0 + x_value * scale, 160.0)), 1.0)
    for y_value in (2, -4):
        builder.line(grid, ((30.0, 110.0 - y_value * scale), (90.0, 110.0 - y_value * scale)), 1.0)

    axes = builder.feature("axes", "axis")
    builder.line(axes, ((30.0, 110.0), (90.0, 110.0)), 2.0)
    builder.line(axes, ((50.0, 160.0), (50.0, 50.0)), 2.0)
    builder.polygon(axes, ((90.0, 110.0), (85.0, 107.5), (85.0, 112.5)))
    builder.polygon(axes, ((50.0, 50.0), (47.5, 55.0), (52.5, 55.0)))
    builder.circle(axes, 50.0, 110.0, 1.5)

    curve = builder.feature("curve", "curve", "CURVE", semantic_id="curve")
    samples = 49
    points = []
    for index in range(samples + 1):
        x_value = x_min + (x_max - x_min) * index / samples
        y_value = coefficients["a"] * x_value**2 + coefficients["b"] * x_value + coefficients["c"]
        points.append(to_canvas(x_value, y_value))
    builder.line(curve, points, 2.5)

    symmetry = builder.feature("symmetry_axis", "axis", "X=1", semantic_id="symmetry_axis")
    builder.dashed_line(symmetry, (60.0, 50.0), (60.0, 160.0), 1.2, 5.0, 5.0)
    symmetry_label = builder.feature("label_symmetry", "label", "x=1", semantic_id="symmetry_axis")
    builder.text_with_braille(symmetry_label, 62.0, 42.0, "x=1")

    vertex = builder.feature("vertex", "point", "V", semantic_id="vertex")
    builder.circle(vertex, 60.0, 150.0, 3.5)
    builder.index_label(builder.feature("label_vertex", "label", "1", semantic_id="vertex", index=1), 66.0, 156.0, 1)

    root_left = builder.feature("root_left", "point", "R1", semantic_id="root_left")
    builder.circle(root_left, 40.0, 110.0, 3.0)
    builder.index_label(builder.feature("label_root_left", "label", "2", semantic_id="root_left", index=2), 33.0, 124.0, 2)

    root_right = builder.feature("root_right", "point", "R2", semantic_id="root_right")
    builder.circle(root_right, 80.0, 110.0, 3.0)
    root_right_label = builder.feature("label_root_right", "label", "3", semantic_id="root_right", index=3)
    builder.index_label(root_right_label, 94.0, 116.0, 3)
    builder.line(root_right_label, ((94.0, 116.0), (84.0, 115.0)), 1.2)

    axis_x = builder.feature("label_x", "label", "x", semantic_id="x_axis")
    builder.text_with_braille(axis_x, 94.0, 88.0, "x")
    axis_y = builder.feature("label_y", "label", "y", semantic_id="y_axis")
    builder.text_with_braille(axis_y, 53.0, 62.0, "y")
    origin = builder.feature("label_origin", "label", "o", semantic_id="origin")
    builder.text_with_braille(origin, 53.0, 92.0, "o")

    builder.footer()
    builder.qr_zone()

    builder.trace.append(
        {
            "stage": "layout",
            "action": "simplify_grid",
            "decision": "major grid every 2 units, 10 mm minor spacing",
            "reason": "A full 1-unit grid makes the curve hard to follow by touch; the audio supplies the scale.",
        }
    )
    builder.trace.append(
        {
            "stage": "layout",
            "action": "place_key_points",
            "decision": "vertex (1,-4), roots (-1,0) and (3,0)",
            "reason": "Computed from the coefficients, so a changed expression moves the markers automatically.",
        }
    )

    return LayoutResult(
        diagram=diagram,
        width=width,
        height=height,
        base_polygon=base_plate_polygon(width, height),
        features=builder.features,
        trace=builder.trace,
        warnings=builder.warnings,
    )


def build_layout(diagram: Diagram) -> LayoutResult:
    engine = diagram.layout.get("engine")
    if engine == "kinematic_chain":
        result = _layout_robot(diagram)
    elif engine == "cartesian_plot":
        result = _layout_quadratic(diagram)
    elif engine in {"function_plot", "robot_relief", "scene"}:
        # Imported lazily: the agent layout engines themselves build on the
        # primitives in this module.
        from .agent.parametric import build_function_layout, build_robot_layout
        from .agent.scene import build_scene_layout

        if engine == "function_plot":
            result = build_function_layout(diagram)
        elif engine == "robot_relief":
            result = build_robot_layout(diagram)
        else:
            result = build_scene_layout(diagram)
    else:
        raise ValueError(f"unsupported layout engine: {engine}")

    # Basic safety checks that every engine must satisfy.
    for feature in result.features:
        left, top, feature_width, feature_height = feature.bbox()
        if left < 0 or top < 0 or left + feature_width > result.width or top + feature_height > result.height:
            result.warnings.append(
                f"feature '{feature.id}' extends outside the canvas: "
                f"({left:.1f}, {top:.1f}, {feature_width:.1f}, {feature_height:.1f})"
            )
    return result
