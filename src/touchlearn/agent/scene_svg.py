"""Render a normalized scene as an SVG the browser editor can manipulate.

The tactile pipeline draws everything in millimetres on an A5 sheet.  The
editor must show the same geometry, otherwise a drag that looks fine on screen
would move a component somewhere else on the printed plate.  This renderer
therefore uses the real sheet coordinate system (148 x 210 mm) and applies the
same content-box mapping as :func:`touchlearn.agent.scene.build_scene_layout`.
"""

from __future__ import annotations

import math
from html import escape
from typing import Any

from .. import braille, font
from ..layout import base_plate_polygon

PLATE_WIDTH = 148.0
PLATE_HEIGHT = 210.0
SHEET_MARGIN = 10.0
QR_SIZE = 30.0

COLORS = {
    "title": "#111111",
    "subtitle": "#666666",
    "plate": "#d8d5cd",
    "content": "#e4e0d7",
    "block": "#1d1d1b",
    "wire": "#1d1d1b",
    "selected": "#0f766e",
    "label": "#5b5b57",
    "qr": "#c9c5bc",
}


def _content_box() -> tuple[float, float, float, float]:
    # Imported lazily so scene.py can import this module for thumbnails without
    # creating an import cycle at module load time.
    from .scene import CONTENT_BOX

    return CONTENT_BOX


def _fmt(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".")


def _points(points: list[list[float]] | tuple[tuple[float, float], ...]) -> str:
    return " ".join(f"{_fmt(float(x))},{_fmt(float(y))}" for x, y in points)


def _fit_text(text: str, max_width: float, preferred: float, minimum: float) -> tuple[str, float]:
    text = (text or "").strip()
    height = preferred
    while height > minimum and font.text_width(text, height) > max_width:
        height -= 0.2
    return text, max(round(height, 2), minimum)


def _text(text: str, x: float, y: float, height: float, color: str) -> str:
    width = font.stroke_width(height)
    strokes = font.text_strokes(text, x, y, height)
    if not strokes:
        return ""
    return "".join(
        f'<polyline points="{_points([list(point) for point in stroke])}" fill="none" '
        f'stroke="{color}" stroke-width="{_fmt(width)}" stroke-linecap="round" '
        f'stroke-linejoin="round"/>'
        for stroke in strokes
        if len(stroke) >= 2
    )


def _arc_points(cx: float, cy: float, radius: float, start_deg: float, end_deg: float, segments: int = 24) -> list[tuple[float, float]]:
    start = math.radians(start_deg)
    end = math.radians(end_deg)
    return [
        (
            cx + radius * math.cos(start + (end - start) * index / segments),
            cy + radius * math.sin(start + (end - start) * index / segments),
        )
        for index in range(segments + 1)
    ]


def _primitive_svg(primitive: dict[str, Any], to_mm_x: Any, to_mm_y: Any, color: str) -> str:
    kind = primitive.get("type")
    if kind == "polyline":
        return (
            f'<polyline points="{_points([(to_mm_x(x), to_mm_y(y)) for x, y in primitive["points"]])}" '
            f'fill="none" stroke="{color}" stroke-width="{_fmt(primitive["width"])}" '
            f'stroke-linecap="round" stroke-linejoin="round"/>'
        )
    if kind == "polygon":
        return (
            f'<polygon points="{_points([(to_mm_x(x), to_mm_y(y)) for x, y in primitive["points"]])}" '
            f'fill="none" stroke="{color}" stroke-width="{_fmt(primitive["width"])}" '
            f'stroke-linejoin="round"/>'
        )
    if kind == "rect":
        x = to_mm_x(primitive["x"])
        y = to_mm_y(primitive["y"])
        width = primitive["w"] / 100.0 * (_content_box()[2])
        height = primitive["h"] / 100.0 * (_content_box()[3])
        return (
            f'<rect x="{_fmt(x)}" y="{_fmt(y)}" width="{_fmt(width)}" height="{_fmt(height)}" '
            f'fill="none" stroke="{color}" stroke-width="{_fmt(primitive["width"])}"/>'
        )
    if kind == "circle":
        cx, cy = to_mm_x(primitive["cx"]), to_mm_y(primitive["cy"])
        radius = primitive["r"] / 100.0 * min(_content_box()[2], _content_box()[3])
        return (
            f'<circle cx="{_fmt(cx)}" cy="{_fmt(cy)}" r="{_fmt(radius)}" fill="none" '
            f'stroke="{color}" stroke-width="{_fmt(primitive["width"])}"/>'
        )
    if kind == "point":
        cx, cy = to_mm_x(primitive["cx"]), to_mm_y(primitive["cy"])
        radius = max(0.8, primitive["r"] / 100.0 * min(_content_box()[2], _content_box()[3]))
        return f'<circle cx="{_fmt(cx)}" cy="{_fmt(cy)}" r="{_fmt(radius)}" fill="{color}"/>'
    if kind == "arc":
        cx, cy = to_mm_x(primitive["cx"]), to_mm_y(primitive["cy"])
        radius = primitive["r"] / 100.0 * min(_content_box()[2], _content_box()[3])
        points = _arc_points(cx, cy, radius, primitive["start_deg"], primitive["end_deg"])
        return (
            f'<polyline points="{_points(points)}" fill="none" stroke="{color}" '
            f'stroke-width="{_fmt(primitive["width"])}" stroke-linecap="round"/>'
        )
    if kind == "arrow":
        points = [(to_mm_x(x), to_mm_y(y)) for x, y in primitive["points"]]
        if len(points) < 2:
            return ""
        tip, previous = points[-1], points[-2]
        length = min(6.0, max(2.5, math.dist(tip, previous) * 0.35))
        angle = math.atan2(tip[1] - previous[1], tip[0] - previous[0])
        left = (tip[0] - length * math.cos(angle - 0.5), tip[1] - length * math.sin(angle - 0.5))
        right = (tip[0] - length * math.cos(angle + 0.5), tip[1] - length * math.sin(angle + 0.5))
        return (
            f'<polyline points="{_points(points)}" fill="none" stroke="{color}" '
            f'stroke-width="{_fmt(primitive["width"])}" stroke-linecap="round" stroke-linejoin="round"/>'
            f'<polygon points="{_points([tip, left, right])}" fill="{color}"/>'
        )
    if kind == "text":
        return _text(
            str(primitive.get("text") or ""),
            to_mm_x(primitive["x"]),
            to_mm_y(primitive["y"]),
            float(primitive["height"]),
            color,
        )
    if kind == "braille":
        radius = braille.DOT_DIAMETER_MM / 2.0
        return "".join(
            f'<circle cx="{_fmt(cx)}" cy="{_fmt(cy)}" r="{_fmt(radius)}" fill="{color}"/>'
            for cx, cy in braille.dot_centers(
                str(primitive.get("text") or ""),
                to_mm_x(primitive["x"]),
                to_mm_y(primitive["y"]),
            )
        )
    return ""


def _primitive_bbox(primitive: dict[str, Any]) -> tuple[float, float, float, float] | None:
    kind = primitive.get("type")
    if kind in {"polyline", "polygon", "arrow"}:
        points = primitive.get("points") or []
        xs = [float(point[0]) for point in points if len(point) >= 2]
        ys = [float(point[1]) for point in points if len(point) >= 2]
        return (min(xs), min(ys), max(xs), max(ys)) if xs and ys else None
    if kind == "rect":
        x, y = float(primitive["x"]), float(primitive["y"])
        return (x, y, x + float(primitive["w"]), y + float(primitive["h"]))
    if kind in {"circle", "point", "arc"}:
        cx, cy, radius = float(primitive["cx"]), float(primitive["cy"]), float(primitive["r"])
        return (cx - radius, cy - radius, cx + radius, cy + radius)
    if kind == "text":
        x, y = float(primitive["x"]), float(primitive["y"])
        height = float(primitive["height"])
        return (x, y, x + font.text_width(str(primitive.get("text") or ""), height) / 1.32, y + height / 1.42)
    if kind == "braille":
        x, y = float(primitive["x"]), float(primitive["y"])
        width, height = braille.bbox(str(primitive.get("text") or ""), 0.0, 0.0)[2:]
        return (x, y, x + width / 1.32, y + height / 1.42)
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


def _label_svg(index: int, block: dict[str, Any], to_mm_x: Any, to_mm_y: Any) -> str:
    marker = block.get("marker")
    if not marker:
        box = _block_bbox(block)
        if box is None:
            return ""
        marker = (box[0], box[1])
    x = to_mm_x(float(marker[0]))
    y = to_mm_y(float(marker[1]))
    label = str(block.get("label_zh") or block.get("label") or block.get("id") or "")
    radius = 2.6
    text_x = x + radius + 1.2
    label_width = max(12.0, float(len(label)) * 2.5 + 2.0)
    return (
        f'<g class="scene-label" pointer-events="none">'
        f'<circle cx="{_fmt(x)}" cy="{_fmt(y)}" r="{_fmt(radius)}" fill="#ffffff" stroke="{COLORS["label"]}" stroke-width="0.6"/>'
        f'<text x="{_fmt(x)}" y="{_fmt(y)}" text-anchor="middle" dominant-baseline="central" '
        f'font-family="system-ui, sans-serif" font-size="3.1" fill="{COLORS["label"]}">{index}</text>'
        f'<text x="{_fmt(text_x)}" y="{_fmt(y)}" dominant-baseline="central" '
        f'font-family="system-ui, sans-serif" font-size="3.2" fill="{COLORS["label"]}">{escape(label)}</text>'
        f'</g>'
    )


def _scene_block_svg(
    index: int,
    block: dict[str, Any],
    *,
    show_names: bool,
    to_mm_x: Any,
    to_mm_y: Any,
) -> str:
    kind = str(block.get("kind") or "scene")
    color = COLORS["wire"] if kind in {"wire", "wires"} else COLORS["block"]
    parts: list[str] = [
        f'<g class="scene-block" data-block-id="{escape(str(block.get("id")))}" '
        f'data-kind="{escape(kind)}" data-label="{escape(str(block.get("label") or ""))}" '
        f'data-label-zh="{escape(str(block.get("label_zh") or ""))}">'
    ]
    for primitive in block.get("primitives") or []:
        fragment = _primitive_svg(primitive, to_mm_x, to_mm_y, color)
        if fragment:
            parts.append(fragment)
    box = _block_bbox(block)
    if box is not None:
        x0, y0, x1, y1 = box
        pad = 1.6
        parts.append(
            f'<rect class="block-hit" x="{_fmt(to_mm_x(max(0.0, x0 - pad)))}" '
            f'y="{_fmt(to_mm_y(max(0.0, y0 - pad)))}" '
            f'width="{_fmt(to_mm_x(min(100.0, x1 + pad)) - to_mm_x(max(0.0, x0 - pad)))}" '
            f'height="{_fmt(to_mm_y(min(100.0, y1 + pad)) - to_mm_y(max(0.0, y0 - pad)))}" '
            f'fill="transparent" stroke="none" data-bbox="{_fmt(x0)},{_fmt(y0)},{_fmt(x1)},{_fmt(y1)}"/>'
        )
        parts.append(
            f'<rect class="block-outline" x="{_fmt(to_mm_x(max(0.0, x0 - pad)))}" '
            f'y="{_fmt(to_mm_y(max(0.0, y0 - pad)))}" '
            f'width="{_fmt(to_mm_x(min(100.0, x1 + pad)) - to_mm_x(max(0.0, x0 - pad)))}" '
            f'height="{_fmt(to_mm_y(min(100.0, y1 + pad)) - to_mm_y(max(0.0, y0 - pad)))}" '
            f'fill="none" stroke="{COLORS["selected"]}" stroke-width="0.5" stroke-dasharray="2.5 2"/>'
        )
    if show_names and block.get("show_label", True):
        parts.append(_label_svg(index, block, to_mm_x, to_mm_y))
    parts.append("</g>")
    return "".join(parts)


def scene_to_svg(scene: dict[str, Any], *, show_names: bool = False) -> str:
    """Return an editor-ready SVG for ``scene``.

    ``show_names`` adds a numbered Chinese badge at each block marker.  The
    badges are pointer-transparent so a drag always lands on the block itself.
    """
    left, top, width, height = _content_box()

    def to_mm_x(value: float) -> float:
        return left + float(value) / 100.0 * width

    def to_mm_y(value: float) -> float:
        return top + float(value) / 100.0 * height

    parts: list[str] = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {_fmt(PLATE_WIDTH)} {_fmt(PLATE_HEIGHT)}" '
        f'class="scene-svg" role="img" aria-label="{escape(str(scene.get("title_zh") or scene.get("title") or "触觉示意图"))}" '
        f'data-plate-width="{_fmt(PLATE_WIDTH)}" data-plate-height="{_fmt(PLATE_HEIGHT)}" '
        f'data-content-box="{_fmt(left)},{_fmt(top)},{_fmt(width)},{_fmt(height)}">',
        f"<title>{escape(str(scene.get('title') or 'Tactile diagram'))}</title>",
        f"<desc>{escape(str(scene.get('title_zh') or ''))} - TouchLearn scene preview</desc>",
        f'<rect width="{_fmt(PLATE_WIDTH)}" height="{_fmt(PLATE_HEIGHT)}" fill="#ffffff"/>',
    ]

    plate = base_plate_polygon(PLATE_WIDTH, PLATE_HEIGHT)
    parts.append(
        f'<polygon class="scene-plate" points="{_points(plate)}" fill="#fbfaf6" '
        f'stroke="{COLORS["plate"]}" stroke-width="0.7"/>'
    )
    parts.append(
        f'<rect class="scene-content-box" x="{_fmt(left)}" y="{_fmt(top)}" width="{_fmt(width)}" '
        f'height="{_fmt(height)}" fill="none" stroke="{COLORS["content"]}" stroke-width="0.4" stroke-dasharray="3 3"/>'
    )

    title = str(scene.get("title") or "")
    if title:
        fitted, size = _fit_text(title, PLATE_WIDTH - 2 * SHEET_MARGIN - 4.0, 5.4, 4.2)
        title_x = (PLATE_WIDTH - font.text_width(fitted, size)) / 2.0
        parts.append(_text(fitted, title_x, 8.5, size, COLORS["title"]))
    subtitle = str(scene.get("subtitle") or "")
    if subtitle:
        fitted, size = _fit_text(subtitle, PLATE_WIDTH - 2 * SHEET_MARGIN - 8.0, 3.4, 2.8)
        subtitle_x = (PLATE_WIDTH - font.text_width(fitted, size)) / 2.0
        parts.append(_text(fitted, subtitle_x, 17.4, size, COLORS["subtitle"]))

    for index, block in enumerate(scene.get("blocks") or [], start=1):
        parts.append(
            _scene_block_svg(
                index,
                block,
                show_names=show_names,
                to_mm_x=to_mm_x,
                to_mm_y=to_mm_y,
            )
        )

    qr_left = PLATE_WIDTH - SHEET_MARGIN - QR_SIZE
    qr_top = PLATE_HEIGHT - SHEET_MARGIN - QR_SIZE
    parts.append(
        f'<rect class="scene-qr" x="{_fmt(qr_left + 0.5)}" y="{_fmt(qr_top + 0.5)}" '
        f'width="{_fmt(QR_SIZE - 1.0)}" height="{_fmt(QR_SIZE - 1.0)}" fill="none" '
        f'stroke="{COLORS["qr"]}" stroke-width="0.6" stroke-dasharray="3 3"/>'
    )
    parts.append(_text("TOUCHLEARN", SHEET_MARGIN, PLATE_HEIGHT - SHEET_MARGIN - 4.5, 4.0, COLORS["subtitle"]))
    footer_lines = list(scene.get("footer_lines") or [])
    if scene.get("scale_note") and scene["scale_note"] not in footer_lines:
        footer_lines.insert(0, scene["scale_note"])
    for text, baseline in zip(footer_lines[:2], (PLATE_HEIGHT - SHEET_MARGIN - 12.0, PLATE_HEIGHT - SHEET_MARGIN - 19.0)):
        fitted, size = _fit_text(str(text), max(qr_left - SHEET_MARGIN, 20.0), 3.2, 2.6)
        parts.append(_text(fitted, SHEET_MARGIN, baseline, size, COLORS["subtitle"]))

    parts.append("</svg>")
    return "\n".join(parts) + "\n"
