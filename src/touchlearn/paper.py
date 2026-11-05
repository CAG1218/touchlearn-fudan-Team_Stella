"""Paper sizing for tactile plates.

The product never prints below A5.  A simple plate keeps A5; denser scenes and
larger robot models step up to A4 or A3 so the same tactile feature sizes do not
collapse into each other.  The caller may always ask for an explicit size.
"""

from __future__ import annotations

from typing import Any

PAPER_SIZES: dict[str, tuple[float, float]] = {
    "A5": (148.0, 210.0),
    "A4": (210.0, 297.0),
    "A3": (297.0, 420.0),
}
MIN_PAPER = "A5"


def paper_size(name: str | None) -> tuple[float, float]:
    key = str(name or MIN_PAPER).strip().upper()
    return PAPER_SIZES.get(key, PAPER_SIZES[MIN_PAPER])


def paper_name(width: float, height: float) -> str:
    """Return the closest catalogue name for a plate footprint."""
    best = MIN_PAPER
    best_error = float("inf")
    for name, (paper_width, paper_height) in PAPER_SIZES.items():
        error = abs(width - paper_width) + abs(height - paper_height)
        if error < best_error:
            best = name
            best_error = error
    return best


def choose_paper(
    *,
    requested: Any = "",
    blocks: int = 0,
    primitives: int = 0,
    chars: int = 0,
    dof: int = 0,
) -> str:
    """Pick A5/A4/A3 from content density.

    ``requested`` wins when it is a known paper name.  Otherwise the score is a
    deliberately conservative proxy: normal worksheets stay A5, crowded scenes
    and high-DOF arms grow, and only genuinely large work goes to A3.
    """
    key = str(requested or "").strip().upper()
    if key in PAPER_SIZES:
        return key
    score = blocks * 1.6 + primitives * 0.32 + chars / 120.0 + dof * 1.2
    if blocks <= 6 and primitives <= 30 and chars <= 700 and dof <= 4:
        return "A5"
    if blocks <= 10 and primitives <= 46 and chars <= 1500 and dof <= 6:
        return "A4"
    return "A3"


def canvas_for(name: str, margin_mm: float = 6.0) -> dict[str, Any]:
    width, height = paper_size(name)
    return {"size": name, "width_mm": width, "height_mm": height, "margin_mm": margin_mm}
