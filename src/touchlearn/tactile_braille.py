"""Braille geometry that picks the right code for the text it is given.

The plate compiler has a single ``braille`` primitive kind.  Which code the
dots belong to depends on the label: ASCII text uses the bundled UEB cells,
Chinese text uses the Common Chinese Braille tables (GF 0019-2018) from
:mod:`touchlearn.cnbraille`.  Keeping the dispatch in one place means the STL
builder, the SVG preview and the collision checks cannot disagree about where
the dots land.

The Chinese table raises :class:`touchlearn.cnbraille.BrailleError` for a
character it does not know.  Callers should treat that as "this label cannot be
brailled" rather than silently printing an unrelated syllable; :func:`safe_text`
gives them a cheap way to check.
"""

from __future__ import annotations

from . import braille, cnbraille

# Chinese cells are slightly larger than the UEB cells.  The renderers ask for
# the diameter per text instead of importing one global constant.
DOT_DIAMETER_MM = cnbraille.DOT_DIAMETER_MM
CELL_HEIGHT_MM = cnbraille.CELL_HEIGHT_MM


def contains_chinese(text: str) -> bool:
    return any("\u4e00" <= character <= "\u9fff" for character in (text or ""))


def dot_diameter(text: str) -> float:
    return cnbraille.DOT_DIAMETER_MM if contains_chinese(text) else braille.DOT_DIAMETER_MM


def dot_centers(text: str, x: float, y: float) -> list[tuple[float, float]]:
    if contains_chinese(text):
        return cnbraille.dot_centers(text, x, y)
    return braille.dot_centers(text, x, y)


def width(text: str) -> float:
    if contains_chinese(text):
        return cnbraille.width(text, syllable_gap_mm=cnbraille.SYLLABLE_GAP_MM)
    return braille.width(text)


def height(text: str = "") -> float:
    return cnbraille.height() if contains_chinese(text) else braille.height()


def bbox(text: str, x: float, y: float) -> tuple[float, float, float, float]:
    return (x, y, width(text), height(text))


def safe_text(text: str) -> bool:
    """True when every character can be encoded without guessing a reading."""
    if not contains_chinese(text):
        return True
    try:
        cnbraille.encode_text(text)
    except cnbraille.BrailleError:
        return False
    return True
