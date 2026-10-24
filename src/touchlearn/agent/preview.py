"""Vector previews for meshes that have no 2D layout.

The browser can render SVG directly, so the volumetric and silhouette modes do
not need a rasterizer.  Triangles are painted back to front with flat shading;
for very dense relief grids the preview is subsampled, while the STL keeps the
full mesh.
"""

from __future__ import annotations

import html
import math
from pathlib import Path
from typing import Sequence

from ..geometry import Mesh, Point3


def _rotate(point: Point3, yaw: float, pitch: float) -> Point3:
    x, y, z = point
    cy, sy = math.cos(yaw), math.sin(yaw)
    x, y = x * cy - y * sy, x * sy + y * cy
    cp, sp = math.cos(pitch), math.sin(pitch)
    y, z = y * cp - z * sp, y * sp + z * cp
    return x, y, z


def _normal(a: Point3, b: Point3, c: Point3) -> Point3:
    ux, uy, uz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
    vx, vy, vz = c[0] - a[0], c[1] - a[1], c[2] - a[2]
    nx = uy * vz - uz * vy
    ny = uz * vx - ux * vz
    nz = ux * vy - uy * vx
    length = math.sqrt(nx * nx + ny * ny + nz * nz)
    if length <= 1e-12:
        return (0.0, 0.0, 0.0)
    return (nx / length, ny / length, nz / length)


def mesh_preview_svg(
    mesh: Mesh,
    path: str | Path,
    *,
    title: str,
    subtitle: str = "",
    width_mm: float = 148.0,
    height_mm: float = 210.0,
    margin_mm: float = 10.0,
    max_triangles: int = 18000,
) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    triangles = mesh.triangles
    step = max(1, math.ceil(len(triangles) / max_triangles))
    if step > 1:
        triangles = triangles[::step]

    yaw = math.radians(-35.0)
    pitch = math.radians(28.0)
    rotated = [_rotate(vertex, yaw, pitch) for triangle in triangles for vertex in triangle]
    xs = [point[0] for point in rotated]
    ys = [-point[2] for point in rotated]
    if not xs:
        raise ValueError("mesh has no triangles")
    span_x = max(max(xs) - min(xs), 1e-6)
    span_y = max(max(ys) - min(ys), 1e-6)
    content_w = width_mm - 2 * margin_mm
    content_h = height_mm - 2 * margin_mm - 34.0
    scale = min(content_w / span_x, content_h / span_y)
    ox = (width_mm - span_x * scale) / 2.0 - min(xs) * scale
    oy = margin_mm + 28.0 + (content_h - span_y * scale) / 2.0 - min(ys) * scale

    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width_mm}mm" height="{height_mm}mm" viewBox="0 0 {width_mm} {height_mm}">',
        f"<title>{html.escape(title)}</title>",
        f'<rect width="{width_mm}" height="{height_mm}" fill="#f7f3e9"/>',
        f'<rect x="2" y="2" width="{width_mm - 4}" height="{height_mm - 4}" rx="3" fill="none" stroke="#d8d0bf" stroke-width="0.5"/>',
        f'<text x="{margin_mm}" y="{margin_mm + 7}" font-family="Arial, Helvetica, sans-serif" font-size="7" font-weight="700" letter-spacing="0.6" fill="#172033">{html.escape(title)}</text>',
    ]
    if subtitle:
        parts.append(
            f'<text x="{margin_mm}" y="{margin_mm + 14}" font-family="Arial, Helvetica, sans-serif" font-size="3.4" fill="#5b6472">{html.escape(subtitle)}</text>'
        )
    parts.append(f'<text x="{width_mm - margin_mm}" y="{margin_mm + 7}" text-anchor="end" font-family="Arial, Helvetica, sans-serif" font-size="3" fill="#8a8170">PREVIEW / NOT TO SCALE</text>')

    faces: list[tuple[float, list[tuple[float, float]], str]] = []
    light = (-0.45, 0.35, 0.82)
    for triangle in triangles:
        a, b, c = triangle
        normal = _normal(a, b, c)
        shade = 0.42 + 0.58 * max(0.0, normal[0] * light[0] + normal[1] * light[1] + normal[2] * light[2])
        r = int(34 + 62 * shade)
        g = int(63 + 78 * shade)
        b_value = int(94 + 84 * shade)
        color = f"rgb({min(r, 255)},{min(g, 255)},{min(b_value, 255)})"
        points = []
        depths = []
        for vertex in triangle:
            x, y, z = _rotate(vertex, yaw, pitch)
            points.append((x * scale + ox, -z * scale + oy))
            depths.append(y)
        faces.append((sum(depths) / 3.0, points, color))
    faces.sort(key=lambda item: item[0], reverse=True)
    for _, points, color in faces:
        coords = " ".join(f"{x:.3f},{y:.3f}" for x, y in points)
        parts.append(f'<polygon points="{coords}" fill="{color}" stroke="{color}" stroke-width="0.08"/>')

    parts.append(
        f'<text x="{margin_mm}" y="{height_mm - margin_mm - 2}" font-family="Arial, Helvetica, sans-serif" font-size="3" fill="#6b7280">TOUCHLEARN / TEAM STELLA</text>'
    )
    parts.append(
        f'<text x="{width_mm - margin_mm}" y="{height_mm - margin_mm - 2}" text-anchor="end" font-family="Arial, Helvetica, sans-serif" font-size="3" fill="#6b7280">{mesh.triangle_count()} TRIANGLES</text>'
    )
    parts.append("</svg>")
    target.write_text("\n".join(parts), encoding="utf-8")
    return target
