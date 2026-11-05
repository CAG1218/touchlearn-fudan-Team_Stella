"""Turn a reconstructed photo-real mesh into a printable tactile display.

The provider owns the likeness; this module owns the print.  It scales the
incoming triangle soup to the user's target height, puts it on a display base,
adds a stable plinth, a title plaque with Chinese braille, a numbered braille
legend on the front tab and a QR pad.  Nothing here guesses at the object's
shape: every added millimetre is a deterministic stand or label.

The reconstructed surface is provider geometry, so the compiler cannot know
where a named feature is.  Feature names therefore go on the base legend
instead of being attached to invented points on the mesh.  The textured GLB is
kept separately; an STL has no colour, so the print is a single-material
silhouette of the reconstructed object with tactile annotation.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Sequence

from .. import cnbraille
from ..geometry import Mesh
from ..model3d import (
    BRAILLE_DOT_HEIGHT_MM,
    LEGEND_ROW_MM,
    _base_outline,
    _braille_on_top,
    _emboss_text_on_top,
    _legend_panel,
)
from .volumetric import _box_xy, _cylinder, _prism_xy
from .reconstruct import ReconstructionPlan

BUILD_VOLUME_MM = (220.0, 220.0, 250.0)
BASE_THICKNESS_MM = 4.5
PLINTH_HEIGHT_MM = 2.5
MAX_FOOTPRINT_MM = 152.0
MIN_BASE_RADIUS_MM = 58.0
# 2 * 88 mm disc + a 40 mm legend tab = 216 mm, leaving 4 mm for the rim and
# slicer tolerance inside the 220 mm build volume.
MAX_BASE_RADIUS_MM = 88.0
TAB_EXTRA_MM = 40.0
MAX_LEGEND_LABELS = 6


@dataclass
class PrintableResult:
    mesh: Mesh
    meta: dict[str, Any] = field(default_factory=dict)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _transform(mesh: Mesh, scale: float, dx: float, dy: float, dz: float) -> Mesh:
    return Mesh(
        [
            (
                (a[0] * scale + dx, a[1] * scale + dy, a[2] * scale + dz),
                (b[0] * scale + dx, b[1] * scale + dy, b[2] * scale + dz),
                (c[0] * scale + dx, c[1] * scale + dy, c[2] * scale + dz),
            )
            for a, b, c in mesh.triangles
        ]
    )


def _fit_braille(text: str, max_width: float) -> str:
    value = "".join(ch for ch in (text or "") if "\u4e00" <= ch <= "\u9fff" or ch.isalnum()) or "模型"
    while len(value) > 1 and cnbraille.width(value, syllable_gap_mm=cnbraille.SYLLABLE_GAP_MM) > max_width:
        value = value[:-1]
    return value


def _rounded_rect(width: float, depth: float, radius: float, segments: int = 8) -> list[tuple[float, float]]:
    hw, hd = width / 2.0, depth / 2.0
    radius = min(radius, hw, hd)
    points: list[tuple[float, float]] = []
    corners = (
        (hw - radius, -hd + radius, -90.0),
        (hw - radius, hd - radius, 0.0),
        (-hw + radius, hd - radius, 90.0),
        (-hw + radius, -hd + radius, 180.0),
    )
    for cx, cy, start in corners:
        for index in range(segments + 1):
            angle = math.radians(start + 90.0 * index / segments)
            points.append((cx + radius * math.cos(angle), cy + radius * math.sin(angle)))
    return points


def _base(
    mesh: Mesh,
    shape: str,
    radius: float,
    tab_half: float,
    tab_front: float,
    top: float,
) -> dict[str, Any]:
    if shape == "rounded_rect":
        width = radius * 1.92
        depth = radius * 1.62
        _prism_xy(mesh, _rounded_rect(width, depth, max(12.0, radius * 0.22)), 0.0, top)
        _prism_xy(
            mesh,
            [(-tab_half, -depth / 2.0 + 2.0), (tab_half, -depth / 2.0 + 2.0), (tab_half, tab_front), (-tab_half, tab_front)],
            0.0,
            top,
        )
        return {
            "shape": "rounded_rect",
            "size_mm": [round(width, 2), round(depth + abs(tab_front) - depth / 2.0, 2), BASE_THICKNESS_MM],
            "radius_mm": None,
        }
    _prism_xy(mesh, _base_outline(radius, tab_front), 0.0, top)
    _cylinder(mesh, (0.0, 0.0, top - 0.1), (0.0, 0.0, top + 0.4), max(4.0, radius - 1.5), segments=96)
    return {
        "shape": "disc",
        "size_mm": [round(radius * 2.0, 2), round(radius - tab_front, 2), BASE_THICKNESS_MM],
        "radius_mm": round(radius, 2),
    }


def compose_printable(
    source: Mesh,
    plan: ReconstructionPlan,
    *,
    target_height_mm: float | None = None,
    title: str | None = None,
) -> PrintableResult:
    """Scale, stand and annotate a reconstructed mesh for a single-material print."""
    if not source.triangles:
        raise ValueError("重建网格为空，无法生成可打印模型。")

    height = float(target_height_mm or plan.height_mm or 120.0)
    height = _clamp(height, 60.0, 200.0)
    bounds = source.bounds()
    size = (bounds[3] - bounds[0], bounds[4] - bounds[1], bounds[5] - bounds[2])
    if max(size) <= 1e-9:
        raise ValueError("重建网格没有有效尺寸。")
    scale = height / size[2] if size[2] > 1e-6 else height / max(size)
    if max(size[0], size[1]) * scale > MAX_FOOTPRINT_MM:
        scale = MAX_FOOTPRINT_MM / max(size[0], size[1], 1e-6)
    dx = -((bounds[0] + bounds[3]) / 2.0) * scale
    dy = -((bounds[1] + bounds[4]) / 2.0) * scale
    model = _transform(source, scale, dx, dy, -bounds[2] * scale)
    if model.volume() < 0:
        model = Mesh([(a, c, b) for a, b, c in model.triangles])
    model_bounds = model.bounds()
    model_size = (model_bounds[3] - model_bounds[0], model_bounds[4] - model_bounds[1], model_bounds[5] - model_bounds[2])
    footprint_radius = 0.5 * max(model_size[0], model_size[1])

    top = BASE_THICKNESS_MM
    base_radius = _clamp(footprint_radius + float(plan.margin_mm or 10.0), MIN_BASE_RADIUS_MM, MAX_BASE_RADIUS_MM)

    title_text = _fit_braille(str(title or plan.title or "模型"), 60.0)
    labels = [dict(item) for item in (plan.labels or []) if str(item.get("text") or "").strip()]
    if not labels:
        labels = [{"text": title_text, "target": "主体"}]
    if not any(item["text"] == title_text for item in labels):
        labels.insert(0, {"text": title_text, "target": "主体"})

    # Feature names go into one numbered braille legend on the front tab.  The
    # reconstructed mesh has no reliable feature coordinates, so attaching a
    # name to an invented point on it would be worse than printing the name on
    # the base.  No standing flags or support posts are generated.
    legend_lines: list[str] = []
    legend_meta: list[dict[str, Any]] = []
    extra = [item for item in labels if str(item.get("text") or "") != title_text][:MAX_LEGEND_LABELS]
    for index, item in enumerate(extra, start=1):
        text = _fit_braille(str(item.get("text") or ""), 54.0)
        line = f"{index}{text}"
        legend_lines.append(line)
        legend_meta.append(
            {
                "text": text,
                "target": item.get("target") or "",
                "legend_index": index,
                "legend_line": line,
                "cells": [list(cell) for cell in cnbraille.encode_text(line)],
                "dots": 0,
                "placement": "base_legend",
            }
        )

    # Work out how wide the tab must be before building the base.  The legend
    # never shrinks the 1.6/0.8 mm braille dots; it gets more columns instead.
    tab_extra = 52.0 if plan.base == "rounded_rect" else TAB_EXTRA_MM
    tab_front = -(base_radius + tab_extra)
    join_y = (-base_radius * 0.81 + 2.0) if plan.base == "rounded_rect" else (-0.68 * base_radius)
    legend_front = tab_front + 26.0
    max_legend_depth = max(LEGEND_ROW_MM, join_y - legend_front - 2.0)
    max_legend_rows = max(1, int((max_legend_depth - 4.0) // LEGEND_ROW_MM))
    legend_max_width = 0.0
    for line in legend_lines:
        legend_max_width = max(legend_max_width, cnbraille.width(line, syllable_gap_mm=cnbraille.SYLLABLE_GAP_MM))
    needed_columns = max(1, math.ceil(len(legend_lines) / max_legend_rows)) if legend_lines else 0
    needed_half = (needed_columns * (legend_max_width + 5.0) + 4.0) / 2.0
    base_tab_half = max(38.0, min(base_radius - 10.0, 54.0))
    tab_half = max(base_tab_half, min(74.0, needed_half))

    mesh = Mesh([])
    base_meta = _base(mesh, plan.base, base_radius, tab_half, tab_front, top)

    # A short plinth hides an open bottom and gives an organic reconstruction a
    # stable footprint.  It overlaps the object by 0.1 mm, which slicers union.
    plinth_radius = _clamp(footprint_radius * 0.62, 11.0, base_radius * 0.62)
    plinth_top = top + PLINTH_HEIGHT_MM
    _cylinder(mesh, (0.0, 0.0, top - 0.1), (0.0, 0.0, plinth_top), plinth_radius, segments=48)

    model_offset = plinth_top - 0.1
    mesh.triangles.extend(
        [((a[0], a[1], a[2] + model_offset), (b[0], b[1], b[2] + model_offset), (c[0], c[1], c[2] + model_offset)) for a, b, c in model.triangles]
    )

    # Title plaque and QR pad share the front tab.  Widths shrink together so a
    # small base never produces an overhanging label.
    # The compliance check rejects a QR pad below 28 mm, so the small tab keeps
    # 28 mm and gives the title plaque the remaining width.  A 3-character
    # Chinese braille title needs about 53 mm, so the plaque leaves a 2 mm gap
    # and uses tighter side margins rather than truncating the name.
    qr_size = 30.0 if tab_half * 2.0 >= 104.0 else 28.0
    plaque_width = _clamp(tab_half * 2.0 - qr_size - 8.0, 32.0, 72.0)
    plaque_depth = 18.0
    plaque_left = -tab_half + 2.0
    plaque_front = tab_front + 4.0
    _box_xy(mesh, plaque_left, plaque_front, plaque_width, plaque_depth, top - 0.1, top + 2.2, chamfer=2.0)
    _emboss_text_on_top(mesh, "AI 3D", plaque_left + 4.0, plaque_front + 4.5, top + 2.0, top + 2.9, 4.0)
    plaque_text = _fit_braille(title_text, plaque_width - 6.0)
    _braille_on_top(mesh, plaque_text, plaque_left + 5.0, plaque_front + 10.5, top + 2.0, top + 2.75)

    qr_left = tab_half - 4.0 - qr_size
    qr_front = tab_front + 4.0
    _box_xy(mesh, qr_left, qr_front, qr_size, qr_size, top - 0.1, top + 1.8, chamfer=2.0)
    _emboss_text_on_top(mesh, "QR", qr_left + qr_size * 0.22, qr_front + qr_size * 0.40, top + 1.7, top + 2.4, 4.0)

    legend_front = plaque_front + plaque_depth + 4.0
    legend_depth, legend_columns = _legend_panel(
        mesh,
        legend_lines,
        left=-tab_half + 2.0,
        front=legend_front,
        width=tab_half * 2.0 - 4.0,
        max_depth=join_y - legend_front - 2.0,
        top=top,
    )

    bounds = mesh.bounds()
    size_mm = [bounds[3] - bounds[0], bounds[4] - bounds[1], bounds[5] - bounds[2]]
    meta = {
        "kind": "reconstruction",
        "strategy": "provider_reconstruction_plus_deterministic_print",
        "title": title_text,
        "base": base_meta,
        "base_tab_mm": round(abs(tab_front) - base_radius, 2),
        "plinth": {"radius_mm": round(plinth_radius, 2), "height_mm": PLINTH_HEIGHT_MM},
        "model_scale": round(scale, 6),
        "model_size_mm": [round(value, 2) for value in model_size],
        "model_footprint_mm": round(footprint_radius * 2.0, 2),
        "model_triangles": model.triangle_count(),
        "resolved_height_mm": round(model_size[2], 2),
        "size_mm": [round(value, 3) for value in size_mm],
        "build_volume_mm": list(BUILD_VOLUME_MM),
        "braille": {
            "standard": "GF 0019-2018",
            "title": plaque_text,
            "dot_diameter_mm": cnbraille.DOT_DIAMETER_MM,
            "dot_height_mm": BRAILLE_DOT_HEIGHT_MM,
            "labels": legend_meta,
            "label_strategy": "base_legend",
            "note": "中文盲文按 GF 0019-2018 编码；标题在正面名牌，特征名按编号刻在底座前缘的盲文图例上。重建网格表面无法确定特征坐标，所以不在模型表面刻猜出来的名字，也不生成竖立名牌。",
        },
        "legend": {
            "placement": "base_front_tab",
            "lines": legend_lines,
            "columns": legend_columns,
            "depth_mm": round(legend_depth, 2),
        },
        "standing_flags": 0,
        "qr_pad_mm": qr_size,
        "watertight": mesh.is_watertight(),
        "triangles": mesh.triangle_count(),
        "volume_mm3": round(mesh.volume(), 3),
        "scale_note": "AI 重建网格按目标高度缩放；GLB 保留纹理，STL 为单色打印。",
        "fits_build_volume": all(
            size_mm[index] <= BUILD_VOLUME_MM[index] for index in range(3)
        ),
    }
    return PrintableResult(mesh, meta)
