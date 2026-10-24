"""Decision trace written next to every generated plate."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


@dataclass
class TraceEvent:
    stage: str
    action: str
    decision: str
    reason: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "stage": self.stage,
            "action": self.action,
            "decision": self.decision,
            "reason": self.reason,
            "evidence": self.evidence,
        }


class Trace:
    def __init__(self) -> None:
        self.events: list[TraceEvent] = []

    def add(self, stage: str, action: str, decision: str, reason: str = "", evidence: dict[str, Any] | None = None) -> None:
        self.events.append(
            TraceEvent(stage=stage, action=action, decision=decision, reason=reason, evidence=evidence or {})
        )

    def add_layout(self, events: Iterable[dict[str, Any]]) -> None:
        for event in events:
            self.add(
                stage=str(event.get("stage", "layout")),
                action=str(event.get("action", "decision")),
                decision=str(event.get("decision", "")),
                reason=str(event.get("reason", "")),
                evidence=dict(event.get("evidence", {})),
            )

    def to_list(self) -> list[dict[str, Any]]:
        return [event.to_dict() for event in self.events]

    def write_jsonl(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8", newline="\n") as handle:
            for event in self.events:
                handle.write(json.dumps(event.to_dict(), ensure_ascii=False, separators=(",", ":")) + "\n")
        return target
