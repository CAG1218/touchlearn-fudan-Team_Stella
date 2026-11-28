from __future__ import annotations

import unittest

from tests import EXAMPLES  # noqa: F401  (ensures src is on sys.path)

from touchlearn.agent.reconstruct import ReconstructionPlan
from touchlearn.agent.reconstruct_print import compose_printable
from touchlearn.agent.volumetric import _box_xy
from touchlearn.geometry import Mesh
from touchlearn.model3d import build_model3d, solar_system_spec


def _box(width: float, depth: float, height: float) -> Mesh:
    mesh = Mesh([])
    _box_xy(mesh, -width / 2.0, -depth / 2.0, width, depth, 0.0, height)
    return mesh


def _top_z_at(mesh: Mesh, x: float, y: float) -> float | None:
    """Highest surface directly above ``(x, y)``, or None if nothing covers it."""
    best: float | None = None
    for (x1, y1, z1), (x2, y2, z2), (x3, y3, z3) in mesh.triangles:
        denominator = (y2 - y3) * (x1 - x3) + (x3 - x2) * (y1 - y3)
        if abs(denominator) < 1e-12:
            continue
        first = ((y2 - y3) * (x - x3) + (x3 - x2) * (y - y3)) / denominator
        second = ((y3 - y1) * (x - x3) + (x1 - x3) * (y - y3)) / denominator
        third = 1.0 - first - second
        if first < -1e-9 or second < -1e-9 or third < -1e-9:
            continue
        z = first * z1 + second * z2 + third * z3
        if best is None or z > best:
            best = z
    return best


class QrPadFlatnessTests(unittest.TestCase):
    """The audio QR is a sticker, so the pad underneath must be one flat plane.

    A raised "QR" mark kept the sticker off the surface along its edges and
    printed as unreadable fragments at label scale.  Every base builder now
    leaves the pad flat; these tests sample the printed top surface instead of
    trusting the meta flag.
    """

    def _assert_flat_pad(self, mesh: Mesh, meta: dict) -> None:
        self.assertEqual(meta["qr_pad_surface"], "flat")
        corner_x, corner_y = meta["qr_pad_corner_mm"]
        size = float(meta["qr_pad_mm"])
        inset = 3.5  # clear the 2 mm chamfer and its sloped face
        span = size - 2.0 * inset
        heights = []
        for column in range(4):
            for row in range(4):
                x = corner_x + inset + span * column / 3.0
                y = corner_y + inset + span * row / 3.0
                heights.append(_top_z_at(mesh, x, y))
        self.assertTrue(all(value is not None for value in heights), heights)
        low, high = min(heights), max(heights)
        self.assertLessEqual(high - low, 0.01, f"QR 平面不平：{sorted(set(heights))}")

    def test_reconstruction_base_has_a_flat_qr_pad(self) -> None:
        plan = ReconstructionPlan(ok=True, title="网球", height_mm=60.0)
        result = compose_printable(_box(30.0, 30.0, 60.0), plan)
        self._assert_flat_pad(result.mesh, result.meta)

    def test_solar_system_base_has_a_flat_qr_pad(self) -> None:
        result = build_model3d(solar_system_spec("太阳系3D模型"))
        self._assert_flat_pad(result.mesh, result.meta)

    def test_assembly_base_has_a_flat_qr_pad(self) -> None:
        result = build_model3d(
            {
                "version": 1,
                "kind": "assembly",
                "title": "DEMO",
                "title_zh": "演示模型",
                "parts": [
                    {"id": "core", "name_zh": "核心", "shape": "sphere", "size": [24, 24, 24], "position": [0, 0, 30]},
                    {"id": "arm", "name_zh": "支架", "shape": "cylinder", "size": [6, 6, 26], "position": [18, 0, 18]},
                ],
            },
            target_height_mm=130.0,
        )
        self._assert_flat_pad(result.mesh, result.meta)


if __name__ == "__main__":
    unittest.main()