from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tests import EXAMPLES
from touchlearn.model import DiagramError, diagram_from_dict, load_diagram


class ModelTests(unittest.TestCase):
    def test_two_cases_load(self) -> None:
        robot = load_diagram(EXAMPLES / "robot_arm" / "diagram.json")
        quadratic = load_diagram(EXAMPLES / "quadratic" / "diagram.json")
        self.assertEqual(robot.id, "robot-arm-6axis")
        self.assertEqual(quadratic.id, "quadratic-y-x2-2x-3")
        self.assertEqual(len(robot.elements), 8)
        self.assertEqual(len(quadratic.elements), 8)
        self.assertEqual(len(robot.relations), 7)
        self.assertEqual(len(quadratic.relations), 6)

    def test_elements_are_sorted_by_order(self) -> None:
        robot = load_diagram(EXAMPLES / "robot_arm" / "diagram.json")
        orders = [element.order for element in robot.elements]
        self.assertEqual(orders, sorted(orders))
        self.assertEqual(robot.elements[0].id, "BASE")

    def test_connected_pairs(self) -> None:
        quadratic = load_diagram(EXAMPLES / "quadratic" / "diagram.json")
        pairs = quadratic.connected_pairs()
        self.assertIn(frozenset(("origin", "x_axis")), pairs)
        self.assertIn(frozenset(("vertex", "curve")), pairs)
        self.assertEqual(len(pairs), 6)

    def test_unknown_element_raises(self) -> None:
        robot = load_diagram(EXAMPLES / "robot_arm" / "diagram.json")
        with self.assertRaises(DiagramError):
            robot.element("nope")

    def test_invalid_json_raises(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaises(DiagramError):
                load_diagram(path)

    def test_duplicate_element_id_raises(self) -> None:
        payload = json.loads((EXAMPLES / "quadratic" / "diagram.json").read_text(encoding="utf-8"))
        payload["elements"].append(dict(payload["elements"][0]))
        with self.assertRaises(DiagramError):
            diagram_from_dict(payload)

    def test_relation_endpoint_must_exist(self) -> None:
        payload = json.loads((EXAMPLES / "quadratic" / "diagram.json").read_text(encoding="utf-8"))
        payload["relations"][0]["to"] = "missing"
        with self.assertRaises(DiagramError):
            diagram_from_dict(payload)


if __name__ == "__main__":
    unittest.main()
