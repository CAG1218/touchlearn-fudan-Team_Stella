"""Parametric layout engines for the interactive agent.

The two v0 engines are hand-written for a single demo each.  These engines are
driven by the diagram options instead: any one-variable expression can become
a function plate, and any serial robot specification can become a relief
plate.  Nothing here traces pixels; every curve and link is reconstructed from
semantics so the tactile geometry stays measurable.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from .. import font, tactile_braille
from ..layout import (
    Feature,
    LayoutResult,
    Point,
    Primitive,
    _Builder,
    base_plate_polygon,
    rect_polygon,
)
from ..model import Diagram
from ..printability import feature_distance
from .expression import Expression, ExpressionError, expression_from_coefficients, parse_expression

BBox = tuple[float, float, float, float]

LABEL_TEXT_HEIGHT = 4.5
LABEL_BRAILLE_GAP = 1.2
MIN_CLEARANCE = 2.0
MIN_LABEL_SPACING = 4.5

_ALLOWED = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 -.,:/()[]^+=_?")


# ---------------------------------------------------------------------------
# Shared text and label helpers
# ---------------------------------------------------------------------------


def _sanitize(text: str) -> str:
    cleaned = []
    for character in text.upper():
        if character in _ALLOWED:
            cleaned.append(character)
        elif character in {"×", "X"} and character == "×":
            cleaned.append("X")
        else:
            cleaned.append(" ")
    return " ".join("".join(cleaned).split())


def _fit_text(text: str, max_width: float, preferred: float = 6.0, minimum: float = 3.5) -> tuple[str, float]:
    text = _sanitize(text) or "DIAGRAM"
    height = preferred
    width = font.text_width(text, height)
    if width > max_width:
        height = max(minimum, height * max_width / width)
    while len(text) > 1 and font.text_width(text, height) > max_width:
        text = text[:-1].rstrip()
    return text, round(height, 3)


def _label_feature(
    feature_id: str,
    text: str,
    x: float,
    y: float,
    *,
    semantic_id: str | None = None,
    index: int | None = None,
    text_height: float = LABEL_TEXT_HEIGHT,
    braille_text: str | None = None,
) -> Feature:
    feature = Feature(id=feature_id, kind="label", label=text, semantic_id=semantic_id, index=index)
    if text:
        feature.primitives.append(Primitive("text", ((x, y),), text=text, height=text_height))
    value = text if braille_text is None else braille_text
    if not value:
        return feature
    if tactile_braille.contains_chinese(value):
        # A Chinese word is several cells long; stacking it under the printed
        # word keeps the pair compact instead of pushing the braille far right.
        # With no printed text the braille becomes the whole label.
        offset = text_height + 0.8 if text else 0.0
        feature.primitives.append(
            Primitive("braille", ((x, y + offset),), text=value)
        )
        return feature
    # Print and braille sit side by side: this keeps the label 6.5 mm tall
    # instead of stacking two rows, which is what lets it fit beside a curve.
    # Short labels do not need a capital sign in braille; lower-case cells are
    # read as letters and stay compact.
    braille_x = x + (font.text_width(text, text_height) + 1.5 if text else 0.0)
    braille_y = y - 0.5 if text else y
    feature.primitives.append(Primitive("braille", ((braille_x, braille_y),), text=value.lower()))
    return feature


def _label_size(
    text: str,
    text_height: float = LABEL_TEXT_HEIGHT,
    braille_text: str | None = None,
) -> tuple[float, float]:
    probe = _label_feature("probe", text, 0.0, 0.0, text_height=text_height, braille_text=braille_text)
    _, _, width, height = probe.bbox()
    return width, height


def _rect_overlap(a: BBox, b: BBox, gap: float = 0.0) -> bool:
    return not (
        a[0] + a[2] + gap <= b[0]
        or b[0] + b[2] + gap <= a[0]
        or a[1] + a[3] + gap <= b[1]
        or b[1] + b[3] + gap <= a[1]
    )


def _inside(bbox: BBox, width: float, height: float, margin: float) -> bool:
    return (
        bbox[0] >= margin - 0.25
        and bbox[1] >= margin - 0.25
        and bbox[0] + bbox[2] <= width - margin + 0.25
        and bbox[1] + bbox[3] <= height - margin + 0.25
    )


def _candidate_positions(anchor: Point, label_width: float, label_height: float) -> list[Point]:
    ax, ay = anchor
    positions: list[Point] = []
    # Larger gaps matter once a label stacks a standard coordinate and a
    # Chinese braille name: that box can be 40 mm wide, and the only clear
    # space may be on the far side of an axis.
    for gap in (3.0, 6.5, 10.5, 15.0, 20.0, 25.0, 32.0, 40.0):
        positions.extend(
            [
                (ax + gap, ay - label_height / 2.0),
                (ax - gap - label_width, ay - label_height / 2.0),
                (ax - label_width / 2.0, ay - gap - label_height),
                (ax - label_width / 2.0, ay + gap),
                (ax + gap, ay - gap - label_height),
                (ax - gap - label_width, ay - gap - label_height),
                (ax + gap, ay + gap),
                (ax - gap - label_width, ay + gap),
                # Beside the anchor, lifted clear of the coordinate it names.
                # A wide label can then use the empty quarter above an axis
                # instead of jumping diagonally across the plot.
                (ax + gap, ay - label_height - 3.0),
                (ax - gap - label_width, ay - label_height - 3.0),
                (ax + gap, ay + 3.0),
                (ax - gap - label_width, ay + 3.0),
            ]
        )
    return positions


def _placement_score(candidate: Feature, anchor: Point, tightest: float, preferred: Point | None) -> float:
    left, top, width, height = candidate.bbox()
    dx = left + width / 2.0 - anchor[0]
    dy = top + height / 2.0 - anchor[1]
    distance = math.hypot(dx, dy) or 1e-9
    alignment = 0.0
    if preferred is not None and math.hypot(*preferred) > 1e-9:
        length = math.hypot(*preferred)
        alignment = (dx * preferred[0] + dy * preferred[1]) / (distance * length)
    clarity = 5.0 if tightest == float("inf") else min(tightest, 5.0)
    return -distance + alignment * 2.0 + clarity * 0.15


def _place_label(
    builder: _Builder,
    feature_id: str,
    text: str,
    anchor: Point,
    obstacles: Sequence[Feature],
    reserved: Sequence[BBox],
    margin: float,
    *,
    semantic_id: str | None = None,
    index: int | None = None,
    text_height: float = LABEL_TEXT_HEIGHT,
    preferred: Point | None = None,
    braille_text: str | None = None,
    require_clear: bool = False,
) -> Feature | None:
    """Place a text+braille label near ``anchor`` without touching other ink.

    ``require_clear`` returns ``None`` instead of placing a crowded fallback,
    so a caller can try several anchors along a feature and keep the first one
    with real clearance.
    """
    width, height = _label_size(text, text_height, braille_text)
    positions = _candidate_positions(anchor, width, height)

    labels = [item for item in builder.features if item.kind == "label"]
    accepted: list[tuple[Feature, float]] = []
    fallback: list[tuple[Feature, float]] = []

    for x, y in positions:
        candidate = _label_feature(
            feature_id,
            text,
            x,
            y,
            semantic_id=semantic_id,
            index=index,
            text_height=text_height,
            braille_text=braille_text,
        )
        bbox = candidate.bbox()
        if not _inside(bbox, builder.diagram.canvas.width_mm, builder.diagram.canvas.height_mm, margin):
            continue
        if any(_rect_overlap(bbox, rect, 0.5) for rect in reserved):
            continue

        tightest = float("inf")
        clear = True
        for other in obstacles:
            if semantic_id and other.semantic_id == semantic_id:
                continue
            # The grid is redrawn after placement with a 2 mm gap around every
            # label, so its current segments must not veto a candidate.
            if other.kind == "grid":
                continue
            distance = feature_distance(candidate, other, MIN_CLEARANCE)
            if distance < MIN_CLEARANCE - 1e-6:
                clear = False
                break
            tightest = min(tightest, distance)

        if clear:
            for other in labels:
                distance = feature_distance(candidate, other, MIN_LABEL_SPACING)
                if distance < MIN_LABEL_SPACING - 1e-6:
                    clear = False
                    break
                tightest = min(tightest, distance)
        if clear:
            accepted.append((candidate, _placement_score(candidate, anchor, tightest, preferred)))
            continue

        # Keep a ranked fallback so a crowded plate still gets a label, but the
        # warning and the compliance report stay honest about the clearance.
        score = float("inf")
        for other in [*obstacles, *labels]:
            if semantic_id and other.semantic_id == semantic_id:
                continue
            if other.kind == "grid":
                continue
            score = min(score, feature_distance(candidate, other, 30.0))
        fallback.append((candidate, _placement_score(candidate, anchor, score, preferred)))

    if accepted:
        accepted.sort(key=lambda item: item[1], reverse=True)
        chosen = accepted[0][0]
        builder.features.append(chosen)
        return chosen
    if require_clear:
        return None
    if fallback:
        fallback.sort(key=lambda item: item[1], reverse=True)
        chosen = fallback[0][0]
        builder.features.append(chosen)
        builder.warnings.append(
            f"label '{feature_id}' could not keep {MIN_CLEARANCE:.1f} mm clearance / "
            f"{MIN_LABEL_SPACING:.1f} mm label spacing; verify the report before printing."
        )
        return chosen
    builder.warnings.append(f"label '{feature_id}' has no valid position inside the margins and was omitted.")
    return None


def _indices(expression: Expression, x_min: float, x_max: float, count: int = 900) -> list[list[tuple[float, float]]]:
    return expression.sample(x_min, x_max, count)


def _clip_segment(point_a: tuple[float, float], point_b: tuple[float, float], low: float, high: float) -> list[tuple[float, float]]:
    x0, y0 = point_a
    x1, y1 = point_b
    # Keep the original vertices when the whole segment is inside.  Computing
    # t = 0/1 and rebuilding the points perturbs the last bits of a float; the
    # caller then sees a different point and starts a spurious new run, which
    # used to leave a 0.1 mm sliver at the vertex of a parabola.
    if low <= y0 <= high and low <= y1 <= high:
        return [point_a, point_b]
    if y1 == y0:
        return [] if y0 < low or y0 > high else [point_a, point_b]
    t_low = (low - y0) / (y1 - y0)
    t_high = (high - y0) / (y1 - y0)
    t_enter = max(0.0, min(t_low, t_high))
    t_exit = min(1.0, max(t_low, t_high))
    if t_enter > t_exit + 1e-12:
        return []
    if t_enter >= 1.0 - 1e-12 or t_exit <= 1e-12:
        return []
    enter = (x0 + (x1 - x0) * t_enter, y0 + (y1 - y0) * t_enter)
    exit_point = (x0 + (x1 - x0) * t_exit, y0 + (y1 - y0) * t_exit)
    return [enter, exit_point]


def _clip_runs(
    segments: Sequence[Sequence[tuple[float, float]]],
    low: float,
    high: float,
) -> list[list[tuple[float, float]]]:
    runs: list[list[tuple[float, float]]] = []
    for segment in segments:
        current: list[tuple[float, float]] = []
        for index in range(len(segment) - 1):
            clipped = _clip_segment(segment[index], segment[index + 1], low, high)
            if not clipped:
                if len(current) >= 2:
                    runs.append(current)
                current = []
                continue
            if not current or math.dist(current[-1], clipped[0]) > 1e-9:
                if len(current) >= 2:
                    runs.append(current)
                current = [clipped[0]]
            current.append(clipped[1])
        if len(current) >= 2:
            runs.append(current)
    return runs


def _nice_step(span: float, target: int = 6) -> float:
    if span <= 0:
        return 1.0
    raw = span / max(target, 1)
    magnitude = 10.0 ** math.floor(math.log10(raw))
    for multiplier in (1.0, 2.0, 5.0, 10.0):
        if raw <= multiplier * magnitude:
            return multiplier * magnitude
    return 10.0 * magnitude


def _simplify_canvas_points(points: Sequence[Point], minimum: float = 0.8) -> list[Point]:
    """Drop samples closer than the tactile resolution.

    Sampling a smooth curve at 900 points creates sub-0.1 mm triangles when the
    span is narrow.  Those are below both the nozzle and the fingertip, so they
    only inflate the STL and trip numerical-degeneracy checks.
    """
    if len(points) <= 2:
        return list(points)
    result = [points[0]]
    for point in points[1:-1]:
        if math.dist(point, result[-1]) >= minimum:
            result.append(point)
    if len(result) == 1:
        result.append(points[-1])
    elif math.dist(points[-1], result[-1]) > 1e-9:
        # Snap the run's last sample instead of adding a short tail; a retained
        # penultimate point a fraction of a millimetre away would otherwise
        # create a sliver triangle that fails the mesh sanity check.
        if math.dist(points[-1], result[-1]) < minimum and len(result) >= 2:
            result[-1] = points[-1]
        else:
            result.append(points[-1])
    return result


def _redraw_grid(
    builder: _Builder,
    grid: Feature,
    x_lines: Sequence[float],
    y_lines: Sequence[float],
    data_left: float,
    data_right: float,
    data_top: float,
    data_bottom: float,
) -> None:
    """Rebuild the grid with a 2 mm gap around every placed label."""
    labels = [item for item in builder.features if item.kind == "label"]
    grid.primitives.clear()

    def segments(start: float, end: float, gaps: Sequence[tuple[float, float]]) -> list[tuple[float, float]]:
        result: list[tuple[float, float]] = []
        cursor = start
        for low, high in sorted(gaps):
            if high <= cursor:
                continue
            if low > cursor:
                result.append((cursor, min(low, end)))
            cursor = max(cursor, high)
            if cursor >= end:
                break
        if cursor < end:
            result.append((cursor, end))
        return [(low, high) for low, high in result if high - low >= 1.0]

    for x in x_lines:
        gaps = [
            (bbox[1] - 2.0, bbox[1] + bbox[3] + 2.0)
            for bbox in (label.bbox() for label in labels)
            if bbox[0] - 2.0 <= x <= bbox[0] + bbox[2] + 2.0
        ]
        for low, high in segments(data_top, data_bottom, gaps):
            builder.line(grid, ((x, low), (x, high)), 1.0)
    for y in y_lines:
        gaps = [
            (bbox[0] - 2.0, bbox[0] + bbox[2] + 2.0)
            for bbox in (label.bbox() for label in labels)
            if bbox[1] - 2.0 <= y <= bbox[1] + bbox[3] + 2.0
        ]
        for low, high in segments(data_left, data_right, gaps):
            builder.line(grid, ((low, y), (high, y)), 1.0)


def _frange(start: float, stop: float, step: float, limit: int = 40) -> list[float]:
    if step <= 0:
        return []
    first = math.ceil(start / step - 1e-9) * step
    values = []
    value = first
    while value <= stop + 1e-9 and len(values) < limit:
        values.append(round(value, 9))
        value += step
    return values


def _qr_zone(builder: _Builder, width: float, height: float, margin: float) -> tuple[float, float, float, float]:
    size = 30.0
    left = width - margin - size
    top = height - margin - size
    feature = builder.feature("qr_zone", "qr_zone")
    inset = 0.5
    frame = (left + inset, top + inset, size - 2 * inset, size - 2 * inset)
    corners = [
        (frame[0], frame[1]),
        (frame[0] + frame[2], frame[1]),
        (frame[0] + frame[2], frame[1] + frame[3]),
        (frame[0], frame[1] + frame[3]),
    ]
    for index in range(4):
        builder.dashed_line(feature, corners[index], corners[(index + 1) % 4], 1.0, 3.0, 3.0)
    return (left, top, size, size)


def _chinese_label(text: str, *, max_chars: int = 8) -> str:
    """Keep only what a tactile plate can carry: Chinese, digits, math signs.

    Raised Latin letter shapes cannot be read by touch, so English words are
    stripped here rather than printed.  Arabic digits and the few arithmetic
    symbols still belong to a maths plate and are kept.
    """
    allowed_symbols = " +-./=():，。"
    value = "".join(
        character
        for character in str(text or "")
        if "\u4e00" <= character <= "\u9fff"
        or character.isdigit()
        or character in allowed_symbols
    ).strip()
    return value[:max_chars]


def _han_label(text: str, *, max_chars: int = 10) -> str:
    """Keep only Chinese characters, for a heading that has no maths in it.

    A title such as "二次函数 y=x²-2x-3 图像" cannot be one braille line: the
    ASCII part is not raised for touch and "²" has no checked cell.  The
    equation is spelled on its own line below the heading, so the heading
    keeps just the Chinese words.
    """
    value = "".join(
        character for character in str(text or "") if "\u4e00" <= character <= "\u9fff"
    )
    return value[:max_chars]


def _footer(
    builder: _Builder,
    width: float,
    height: float,
    margin: float,
    qr_rect: BBox,
    *,
    braille_lines: Sequence[str] = (),
    text_lines: Sequence[str] = (),
) -> None:
    """Draw the Chinese braille brand, optional standard text and legend lines.

    Chinese words travel in braille.  Mathematical notation such as an axis
    range keeps the standard raised glyphs a sighted teacher would write, so
    it is printed with the outline font instead of being turned into dots.
    Braille keeps its standard pitch; a line that cannot fit is skipped
    instead of being squeezed below the readable pitch.
    """
    feature = builder.feature("footer", "footer")
    # Text bboxes include half a stroke width on each side.  Starting exactly on
    # the margin line pushed the footer 0.5 mm into the 6 mm edge reserve, which
    # the printability check reports as an error.
    x = margin + 0.5
    available = max(qr_rect[0] - x - 1.0, 20.0)
    brand = "触摸学习"
    if tactile_braille.safe_text(brand) and tactile_braille.width(brand) <= available:
        builder.braille(feature, x, height - margin - 7.0, brand)

    # Lines stack upward from just above the brand; y is the top of a row.
    baseline = height - margin - 15.5
    for text in text_lines[:2]:
        value = "".join(character for character in str(text).upper() if character in _ALLOWED).strip()
        if not value:
            continue
        text_height = 4.0
        if font.text_width(value, text_height) > available:
            text_height = max(3.5, text_height * available / font.text_width(value, text_height))
        if font.text_width(value, text_height) > available:
            continue
        builder.text(feature, x, baseline, value, text_height)
        baseline -= text_height + 1.8

    step = tactile_braille.height("中") + 1.8
    valid: list[str] = []
    for text in braille_lines[:3]:
        value = _chinese_label(text, max_chars=28)
        if not value:
            continue
        if tactile_braille.width(value) > available:
            continue
        valid.append(value)
    # A sheet is read top-down: the first line sits at the smallest y, even
    # though the block grows upward from the same baseline as before.
    for index, value in enumerate(valid):
        builder.braille(feature, x, baseline - (len(valid) - 1 - index) * step, value)


def _cn_count(value: int) -> str:
    """Spell small counts so braille never has to carry an Arabic digit."""
    names = {0: "零", 1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六", 7: "七", 8: "八", 9: "九", 10: "十"}
    return names.get(int(value), str(value))


def _format_number(value: float) -> str:
    """Compact axis-range text: integers stay integers, others use 2 decimals."""
    if abs(value) < 1e-9:
        return "0"
    if abs(value - round(value)) < 1e-6:
        return str(int(round(value)))
    return f"{value:.2f}".rstrip("0").rstrip(".")


class MathLabelError(ValueError):
    """The expression needs a symbol whose GB/T 18028 cell is not checked."""


# Linearising the AST into one line can drop a bracket only when the child is
# strong enough to bind on its own.  Levels follow school maths: + - then * /,
# a unary sign binds tighter, and a power expands to a product, so it prints at
# the product level.
_MATH_LEVEL = {"+": 1, "-": 1, "*": 2, "/": 2}
_MATH_MAX_POWER = 4


def _math_level(node: tuple) -> int:
    kind = node[0]
    if kind in {"num", "var"}:
        return 4
    if kind == "unary":
        return 3
    if kind == "call":
        raise MathLabelError("函数名暂未收录函数号")
    if kind == "bin":
        operator = node[1]
        if operator == "^":
            return 2
        if operator in _MATH_LEVEL:
            return _MATH_LEVEL[operator]
    raise MathLabelError(f"未收录的表达式结构：{kind}")


def _math_label(node: tuple, minimum: int = 0) -> str:
    """Print one AST as GB/T 18028 maths text for the braille encoder.

    ``x^2`` becomes ``x·x`` because the 乘方号 cell is not verified here; the
    scalar product uses the 点乘号 that print itself puts between letters.
    Anything that would need brackets, a 乘方号 or a 函数号 raises
    :class:`MathLabelError`, so the plate drops the line instead of printing a
    guessed dot pattern.
    """
    if _math_level(node) < minimum:
        raise MathLabelError("需要括号")
    kind = node[0]
    if kind == "num":
        value = float(node[1])
        if not math.isfinite(value) or value < 0:
            raise MathLabelError("数字超出可打印范围")
        return _format_number(value)
    if kind == "var":
        return "x"
    if kind == "unary":
        sign = node[1]
        if sign not in {"+", "-"}:
            raise MathLabelError(f"未收录一元符号：{sign}")
        body = _math_label(node[2], 2)
        if sign == "+":
            return body
        if body.startswith("-"):
            raise MathLabelError("双重负号需要括号")
        return "-" + body

    operator, left_node, right_node = node[1], node[2], node[3]
    if operator == "^":
        if right_node[0] != "num":
            raise MathLabelError("乘方指数暂只支持整数")
        exponent = float(right_node[1])
        if exponent != int(exponent) or not 1 <= exponent <= _MATH_MAX_POWER:
            raise MathLabelError(f"乘方指数 {exponent:g} 暂无规范点形（只支持 1-4 次）")
        base = _math_label(left_node, 4)
        return "·".join([base] * int(exponent))
    if operator == "*":
        # A factor of one is invisible on paper, so it is invisible here too.
        if left_node[0] == "num" and float(left_node[1]) == 1.0:
            return _math_label(right_node, minimum)
        left = _math_label(left_node, 2)
        if left_node[0] == "num" and right_node[0] == "var":
            return left + "x"
        right = _math_label(right_node, 2)
        if right[0] in "+-":
            raise MathLabelError("点乘号后不能直接跟正负号")
        return left + "·" + right
    if operator == "/":
        return _math_label(left_node, 2) + "÷" + _math_label(right_node, 4)

    left = _math_label(left_node, 1)
    if operator == "+" and right_node[0] == "unary" and right_node[1] == "-":
        # "+ -2x" is just "-2x"; two signs would read as two operations.
        return left + _math_label(right_node, 2)
    right = _math_label(right_node, 2)
    if operator == "+" and right[0] == "-":
        return left + right
    if operator == "-" and right[0] == "-":
        raise MathLabelError("减去负数需要括号")
    return left + operator + right


def _axis_arrow(builder: _Builder, feature: Feature, tip: Point, direction: Point, length: float = 5.0, half: float = 2.0) -> None:
    dx, dy = direction
    magnitude = math.hypot(dx, dy)
    if magnitude <= 1e-9:
        return
    ux, uy = dx / magnitude, dy / magnitude
    px, py = -uy, ux
    base_x, base_y = tip[0] - ux * length, tip[1] - uy * length
    builder.polygon(
        feature,
        (
            tip,
            (base_x + px * half, base_y + py * half),
            (base_x - px * half, base_y - py * half),
        ),
    )


# ---------------------------------------------------------------------------
# Function plate
# ---------------------------------------------------------------------------


def _expression_from_diagram(diagram: Diagram) -> Expression:
    options = diagram.layout.get("options", {})
    source = diagram.source
    candidate = options.get("expression") or source.get("expression") or source.get("ref") or ""
    coefficients = options.get("coefficients") or source.get("coefficients")
    if isinstance(coefficients, dict) and {"a", "b", "c"} <= set(coefficients):
        return expression_from_coefficients(float(coefficients["a"]), float(coefficients["b"]), float(coefficients["c"]))
    text = str(candidate).strip()
    if text.lower().startswith("y="):
        text = text[2:]
    if not text:
        raise ExpressionError("函数板缺少解析式：请提供 expression 或 coefficients")
    return parse_expression(text)


def _point_element_id(diagram: Diagram, x: float, y: float, fallback: str) -> str:
    tolerance = max(abs(x), abs(y), 1.0) * 1e-3
    for element in diagram.elements:
        if element.kind != "point":
            continue
        ex = element.semantics.get("x")
        ey = element.semantics.get("y")
        if isinstance(ex, (int, float)) and isinstance(ey, (int, float)):
            if abs(float(ex) - x) <= tolerance and abs(float(ey) - y) <= tolerance:
                return element.id
    return fallback


def build_function_layout(diagram: Diagram) -> LayoutResult:
    builder = _Builder(diagram)
    width = diagram.canvas.width_mm
    height = diagram.canvas.height_mm
    margin = max(float(diagram.canvas.margin_mm), 6.0)
    options = diagram.layout.get("options", {})
    expression = _expression_from_diagram(diagram)

    if options.get("x_min") is not None and options.get("x_max") is not None:
        x_min = float(options["x_min"])
        x_max = float(options["x_max"])
    else:
        x_min, x_max, _, _ = expression.suggest_window()
    if x_max <= x_min:
        raise ExpressionError("x 的上界必须大于下界")
    if options.get("y_min") is not None and options.get("y_max") is not None:
        y_min = float(options["y_min"])
        y_max = float(options["y_max"])
    else:
        y_min, y_max = expression.auto_y_range(x_min, x_max)
    if y_max <= y_min:
        raise ExpressionError("y 的上界必须大于下界")

    builder.trace.append(
        {
            "stage": "layout",
            "action": "choose_engine",
            "decision": "function_plot / parametric cartesian plot",
            "reason": "The curve is reconstructed from the parsed expression, not traced from the uploaded image.",
        }
    )
    builder.trace.append(
        {
            "stage": "layout",
            "action": "choose_window",
            "decision": f"x in [{x_min:g}, {x_max:g}], y in [{y_min:g}, {y_max:g}]",
            "reason": "The window keeps the key points inside the plate; the audio states the same values.",
        }
    )

    # The plate carries no raised Latin title.  A blind reader cannot feel
    # letter shapes, so the title is Chinese braille only; the English title
    # stays in the web UI and the audio track.
    raw_title = getattr(diagram, "title_zh", "") or "函数图像"
    title_zh = _han_label(raw_title) or _chinese_label(raw_title, max_chars=10)
    title = builder.feature("title", "title", label=title_zh)
    if title_zh and tactile_braille.safe_text(title_zh):
        title_braille_width = tactile_braille.width(title_zh)
        if title_braille_width <= width - 2 * margin:
            builder.braille(title, (width - title_braille_width) / 2.0, margin + 2.0, title_zh)
    elif title_zh:
        builder.warnings.append(f"标题“{title_zh}”含无法编码的字符，盲文标题已省略。")

    # The expression is mathematical notation, not prose: keep the standard
    # raised glyphs so it can be compared with a printed textbook.  Chinese
    # explanations travel in braille elsewhere on the plate.
    equation = builder.feature("equation", "equation", label=expression.canonical)
    raw = f"y={expression.canonical.lower().replace('**', '^')}"
    equation_label = "".join(character for character in raw.upper() if character in _ALLOWED)
    if equation_label:
        equation_height = 5.0
        available = width - 2 * margin
        if font.text_width(equation_label, equation_height) > available:
            equation_height = max(3.5, equation_height * available / font.text_width(equation_label, equation_height))
        if font.text_width(equation_label, equation_height) <= available:
            builder.text(
                equation,
                (width - font.text_width(equation_label, equation_height)) / 2.0,
                margin + 10.0,
                equation_label,
                equation_height,
            )

    # Below the printed form the same equation is repeated in GB/T 18028
    # maths code: 数号 before every digit, 字母号 before each variable, and the
    # 点乘号 between the factors of x².  A sign without a checked cell stops the
    # line rather than guessing a pattern, and the warning says which one.
    equation_braille = None
    try:
        math_text = "y=" + _math_label(expression.node)
    except MathLabelError as error:
        math_text = ""
        builder.warnings.append(f"解析式盲文已省略：{error}")
        builder.trace.append(
            {
                "stage": "layout",
                "action": "skip_equation_braille",
                "decision": str(error),
                "reason": "GB/T 18028 has no verified cell for this construct, so the braille line is left out instead of printed with a guess.",
            }
        )
    if math_text:
        math_width = tactile_braille.width(math_text)
        if math_width <= width - 2.0 * margin:
            equation_braille = builder.feature("equation_braille", "equation", label=expression.canonical)
            builder.braille(equation_braille, (width - math_width) / 2.0, margin + 16.0, math_text)
            builder.trace.append(
                {
                    "stage": "layout",
                    "action": "encode_equation",
                    "decision": f"GB/T 18028 maths: {math_text} ({math_width:.1f} mm)",
                    "reason": "Digits take 数号, letters take 字母号 and the squared term is spelled x·x because the 乘方号 cell is unverified.",
                }
            )
        else:
            builder.warnings.append(f"解析式盲文 {math_width:.1f} mm 超出版面宽度，已省略。")

    # Header stack measured from the top margin: braille title 2.0-8.8,
    # printed equation 10-15, braille equation 16-22.8.  plot_top leaves 3.2 mm
    # of clear paper under the last braille line so the y-axis label, placed a
    # few mm below it, keeps its 2 mm annotation clearance.
    plot_left = margin + 11.0
    plot_right = width - margin - 3.0
    plot_top = margin + 26.0
    plot_bottom = height - margin - 42.0
    plot_width = plot_right - plot_left
    plot_height = plot_bottom - plot_top
    if plot_width <= 20 or plot_height <= 20:
        raise ExpressionError("画布太小，放不下函数图与二维码区")

    x_span = x_max - x_min
    y_span = y_max - y_min
    scale = min(plot_width / x_span, plot_height / y_span)
    scale_cap = options.get("mm_per_unit")
    if scale_cap is not None:
        scale = min(scale, float(scale_cap))
    scale = max(scale, 0.05)

    draw_width = x_span * scale
    draw_height = y_span * scale
    data_left = plot_left + (plot_width - draw_width) / 2.0
    data_top = plot_top + (plot_height - draw_height) / 2.0
    origin_x = data_left - x_min * scale
    origin_y = data_top + y_max * scale

    def to_canvas(x_value: float, y_value: float) -> Point:
        return (origin_x + x_value * scale, origin_y - y_value * scale)

    data_right = data_left + draw_width
    data_bottom = data_top + draw_height
    builder.trace.append(
        {
            "stage": "layout",
            "action": "equal_aspect_scale",
            "decision": f"{scale:.2f} mm per unit",
            "reason": "x and y share one scale so slopes and symmetry are not distorted.",
        }
    )

    grid_step = options.get("grid_step")
    if grid_step is None:
        grid_step = _nice_step(max(x_span, y_span))
    grid_step = float(grid_step)
    if grid_step <= 0:
        grid_step = _nice_step(max(x_span, y_span))

    key_points = expression.key_points(x_min, x_max, y_min, y_max, max_points=6)
    key_x_canvas = [origin_x + point.x * scale for point in key_points]
    key_y_canvas = [origin_y - point.y * scale for point in key_points]

    grid = builder.feature("grid", "grid")
    x_ticks = [value for value in _frange(x_min, x_max, grid_step) if abs(value) > 1e-9]
    y_ticks = [value for value in _frange(y_min, y_max, grid_step) if abs(value) > 1e-9]
    grid_x_lines: list[float] = []
    grid_y_lines: list[float] = []
    drawn_x = 0
    for value in x_ticks:
        x = origin_x + value * scale
        # Leave a breathing gap around key points; a grid line 1 mm from a
        # braille label is tactile noise, not help.
        if any(abs(x - key_x) < 12.0 for key_x in key_x_canvas):
            continue
        if drawn_x >= 5:
            break
        grid_x_lines.append(x)
        builder.line(grid, ((x, data_top), (x, data_bottom)), 1.0)
        drawn_x += 1
    drawn_y = 0
    for value in y_ticks:
        y = origin_y - value * scale
        if any(abs(y - key_y) < 10.0 for key_y in key_y_canvas):
            continue
        if drawn_y >= 5:
            break
        grid_y_lines.append(y)
        builder.line(grid, ((data_left, y), (data_right, y)), 1.0)
        drawn_y += 1
    x_ticks = [value for value in x_ticks if not any(abs(origin_x + value * scale - key_x) < 12.0 for key_x in key_x_canvas)][:5]
    y_ticks = [value for value in y_ticks if not any(abs(origin_y - value * scale - key_y) < 10.0 for key_y in key_y_canvas)][:5]
    builder.trace.append(
        {
            "stage": "layout",
            "action": "reduce_grid",
            "decision": f"major grid every {grid_step:g} units",
            "reason": "A dense grid turns into noise under a fingertip; only major divisions are raised.",
        }
    )

    axis_x_value = min(max(0.0, x_min), x_max)
    axis_y_value = min(max(0.0, y_min), y_max)
    axis_y_canvas = origin_y - axis_y_value * scale
    axis_x_canvas = origin_x + axis_x_value * scale
    axes = builder.feature("axes", "axis")
    builder.line(axes, ((data_left, axis_y_canvas), (data_right, axis_y_canvas)), 2.2)
    builder.line(axes, ((axis_x_canvas, data_bottom), (axis_x_canvas, data_top)), 2.2)
    _axis_arrow(builder, axes, (data_right, axis_y_canvas), (1.0, 0.0))
    _axis_arrow(builder, axes, (axis_x_canvas, data_top), (0.0, -1.0))
    for value in x_ticks:
        x = origin_x + value * scale
        builder.line(axes, ((x, axis_y_canvas - 3.0), (x, axis_y_canvas + 3.0)), 1.4)
    for value in y_ticks:
        y = origin_y - value * scale
        builder.line(axes, ((axis_x_canvas - 3.0, y), (axis_x_canvas + 3.0, y)), 1.4)
    if x_min <= 0.0 <= x_max and y_min <= 0.0 <= y_max:
        builder.circle(axes, origin_x, origin_y, 1.8)

    # The braille title and equation occupy physical space above the plot even
    # though they are not obstacles for the curve; without them a crowded plate
    # can drop an axis label into the title band.  A label that could not be
    # encoded has no primitive and no bbox, so it is left out of the geometry.
    obstacles: list[Feature] = [
        item for item in (title, equation, equation_braille, axes) if item is not None and item.primitives
    ]
    reserved: list[BBox] = [(*_qr_zone(builder, width, height, margin),)]

    # Mark discontinuities with a dashed vertical line before clipping the curve.
    raw_segments = _indices(expression, x_min, x_max, 900)
    sample_step = x_span / 900.0
    asymptotes: list[float] = []
    for first, second in zip(raw_segments, raw_segments[1:]):
        if not first or not second:
            continue
        gap = second[0][0] - first[-1][0]
        if 0.0 < gap <= sample_step * 4.0 and abs(second[0][1] - first[-1][1]) > max(y_span * 0.8, 10.0):
            asymptotes.append((first[-1][0] + second[0][0]) / 2.0)
    if asymptotes:
        asymptote_feature = builder.feature("asymptote", "asymptote")
        for x_value in asymptotes:
            x = origin_x + x_value * scale
            builder.dashed_line(asymptote_feature, (x, data_top), (x, data_bottom), 1.0, 4.0, 3.0)
        obstacles.append(asymptote_feature)
        builder.trace.append(
            {
                "stage": "layout",
                "action": "mark_discontinuity",
                "decision": f"{len(asymptotes)} dashed vertical line(s)",
                "reason": "The sampler split the curve at an undefined point or a large jump; the dashed line tells the reader not to trace across it.",
            }
        )

    curve = builder.feature("curve", "curve", "CURVE", semantic_id="curve")
    runs = _clip_runs(raw_segments, y_min, y_max)
    for run in runs:
        points = _simplify_canvas_points([to_canvas(x_value, y_value) for x_value, y_value in run])
        if len(points) >= 2:
            builder.line(curve, points, 2.5)
    if not runs:
        builder.warnings.append("表达式在当前窗口内没有可见曲线，请扩大定义域或值域。")
    obstacles.append(curve)

    for order, point in enumerate(key_points, start=1):
        x_canvas, y_canvas = to_canvas(point.x, point.y)
        feature_id = _point_element_id(diagram, point.x, point.y, f"kp_{order}")
        # Keep each tactile name to two syllables.  The braille line sits
        # under the printed coordinate, and a four-character name (80 mm)
        # cannot fit beside the curve without breaking the ink clearance.
        point_name = {
            "root": "零点",
            "minimum": "最低",
            "maximum": "最高",
            "y_intercept": "纵轴",
        }.get(point.kind, "关键")
        point_feature = builder.feature(feature_id, "point", point_name, semantic_id=feature_id, index=order)
        builder.circle(point_feature, x_canvas, y_canvas, 3.0)
        obstacles.append(point_feature)
        if point.kind == "root":
            derivative = expression._derivative(point.x, max(x_span / 2000.0, 1e-6))
            if derivative is not None and derivative > 0:
                preferred = (-0.6, -1.0)
            else:
                preferred = (0.6, -1.0)
        elif point.kind == "minimum":
            preferred = (0.2, 1.0)
        elif point.kind == "maximum":
            preferred = (0.2, -1.0)
        elif point.kind == "y_intercept":
            preferred = (1.0, -0.4)
        else:
            preferred = (1.0, -0.6)
        # Coordinates are mathematics, so they stay in standard raised glyphs.
        # The point name is Chinese prose and therefore travels in braille
        # underneath the coordinate.
        coordinate = f"({_format_number(point.x)},{_format_number(point.y)})"
        _place_label(
            builder,
            f"label_{feature_id}",
            coordinate,
            (x_canvas, y_canvas),
            obstacles,
            reserved,
            margin,
            semantic_id=feature_id,
            index=order,
            preferred=preferred,
            braille_text=point_name,
        )
        builder.trace.append(
            {
                "stage": "layout",
                "action": "key_point",
                "decision": f"({point.x:.3f}, {point.y:.3f}) {point.kind}",
                "reason": "Computed from the expression by sign change and derivative tests, then filtered to the visible window.",
            }
        )

    # Axis labels are 3 braille cells wide.  Try several points along the
    # axis and keep the first one that is genuinely clear; a single anchor
    # near the arrowhead can be boxed in by a key-point label.
    axis_x_label = None
    for anchor in (
        (data_left + 4.0, axis_y_canvas),
        (data_left + (data_right - data_left) * 0.25, axis_y_canvas),
        (data_left + (data_right - data_left) * 0.5, axis_y_canvas),
        (data_right, axis_y_canvas),
    ):
        axis_x_label = _place_label(
            builder,
            "label_x",
            "",
            anchor,
            obstacles,
            reserved,
            margin,
            semantic_id="x_axis",
            preferred=(1.0, -0.6),
            braille_text="横轴",
            require_clear=True,
        )
        if axis_x_label is not None:
            break
    if axis_x_label is None:
        axis_x_label = _place_label(
            builder,
            "label_x",
            "",
            (data_right, axis_y_canvas),
            obstacles,
            reserved,
            margin,
            semantic_id="x_axis",
            preferred=(1.0, -0.6),
            braille_text="横轴",
        )

    axis_y_label = None
    for anchor in (
        (axis_x_canvas, data_top + 4.0),
        (axis_x_canvas, data_top + (data_bottom - data_top) * 0.25),
        (axis_x_canvas, data_top + (data_bottom - data_top) * 0.5),
        (axis_x_canvas, data_top),
    ):
        axis_y_label = _place_label(
            builder,
            "label_y",
            "",
            anchor,
            obstacles,
            reserved,
            margin,
            semantic_id="y_axis",
            preferred=(-0.6, -1.0),
            braille_text="纵轴",
            require_clear=True,
        )
        if axis_y_label is not None:
            break
    if axis_y_label is None:
        axis_y_label = _place_label(
            builder,
            "label_y",
            "",
            (axis_x_canvas, data_top),
            obstacles,
            reserved,
            margin,
            semantic_id="y_axis",
            preferred=(-0.6, -1.0),
            braille_text="纵轴",
        )
    if axis_x_label is not None:
        obstacles.append(axis_x_label)
    if axis_y_label is not None:
        obstacles.append(axis_y_label)

    qr_rect = (width - margin - 30.0, height - margin - 30.0, 30.0, 30.0)
    _place_label(
        builder,
        "label_qr",
        "",
        (qr_rect[0] + 4.0, qr_rect[1] - 2.0),
        obstacles,
        [],
        margin,
        semantic_id="qr",
        preferred=(0.0, -1.0),
        braille_text="扫码",
    )

    _redraw_grid(builder, grid, grid_x_lines, grid_y_lines, data_left, data_right, data_top, data_bottom)
    if not grid.primitives:
        # Every candidate line can be suppressed near key points (sin/cos place
        # ticks and extrema on the same spots).  An empty feature would raise
        # when the layout later asks for its bounding box.
        builder.features.remove(grid)
        builder.warnings.append("刻度线都与关键点重叠，已省略网格；关键点与坐标轴仍然可触摸。")

    # Footer: the window is mathematical notation, so print it with the
    # standard glyphs.  Natural-language notes stay in Chinese braille.
    window_text = (
        f"x[{_format_number(x_min)},{_format_number(x_max)}] "
        f"y[{_format_number(y_min)},{_format_number(y_max)}]"
    )
    _footer(builder, width, height, margin, qr_rect, text_lines=[window_text])

    units = 1
    for candidate in (1, 2, 5, 10, 20, 50):
        if candidate * scale <= min(34.0, width - margin - qr_rect[2] - margin - 4.0):
            units = candidate
    bar_length = units * scale
    scale_feature = builder.feature("scale_bar", "scale_bar")
    # Keep the bar on the brand row so it cannot collide with stacked braille
    # legend lines above it.
    bar_x = margin + 32.0
    bar_y = height - margin - 7.0
    builder.line(scale_feature, ((bar_x, bar_y), (bar_x + bar_length, bar_y)), 1.6)
    builder.line(scale_feature, ((bar_x, bar_y - 2.0), (bar_x, bar_y + 1.0)), 1.2)
    builder.line(scale_feature, ((bar_x + bar_length, bar_y - 2.0), (bar_x + bar_length, bar_y + 1.0)), 1.2)
    # Keep numerals out of braille: a Chinese word for the count reads the
    # same way a textbook caption would.
    cn_units = {1: "一", 2: "二", 5: "五", 10: "十", 20: "二十", 50: "五十"}
    unit_text = f"{cn_units.get(units, str(units))}单位"
    if tactile_braille.safe_text(unit_text):
        builder.braille(scale_feature, bar_x + bar_length + 3.5, bar_y, unit_text)

    builder.trace.append(
        {
            "stage": "layout",
            "action": "place_labels",
            "decision": "candidate search right / left / above / below, 2 mm ink clearance, 4.5 mm label spacing",
            "reason": "Labels are placed by geometry, not by hand; a failed search becomes a warning instead of silently overlapping the curve.",
        }
    )

    builder.prune_empty()
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
# Robot relief plate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RobotSpec:
    """A serial chain that is sufficient for an accurate side-view relief."""

    link_lengths: tuple[float, ...]
    link_widths: tuple[float, ...]
    joint_radii: tuple[float, ...]
    joint_angles_deg: tuple[float, ...]
    base_width: float = 34.0
    base_height: float = 14.0
    end_effector: str = "gripper"

    @property
    def dof(self) -> int:
        return len(self.joint_angles_deg)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dof": self.dof,
            "link_lengths_mm": list(self.link_lengths),
            "link_widths_mm": list(self.link_widths),
            "joint_radii_mm": list(self.joint_radii),
            "joint_angles_deg": list(self.joint_angles_deg),
            "base_width_mm": self.base_width,
            "base_height_mm": self.base_height,
            "end_effector": self.end_effector,
        }


DEFAULT_ROBOT = RobotSpec(
    link_lengths=(18.0, 40.0, 32.0, 8.0, 6.0, 8.0),
    link_widths=(15.0, 12.0, 10.0, 7.0, 6.0, 5.0),
    joint_radii=(7.0, 6.0, 5.0, 4.0, 3.5, 3.0),
    joint_angles_deg=(0.0, -20.0, 65.0, -10.0, 35.0, 0.0),
    base_width=34.0,
    base_height=14.0,
    end_effector="gripper",
)


def _as_floats(value: Any, fallback: Sequence[float]) -> tuple[float, ...]:
    if isinstance(value, (list, tuple)) and value:
        try:
            return tuple(float(item) for item in value)
        except (TypeError, ValueError):
            return tuple(fallback)
    return tuple(fallback)


def robot_spec_from_mapping(payload: dict[str, Any]) -> RobotSpec:
    """Build a validated RobotSpec from API/JSON fields."""
    angles = _as_floats(payload.get("joint_angles_deg"), DEFAULT_ROBOT.joint_angles_deg)
    dof = len(angles)
    if dof == 0:
        raise ValueError("joint_angles_deg must contain at least one angle")
    lengths = _as_floats(payload.get("link_lengths_mm"), DEFAULT_ROBOT.link_lengths)
    widths = _as_floats(payload.get("link_widths_mm"), DEFAULT_ROBOT.link_widths)
    radii = _as_floats(payload.get("joint_radii_mm"), DEFAULT_ROBOT.joint_radii)
    if len(lengths) < dof:
        lengths = lengths + tuple([lengths[-1] if lengths else 10.0] * (dof - len(lengths)))
    if len(widths) < dof:
        widths = widths + tuple([widths[-1] if widths else 8.0] * (dof - len(widths)))
    if len(radii) < dof:
        radii = radii + tuple([radii[-1] if radii else 4.0] * (dof - len(radii)))
    return RobotSpec(
        link_lengths=tuple(lengths[:dof]),
        link_widths=tuple(widths[:dof]),
        joint_radii=tuple(radii[:dof]),
        joint_angles_deg=angles,
        base_width=float(payload.get("base_width_mm", DEFAULT_ROBOT.base_width)),
        base_height=float(payload.get("base_height_mm", DEFAULT_ROBOT.base_height)),
        end_effector=str(payload.get("end_effector", DEFAULT_ROBOT.end_effector)),
    )


def robot_spec_from_brief(
    *,
    dof: int = 6,
    link_length_mm: float = 40.0,
    end_effector: str = "gripper",
) -> RobotSpec:
    """Build a plausible serial chain from the front-end quick fields.

    The front end asks for degrees of freedom and a representative link
    length, not six independent measurements.  We therefore scale the
    default demonstration proportions rather than pretending the brief is a
    measured engineering drawing.  The draft states this explicitly.
    """
    dof = max(3, min(6, int(dof)))
    length = max(8.0, float(link_length_mm))
    scale = length / max(DEFAULT_ROBOT.link_lengths)
    lengths = tuple(round(value * scale, 3) for value in DEFAULT_ROBOT.link_lengths[:dof])
    widths = tuple(round(value * max(0.75, scale * 0.9), 3) for value in DEFAULT_ROBOT.link_widths[:dof])
    radii = tuple(round(value * max(0.8, scale * 0.85), 3) for value in DEFAULT_ROBOT.joint_radii[:dof])
    angles = tuple(DEFAULT_ROBOT.joint_angles_deg[:dof])
    return RobotSpec(
        link_lengths=lengths,
        link_widths=widths,
        joint_radii=radii,
        joint_angles_deg=angles,
        base_width=round(min(60.0, max(24.0, DEFAULT_ROBOT.base_width * min(scale, 1.8))), 3),
        base_height=round(min(24.0, max(10.0, DEFAULT_ROBOT.base_height * min(scale, 1.8))), 3),
        end_effector=end_effector if end_effector in {"gripper", "suction"} else "gripper",
    )


def robot_spec_from_diagram(diagram: Diagram) -> RobotSpec:
    options = diagram.layout.get("options", {})
    payload = options.get("robot") if isinstance(options.get("robot"), dict) else options
    return robot_spec_from_mapping(payload)


def _forward_kinematics(spec: RobotSpec) -> list[Point]:
    joints: list[Point] = [(0.0, spec.base_height)]
    theta = 0.0
    for index in range(spec.dof):
        theta += math.radians(spec.joint_angles_deg[index])
        length = spec.link_lengths[index]
        x, y = joints[-1]
        joints.append((x + length * math.sin(theta), y + length * math.cos(theta)))
    return joints


def _ee_polygons(ee: Point, direction: Point, kind: str) -> list[list[Point]]:
    dx, dy = direction
    magnitude = math.hypot(dx, dy)
    if magnitude <= 1e-9:
        return []
    ux, uy = dx / magnitude, dy / magnitude
    px, py = -uy, ux
    ex, ey = ee
    if kind == "none":
        return []
    if kind == "suction":
        cup = [
            (ex + px * 4.0, ey + py * 4.0),
            (ex + ux * 5.0 + px * 4.0, ey + uy * 5.0 + py * 4.0),
            (ex + ux * 5.0 - px * 4.0, ey + uy * 5.0 - py * 4.0),
            (ex - px * 4.0, ey - py * 4.0),
        ]
        return [cup]
    palm = [
        (ex - ux * 2.0 + px * 5.0, ey - uy * 2.0 + py * 5.0),
        (ex + ux * 3.0 + px * 5.0, ey + uy * 3.0 + py * 5.0),
        (ex + ux * 3.0 - px * 5.0, ey + uy * 3.0 - py * 5.0),
        (ex - ux * 2.0 - px * 5.0, ey - uy * 2.0 - py * 5.0),
    ]
    return [palm]


def build_robot_layout(diagram: Diagram) -> LayoutResult:
    builder = _Builder(diagram)
    width = diagram.canvas.width_mm
    height = diagram.canvas.height_mm
    margin = max(float(diagram.canvas.margin_mm), 6.0)
    spec = robot_spec_from_diagram(diagram)
    joints = _forward_kinematics(spec)

    builder.trace.append(
        {
            "stage": "layout",
            "action": "choose_engine",
            "decision": f"robot_relief / {spec.dof}-DOF serial chain",
            "reason": "Joint positions come from forward kinematics of the extracted link lengths and joint angles, so the joint order is preserved.",
        }
    )

    # The plate carries Chinese braille only.  Raised Latin letters such as
    # "ROBOT ARM" cannot be read by touch, so the title and subtitle are
    # plain Chinese braille lines.
    title_zh = _chinese_label(f"{spec.dof}轴机械臂", max_chars=8)
    title = builder.feature("title", "title", label=title_zh)
    if title_zh and tactile_braille.safe_text(title_zh):
        title_braille_width = tactile_braille.width(title_zh)
        if title_braille_width <= width - 2 * margin:
            builder.braille(title, (width - title_braille_width) / 2.0, margin + 2.0, title_zh)

    subtitle_zh = "夹爪" if spec.end_effector == "gripper" else "吸盘"
    subtitle = builder.feature("subtitle", "subtitle", label=subtitle_zh)
    if tactile_braille.safe_text(subtitle_zh):
        subtitle_width = tactile_braille.width(subtitle_zh)
        if subtitle_width <= width - 2 * margin:
            builder.braille(subtitle, (width - subtitle_width) / 2.0, margin + 10.5, subtitle_zh)

    # Raw bounds in robot coordinates (y up).
    raw_min_x = -spec.base_width / 2.0
    raw_max_x = spec.base_width / 2.0
    raw_min_y = 0.0
    raw_max_y = max(spec.base_height, joints[-1][1])
    for index, joint in enumerate(joints):
        radius = spec.joint_radii[index] if index < len(spec.joint_radii) else 3.0
        raw_min_x = min(raw_min_x, joint[0] - radius)
        raw_max_x = max(raw_max_x, joint[0] + radius)
        raw_min_y = min(raw_min_y, joint[1] - radius)
        raw_max_y = max(raw_max_y, joint[1] + radius)
    for index in range(spec.dof):
        start = joints[index]
        end = joints[index + 1]
        half = spec.link_widths[index] / 2.0
        raw_min_x = min(raw_min_x, start[0] - half, end[0] - half)
        raw_max_x = max(raw_max_x, start[0] + half, end[0] + half)
    last_direction = (joints[-1][0] - joints[-2][0], joints[-1][1] - joints[-2][1])
    ee_length = math.hypot(last_direction[0], last_direction[1]) or 1.0
    ee_unit = (last_direction[0] / ee_length, last_direction[1] / ee_length)
    ee_extent = 14.0 if spec.end_effector == "gripper" else 6.0
    raw_max_x = max(raw_max_x, joints[-1][0] + ee_unit[0] * ee_extent + 7.0)
    raw_max_y = max(raw_max_y, joints[-1][1] + ee_unit[1] * ee_extent + 7.0)
    raw_min_x = min(raw_min_x, joints[-1][0] + ee_unit[0] * ee_extent - 7.0)
    raw_min_y = min(raw_min_y, joints[-1][1] + ee_unit[1] * ee_extent - 7.0)

    raw_width = max(raw_max_x - raw_min_x, 1.0)
    raw_height = max(raw_max_y - raw_min_y, 1.0)
    area_left = margin + 3.0
    area_right = width - margin - 3.0
    area_top = margin + 24.0
    area_bottom = height - margin - 40.0
    area_width = area_right - area_left
    area_height = area_bottom - area_top
    scale = min(area_width / raw_width, area_height / raw_height, 1.15)
    scale = max(scale, 0.05)
    offset_x = area_left + (area_width - raw_width * scale) / 2.0 - raw_min_x * scale
    offset_y = area_bottom - raw_min_y * scale

    def to_canvas(point: Point) -> Point:
        return (offset_x + point[0] * scale, offset_y - point[1] * scale)

    builder.trace.append(
        {
            "stage": "layout",
            "action": "fit_model",
            "decision": f"uniform scale {scale * 100:.0f}%",
            "reason": "One uniform scale keeps link ratios and joint angles true; the plate is a scaled relief, not a stretched sketch.",
        }
    )

    base = builder.feature("BASE", "base", "BASE", semantic_id="BASE")
    base_local = [
        (-spec.base_width / 2.0, 0.0),
        (spec.base_width / 2.0, 0.0),
        (spec.base_width * 0.34, spec.base_height),
        (-spec.base_width * 0.34, spec.base_height),
    ]
    builder.polygon(base, [to_canvas(point) for point in base_local])
    builder.line(base, (to_canvas((-spec.base_width * 0.62, 0.0)), to_canvas((spec.base_width * 0.62, 0.0))), 1.4)
    builder.line(base, (to_canvas((-spec.base_width * 0.30, 3.0)), to_canvas((-spec.base_width * 0.14, 6.0))), 1.2)
    builder.line(base, (to_canvas((spec.base_width * 0.30, 3.0)), to_canvas((spec.base_width * 0.14, 6.0))), 1.2)
    obstacles: list[Feature] = [title, subtitle, base]
    link_features: list[Feature] = []
    joint_features: list[Feature] = []
    for index in range(spec.dof):
        start = joints[index]
        end = joints[index + 1]
        link_id = f"L{index + 1}"
        link = builder.feature(link_id, "link", link_id, semantic_id=link_id)
        builder.line(link, (to_canvas(start), to_canvas(end)), max(2.0, spec.link_widths[index] * scale))
        link_features.append(link)
        obstacles.append(link)

        joint_id = f"J{index + 1}"
        joint = builder.feature(joint_id, "joint", joint_id, semantic_id=joint_id)
        builder.circle(joint, *to_canvas(start), max(3.0, spec.joint_radii[index] * scale))
        joint_features.append(joint)
        obstacles.append(joint)

    last_joint = joints[-1]
    ee_feature = builder.feature("EE", "end_effector", "EE", semantic_id="EE")
    polygon_list = _ee_polygons(last_joint, ee_unit, spec.end_effector)
    for polygon in polygon_list:
        builder.polygon(ee_feature, [to_canvas(point) for point in polygon])
    if spec.end_effector == "gripper":
        px, py = -ee_unit[1], ee_unit[0]
        for side in (-1.0, 1.0):
            root = (last_joint[0] + ee_unit[0] * 3.0 + px * 3.5 * side, last_joint[1] + ee_unit[1] * 3.0 + py * 3.5 * side)
            tip = (root[0] + ee_unit[0] * 9.0, root[1] + ee_unit[1] * 9.0)
            builder.line(ee_feature, (to_canvas(root), to_canvas(tip)), 2.5)
    obstacles.append(ee_feature)

    reserved: list[BBox] = [(*_qr_zone(builder, width, height, margin),)]

    # Joint labels: numbers matching the textbook convention.  The outward
    # direction is away from the bisector of the two adjacent links.
    for index, joint_feature in enumerate(joint_features):
        point = joints[index]
        previous = joints[index - 1] if index > 0 else joints[index]
        following = joints[index + 1] if index + 1 < len(joints) else joints[index]
        incoming = (previous[0] - point[0], previous[1] - point[1])
        outgoing = (following[0] - point[0], following[1] - point[1])
        incoming_length = math.hypot(*incoming) or 1.0
        outgoing_length = math.hypot(*outgoing) or 1.0
        bisector = (
            incoming[0] / incoming_length + outgoing[0] / outgoing_length,
            incoming[1] / incoming_length + outgoing[1] / outgoing_length,
        )
        magnitude = math.hypot(*bisector)
        if index == 0 or magnitude < 1e-6:
            outward = (-1.0, 0.0)
        else:
            outward = (-bisector[0] / magnitude, -bisector[1] / magnitude)
        anchor = (
            to_canvas(point)[0] + outward[0] * (max(3.0, spec.joint_radii[index] * scale) + 4.0),
            to_canvas(point)[1] - outward[1] * (max(3.0, spec.joint_radii[index] * scale) + 4.0),
        )
        _place_label(
            builder,
            f"label_{joint_feature.id}",
            str(index + 1),
            anchor,
            obstacles,
            reserved,
            margin,
            semantic_id=joint_feature.semantic_id,
            index=index + 1,
            preferred=(outward[0], -outward[1]),
        )

    # Link labels sit at the midpoint, on the side of the link away from the
    # chain centroid, so they do not sit on top of the next link.
    centroid_x = sum(point[0] for point in joints) / len(joints)
    centroid_y = sum(point[1] for point in joints) / len(joints)
    for index, link_feature in enumerate(link_features):
        start = joints[index]
        end = joints[index + 1]
        midpoint = ((start[0] + end[0]) / 2.0, (start[1] + end[1]) / 2.0)
        dx, dy = end[0] - start[0], end[1] - start[1]
        length = math.hypot(dx, dy) or 1.0
        normal = (-dy / length, dx / length)
        away = (midpoint[0] - centroid_x, midpoint[1] - centroid_y)
        if normal[0] * away[0] + normal[1] * away[1] < 0:
            normal = (-normal[0], -normal[1])
        anchor_canvas = to_canvas(midpoint)
        anchor = (
            anchor_canvas[0] + normal[0] * (spec.link_widths[index] * scale / 2.0 + 4.0),
            anchor_canvas[1] - normal[1] * (spec.link_widths[index] * scale / 2.0 + 4.0),
        )
        _place_label(
            builder,
            f"label_{link_feature.id}",
            "",
            anchor,
            obstacles,
            reserved,
            margin,
            semantic_id=link_feature.semantic_id,
            braille_text=f"杆{index + 1}",
            preferred=(normal[0], -normal[1]),
        )

    _place_label(
        builder,
        "label_BASE",
        "0",
        (to_canvas((-spec.base_width / 2.0, spec.base_height))[0] - 3.0, to_canvas((-spec.base_width / 2.0, spec.base_height))[1]),
        obstacles,
        reserved,
        margin,
        semantic_id="BASE",
        preferred=(-1.0, -0.2),
    )
    ee_anchor = (to_canvas(last_joint)[0] + ee_unit[0] * 16.0, to_canvas(last_joint)[1] - ee_unit[1] * 16.0)
    _place_label(
        builder,
        "label_EE",
        "",
        ee_anchor,
        obstacles,
        reserved,
        margin,
        semantic_id="EE",
        braille_text=subtitle_zh,
        preferred=(ee_unit[0], -ee_unit[1]),
    )

    qr_rect = (width - margin - 30.0, height - margin - 30.0, 30.0, 30.0)
    _place_label(
        builder,
        "label_qr",
        "",
        (qr_rect[0] + 4.0, qr_rect[1] - 2.0),
        obstacles,
        [],
        margin,
        semantic_id="qr",
        preferred=(0.0, -1.0),
        braille_text="扫码",
    )

    _footer(
        builder,
        width,
        height,
        margin,
        qr_rect,
        braille_lines=[
            "侧视图",
            f"零基座 关节一到{_cn_count(spec.dof)}",
            f"连杆一到{_cn_count(spec.dof)} {subtitle_zh}",
        ],
    )

    builder.trace.append(
        {
            "stage": "layout",
            "action": "legend_scheme",
            "decision": "0 base, numbers for joints, 杆+number for links, Chinese name for the tool",
            "reason": "A plate uses Chinese braille and Arabic numerals only; Latin letter labels cannot be read by touch.",
        }
    )

    builder.prune_empty()
    return LayoutResult(
        diagram=diagram,
        width=width,
        height=height,
        base_polygon=base_plate_polygon(width, height),
        features=builder.features,
        trace=builder.trace,
        warnings=builder.warnings,
    )
