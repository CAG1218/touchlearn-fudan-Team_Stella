"""Schema loading and validation.

The package is deliberately usable with the standard library only.  If
``jsonschema`` is installed, the full JSON Schema is enforced.  Otherwise a
small set of semantic checks still protects the pipeline from broken input.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .model import DiagramError

ALLOWED_ENGINES = {"kinematic_chain", "cartesian_plot"}
ALLOWED_DOMAINS = {"robotics", "math", "other"}
ALLOWED_ELEMENT_KINDS = {
    "link",
    "joint",
    "joint_group",
    "end_effector",
    "curve",
    "point",
    "axis",
    "label",
    "legend",
    "other",
}

A5_WIDTH = 148.0
A5_HEIGHT = 210.0


@dataclass(frozen=True)
class SchemaIssue:
    severity: str
    path: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"severity": self.severity, "path": self.path, "message": self.message}


def _schema_candidates() -> list[Path]:
    here = Path(__file__).resolve()
    return [
        Path.cwd() / "schemas" / "diagram.schema.json",
        here.parents[2] / "schemas" / "diagram.schema.json",
        here.parent / "data" / "diagram.schema.json",
    ]


def load_schema() -> dict[str, Any] | None:
    """Return the JSON Schema, or None when it cannot be located."""
    for candidate in _schema_candidates():
        if candidate.is_file():
            return json.loads(candidate.read_text(encoding="utf-8"))
    return None


def _json_path(error: Any) -> str:
    parts = [str(item) for item in error.absolute_path]
    return "$" + "".join(f"[{part}]" if part.isdigit() else f".{part}" for part in parts)


def _run_jsonschema(payload: dict[str, Any]) -> list[SchemaIssue]:
    try:
        import jsonschema  # type: ignore
    except ImportError:
        return []

    schema = load_schema()
    if schema is None:
        return [
            SchemaIssue(
                "warning",
                "$",
                "diagram.schema.json not found; only built-in semantic checks ran",
            )
        ]

    validator = jsonschema.Draft202012Validator(schema)
    issues: list[SchemaIssue] = []
    for error in sorted(validator.iter_errors(payload), key=lambda item: list(item.absolute_path)):
        issues.append(SchemaIssue("error", _json_path(error), error.message))
    return issues


def _manual_checks(payload: dict[str, Any]) -> list[SchemaIssue]:
    issues: list[SchemaIssue] = []

    def error(path: str, message: str) -> None:
        issues.append(SchemaIssue("error", path, message))

    def warning(path: str, message: str) -> None:
        issues.append(SchemaIssue("warning", path, message))

    required = [
        "schema_version",
        "id",
        "title",
        "domain",
        "canvas",
        "elements",
        "layout",
        "constraints",
        "audio",
    ]
    for key in required:
        if key not in payload:
            error("$", f"missing required field '{key}'")

    if payload.get("schema_version") != "0.1.0":
        error("$.schema_version", "expected schema_version '0.1.0'")

    domain = payload.get("domain")
    if domain not in ALLOWED_DOMAINS:
        error("$.domain", f"domain must be one of {sorted(ALLOWED_DOMAINS)}")

    canvas = payload.get("canvas")
    if isinstance(canvas, dict):
        width = canvas.get("width_mm")
        height = canvas.get("height_mm")
        size = canvas.get("size")
        if not isinstance(width, (int, float)) or not isinstance(height, (int, float)):
            error("$.canvas", "width_mm and height_mm must be numbers")
        elif size == "A5" and (abs(width - A5_WIDTH) > 0.01 or abs(height - A5_HEIGHT) > 0.01):
            error("$.canvas", "A5 must be 148 x 210 mm")
        margin = canvas.get("margin_mm", 5.0)
        if isinstance(margin, (int, float)) and isinstance(width, (int, float)):
            if margin * 2 >= min(width, height):
                error("$.canvas.margin_mm", "margin leaves no printable area")
    else:
        error("$.canvas", "canvas must be an object")

    elements = payload.get("elements")
    if not isinstance(elements, list) or not elements:
        error("$.elements", "elements must be a non-empty list")
        elements = []

    seen_ids: set[str] = set()
    seen_orders: set[int] = set()
    for index, item in enumerate(elements):
        where = f"$.elements[{index}]"
        if not isinstance(item, dict):
            error(where, "element must be an object")
            continue
        element_id = item.get("id")
        if not isinstance(element_id, str) or not element_id:
            error(f"{where}.id", "id must be a non-empty string")
        elif element_id in seen_ids:
            error(f"{where}.id", f"duplicate element id '{element_id}'")
        else:
            seen_ids.add(element_id)

        kind = item.get("kind")
        if kind not in ALLOWED_ELEMENT_KINDS:
            error(f"{where}.kind", f"unsupported kind '{kind}'")

        order = item.get("order")
        if not isinstance(order, int) or isinstance(order, bool) or order < 1:
            error(f"{where}.order", "order must be a positive integer")
        elif order in seen_orders:
            warning(f"{where}.order", f"order {order} is reused")
        else:
            seen_orders.add(order)

        if not isinstance(item.get("label"), str) or not item.get("label"):
            error(f"{where}.label", "label must be a non-empty string")

    relations = payload.get("relations", [])
    if not isinstance(relations, list):
        error("$.relations", "relations must be a list")
        relations = []
    for index, item in enumerate(relations):
        where = f"$.relations[{index}]"
        if not isinstance(item, dict):
            error(where, "relation must be an object")
            continue
        for key in ("from", "to"):
            value = item.get(key)
            if not isinstance(value, str) or not value:
                error(f"{where}.{key}", f"{key} must be a non-empty string")
            elif value not in seen_ids:
                error(f"{where}.{key}", f"unknown element '{value}'")
        if not isinstance(item.get("kind"), str) or not item.get("kind"):
            error(f"{where}.kind", "kind must be a non-empty string")

    layout = payload.get("layout")
    if isinstance(layout, dict):
        engine = layout.get("engine")
        if engine not in ALLOWED_ENGINES:
            error("$.layout.engine", f"engine must be one of {sorted(ALLOWED_ENGINES)}")
        if not isinstance(layout.get("options"), dict):
            error("$.layout.options", "options must be an object")
    else:
        error("$.layout", "layout must be an object")

    constraints = payload.get("constraints")
    if isinstance(constraints, dict):
        numeric = [
            "min_line_width_mm",
            "min_spacing_mm",
            "base_thickness_mm",
            "relief_height_mm",
            "braille_dot_mm",
            "braille_height_mm",
        ]
        for key in numeric:
            value = constraints.get(key)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                error(f"$.constraints.{key}", f"{key} must be a number")
        min_line = constraints.get("min_line_width_mm")
        if isinstance(min_line, (int, float)) and not isinstance(min_line, bool) and min_line < 1.0:
            error("$.constraints.min_line_width_mm", "line width below 1.0 mm is not printable")
        min_space = constraints.get("min_spacing_mm")
        if isinstance(min_space, (int, float)) and not isinstance(min_space, bool) and min_space < 4.0:
            error("$.constraints.min_spacing_mm", "spacing below 4.0 mm is not tactile-safe")
        relief = constraints.get("relief_height_mm")
        if isinstance(relief, (int, float)) and not isinstance(relief, bool) and not 0.4 <= relief <= 0.8:
            error("$.constraints.relief_height_mm", "relief must be within 0.4-0.8 mm")
    else:
        error("$.constraints", "constraints must be an object")

    audio = payload.get("audio")
    if isinstance(audio, dict):
        if not isinstance(audio.get("intro"), str) or not audio.get("intro"):
            error("$.audio.intro", "intro must be a non-empty string")
        steps = audio.get("steps")
        if not isinstance(steps, list) or not steps:
            error("$.audio.steps", "steps must be a non-empty list")
        elif not all(isinstance(step, str) and step for step in steps):
            error("$.audio.steps", "every step must be a non-empty string")
    else:
        error("$.audio", "audio must be an object")

    return issues


def validate_payload(payload: dict[str, Any], use_jsonschema: bool = True) -> list[SchemaIssue]:
    issues: list[SchemaIssue] = []
    if use_jsonschema:
        issues.extend(_run_jsonschema(payload))
    issues.extend(_manual_checks(payload))

    # De-duplicate identical messages produced by both layers.
    unique: list[SchemaIssue] = []
    seen: set[tuple[str, str, str]] = set()
    for issue in issues:
        key = (issue.severity, issue.path, issue.message)
        if key not in seen:
            seen.add(key)
            unique.append(issue)
    return unique


def has_errors(issues: list[SchemaIssue]) -> bool:
    return any(issue.severity == "error" for issue in issues)


def validate_or_raise(payload: dict[str, Any], source: str = "diagram") -> list[SchemaIssue]:
    issues = validate_payload(payload)
    if has_errors(issues):
        messages = "; ".join(f"{issue.path}: {issue.message}" for issue in issues if issue.severity == "error")
        raise DiagramError(f"{source} failed validation: {messages}")
    return issues
