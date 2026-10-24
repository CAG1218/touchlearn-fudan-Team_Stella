from __future__ import annotations

import json
import unittest
from copy import deepcopy

from tests import EXAMPLES
from touchlearn.schema import has_errors, validate_payload


def _load(name: str) -> dict:
    return json.loads((EXAMPLES / name / "diagram.json").read_text(encoding="utf-8"))


class SchemaTests(unittest.TestCase):
    def test_examples_validate_without_jsonschema(self) -> None:
        for name in ("robot_arm", "quadratic"):
            issues = validate_payload(_load(name), use_jsonschema=False)
            errors = [issue for issue in issues if issue.severity == "error"]
            self.assertEqual(errors, [], f"{name}: {errors}")

    def test_examples_validate_with_jsonschema_when_available(self) -> None:
        for name in ("robot_arm", "quadratic"):
            issues = validate_payload(_load(name), use_jsonschema=True)
            self.assertFalse(has_errors(issues), f"{name}: {issues}")

    def test_a5_size_is_enforced(self) -> None:
        payload = _load("robot_arm")
        payload["canvas"]["width_mm"] = 100
        issues = validate_payload(payload, use_jsonschema=False)
        self.assertTrue(any(issue.path == "$.canvas" for issue in issues))

    def test_duplicate_ids_are_reported(self) -> None:
        payload = _load("quadratic")
        payload["elements"].append(deepcopy(payload["elements"][0]))
        issues = validate_payload(payload, use_jsonschema=False)
        self.assertTrue(any("duplicate element id" in issue.message for issue in issues))

    def test_thin_lines_are_rejected(self) -> None:
        payload = _load("robot_arm")
        payload["constraints"]["min_line_width_mm"] = 0.5
        issues = validate_payload(payload, use_jsonschema=False)
        self.assertTrue(any(issue.path == "$.constraints.min_line_width_mm" for issue in issues))

    def test_unknown_engine_is_rejected(self) -> None:
        payload = _load("robot_arm")
        payload["layout"]["engine"] = "magic"
        issues = validate_payload(payload, use_jsonschema=False)
        self.assertTrue(any(issue.path == "$.layout.engine" for issue in issues))

    def test_non_numeric_constraint_does_not_crash(self) -> None:
        payload = _load("robot_arm")
        payload["constraints"]["min_line_width_mm"] = "wide"
        issues = validate_payload(payload, use_jsonschema=False)
        self.assertTrue(any(issue.path == "$.constraints.min_line_width_mm" for issue in issues))

    def test_package_data_schema_matches_repository_schema(self) -> None:
        from tests import ROOT

        repository = json.loads((ROOT / "schemas" / "diagram.schema.json").read_text(encoding="utf-8"))
        packaged = json.loads(
            (ROOT / "src" / "touchlearn" / "data" / "diagram.schema.json").read_text(encoding="utf-8")
        )
        self.assertEqual(repository, packaged)


if __name__ == "__main__":
    unittest.main()
