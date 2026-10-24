"""Unified English Braille (UEB) encoding and cell geometry.

The v0 product uses English letters and numbers on the plate and Chinese
narration in the audio track.  Chinese Braille is intentionally out of scope.
"""

from __future__ import annotations

# Dot numbering:
#   1 4
#   2 5
#   3 6
LETTERS: dict[str, tuple[int, ...]] = {
    "a": (1,),
    "b": (1, 2),
    "c": (1, 4),
    "d": (1, 4, 5),
    "e": (1, 5),
    "f": (1, 2, 4),
    "g": (1, 2, 4, 5),
    "h": (1, 2, 5),
    "i": (2, 4),
    "j": (2, 4, 5),
    "k": (1, 3),
    "l": (1, 2, 3),
    "m": (1, 3, 4),
    "n": (1, 3, 4, 5),
    "o": (1, 3, 5),
    "p": (1, 2, 3, 4),
    "q": (1, 2, 3, 4, 5),
    "r": (1, 2, 3, 5),
    "s": (2, 3, 4),
    "t": (2, 3, 4, 5),
    "u": (1, 3, 6),
    "v": (1, 2, 3, 6),
    "w": (2, 4, 5, 6),
    "x": (1, 3, 4, 6),
    "y": (1, 3, 4, 5, 6),
    "z": (1, 3, 5, 6),
}

DIGITS: dict[str, str] = {
    "1": "a",
    "2": "b",
    "3": "c",
    "4": "d",
    "5": "e",
    "6": "f",
    "7": "g",
    "8": "h",
    "9": "i",
    "0": "j",
}

PUNCTUATION: dict[str, tuple[int, ...]] = {
    "-": (3, 6),
    ".": (2, 5, 6),
    ":": (2, 5),
    "/": (3, 4),
    "(": (1, 2, 3, 5, 6),
    ")": (2, 3, 4, 5, 6),
    "^": (4, 5),
    "+": (3, 4, 6),
    "=": (2, 3, 5, 6),
    "_": (4, 5, 6),
    "?": (2, 3, 6),
}

CAPITAL_SIGN = (6,)
NUMBER_SIGN = (3, 4, 5, 6)

DOT_DIAMETER_MM = 1.5
DOT_SPACING_MM = 2.5
CELL_SPACING_MM = 6.0
CELL_WIDTH_MM = 2.5 + DOT_DIAMETER_MM
CELL_HEIGHT_MM = 5.0 + DOT_DIAMETER_MM


def encode(text: str) -> list[tuple[int, ...]]:
    """Encode ASCII text as a list of braille cells (dot-number tuples)."""
    cells: list[tuple[int, ...]] = []
    previous_was_digit = False
    for character in text:
        if character.isdigit():
            if not previous_was_digit:
                cells.append(NUMBER_SIGN)
            cells.append(LETTERS[DIGITS[character]])
            previous_was_digit = True
            continue
        previous_was_digit = False
        if character == " ":
            cells.append(())
            continue
        if character.isalpha():
            if character.isupper():
                cells.append(CAPITAL_SIGN)
            cells.append(LETTERS[character.lower()])
            continue
        cells.append(PUNCTUATION.get(character, PUNCTUATION["?"]))
    return cells


def width(text: str) -> float:
    cells = encode(text)
    if not cells:
        return 0.0
    return CELL_SPACING_MM * (len(cells) - 1) + CELL_WIDTH_MM


def height() -> float:
    return CELL_HEIGHT_MM


def dot_centers(text: str, x: float, y: float) -> list[tuple[float, float]]:
    """Return dot centres for ``text``.

    ``x`` and ``y`` are the top-left corner of the braille bounding box.
    """
    cells = encode(text)
    radius = DOT_DIAMETER_MM / 2.0
    centres: list[tuple[float, float]] = []
    for cell_index, cell in enumerate(cells):
        cell_x = x + cell_index * CELL_SPACING_MM + radius
        cell_y = y + radius
        for dot in cell:
            column = 1 if dot >= 4 else 0
            row = (dot - 1) % 3
            centres.append((cell_x + column * DOT_SPACING_MM, cell_y + row * DOT_SPACING_MM))
    return centres


def bbox(text: str, x: float, y: float) -> tuple[float, float, float, float]:
    return (x, y, width(text), height())
