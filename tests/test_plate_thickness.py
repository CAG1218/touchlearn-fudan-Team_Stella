from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import EXAMPLES  # noqa: F401  (ensures src is on sys.path)

from touchlearn.agent.agent import (
    DEFAULT_CONSTRAINTS,
    TouchLearnAgent,
    _plate_constraints,
    _plate_thickness,
)
from touchlearn.agent.volumetric import read_stl_mesh


class PlateThicknessValueTests(unittest.TestCase):
    """The optional thin-plate thickness is the only user-facing height."""

    def test_accepts_one_and_two_millimetres(self) -> None:
        self.assertEqual(_plate_thickness(1), 1.0)
        self.assertEqual(_plate_thickness("2"), 2.0)
        self.assertEqual(_plate_thickness(1.5), 1.5)
        self.assertEqual(_plate_thickness(1.234), 1.23)

    def test_blank_keeps_the_durable_default(self) -> None:
        self.assertIsNone(_plate_thickness(None))
        self.assertIsNone(_plate_thickness(""))
        self.assertEqual(_plate_constraints(None)["base_thickness_mm"], DEFAULT_CONSTRAINTS["base_thickness_mm"])

    def test_rejects_out_of_range_or_non_numeric(self) -> None:
        for value in (0.9, 2.5, 3, -1):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    _plate_thickness(value)
        with self.assertRaises(ValueError):
            _plate_thickness("abc")

    def test_constraints_override_only_the_base(self) -> None:
        constraints = _plate_constraints(2)
        self.assertEqual(constraints["base_thickness_mm"], 2.0)
        for key in DEFAULT_CONSTRAINTS:
            if key != "base_thickness_mm":
                self.assertEqual(constraints[key], DEFAULT_CONSTRAINTS[key])
        # The default dict must not be mutated by a later call.
        self.assertEqual(DEFAULT_CONSTRAINTS["base_thickness_mm"], 3.0)


class ThinSquarePlateBuildTests(unittest.TestCase):
    """A 150×150×2 mm plate is the bulk-print case the user asked for."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.agent = TouchLearnAgent(output_root=Path(self.temp.name))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _build(self, thickness: object) -> tuple[Path, dict[str, object], dict[str, object]]:
        draft = self.agent.analyze(
            {
                "subject": "function",
                "expression": "x^2-2x-3",
                "paper": "自定义150×150",
                "thickness_mm": thickness,
            }
        )
        self.assertTrue(draft["ready"], msg=draft.get("questions"))
        self.assertEqual(draft["fields"].get("paper"), "自定义150×150")
        self.assertEqual(draft["fields"].get("thickness_mm"), float(thickness))
        with patch(
            "touchlearn.agent.agent.synthesize_speech", return_value=(False, "test environment", None)
        ):
            result = self.agent.build(draft["id"], public_base_url="http://127.0.0.1:8765")
        job_dir = Path(self.temp.name) / "jobs" / result["job_id"]
        report = json.loads((job_dir / "compliance.json").read_text(encoding="utf-8"))
        manifest = json.loads((job_dir / "manifest.json").read_text(encoding="utf-8"))
        return job_dir, report, manifest

    def test_two_millimetre_square_plate_prints_flat(self) -> None:
        job_dir, report, manifest = self._build(2)
        # Base 2.0 mm plus the 0.8 mm relief is the top of the printed part.
        mesh = read_stl_mesh(job_dir / "model.stl")
        bounds = mesh.bounds()
        self.assertAlmostEqual(bounds[3] - bounds[0], 150.0, places=1)
        self.assertAlmostEqual(bounds[4] - bounds[1], 150.0, places=1)
        self.assertAlmostEqual(bounds[5], 2.8, places=2)  # top surface
        self.assertEqual(report["metrics"]["base_thickness_mm"], 2.0)
        self.assertEqual(manifest["print"]["thickness_mm"], 2.0)
        self.assertIn("150", manifest["print"]["paper"])
        rules = {check["rule_id"]: check for check in report["checks"]}
        self.assertEqual(rules["plate.base_thickness"]["status"], "PASS")
        self.assertTrue(report["printable"])

    def test_thin_plate_is_reviewed_not_failed(self) -> None:
        job_dir, report, manifest = self._build(1.5)
        mesh = read_stl_mesh(job_dir / "model.stl")
        self.assertAlmostEqual(mesh.bounds()[5], 2.3, places=2)
        self.assertEqual(report["metrics"]["base_thickness_mm"], 1.5)
        self.assertEqual(manifest["print"]["thickness_mm"], 1.5)
        self.assertIn("薄板", manifest["print"]["notes"])
        rules = {check["rule_id"]: check for check in report["checks"]}
        self.assertEqual(rules["plate.base_thickness"]["status"], "NEEDS_REVIEW")
        self.assertTrue(report["printable"])
        statuses = {check["rule_id"]: check["status"] for check in report["checks"]}
        self.assertNotIn("FAIL", statuses.values())


if __name__ == "__main__":
    unittest.main()