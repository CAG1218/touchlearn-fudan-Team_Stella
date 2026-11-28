"""Paper sizing for tactile plates.

The product never prints below A5.  A simple plate keeps A5; denser scenes and
larger robot models step up so the same tactile feature sizes do not collapse
into each other.  Braille decides the floor: a title and legend that need more
width or more rows than A5 can hold move the plate to the next size.  The
caller may always ask for an explicit size, including B5/B4 and Chinese 16K.
"""

from __future__ import annotations

import re
from typing import Any

# Ordered by usable area.  ISO A/B sizes are portrait millimetres; 16K is the
# common Chinese 185 x 260 mm book size.
PAPER_SIZES: dict[str, tuple[float, float]] = {
    "A5": (148.0, 210.0),
    "B5": (176.0, 250.0),
    "16K": (185.0, 260.0),
    "A4": (210.0, 297.0),
    "B4": (250.0, 353.0),
    "A3": (297.0, 420.0),
}
PAPER_ORDER: tuple[str, ...] = tuple(PAPER_SIZES)
MIN_PAPER = "A5"

# Free-form plates stay inside what an ordinary office printer or a 220 mm
# FDM bed can actually take.  The short side keeps the A5 width because the
# braille legend needs that much room; the long side stops at A3, above which
# the plate is a poster rather than a worksheet a student can carry.  Square
# plates are allowed (150 x 150 mm is a common bulk-print size), so there is no
# separate area floor: whether the content actually fits is decided by the
# layout and reported by the compliance checks, not guessed from an area.
CUSTOM_MIN_SIDE_MM = 148.0
CUSTOM_MAX_SIDE_MM = 420.0
CUSTOM_PREFIX = "自定义"
_CUSTOM_RE = re.compile(
    r"(?:自定义|CUSTOM)[\s:：\-]*(\d{2,4}(?:\.\d+)?)\s*[x×*]\s*(\d{2,4}(?:\.\d+)?)",
    re.IGNORECASE,
)


def custom_name(width: float, height: float) -> str:
    """Canonical size label for a free-form plate."""
    return f"{CUSTOM_PREFIX}{width:g}×{height:g}"


def custom_size(value: object) -> tuple[float, float] | None:
    """Parse a ``自定义200×280`` label; ``None`` when absent or out of bounds."""
    match = _CUSTOM_RE.search(str(value or ""))
    if not match:
        return None
    width, height = float(match.group(1)), float(match.group(2))
    short_side, long_side = sorted((width, height))
    if short_side < CUSTOM_MIN_SIDE_MM - 1e-6:
        return None
    if long_side > CUSTOM_MAX_SIDE_MM + 1e-6:
        return None
    return width, height


def is_custom(value: object) -> bool:
    return str(value or "").startswith(CUSTOM_PREFIX)

# Braille geometry used only to estimate layout demand before the plate is
# built.  The real dots come from cnbraille; this module must not import it to
# avoid a cycle through layout code.
BRAILLE_LINE_MM = 8.6          # cell height 6.8 plus one line gap 1.8
BRAILLE_SIDE_MM = 14.0         # two 6 mm margins plus breathing room
BRAILLE_HEADER_MM = 42.0       # title row, QR reserve and footer
BRAILLE_COLUMN_GAP_MM = 6.0    # slack between the widest label and the edge


def paper_size(name: str | None) -> tuple[float, float]:
    parsed = custom_size(name)
    if parsed:
        return parsed
    key = str(name or MIN_PAPER).strip().upper()
    return PAPER_SIZES.get(key, PAPER_SIZES[MIN_PAPER])


def paper_name(width: float, height: float) -> str:
    """Return a catalogue name, or a custom label when nothing is close."""
    best = MIN_PAPER
    best_error = float("inf")
    for name, (paper_width, paper_height) in PAPER_SIZES.items():
        error = abs(width - paper_width) + abs(height - paper_height)
        if error < best_error:
            best = name
            best_error = error
    if best_error > 2.0:
        return custom_name(width, height)
    return best


def paper_options() -> list[dict[str, Any]]:
    """Catalogue for the UI: increasing size, with millimetre dimensions."""
    return [
        {"name": name, "width_mm": width, "height_mm": height}
        for name, (width, height) in PAPER_SIZES.items()
    ]


_ALIASES = {
    "16开": "16K",
    "十六开": "16K",
    "大16开": "16K",
    "B5纸": "B5",
    "B4纸": "B4",
    "A5纸": "A5",
    "A4纸": "A4",
    "A3纸": "A3",
    "16K纸": "16K",
}
_EXPLICIT = re.compile(r"(?<![A-Z0-9])(A5|A4|A3|B5|B4|16K)(?![A-Z0-9])", re.IGNORECASE)


def parse_paper(text: Any) -> str:
    """Return a catalogue name, or ``""`` when the text names no paper.

    Accepts ``A4``, ``a4纸``, ``16开`` and ``B4 尺寸``.  A bare letter-number
    in free text is only trusted when it is a whole token, so model names such
    as ``A4Mini`` do not silently change the sheet.
    """
    value = str(text or "").strip()
    if not value:
        return ""
    compact = value.replace(" ", "").upper()
    if compact in PAPER_SIZES:
        return compact
    if compact in _ALIASES:
        return _ALIASES[compact]
    for alias, name in _ALIASES.items():
        if compact == alias.upper():
            return name
    match = _EXPLICIT.search(value)
    if match:
        name = match.group(1).upper()
        if name in PAPER_SIZES:
            return name
    if _CUSTOM_RE.search(value):
        parsed = custom_size(value)
        if parsed:
            return custom_name(*parsed)
        # Only an explicit "自定义…" request is an error worth surfacing; a
        # stray "200x280" in prose should fall back to automatic sizing.
        if re.match(rf"^\s*(?:{CUSTOM_PREFIX}|CUSTOM)", value, re.IGNORECASE):
            raise ValueError(
                "自定义尺寸的短边需不小于 148 mm（A5 宽度），长边不超过 420 mm（A3 长度）；"
                "超出范围请改用 A3 或分块打印。"
            )
    return ""


def _legacy_size(blocks: int, primitives: int, chars: int, dof: int) -> str:
    if blocks <= 6 and primitives <= 30 and chars <= 700 and dof <= 4:
        return "A5"
    if blocks <= 10 and primitives <= 46 and chars <= 1500 and dof <= 6:
        return "A4"
    return "A3"


def _index(name: str) -> int:
    try:
        return PAPER_ORDER.index(name)
    except ValueError:
        return 0


def choose_paper(
    *,
    requested: Any = "",
    blocks: int = 0,
    primitives: int = 0,
    chars: int = 0,
    dof: int = 0,
    braille_chars: int = 0,
    braille_rows: int = 0,
    braille_width_mm: float = 0.0,
    margin_mm: float = 6.0,
) -> str:
    """Pick the smallest sheet that holds the content and its braille.

    ``requested`` wins when it names a catalogue size.  Otherwise density sets
    a starting size, then the estimate of printed Chinese braille pushes it up
    until every row fits inside the printable area.  Braille never shrinks the
    1.6 mm dots, so growing the sheet is the only honest fix.
    """
    key = parse_paper(requested)
    if key:
        return key

    base = _legacy_size(blocks, primitives, chars, dof)
    if braille_chars:
        # Character count alone is a weak signal; width and rows are physical.
        extra_rows = max(0, int(braille_chars / 18))
        braille_rows = max(braille_rows, min(6, extra_rows))

    index = _index(base)
    while index < len(PAPER_ORDER):
        name = PAPER_ORDER[index]
        width, height = PAPER_SIZES[name]
        usable_width = width - 2.0 * margin_mm
        usable_height = height - 2.0 * margin_mm
        if braille_rows:
            need_height = braille_rows * BRAILLE_LINE_MM + BRAILLE_HEADER_MM
            if need_height > usable_height:
                index += 1
                continue
        if braille_width_mm:
            need_width = braille_width_mm + BRAILLE_COLUMN_GAP_MM + 2.0 * margin_mm
            if need_width > usable_width:
                index += 1
                continue
        return name
    return PAPER_ORDER[-1]


def canvas_for(name: str, margin_mm: float = 6.0) -> dict[str, Any]:
    width, height = paper_size(name)
    return {
        "size": name,
        "width_mm": width,
        "height_mm": height,
        "margin_mm": margin_mm,
        "custom": is_custom(name),
    }
