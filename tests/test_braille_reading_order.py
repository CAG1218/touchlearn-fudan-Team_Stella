"""Reading orientation and line order of the Chinese braille on every product.

The encoder (``cnbraille``) is shared, but each surface puts the dots on a
different plane.  A glyph that reads correctly in the y-down SVG preview is
mirrored when it is raised on a horizontal plaque a reader approaches from -Y,
and a multi-line block has to start at the far edge.  These tests pin both
rules down so a later layout change cannot silently turn the braille upside
down again.
"""

from __future__ import annotations

import unittest

from tests import EXAMPLES  # noqa: F401  (ensures src is on sys.path)

from touchlearn import cnbraille, model3d
from touchlearn.agent import reconstruct_print
from touchlearn.agent.parametric import _footer
from touchlearn.agent.reconstruct import ReconstructionPlan
from touchlearn.agent.reconstruct_print import compose_printable
from touchlearn.geometry import Mesh
from touchlearn.layout import _Builder
from touchlearn.agent.volumetric import _box_xy


def _box(width: float, depth: float, height: float) -> Mesh:
    mesh = Mesh([])
    _box_xy(mesh, -width / 2.0, -depth / 2.0, width, depth, 0.0, height)
    return mesh


class TopFaceBrailleTests(unittest.TestCase):
    """``网`` is ⠶⠄: a 2-row cell plus a lone dot-3 cell."""

    def setUp(self) -> None:
        self.calls: list[tuple[tuple[float, float, float], tuple[float, float, float], float]] = []
        self._original = model3d._cylinder

        def record(mesh, start, end, radius, segments=12):  # noqa: ANN001
            self.calls.append((start, end, radius))

        model3d._cylinder = record

    def tearDown(self) -> None:
        model3d._cylinder = self._original

    def test_dot_rows_are_mirrored_towards_the_reader_at_minus_y(self) -> None:
        model3d._braille_on_top(Mesh([]), "网球", 0.0, 0.0, 0.0, 0.8)
        expected = cnbraille.dot_centers("网球", 0.0, 0.0)
        lift = cnbraille.height()
        self.assertEqual(len(self.calls), len(expected))
        for (dot_x, dot_y), (start, _end, radius) in zip(expected, self.calls):
            self.assertAlmostEqual(start[0], dot_x)
            self.assertAlmostEqual(start[1], lift - dot_y)
            self.assertAlmostEqual(radius, cnbraille.DOT_DIAMETER_MM / 2.0)

    def test_tone_cell_lands_on_the_near_bottom_row(self) -> None:
        """Dot 3 is the bottom row, which is nearest a reader at -Y."""

        model3d._braille_on_top(Mesh([]), "网球", 0.0, 0.0, 0.0, 0.8)
        cells = cnbraille.encode_text("网球")
        tone_index = cells.index((3,))
        left = tone_index * cnbraille.CELL_SPACING_MM
        tone_dots = [
            start
            for start, _end, _radius in self.calls
            if left - 1e-6 <= start[0] < left + cnbraille.CELL_SPACING_MM
        ]
        self.assertEqual(len(tone_dots), 1)
        self.assertAlmostEqual(tone_dots[0][1], cnbraille.DOT_DIAMETER_MM / 2.0)

    def test_dots_stand_a_full_cell_height_proud(self) -> None:
        model3d._braille_on_top(Mesh([]), "网", 0.0, 0.0, 2.1, 3.0)
        self.assertTrue(all(abs(end[2] - 3.0) < 1e-9 for _start, end, _radius in self.calls))
        self.assertTrue(all(abs(start[2] - 2.0) < 1e-9 for start, _end, _radius in self.calls))


class LegendPanelOrderTests(unittest.TestCase):
    def test_first_legend_line_sits_at_the_far_edge(self) -> None:
        seen: list[tuple[str, float, float]] = []
        original = model3d._braille_on_top

        def record(mesh, text, x, y, z0, z1):  # noqa: ANN001
            seen.append((text, y, z1))

        model3d._braille_on_top = record
        try:
            model3d._legend_panel(
                Mesh([]),
                ["1 球面", "2 缝线"],
                left=-40.0,
                front=-60.0,
                width=80.0,
                max_depth=30.0,
                top=10.0,
            )
        finally:
            model3d._braille_on_top = original
        self.assertEqual([item[0] for item in seen], ["1 球面", "2 缝线"])
        self.assertGreater(seen[0][1], seen[1][1])
        self.assertAlmostEqual(seen[0][2], 10.0 + 1.2 + model3d.BRAILLE_DOT_HEIGHT_MM)


class ReconstructionTitleOrderTests(unittest.TestCase):
    def _compose(self, lines: list[str]):
        seen: list[tuple[str, float, float, float]] = []
        original_top = reconstruct_print._braille_on_top
        original_wrap = reconstruct_print._wrap_braille

        def record(mesh, text, x, y, z0, z1):  # noqa: ANN001
            seen.append((text, y, z0, z1))

        reconstruct_print._braille_on_top = record
        # Pin the wrapping so the test measures the line placement, not how a
        # particular title happens to split on a particular plaque width.
        reconstruct_print._wrap_braille = lambda text, width: (list(lines), False)
        try:
            result = compose_printable(
                _box(100.0, 100.0, 100.0),
                ReconstructionPlan(ok=True, title="网球拍柄", labels=[{"text": "缝线"}]),
                target_height_mm=60.0,
                title="网球拍柄",
            )
        finally:
            reconstruct_print._braille_on_top = original_top
            reconstruct_print._wrap_braille = original_wrap
        return result, seen

    def test_two_line_title_reads_from_the_far_edge(self) -> None:
        result, seen = self._compose(["网球", "拍柄"])
        lines = result.meta["braille"]["title_lines"]
        title_seen = seen
        self.assertEqual(lines, ["网球", "拍柄"])
        self.assertEqual(len(title_seen), 2)
        self.assertGreater(title_seen[0][1], title_seen[1][1])
        self.assertAlmostEqual(result.meta["braille"]["title_line_pitch_mm"], 6.8 + 1.8)
        for _text, _y, z0, z1 in title_seen:
            self.assertAlmostEqual(z1 - z0, reconstruct_print.BRAILLE_DOT_HEIGHT_MM + 0.1)

    def test_three_line_title_uses_the_tight_pitch(self) -> None:
        result, seen = self._compose(["网", "球", "拍柄"])
        lines = result.meta["braille"]["title_lines"]
        self.assertEqual(lines, ["网", "球", "拍柄"])
        self.assertEqual(len(seen), 3)
        ys = [item[1] for item in seen]
        self.assertEqual(ys, sorted(ys, reverse=True))
        self.assertAlmostEqual(result.meta["braille"]["title_line_pitch_mm"], 6.8 + 1.2)


class FlatPlateFooterTests(unittest.TestCase):
    def test_three_footer_lines_read_top_down(self) -> None:
        builder = _Builder(None)  # type: ignore[arg-type]
        _footer(
            builder,
            width=420.0,
            height=297.0,
            margin=5.0,
            qr_rect=(370.0, 250.0, 30.0, 25.0),
            braille_lines=["侧视图", "零基座 关节一到六", "连杆一到六 六轴机械臂"],
        )
        footer = builder.features[0]
        lines = [primitive for primitive in footer.primitives if primitive.kind == "braille"]
        texts = [primitive.text for primitive in lines]
        self.assertIn("侧视图", texts)
        ordered = [primitive for primitive in lines if primitive.text != "触摸学习"]
        self.assertEqual(
            [primitive.text for primitive in ordered],
            ["侧视图", "零基座 关节一到六", "连杆一到六 六轴机械臂"],
        )
        ys = [primitive.points[0][1] for primitive in ordered]
        self.assertEqual(ys, sorted(ys))


if __name__ == "__main__":
    unittest.main()