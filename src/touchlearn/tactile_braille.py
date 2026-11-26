"""Braille geometry that picks the right code for the text it is given.

The plate compiler has a single ``braille`` primitive kind.  A plate for the
Chinese curriculum is read with the Chinese tables: Han characters use
GF 0019-2018, while the Arabic digits, Latin letters and maths signs inside a
label use the GB/T 18028-2010 forms (数号、字母号、运算符号).  That is why a
label such as ``y=x·x-2x-3`` must not fall back to the bare UEB letters even
though it contains no Han character.

:mod:`touchlearn.braille` (UEB) stays as the reference implementation for
English-only output; :func:`uses_chinese_code` decides which table a plate
label belongs to.  The Chinese table raises
:class:`touchlearn.cnbraille.BrailleError` for a character it does not know.
Callers treat that as "this label cannot be brailled" rather than silently
printing an unrelated syllable; :func:`safe_text` gives them a cheap way to
check.
"""

from __future__ import annotations

from . import braille, cnbraille

# Chinese cells are slightly larger than the UEB cells.  The renderers ask for
# the diameter per text instead of importing one global constant.
DOT_DIAMETER_MM = cnbraille.DOT_DIAMETER_MM
CELL_HEIGHT_MM = cnbraille.CELL_HEIGHT_MM

# Every ASCII alphanumeric and maths sign has a place in GB/T 18028; a label
# made only of these still belongs on a Chinese plate.
_MATH_ALPHABET = frozenset(
    "0123456789"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz"
    "+-−×÷=≠≈<>≤≥±·/.()^"
)


def contains_chinese(text: str) -> bool:
    """True when the label has at least one Han character."""
    return any("\u4e00" <= character <= "\u9fff" for character in (text or ""))


def uses_chinese_code(text: str) -> bool:
    """True when the Chinese braille tables must encode this label."""
    return any(
        character in _MATH_ALPHABET or "\u4e00" <= character <= "\u9fff"
        for character in (text or "")
    )


def dot_diameter(text: str) -> float:
    return cnbraille.DOT_DIAMETER_MM if uses_chinese_code(text) else braille.DOT_DIAMETER_MM


def dot_centers(text: str, x: float, y: float) -> list[tuple[float, float]]:
    if uses_chinese_code(text):
        return cnbraille.dot_centers(text, x, y)
    return braille.dot_centers(text, x, y)


def width(text: str) -> float:
    if uses_chinese_code(text):
        return cnbraille.width(text)
    return braille.width(text)


def height(text: str = "") -> float:
    return cnbraille.height() if uses_chinese_code(text) else braille.height()


def bbox(text: str, x: float, y: float) -> tuple[float, float, float, float]:
    return (x, y, width(text), height(text))


def safe_text(text: str) -> bool:
    """True when every character can be encoded without guessing a reading."""
    if not uses_chinese_code(text):
        return True
    try:
        cnbraille.encode_text(text)
    except cnbraille.BrailleError:
        return False
    return True


def unverified_segments(text: str, *, minimum: int = 4) -> list[str]:
    """Word runs the lexicon did not cover, so the blank cells are unchecked."""
    if not contains_chinese(text):
        return []
    try:
        return cnbraille.unverified_words(text, minimum=minimum)
    except cnbraille.BrailleError:
        return []