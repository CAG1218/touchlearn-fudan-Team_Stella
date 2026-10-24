"""A6 legend card for a compiled tactile plate.

The card is a print insert for teachers and accompanying sighted readers; it
is not a second tactile plate.  It maps the numeric braille indices on the A5
plate to English and Chinese names.  A tactile A6 card is on the roadmap.
"""

from __future__ import annotations

from dataclasses import dataclass
from html import escape
from pathlib import Path

from .layout import LayoutResult, base_plate_polygon, text_width, _Builder
from .svg import layout_to_svg

WIDTH_MM = 105.0
HEIGHT_MM = 148.0
MARGIN_MM = 6.0

ROW_START_MM = 36.0
ROW_STEP_MM = 13.0
NUMBER_X_MM = 8.0
LABEL_X_MM = 24.0
CHINESE_X_MM = 50.0


@dataclass(frozen=True)
class LegendEntry:
    index: int
    label: str
    label_zh: str


def indexed_entries(layout: LayoutResult) -> list[LegendEntry]:
    """Entries that carry a numeric index, ordered by that index."""
    entries: list[LegendEntry] = []
    seen: set[int] = set()
    for feature in layout.labels():
        if feature.index is None or feature.index in seen:
            continue
        if not feature.semantic_id:
            continue
        try:
            element = layout.diagram.element(feature.semantic_id)
        except Exception:
            continue
        seen.add(feature.index)
        entries.append(LegendEntry(feature.index, element.label, element.label_zh))
    return sorted(entries, key=lambda entry: entry.index)


def other_marks(layout: LayoutResult) -> list[tuple[str, str]]:
    """Unnumbered marks such as ``x=1``, ``x``, ``y`` and ``o``."""
    marks: list[tuple[str, str]] = []
    for feature in layout.labels():
        if feature.index is not None or not feature.semantic_id:
            continue
        visible = next((primitive.text for primitive in feature.primitives if primitive.kind == "text"), "")
        if not visible:
            continue
        try:
            element = layout.diagram.element(feature.semantic_id)
        except Exception:
            continue
        marks.append((visible, element.label_zh))
    return marks


def _add_heading(builder: _Builder) -> None:
    title = builder.feature("legend_title", "title")
    title_text = "TOUCHLEARN"
    builder.text(title, (WIDTH_MM - text_width(title_text, 5.0)) / 2.0, 9.0, title_text, 5.0)

    subtitle_text = builder.diagram.title.upper()
    subtitle = builder.feature("legend_subtitle", "title")
    builder.text(subtitle, (WIDTH_MM - text_width(subtitle_text, 4.0)) / 2.0, 17.0, subtitle_text, 4.0)

    rule = builder.feature("legend_rule", "footer")
    builder.line(rule, ((MARGIN_MM + 1.0, 24.0), (WIDTH_MM - MARGIN_MM - 1.0, 24.0)), 1.0)

    heading = builder.feature("legend_heading", "label")
    builder.text(heading, NUMBER_X_MM, 27.0, "LEGEND", 4.0)


def build_legend_layout(layout: LayoutResult) -> tuple[LayoutResult, list[tuple[float, str]]]:
    """Build the A6 card layout and the Chinese text rows to overlay."""
    builder = _Builder(layout.diagram)
    _add_heading(builder)

    entries = indexed_entries(layout)
    chinese_rows: list[tuple[float, str]] = []
    y = ROW_START_MM
    for entry in entries:
        feature = builder.feature(f"legend_entry_{entry.index}", "label")
        builder.text(feature, NUMBER_X_MM, y, str(entry.index), 6.0)
        builder.braille(feature, NUMBER_X_MM, y + 7.5, str(entry.index))
        builder.text(feature, LABEL_X_MM, y + 0.8, entry.label, 4.5)
        chinese_rows.append((y + 4.2, entry.label_zh))
        y += ROW_STEP_MM

    marks = other_marks(layout)
    if marks:
        divider_y = y + 3.0
        divider = builder.feature("legend_divider", "footer")
        builder.line(divider, ((MARGIN_MM + 1.0, divider_y), (WIDTH_MM - MARGIN_MM - 1.0, divider_y)), 1.0)
        heading = builder.feature("legend_other_heading", "label")
        builder.text(heading, NUMBER_X_MM, divider_y + 5.0, "OTHER MARKS", 4.0)
        y = divider_y + 14.0
        for visible, label_zh in marks:
            feature = builder.feature(f"legend_mark_{len(chinese_rows)}", "label")
            builder.text(feature, NUMBER_X_MM, y, visible.upper(), 4.5)
            chinese_rows.append((y + 4.2, label_zh))
            y += 10.0

    if layout.diagram.domain == "math":
        equation = builder.feature("legend_equation", "label")
        builder.text(equation, NUMBER_X_MM, y + 4.5, "Y=X^2-2X-3", 4.5)

    legend_layout = LayoutResult(
        diagram=layout.diagram,
        width=WIDTH_MM,
        height=HEIGHT_MM,
        base_polygon=base_plate_polygon(WIDTH_MM, HEIGHT_MM),
        features=builder.features,
        trace=[],
        warnings=[],
    )
    return legend_layout, chinese_rows


def legend_to_svg(layout: LayoutResult) -> str:
    legend_layout, chinese_rows = build_legend_layout(layout)
    svg = layout_to_svg(legend_layout)
    chinese = "\n".join(
        f'<text x="{CHINESE_X_MM:.1f}" y="{row_y:.1f}" font-family="Microsoft YaHei, Noto Sans CJK SC, sans-serif" '
        f'font-size="4.2" fill="#111111">{escape(label_zh)}</text>'
        for row_y, label_zh in chinese_rows
    )
    return svg.replace("</svg>", chinese + "\n</svg>")


def write_legend_svg(layout: LayoutResult, path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(legend_to_svg(layout), encoding="utf-8")
    return target
