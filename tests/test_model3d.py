from __future__ import annotations

import unittest

from tests import EXAMPLES  # noqa: F401  (ensures src is on sys.path)

from touchlearn.model3d import (
    build_model3d,
    companion_scene,
    compress_planet_radius,
    normalize_model_spec,
    solar_system_spec,
)


class SolarSystemModelTests(unittest.TestCase):
    def test_default_catalogue_has_seven_planets_in_order(self) -> None:
        spec = solar_system_spec("需要一个太阳系3D模型")
        planets = spec["planets"]
        self.assertEqual(spec["kind"], "solar_system")
        self.assertEqual(len(planets), 7)
        self.assertEqual([item["name_zh"] for item in planets[:3]], ["水星", "金星", "地球"])
        for item in planets:
            self.assertAlmostEqual(item["radius_mm"], compress_planet_radius(item["relative_radius"]), places=2)
        by_radius = sorted(planets, key=lambda item: item["relative_radius"])
        self.assertEqual([item["id"] for item in by_radius][-1], "jupiter")
        self.assertEqual([item["id"] for item in by_radius][0], "mercury")
        orbits = [item["orbit_mm"] for item in planets]
        self.assertEqual(orbits, sorted(orbits))

    def test_eight_planets_available_on_request(self) -> None:
        spec = solar_system_spec("做一个八大行星的太阳系模型")
        self.assertEqual(len(spec["planets"]), 8)

    def test_build_is_watertight_and_fits_build_volume(self) -> None:
        spec = solar_system_spec("太阳系3D模型")
        result = build_model3d(spec)
        self.assertTrue(result.mesh.is_watertight())
        width, depth, height = result.meta["size_mm"]
        self.assertLessEqual(width, 220.0 + 1e-6)
        self.assertLessEqual(depth, 220.0 + 1e-6)
        self.assertLessEqual(height, 250.0 + 1e-6)
        self.assertGreater(result.mesh.volume(), 0.0)

    def test_every_planet_has_a_chinese_braille_label(self) -> None:
        spec = solar_system_spec("太阳系3D模型")
        result = build_model3d(spec)
        labels = result.meta["braille"]["labels"]
        self.assertEqual(len(labels), 7)
        self.assertEqual([item["text"] for item in labels], [item["name_zh"] for item in spec["planets"]])
        self.assertTrue(all(item["cells"] for item in labels))

    def test_companion_scene_covers_the_same_planets(self) -> None:
        spec = solar_system_spec("太阳系3D模型")
        result = build_model3d(spec)
        scene = companion_scene(spec, result.meta)
        ids = [block["id"] for block in scene["blocks"]]
        self.assertEqual(len(ids), 1 + 1 + len(spec["planets"]))
        self.assertIn("orbits", ids)
        self.assertIn("sun", ids)

    def test_solar_uses_base_wall_legend_not_standing_flags(self) -> None:
        spec = solar_system_spec("太阳系3D模型")
        result = build_model3d(spec)
        meta = result.meta
        self.assertEqual(meta["standing_flags"], 0)
        self.assertEqual(meta["legend"]["placement"], "base_outer_wall")
        self.assertEqual(meta["braille"]["label_strategy"], "base_wall_legend_in_orbit_order")
        self.assertEqual(meta["legend"]["lines"], [item["name_zh"] for item in spec["planets"]])
        self.assertTrue(all(item["placement"] == "base_wall_legend" for item in meta["braille"]["labels"]))
        self.assertTrue(all(item["dots"] > 0 for item in meta["braille"]["labels"]))


class AssemblyModelTests(unittest.TestCase):
    def test_generic_spec_is_validated_without_inventing_parts(self) -> None:
        with self.assertRaises(Exception):
            normalize_model_spec({"kind": "assembly", "title": "EMPTY"})
        spec, warnings = normalize_model_spec(
            {
                "kind": "assembly",
                "title": "WATER",
                "title_zh": "水分子",
                "parts": [
                    {"id": "o", "name_zh": "氧原子", "shape": "sphere", "size": [30, 30, 30], "position": [50, 50, 10]},
                    {"id": "h1", "name_zh": "氢原子", "shape": "sphere", "size": [18, 18, 18], "position": [38, 50, 35]},
                ],
            }
        )
        self.assertEqual(spec["kind"], "assembly")
        self.assertEqual(len(spec["parts"]), 2)
        self.assertEqual(warnings, [])

    def test_manual_assembly_has_braille_and_base(self) -> None:
        spec = {
            "version": 1,
            "kind": "assembly",
            "title": "DEMO",
            "title_zh": "演示模型",
            "parts": [
                {"id": "core", "name_zh": "核心", "shape": "sphere", "size": [24, 24, 24], "position": [0, 0, 30]},
                {"id": "arm", "name_zh": "支架", "shape": "cylinder", "size": [6, 6, 26], "position": [18, 0, 18]},
            ],
        }
        result = build_model3d(spec, target_height_mm=130.0)
        self.assertTrue(result.mesh.is_watertight())
        self.assertEqual(result.meta["kind"], "assembly")
        labels = result.meta["braille"]["labels"]
        self.assertEqual(len(labels), 3)
        self.assertEqual([item["text"] for item in labels], ["演示模型", "核心", "支架"])
        self.assertTrue(all(item["cells"] for item in labels))
        self.assertLessEqual(max(result.meta["size_mm"][2], 0.0), 130.0 + 1e-6)

    def _build(self, title: str, parts: list[dict]) -> object:
        return build_model3d(
            {
                "version": 1,
                "kind": "assembly",
                "title": title,
                "title_zh": title,
                "parts": parts,
            }
        )

    def test_tail_wing_uses_a_profile_surface_not_a_plate_cluster(self) -> None:
        result = self._build(
            "尾翼",
            [
                {"id": "body", "name_zh": "机身", "shape": "cylinder", "size": [26, 26, 50], "position": [50, 50, 10]},
                {
                    "id": "wing",
                    "name_zh": "尾翼",
                    "shape": "profile",
                    "size": [60, 8, 40],
                    "position": [70, 50, 45],
                    "outline": [[0, 50], [20, 80], [60, 80], [100, 50], [60, 20], [20, 20]],
                },
            ],
        )
        self.assertTrue(result.mesh.is_watertight())
        self.assertEqual(result.meta["standing_flags"], 0)
        placements = {item["text"]: item["placement"] for item in result.meta["braille"]["labels"]}
        self.assertEqual(placements["尾翼"], "profile_face")
        self.assertEqual(placements["机身"], "wall")
        self.assertNotIn("flag", {p for p in placements.values()})
        self.assertLessEqual(result.meta["size_mm"][2], 250.0 + 1e-6)

    def test_cup_globe_and_lamp_recipes_compile(self) -> None:
        cases = {
            "水杯": (
                [
                    {"id": "wall", "name_zh": "杯身", "shape": "tube", "size": [30, 24, 50], "position": [50, 50, 20]},
                    {"id": "bottom", "name_zh": "杯底", "shape": "cylinder", "size": [28, 28, 4], "position": [50, 50, 2]},
                    {"id": "handle", "name_zh": "杯柄", "shape": "torus", "size": [20, 4, 4], "position": [72, 50, 28], "tilt_deg": 90},
                ],
                "wall",
            ),
            "地球仪": (
                [
                    {"id": "ball", "name_zh": "地球仪", "shape": "sphere", "size": [40, 40, 40], "position": [50, 50, 60]},
                    {"id": "stand", "name_zh": "支架", "shape": "cylinder", "size": [6, 6, 30], "position": [50, 50, 15]},
                    {"id": "ring", "name_zh": "子午圈", "shape": "torus", "size": [48, 3, 3], "position": [50, 50, 60], "tilt_deg": 90},
                    {"id": "base", "name_zh": "底座", "shape": "cylinder", "size": [26, 26, 6], "position": [50, 50, 3]},
                ],
                "sphere_cap",
            ),
            "台灯": (
                [
                    {"id": "base", "name_zh": "灯座", "shape": "box", "size": [36, 26, 4], "position": [50, 50, 2]},
                    {"id": "pole", "name_zh": "灯杆", "shape": "cylinder", "size": [4, 4, 40], "position": [50, 50, 24]},
                    {"id": "shade", "name_zh": "灯罩", "shape": "frustum", "size": [26, 12, 18], "position": [50, 50, 55]},
                    {"id": "bulb", "name_zh": "灯泡", "shape": "sphere", "size": [8, 8, 8], "position": [50, 50, 48]},
                ],
                "top",
            ),
        }
        for title, (parts, expected_placement) in cases.items():
            with self.subTest(title=title):
                result = self._build(title, parts)
                self.assertTrue(result.mesh.is_watertight())
                self.assertGreater(result.mesh.volume(), 0.0)
                self.assertEqual(result.meta["standing_flags"], 0)
                self.assertLessEqual(result.meta["size_mm"][2], 250.0 + 1e-6)
                placements = [item["placement"] for item in result.meta["braille"]["labels"]]
                self.assertIn(expected_placement, placements)
                self.assertNotIn("flag", placements)

    def test_surface_labels_keep_full_part_names(self) -> None:
        result = self._build(
            "水杯",
            [
                {"id": "wall", "name_zh": "杯身", "shape": "tube", "size": [30, 24, 50], "position": [50, 50, 20]},
                {"id": "bottom", "name_zh": "杯底", "shape": "cylinder", "size": [28, 28, 4], "position": [50, 50, 2]},
                {"id": "handle", "name_zh": "杯柄", "shape": "torus", "size": [20, 4, 4], "position": [72, 50, 28], "tilt_deg": 90},
            ],
        )
        texts = [item["text"] for item in result.meta["braille"]["labels"]]
        self.assertEqual(texts, ["水杯", "杯身", "杯底", "杯柄"])
        self.assertEqual(result.meta["legend"]["lines"], ["2杯底", "3杯柄"])
        self.assertTrue(all(item["cells"] for item in result.meta["braille"]["labels"]))

    def test_parts_are_capped_at_twelve_with_a_warning(self) -> None:
        parts = [
            {
                "id": f"p{index}",
                "name_zh": f"件{index}",
                "shape": "sphere",
                "size": [8, 8, 8],
                "position": [20 + (index % 4) * 15, 20 + (index // 4) * 15, 10],
            }
            for index in range(1, 14)
        ]
        spec, warnings = normalize_model_spec(
            {"version": 1, "kind": "assembly", "title": "MANY", "title_zh": "多部件", "parts": parts}
        )
        self.assertEqual(len(spec["parts"]), 12)
        self.assertEqual(len(warnings), 1)
        self.assertIn("13", warnings[0])


if __name__ == "__main__":
    unittest.main()
