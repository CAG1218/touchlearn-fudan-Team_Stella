"""A6 legend assets for a compiled tactile plate.

Two A6 (105 x 148 mm) inserts are produced for every numbered flat plate:

* ``legend.svg`` is a visual card for teachers and sighted helpers;
* ``legend_card.stl`` is the same key as a printable tactile card.  It carries
  the plate's numeric indices and the full Chinese name of each part in
  Common Chinese Braille (GF 0019-2018), so a braille reader can look up an
  index without a sighted reader reading the SVG aloud.
"""

from __future__ import annotations

from dataclasses import dataclass
from html import escape
from pathlib import Path

from . import tactile_braille as braille
from .geometry import build_mesh
from .layout import LayoutResult, base_plate_polygon, text_width, _Builder
from .stl import write_binary_stl
from .svg import layout_to_svg
from .cnbraille import BrailleError

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
        try:
            element = layout.diagram.element(feature.semantic_id)
        except Exception:
            continue
        # Plates now carry braille only, so the legend falls back to the
        # machine-readable element label for its sighted key column.
        visible = next((primitive.text for primitive in feature.primitives if primitive.kind == "text"), "")
        visible = visible or (element.label or "").lower()
        if not visible:
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


# ---------------------------------------------------------------------------
# Tactile A6 legend card
# ---------------------------------------------------------------------------

CARD_TITLE = "触觉图例"
CARD_BASE_THICKNESS_MM = 1.2
CARD_DOT_HEIGHT_MM = 0.8
CARD_MARGIN_MM = 6.0
CARD_INDEX_X_MM = 8.0
CARD_BRAILLE_X_MM = 18.0
CARD_MARK_BRAILLE_X_MM = 26.0
CARD_LINE_STEP_MM = 8.0
CARD_ROW_GAP_MM = 1.6
CARD_FOOTER_Y_MM = 126.0

CARD_TEXT_WIDTH_MM = WIDTH_MM - 2 * CARD_MARGIN_MM
CARD_NAME_WIDTH_MM = WIDTH_MM - CARD_BRAILLE_X_MM - CARD_MARGIN_MM
CARD_MARK_WIDTH_MM = WIDTH_MM - CARD_MARK_BRAILLE_X_MM - CARD_MARGIN_MM


def _is_han(character: str) -> bool:
    return "\u4e00" <= character <= "\u9fff"


def _clean(text: str) -> str:
    """Keep printable ASCII, Han characters and single spaces."""
    kept = "".join(character if character.isascii() or _is_han(character) else " " for character in (text or ""))
    return " ".join(kept.split())


def _safe_braille_text(text: str, fallback: str = "") -> str:
    """Return text the braille tables can encode, or a safe fallback."""
    candidate = _clean(text)
    if candidate and braille.safe_text(candidate):
        return candidate
    fallback = _clean(fallback)
    if fallback and braille.safe_text(fallback):
        return fallback
    return CARD_TITLE


def _wrap_braille(text: str, limit_mm: float) -> list[str]:
    """Split a braille label into lines that fit ``limit_mm``.

    Short labels stay on one line.  A two-line break is balanced by character
    count so the second line is not left with a single cell; longer labels use
    a greedy split.  Breaks fall between syllables, which is how braille lines
    normally wrap.
    """
    text = _clean(text)
    if not text:
        return []
    if braille.width(text) <= limit_mm:
        return [text]

    best: tuple[int, int] | None = None
    for index in range(1, len(text)):
        head, tail = text[:index].rstrip(), text[index:].lstrip()
        if not head or not tail:
            continue
        try:
            if braille.width(head) <= limit_mm and braille.width(tail) <= limit_mm:
                score = abs(len(head) - len(tail))
                if best is None or score < best[0]:
                    best = (score, index)
        except BrailleError:
            continue
    if best is not None:
        index = best[1]
        return [text[:index].rstrip(), text[index:].lstrip()]

    lines: list[str] = []
    current = ""
    for character in text:
        candidate = current + character
        try:
            fits = not current or braille.width(candidate) <= limit_mm
        except BrailleError:
            fits = True
        if not fits:
            lines.append(current.rstrip())
            current = character.lstrip()
        else:
            current = candidate
    if current.strip():
        lines.append(current.strip())
    return lines


def _fit_one_line(text: str, limit_mm: float) -> str:
    """Longest prefix of ``text`` that fits one braille line."""
    if braille.width(text) <= limit_mm:
        return text
    current = ""
    for character in text:
        candidate = current + character
        if braille.width(candidate) > limit_mm:
            break
        current = candidate
    return current.rstrip() or text[:1]


def _card_subtitle(layout: LayoutResult) -> str:
    title = _clean(getattr(layout.diagram, "title_zh", "") or layout.diagram.title or "")
    for suffix in ("触觉示意图", "示意图", "图像", "图"):
        if title.endswith(suffix) and len(title) > len(suffix):
            title = title[: -len(suffix)].rstrip()
            break
    # The subtitle is a heading, not the plate title.  Drop any trailing ASCII
    # expression ("二次函数 y=x2-2x-3") and keep one line so the entry rows
    # never push into the footer.
    head = title.split()[0] if title.split() else title
    return _fit_one_line(head, CARD_TEXT_WIDTH_MM) or CARD_TITLE


def _add_card_heading(builder: _Builder, layout: LayoutResult) -> float:
    title = builder.feature("card_title", "title")
    title_lines = _wrap_braille(CARD_TITLE, CARD_TEXT_WIDTH_MM)
    for index, line in enumerate(title_lines):
        builder.braille(title, (WIDTH_MM - braille.width(line)) / 2.0, 7.0 + index * CARD_LINE_STEP_MM, line)

    subtitle_text = _safe_braille_text(_card_subtitle(layout))
    subtitle = builder.feature("card_subtitle", "title")
    subtitle_lines = _wrap_braille(subtitle_text, CARD_TEXT_WIDTH_MM)
    y = 7.0 + len(title_lines) * CARD_LINE_STEP_MM + 1.0
    for index, line in enumerate(subtitle_lines):
        builder.braille(subtitle, (WIDTH_MM - braille.width(line)) / 2.0, y + index * CARD_LINE_STEP_MM, line)

    rule_y = y + len(subtitle_lines) * CARD_LINE_STEP_MM + 1.5
    rule = builder.feature("card_rule", "footer")
    builder.line(rule, ((CARD_MARGIN_MM + 1.0, rule_y), (WIDTH_MM - CARD_MARGIN_MM - 1.0, rule_y)), 1.0)
    return rule_y + 4.5


def build_legend_card_layout(layout: LayoutResult) -> LayoutResult:
    """Build the printable A6 tactile legend card for ``layout``.

    Rows mirror the numbered labels on the plate: the index stays standard
    print at the left, the full Chinese name is brailled beside it.  Unnumbered
    marks (``x=1``, ``x``, ``y``, ``o``) follow under a braille heading so the
    card covers the whole key, not just the numbers.
    """
    builder = _Builder(layout.diagram)
    y = _add_card_heading(builder, layout)

    for entry in indexed_entries(layout):
        name = _safe_braille_text(entry.label_zh, entry.label)
        lines = _wrap_braille(name, CARD_NAME_WIDTH_MM)
        feature = builder.feature(f"card_entry_{entry.index}", "label", label=entry.label, index=entry.index)
        builder.text(feature, CARD_INDEX_X_MM, y, str(entry.index), 6.0)
        for line_index, line in enumerate(lines):
            builder.braille(feature, CARD_BRAILLE_X_MM, y + line_index * CARD_LINE_STEP_MM, line)
        y += max(1, len(lines)) * CARD_LINE_STEP_MM + CARD_ROW_GAP_MM

    marks = other_marks(layout)
    if marks:
        heading = builder.feature("card_other_heading", "label")
        builder.braille(heading, CARD_INDEX_X_MM, y + 1.5, "其它标记")
        y += 1.5 + CARD_LINE_STEP_MM + 1.0
        for position, (visible, label_zh) in enumerate(marks):
            name = _safe_braille_text(label_zh)
            lines = _wrap_braille(name, CARD_MARK_WIDTH_MM)
            feature = builder.feature(f"card_mark_{position}", "label")
            builder.text(feature, CARD_INDEX_X_MM, y, _clean(visible).upper(), 5.0)
            for line_index, line in enumerate(lines):
                builder.braille(feature, CARD_MARK_BRAILLE_X_MM, y + line_index * CARD_LINE_STEP_MM, line)
            y += max(1, len(lines)) * CARD_LINE_STEP_MM + CARD_ROW_GAP_MM

    footer = builder.feature("card_footer", "footer")
    builder.braille(footer, CARD_INDEX_X_MM, CARD_FOOTER_Y_MM, "触摸学习")
    builder.braille(footer, CARD_INDEX_X_MM, CARD_FOOTER_Y_MM + 7.0, "单位毫米")

    return LayoutResult(
        diagram=layout.diagram,
        width=WIDTH_MM,
        height=HEIGHT_MM,
        base_polygon=base_plate_polygon(WIDTH_MM, HEIGHT_MM),
        features=builder.features,
        trace=[],
        warnings=[],
    )


def legend_card_mesh(layout: LayoutResult):
    """Return the closed card mesh at card thickness and dot height."""
    card = build_legend_card_layout(layout)
    return build_mesh(
        card,
        base_thickness=CARD_BASE_THICKNESS_MM,
        relief_height=CARD_DOT_HEIGHT_MM,
        braille_height=CARD_DOT_HEIGHT_MM,
    )


def write_legend_card_stl(layout: LayoutResult, path: str | Path) -> Path:
    return write_binary_stl(legend_card_mesh(layout), path)
