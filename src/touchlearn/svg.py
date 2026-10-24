"""SVG preview that mirrors the tactile geometry."""

from __future__ import annotations

from html import escape
from pathlib import Path

from . import braille, font
from .layout import LayoutResult, Primitive

COLORS = {
    "title": "#111111",
    "footer": "#666666",
    "qr_zone": "#999999",
    "grid": "#b8b8b8",
    "axis": "#333333",
    "link": "#1f4e79",
    "joint": "#b00020",
    "joint_group": "#b00020",
    "end_effector": "#b00020",
    "curve": "#6a3d9a",
    "point": "#b00020",
    "label": "#111111",
}


def _fmt(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".")


def _points(points: tuple[tuple[float, float], ...]) -> str:
    return " ".join(f"{_fmt(x)},{_fmt(y)}" for x, y in points)


def _primitive_svg(primitive: Primitive, color: str) -> str:
    if primitive.kind == "polyline":
        points = list(primitive.points)
        if primitive.closed and points:
            points.append(points[0])
        return (
            f'<polyline points="{_points(tuple(points))}" fill="none" stroke="{color}" '
            f'stroke-width="{_fmt(primitive.width)}" stroke-linecap="round" stroke-linejoin="round"/>'
        )
    if primitive.kind == "polygon":
        return (
            f'<polygon points="{_points(primitive.points)}" fill="{color}" stroke="{color}" '
            f'stroke-width="0.2" stroke-linejoin="round"/>'
        )
    if primitive.kind == "circle":
        cx, cy = primitive.points[0]
        return f'<circle cx="{_fmt(cx)}" cy="{_fmt(cy)}" r="{_fmt(primitive.radius)}" fill="{color}"/>'
    if primitive.kind == "text":
        x, y = primitive.points[0]
        width = font.stroke_width(primitive.height)
        return "".join(
            f'<polyline points="{_points(stroke)}" fill="none" stroke="{color}" '
            f'stroke-width="{_fmt(width)}" stroke-linecap="round" stroke-linejoin="round"/>'
            for stroke in font.text_strokes(primitive.text, x, y, primitive.height)
            if len(stroke) >= 2
        )
    if primitive.kind == "braille":
        x, y = primitive.points[0]
        radius = braille.DOT_DIAMETER_MM / 2.0
        return "".join(
            f'<circle cx="{_fmt(cx)}" cy="{_fmt(cy)}" r="{_fmt(radius)}" fill="{color}"/>'
            for cx, cy in braille.dot_centers(primitive.text, x, y)
        )
    raise ValueError(f"unknown primitive kind: {primitive.kind}")


def layout_to_svg(layout: LayoutResult) -> str:
    base_points = _points(tuple(layout.base_polygon))
    parts: list[str] = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{_fmt(layout.width)}mm" height="{_fmt(layout.height)}mm" '
        f'viewBox="0 0 {_fmt(layout.width)} {_fmt(layout.height)}">',
        f"<title>{escape(layout.diagram.title)}</title>",
        f"<desc>{escape(layout.diagram.title_zh)} - TouchLearn tactile plate preview</desc>",
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        f'<polygon points="{base_points}" fill="#faf8f2" stroke="#222222" stroke-width="0.6"/>',
    ]

    for feature in layout.features:
        color = COLORS.get(feature.kind, "#111111")
        parts.append(f'<g id="{feature.id}" data-kind="{feature.kind}">')
        for primitive in feature.primitives:
            parts.append(_primitive_svg(primitive, color))
        parts.append("</g>")

    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def write_svg(layout: LayoutResult, path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(layout_to_svg(layout), encoding="utf-8")
    return target
