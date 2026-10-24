"""Printability and tactile-safety checks.

These checks are deliberately conservative.  A layout that fails should be
fixed in the semantic model or the layout engine, not patched in the slicer.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Sequence

from . import braille, font
from .geometry import Mesh, feature_polygons
from .layout import Feature, LayoutResult, Point, Primitive, primitive_bbox
from .model import Diagram


@dataclass(frozen=True)
class Issue:
    severity: str
    code: str
    message: str
    feature_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "feature_id": self.feature_id,
        }


@dataclass
class Report:
    diagram_id: str
    passed: bool
    issues: list[Issue] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "diagram_id": self.diagram_id,
            "passed": self.passed,
            "issues": [issue.to_dict() for issue in self.issues],
            "metrics": self.metrics,
        }


def _bbox_distance(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    dx = max(ax - (bx + bw), bx - (ax + aw), 0.0)
    dy = max(ay - (by + bh), by - (ay + ah), 0.0)
    return math.hypot(dx, dy)


def _point_segment_distance(point: Point, start: Point, end: Point) -> float:
    px, py = point
    sx, sy = start
    ex, ey = end
    dx = ex - sx
    dy = ey - sy
    length_squared = dx * dx + dy * dy
    if length_squared <= 1e-12:
        return math.hypot(px - sx, py - sy)
    t = max(0.0, min(1.0, ((px - sx) * dx + (py - sy) * dy) / length_squared))
    return math.hypot(px - (sx + t * dx), py - (sy + t * dy))


def _segments_intersect(a: Point, b: Point, c: Point, d: Point) -> bool:
    def cross(p: Point, q: Point, r: Point) -> float:
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    c1 = cross(a, b, c)
    c2 = cross(a, b, d)
    c3 = cross(c, d, a)
    c4 = cross(c, d, b)
    return (c1 * c2 < -1e-9) and (c3 * c4 < -1e-9)


def _point_in_polygon(point: Point, polygon: Sequence[Point]) -> bool:
    x, y = point
    inside = False
    for index, current in enumerate(polygon):
        previous = polygon[index - 1]
        if (current[1] > y) != (previous[1] > y):
            intersection_x = (previous[0] - current[0]) * (y - current[1]) / (previous[1] - current[1]) + current[0]
            if x < intersection_x:
                inside = not inside
    return inside


def polygon_distance(first: Sequence[Point], second: Sequence[Point]) -> float:
    if len(first) < 2 or len(second) < 2:
        return float("inf")
    if any(_point_in_polygon(point, second) for point in first):
        return 0.0
    if any(_point_in_polygon(point, first) for point in second):
        return 0.0
    best = float("inf")
    for index, point in enumerate(first):
        nxt = first[(index + 1) % len(first)]
        for other_index, other in enumerate(second):
            other_next = second[(other_index + 1) % len(second)]
            if _segments_intersect(point, nxt, other, other_next):
                return 0.0
            best = min(
                best,
                _point_segment_distance(point, other, other_next),
                _point_segment_distance(other, point, nxt),
            )
    return best


def feature_distance(first: Feature, second: Feature, bbox_limit: float) -> float:
    if _bbox_distance(first.bbox(), second.bbox()) >= bbox_limit:
        return float("inf")
    polygons_first = feature_polygons(first)
    polygons_second = feature_polygons(second)
    best = float("inf")
    for polygon in polygons_first:
        min_x = min(point[0] for point in polygon)
        max_x = max(point[0] for point in polygon)
        min_y = min(point[1] for point in polygon)
        max_y = max(point[1] for point in polygon)
        for other in polygons_second:
            other_min_x = min(point[0] for point in other)
            other_max_x = max(point[0] for point in other)
            other_min_y = min(point[1] for point in other)
            other_max_y = max(point[1] for point in other)
            dx = max(min_x - other_max_x, other_min_x - max_x, 0.0)
            dy = max(min_y - other_max_y, other_min_y - max_y, 0.0)
            if math.hypot(dx, dy) >= best:
                continue
            best = min(best, polygon_distance(polygon, other))
            if best == 0.0:
                return 0.0
    return best


def _rect_intersects(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    return not (a[0] + a[2] <= b[0] or b[0] + b[2] <= a[0] or a[1] + a[3] <= b[1] or b[1] + b[3] <= a[1])


def _primitive_min_width(primitive: Primitive) -> float:
    if primitive.kind == "polyline":
        return primitive.width
    if primitive.kind == "polygon":
        return float("inf")
    if primitive.kind == "circle":
        return primitive.radius * 2.0
    if primitive.kind == "text":
        return font.stroke_width(primitive.height)
    if primitive.kind == "braille":
        return braille.DOT_DIAMETER_MM
    return 0.0


def check_layout(diagram: Diagram, layout: LayoutResult, mesh: Mesh | None = None) -> Report:
    issues: list[Issue] = []
    constraints = diagram.constraints
    min_width = float(constraints.get("min_line_width_mm", 1.0))
    min_spacing = float(constraints.get("min_spacing_mm", 4.0))
    label_clearance = float(constraints.get("label_clearance_mm", 1.2))
    max_features = int(constraints.get("max_features", 12))
    base_thickness = float(constraints.get("base_thickness_mm", 3.0))
    relief = float(constraints.get("relief_height_mm", 0.8))
    braille_height = float(constraints.get("braille_height_mm", 0.6))

    if len(diagram.elements) > max_features:
        issues.append(
            Issue(
                "error",
                "feature_count",
                f"{len(diagram.elements)} semantic elements exceed the A5 limit of {max_features}.",
            )
        )

    if base_thickness < 3.0:
        issues.append(Issue("error", "base_thickness", f"base is {base_thickness} mm; minimum is 3.0 mm."))
    if not 0.4 <= relief <= 0.8:
        issues.append(Issue("error", "relief", f"relief is {relief} mm; allowed range is 0.4-0.8 mm."))
    if not 0.4 <= braille_height <= 0.8:
        issues.append(Issue("error", "braille_height", f"braille height is {braille_height} mm; allowed range is 0.4-0.8 mm."))

    margin = max(float(diagram.canvas.margin_mm), 5.0)
    qr_rect = (113.0, 175.0, 30.0, 30.0)
    connected = diagram.connected_pairs()
    min_width_seen = float("inf")
    min_spacing_seen = float("inf")
    min_clearance_seen = float("inf")

    for feature in layout.features:
        left, top, width, height = feature.bbox()
        if left < margin - 1e-6 or top < margin - 1e-6:
            issues.append(
                Issue("error", "edge_margin", f"feature '{feature.id}' is {min(left, top):.2f} mm from the edge.", feature.id)
            )
        if left + width > layout.width - margin + 1e-6 or top + height > layout.height - margin + 1e-6:
            issues.append(
                Issue(
                    "error",
                    "edge_margin",
                    f"feature '{feature.id}' reaches the edge margin on the right or bottom.",
                    feature.id,
                )
            )
        if feature.id != "qr_zone" and _rect_intersects((left, top, width, height), qr_rect):
            issues.append(Issue("error", "qr_zone", f"feature '{feature.id}' intrudes into the 30 x 30 mm QR area.", feature.id))

        for primitive in feature.primitives:
            observed = _primitive_min_width(primitive)
            if observed < min_width - 1e-9:
                issues.append(
                    Issue(
                        "error",
                        "line_width",
                        f"feature '{feature.id}' has a {observed:.2f} mm feature; minimum is {min_width:.2f} mm.",
                        feature.id,
                    )
                )
            min_width_seen = min(min_width_seen, observed)

    labels = [feature for feature in layout.features if feature.kind == "label"]
    non_labels = [feature for feature in layout.features if feature.kind != "label"]

    for label in labels:
        for other in non_labels:
            if other.kind == "qr_zone" and label.id == "qr_zone":
                continue
            if other.kind == "grid":
                continue
            if label.semantic_id and label.semantic_id == other.semantic_id:
                continue
            if label.semantic_id and other.semantic_id:
                if frozenset((label.semantic_id, other.semantic_id)) in connected:
                    continue
            distance = feature_distance(label, other, max(min_spacing, label_clearance))
            if distance < label_clearance - 1e-6:
                issues.append(
                    Issue(
                        "error",
                        "label_clearance",
                        f"label '{label.id}' is {distance:.2f} mm from '{other.id}'; minimum clearance is {label_clearance:.2f} mm.",
                        label.id,
                    )
                )
            if distance < float("inf"):
                min_clearance_seen = min(min_clearance_seen, distance)

    for index, first in enumerate(labels):
        for second in labels[index + 1 :]:
            distance = feature_distance(first, second, 100.0)
            if distance < min_spacing - 1e-6:
                issues.append(
                    Issue(
                        "error",
                        "label_collision",
                        f"labels '{first.id}' and '{second.id}' are {distance:.2f} mm apart; minimum is {min_spacing:.2f} mm.",
                        first.id,
                    )
                )
            if distance < float("inf"):
                min_spacing_seen = min(min_spacing_seen, distance)

    metrics: dict[str, Any] = {
        "semantic_features": len(diagram.elements),
        "label_features": len(labels),
        "min_line_width_mm": round(min_width_seen, 3) if min_width_seen != float("inf") else None,
        "min_label_spacing_mm": round(min_spacing_seen, 3) if min_spacing_seen != float("inf") else None,
        "min_label_clearance_mm": round(min_clearance_seen, 3) if min_clearance_seen != float("inf") else None,
        "layout_bounds_mm": [round(value, 3) for value in layout.bounds()],
    }

    if mesh is not None:
        mesh_stats = mesh.stats()
        metrics["mesh"] = mesh_stats
        min_x, min_y, min_z, max_x, max_y, max_z = mesh.bounds()
        if min_x < -1e-6 or min_y < -1e-6 or max_x > layout.width + 1e-6 or max_y > layout.height + 1e-6:
            issues.append(Issue("error", "mesh_bounds", "mesh is outside the A5 footprint."))
        if max_z > 4.0 + 1e-6:
            issues.append(Issue("error", "mesh_height", f"mesh height is {max_z:.2f} mm; maximum is 4.0 mm."))
        if mesh.volume() <= 0:
            issues.append(Issue("error", "mesh_volume", "mesh volume is not positive; normals may be inverted."))
        if not mesh.is_watertight():
            issues.append(Issue("error", "mesh_watertight", "mesh has open or non-manifold edges."))

    passed = not any(issue.severity == "error" for issue in issues)
    return Report(diagram_id=diagram.id, passed=passed, issues=issues, metrics=metrics)
