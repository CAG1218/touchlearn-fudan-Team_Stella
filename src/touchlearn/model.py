"""Typed view of the TouchLearn diagram contract.

The JSON file is the single source of truth.  Everything downstream --
layout, geometry, audio and the decision trace -- is derived from it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class DiagramError(ValueError):
    """Raised when a diagram cannot be parsed or fails validation."""


@dataclass(frozen=True)
class Canvas:
    size: str
    width_mm: float
    height_mm: float
    margin_mm: float = 5.0


@dataclass(frozen=True)
class Element:
    id: str
    kind: str
    label: str
    label_zh: str
    order: int
    semantics: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Relation:
    from_id: str
    to_id: str
    kind: str
    order: int = 0
    joint: str | None = None
    semantics: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Diagram:
    schema_version: str
    id: str
    title: str
    title_zh: str
    domain: str
    canvas: Canvas
    source: dict[str, Any]
    elements: tuple[Element, ...]
    relations: tuple[Relation, ...]
    layout: dict[str, Any]
    constraints: dict[str, Any]
    audio: dict[str, Any]
    raw: dict[str, Any]

    def element(self, element_id: str) -> Element:
        for item in self.elements:
            if item.id == element_id:
                return item
        raise DiagramError(f"unknown element id: {element_id}")

    def element_map(self) -> dict[str, Element]:
        return {item.id: item for item in self.elements}

    def connected_pairs(self) -> set[frozenset[str]]:
        pairs: set[frozenset[str]] = set()
        for relation in self.relations:
            pairs.add(frozenset((relation.from_id, relation.to_id)))
        return pairs

    def to_dict(self) -> dict[str, Any]:
        return self.raw


def _require(mapping: dict[str, Any], key: str, where: str) -> Any:
    if key not in mapping:
        raise DiagramError(f"{where}: missing required field '{key}'")
    return mapping[key]


def _number(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DiagramError(f"{where}: expected a number, got {type(value).__name__}")
    return float(value)


def parse_canvas(payload: dict[str, Any]) -> Canvas:
    size = str(_require(payload, "size", "canvas"))
    width = _number(_require(payload, "width_mm", "canvas"), "canvas.width_mm")
    height = _number(_require(payload, "height_mm", "canvas"), "canvas.height_mm")
    margin = _number(payload.get("margin_mm", 5.0), "canvas.margin_mm")
    if width <= 0 or height <= 0:
        raise DiagramError("canvas: width and height must be positive")
    if margin < 0 or margin * 2 >= min(width, height):
        raise DiagramError("canvas: margin is outside the canvas")
    return Canvas(size=size, width_mm=width, height_mm=height, margin_mm=margin)


def parse_elements(payload: Any) -> tuple[Element, ...]:
    if not isinstance(payload, list) or not payload:
        raise DiagramError("elements: expected a non-empty list")
    result: list[Element] = []
    seen: set[str] = set()
    for index, item in enumerate(payload):
        where = f"elements[{index}]"
        if not isinstance(item, dict):
            raise DiagramError(f"{where}: expected an object")
        element_id = str(_require(item, "id", where))
        if element_id in seen:
            raise DiagramError(f"{where}: duplicate element id '{element_id}'")
        seen.add(element_id)
        result.append(
            Element(
                id=element_id,
                kind=str(_require(item, "kind", where)),
                label=str(_require(item, "label", where)),
                label_zh=str(item.get("label_zh", item.get("label", element_id))),
                order=int(item.get("order", index + 1)),
                semantics=dict(item.get("semantics", {})),
            )
        )
    return tuple(sorted(result, key=lambda element: (element.order, element.id)))


def parse_relations(payload: Any, element_ids: set[str]) -> tuple[Relation, ...]:
    if payload is None:
        return ()
    if not isinstance(payload, list):
        raise DiagramError("relations: expected a list")
    result: list[Relation] = []
    for index, item in enumerate(payload):
        where = f"relations[{index}]"
        if not isinstance(item, dict):
            raise DiagramError(f"{where}: expected an object")
        from_id = str(_require(item, "from", where))
        to_id = str(_require(item, "to", where))
        if from_id not in element_ids:
            raise DiagramError(f"{where}: unknown from element '{from_id}'")
        if to_id not in element_ids:
            raise DiagramError(f"{where}: unknown to element '{to_id}'")
        result.append(
            Relation(
                from_id=from_id,
                to_id=to_id,
                kind=str(_require(item, "kind", where)),
                order=int(item.get("order", index + 1)),
                joint=item.get("joint"),
                semantics=dict(item.get("semantics", {})),
            )
        )
    return tuple(sorted(result, key=lambda relation: relation.order))


def diagram_from_dict(payload: dict[str, Any]) -> Diagram:
    if not isinstance(payload, dict):
        raise DiagramError("diagram: expected a JSON object")
    schema_version = str(_require(payload, "schema_version", "diagram"))
    diagram_id = str(_require(payload, "id", "diagram"))
    title = str(_require(payload, "title", "diagram"))
    title_zh = str(payload.get("title_zh", title))
    domain = str(_require(payload, "domain", "diagram"))
    canvas = parse_canvas(_require(payload, "canvas", "diagram"))
    source = dict(payload.get("source", {}))
    elements = parse_elements(_require(payload, "elements", "diagram"))
    relations = parse_relations(payload.get("relations"), {item.id for item in elements})
    layout = dict(_require(payload, "layout", "diagram"))
    constraints = dict(_require(payload, "constraints", "diagram"))
    audio = dict(_require(payload, "audio", "diagram"))

    if not diagram_id.strip():
        raise DiagramError("diagram.id must not be empty")
    if not title.strip():
        raise DiagramError("diagram.title must not be empty")
    if "engine" not in layout:
        raise DiagramError("layout.engine is required")
    if "intro" not in audio:
        raise DiagramError("audio.intro is required")

    return Diagram(
        schema_version=schema_version,
        id=diagram_id,
        title=title,
        title_zh=title_zh,
        domain=domain,
        canvas=canvas,
        source=source,
        elements=elements,
        relations=relations,
        layout=layout,
        constraints=constraints,
        audio=audio,
        raw=payload,
    )


def load_diagram(path: str | Path) -> Diagram:
    source = Path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DiagramError(f"diagram not found: {source}") from exc
    except json.JSONDecodeError as exc:
        raise DiagramError(f"invalid JSON in {source}: {exc}") from exc
    return diagram_from_dict(payload)
