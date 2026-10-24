from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tests import EXAMPLES
from touchlearn.audio import build_script, write_script
from touchlearn.cli import main
from touchlearn.geometry import build_mesh
from touchlearn.layout import build_layout
from touchlearn.model import load_diagram
from touchlearn.stl import read_stl_info, sha256_file, write_binary_stl
from touchlearn.trace import Trace


class AudioTraceStlTests(unittest.TestCase):
    def test_audio_script_contains_the_narrative(self) -> None:
        diagram = load_diagram(EXAMPLES / "quadratic" / "diagram.json")
        script = build_script(diagram)
        self.assertIn(diagram.title_zh, script)
        for step in diagram.audio["steps"]:
            self.assertIn(step, script)
        self.assertIn("停顿", script)
        self.assertIn("制作说明", script)

    def test_audio_script_is_written(self) -> None:
        diagram = load_diagram(EXAMPLES / "robot_arm" / "diagram.json")
        with tempfile.TemporaryDirectory() as directory:
            path = write_script(diagram, Path(directory) / "audio.txt")
            self.assertTrue(path.is_file())
            self.assertGreater(path.stat().st_size, 100)

    def test_trace_jsonl_round_trip(self) -> None:
        trace = Trace()
        trace.add("test", "act", "decision", "reason", {"value": 1})
        trace.add_layout([{"stage": "layout", "action": "place", "decision": "x", "reason": "y"}])
        with tempfile.TemporaryDirectory() as directory:
            path = trace.write_jsonl(Path(directory) / "trace.jsonl")
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual(json.loads(lines[0])["stage"], "test")
            self.assertEqual(json.loads(lines[1])["stage"], "layout")

    def test_binary_stl_round_trip(self) -> None:
        layout = build_layout(load_diagram(EXAMPLES / "robot_arm" / "diagram.json"))
        mesh = build_mesh(layout)
        with tempfile.TemporaryDirectory() as directory:
            path = write_binary_stl(mesh, Path(directory) / "model.stl")
            info = read_stl_info(path)
            self.assertEqual(info["triangles"], mesh.triangle_count())
            self.assertTrue(str(info["header"]).startswith("TouchLearn"))
            self.assertEqual(info["bytes"], 84 + mesh.triangle_count() * 50)
            self.assertEqual(len(sha256_file(path)), 64)
            second = write_binary_stl(mesh, Path(directory) / "model2.stl")
            self.assertEqual(sha256_file(path), sha256_file(second))


class CliTests(unittest.TestCase):
    def test_validate_and_check_examples(self) -> None:
        for name in ("robot_arm", "quadratic"):
            diagram = str(EXAMPLES / name / "diagram.json")
            self.assertEqual(main(["validate", diagram]), 0)
            self.assertEqual(main(["check", diagram]), 0)

    def test_all_writes_the_expected_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "build"
            code = main(["all", str(EXAMPLES / "quadratic" / "diagram.json"), "--out", str(out)])
            self.assertEqual(code, 0)
            for name in ("model.stl", "preview.svg", "legend.svg", "report.json", "trace.jsonl", "audio.txt", "diagram.json"):
                self.assertTrue((out / name).is_file(), name)
            report = json.loads((out / "report.json").read_text(encoding="utf-8"))
            self.assertTrue(report["passed"])

    def test_missing_file_returns_code_two(self) -> None:
        self.assertEqual(main(["validate", "does-not-exist.json"]), 2)


if __name__ == "__main__":
    unittest.main()
