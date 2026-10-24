"""Pure-Python mesh generation.

The v0 generator deliberately avoids CAD kernels.  Every tactile primitive is
decomposed into simple prisms, triangulated with ear clipping and written as a
binary STL.  This keeps the pipeline reproducible on a clean Windows machine
with no compiled dependencies.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Sequence

from . import braille, font
from .layout import Feature, LayoutResult, Point, Primitive

Point3 = tuple[float, float, float]
Triangle = tuple[Point3, Point3, Point3]


def _is_close(a: Point, b: Point, tolerance: float = 1e-6) -> bool:
    return abs(a[0] - b[0]) <= tolerance and abs(a[1] - b[1]) <= tolerance


def signed_area(polygon: Sequence[Point]) -> float:
    area = 0.0
    for index, point in enumerate(polygon):
        nxt = polygon[(index + 1) % len(polygon)]
        area += point[0] * nxt[1] - nxt[0] * point[1]
    return area / 2.0


def ensure_ccw(polygon: Sequence[Point]) -> list[Point]:
    points = list(polygon)
    if signed_area(points) < 0:
        points.reverse()
    return points


def _clean_polygon(polygon: Sequence[Point]) -> list[Point]:
    points: list[Point] = []
    for point in polygon:
        if not points or not _is_close(points[-1], point):
            points.append((float(point[0]), float(point[1])))
    if len(points) > 1 and _is_close(points[0], points[-1]):
        points.pop()
    # Drop collinear vertices; ear clipping is happier without them.
    changed = True
    while changed and len(points) > 3:
        changed = False
        result: list[Point] = []
        for index, point in enumerate(points):
            previous = points[index - 1]
            nxt = points[(index + 1) % len(points)]
            cross = (point[0] - previous[0]) * (nxt[1] - point[1]) - (point[1] - previous[1]) * (nxt[0] - point[0])
            if abs(cross) < 1e-9:
                changed = True
                continue
            result.append(point)
        if len(result) >= 3:
            points = result
    return points


def _cross(a: Point, b: Point, c: Point) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _point_in_triangle(point: Point, a: Point, b: Point, c: Point) -> bool:
    c1 = _cross(a, b, point)
    c2 = _cross(b, c, point)
    c3 = _cross(c, a, point)
    has_negative = c1 < -1e-9 or c2 < -1e-9 or c3 < -1e-9
    has_positive = c1 > 1e-9 or c2 > 1e-9 or c3 > 1e-9
    return not (has_negative and has_positive)


def triangulate(polygon: Sequence[Point]) -> list[tuple[Point, Point, Point]]:
    """Ear-clipping triangulation for a simple polygon."""
    points = _clean_polygon(ensure_ccw(polygon))
    if len(points) < 3:
        return []
    if len(points) == 3:
        return [(points[0], points[1], points[2])]

    indices = list(range(len(points)))
    triangles: list[tuple[Point, Point, Point]] = []
    guard = 0
    while len(indices) > 3 and guard < 10000:
        guard += 1
        ear_found = False
        for position in range(len(indices)):
            previous_index = indices[position - 1]
            current_index = indices[position]
            next_index = indices[(position + 1) % len(indices)]
            a = points[previous_index]
            b = points[current_index]
            c = points[next_index]
            if _cross(a, b, c) <= 1e-9:
                continue
            if any(
                _point_in_triangle(points[other], a, b, c)
                for other in indices
                if other not in {previous_index, current_index, next_index}
            ):
                continue
            triangles.append((a, b, c))
            del indices[position]
            ear_found = True
            break
        if not ear_found:
            raise ValueError("failed to triangulate polygon; check for self-intersection")

    if len(indices) == 3:
        triangles.append((points[indices[0]], points[indices[1]], points[indices[2]]))
    return triangles


@dataclass
class Mesh:
    triangles: list[Triangle]

    def bounds(self) -> tuple[float, float, float, float, float, float]:
        if not self.triangles:
            return (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
        xs = [vertex[0] for triangle in self.triangles for vertex in triangle]
        ys = [vertex[1] for triangle in self.triangles for vertex in triangle]
        zs = [vertex[2] for triangle in self.triangles for vertex in triangle]
        return (min(xs), min(ys), min(zs), max(xs), max(ys), max(zs))

    def volume(self) -> float:
        total = 0.0
        for a, b, c in self.triangles:
            total += (
                a[0] * (b[1] * c[2] - b[2] * c[1])
                - a[1] * (b[0] * c[2] - b[2] * c[0])
                + a[2] * (b[0] * c[1] - b[1] * c[0])
            )
        return total / 6.0

    def edge_counts(self) -> dict[tuple[Point3, Point3], int]:
        counts: dict[tuple[Point3, Point3], int] = {}
        for a, b, c in self.triangles:
            for first, second in ((a, b), (b, c), (c, a)):
                key = tuple(sorted((first, second)))  # type: ignore[arg-type]
                counts[key] = counts.get(key, 0) + 1
        return counts

    def is_watertight(self) -> bool:
        return all(count == 2 for count in self.edge_counts().values())

    def triangle_count(self) -> int:
        return len(self.triangles)

    def stats(self) -> dict[str, float | int | bool]:
        bounds = self.bounds()
        return {
            "triangles": self.triangle_count(),
            "bounds_mm": [round(value, 3) for value in bounds],
            "size_mm": [round(bounds[3] - bounds[0], 3), round(bounds[4] - bounds[1], 3), round(bounds[5] - bounds[2], 3)],
            "volume_mm3": round(self.volume(), 3),
            "watertight": self.is_watertight(),
        }


def prism_triangles(polygon: Sequence[Point], z0: float, z1: float, sheet_height: float) -> list[Triangle]:
    """Extrude a layout-space polygon into a closed prism.

    Layout coordinates use a downward y axis; the mesh uses a conventional
    upward y axis.  The flip happens before the winding is normalised so that
    the top faces end up counter-clockwise and the volume is positive.
    """
    flipped = [(point[0], sheet_height - point[1]) for point in polygon]
    points = _clean_polygon(ensure_ccw(flipped))
    if len(points) < 3:
        return []
    triangles: list[Triangle] = []
    for a, b, c in triangulate(points):
        triangles.append(((a[0], a[1], z1), (b[0], b[1], z1), (c[0], c[1], z1)))
        triangles.append(((a[0], a[1], z0), (c[0], c[1], z0), (b[0], b[1], z0)))
    for index, point in enumerate(points):
        nxt = points[(index + 1) % len(points)]
        triangles.append(((point[0], point[1], z0), (nxt[0], nxt[1], z0), (nxt[0], nxt[1], z1)))
        triangles.append(((point[0], point[1], z0), (nxt[0], nxt[1], z1), (point[0], point[1], z1)))
    return triangles


def segment_polygon(start: Point, end: Point, width: float) -> list[Point]:
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    length = math.hypot(dx, dy)
    if length <= 1e-9:
        return []
    normal_x = -dy / length * width / 2.0
    normal_y = dx / length * width / 2.0
    return [
        (start[0] + normal_x, start[1] + normal_y),
        (end[0] + normal_x, end[1] + normal_y),
        (end[0] - normal_x, end[1] - normal_y),
        (start[0] - normal_x, start[1] - normal_y),
    ]


def circle_polygon(cx: float, cy: float, radius: float, segments: int = 24, phase: float = 0.0) -> list[Point]:
    return [
        (
            cx + radius * math.cos(phase + 2 * math.pi * index / segments),
            cy + radius * math.sin(phase + 2 * math.pi * index / segments),
        )
        for index in range(segments)
    ]


def primitive_polygons(primitive: Primitive) -> list[list[Point]]:
    """Decompose a tactile primitive into simple 2D polygons."""
    polygons: list[list[Point]] = []
    if primitive.kind == "polyline":
        points = list(primitive.points)
        if len(points) < 2:
            return polygons
        segment_count = len(points) if primitive.closed else len(points) - 1
        for index in range(segment_count):
            start = points[index]
            end = points[(index + 1) % len(points)]
            polygon = segment_polygon(start, end, primitive.width)
            if polygon:
                polygons.append(polygon)
        radius = primitive.width / 2.0
        for point in points:
            polygons.append(circle_polygon(point[0], point[1], radius, segments=12, phase=math.pi / 12.0))
        return polygons

    if primitive.kind == "polygon":
        return [list(primitive.points)]

    if primitive.kind == "circle":
        center = primitive.points[0]
        return [circle_polygon(center[0], center[1], primitive.radius, segments=24)]

    if primitive.kind == "text":
        x, y = primitive.points[0]
        stroke_width = font.stroke_width(primitive.height)
        phase = math.pi / 10.0
        for stroke in font.text_strokes(primitive.text, x, y, primitive.height):
            points: list[Point] = []
            for point in stroke:
                if not points or not _is_close(points[-1], point):
                    points.append(point)
            if len(points) == 1:
                polygons.append(circle_polygon(points[0][0], points[0][1], stroke_width / 2.0, segments=10, phase=phase))
                continue
            for index in range(len(points) - 1):
                polygon = segment_polygon(points[index], points[index + 1], stroke_width)
                if polygon:
                    polygons.append(polygon)
            joints: list[Point] = []
            for point in points:
                if not any(_is_close(point, existing) for existing in joints):
                    joints.append(point)
            for point in joints:
                polygons.append(circle_polygon(point[0], point[1], stroke_width / 2.0, segments=10, phase=phase))
        return polygons

    if primitive.kind == "braille":
        x, y = primitive.points[0]
        radius = braille.DOT_DIAMETER_MM / 2.0
        for cx, cy in braille.dot_centers(primitive.text, x, y):
            polygons.append(circle_polygon(cx, cy, radius, segments=16))
        return polygons

    raise ValueError(f"unknown primitive kind: {primitive.kind}")


def primitive_relief(primitive: Primitive, relief_height: float, braille_height: float) -> float:
    return braille_height if primitive.kind == "braille" else relief_height


def build_mesh(
    layout: LayoutResult,
    base_thickness: float | None = None,
    relief_height: float | None = None,
    braille_height: float | None = None,
    embed: float = 0.05,
) -> Mesh:
    constraints = layout.diagram.constraints
    base_thickness = float(base_thickness if base_thickness is not None else constraints.get("base_thickness_mm", 3.0))
    relief_height = float(relief_height if relief_height is not None else constraints.get("relief_height_mm", 0.8))
    braille_height = float(braille_height if braille_height is not None else constraints.get("braille_height_mm", 0.6))

    triangles: list[Triangle] = []
    triangles.extend(prism_triangles(layout.base_polygon, 0.0, base_thickness, layout.height))

    # Each tactile primitive is emitted as its own closed shell.  A sub-micron
    # z offset keeps coincident shells from sharing edges, so the combined STL
    # has exactly two triangles per edge.  Slicers union the overlapping shells.
    shell_index = 0
    for feature in layout.features:
        for primitive in feature.primitives:
            height = primitive_relief(primitive, relief_height, braille_height)
            z0 = base_thickness - embed
            z1 = base_thickness + height
            for polygon in primitive_polygons(primitive):
                jitter = shell_index * 1e-7
                shell_index += 1
                triangles.extend(prism_triangles(polygon, z0 + jitter, z1 + jitter, layout.height))

    return Mesh(triangles)


def feature_polygons(feature: Feature) -> list[list[Point]]:
    polygons: list[list[Point]] = []
    for primitive in feature.primitives:
        polygons.extend(primitive_polygons(primitive))
    return polygons
