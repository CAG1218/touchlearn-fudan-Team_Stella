"""GB/T 18028 maths cells and the AST → maths-text printer.

The plate prints the equation twice: standard raised glyphs for a sighted
teacher, and GB/T 18028 braille for the student.  These tests pin the cells the
braille line is built from, and pin the cases where the printer must refuse
(brackets, 乘方号, 函数号) instead of inventing a dot pattern.
"""

from __future__ import annotations

import unittest

from tests import EXAMPLES  # noqa: F401  (ensures src is on sys.path)

from touchlearn import cnbraille, mathsymbols, tactile_braille
from touchlearn.agent.expression import parse_expression
from touchlearn.agent.parametric import MathLabelError, _math_label


class MathSymbolTests(unittest.TestCase):
    def test_every_digit_takes_its_own_number_sign(self) -> None:
        # 4.1: a 数号 in front of *every* digit; the whole-line exception does
        # not apply to a mixed label such as an equation.
        self.assertEqual(
            cnbraille.encode_text("12"),
            [(3, 4, 5, 6), (1,), (3, 4, 5, 6), (1, 2)],
        )

    def test_letters_take_a_letter_sign(self) -> None:
        self.assertEqual(cnbraille.encode_text("x"), [(5, 6), (1, 3, 4, 6)])
        self.assertEqual(cnbraille.encode_text("X"), [(6,), (1, 3, 4, 6)])

    def test_operator_cells(self) -> None:
        expected = {
            "+": (2, 3, 5),
            "-": (3, 6),
            "×": (2, 3, 6),
            "÷": (2, 5, 6),
            "=": (2, 3, 5, 6),
            ">": (1, 3, 5),
            "<": (2, 4, 6),
        }
        for symbol, cell in expected.items():
            self.assertEqual(mathsymbols.operator(symbol).cells, (cell,), symbol)
        self.assertEqual(mathsymbols.operator("≠").cells, ((4,), (2, 3, 5, 6)))
        self.assertEqual(mathsymbols.operator("≈").cells, ((2, 6), (2, 6)))
        self.assertEqual(mathsymbols.operator("≥").cells, ((1, 3, 5), (2, 3, 5, 6)))
        self.assertEqual(mathsymbols.operator("≤").cells, ((2, 4, 6), (2, 3, 5, 6)))
        self.assertEqual(mathsymbols.operator("±").cells, ((2, 3, 5), (3, 6)))
        self.assertEqual(mathsymbols.operator(".").cells, ((2,),))

    def test_blank_cell_rules(self) -> None:
        # 运算符号前空一方；点乘号、小数点、不等号与后面的数贴紧。
        self.assertFalse(mathsymbols.operator("·").space_before)
        self.assertFalse(mathsymbols.operator(".").space_before)
        self.assertFalse(mathsymbols.operator("≠").space_before)
        self.assertTrue(mathsymbols.operator("+").space_before)
        self.assertTrue(mathsymbols.operator("=").space_before)

    def test_equation_layout_matches_the_symbol_table(self) -> None:
        cells = cnbraille.encode_text("y=x·x-2x-3")
        expected = [
            (5, 6), (1, 3, 4, 5, 6),        # 字母号 y
            (), (2, 3, 5, 6),               # 空方 + 等号
            (5, 6), (1, 3, 4, 6),           # 字母号 x
            (3,),                           # 点乘号
            (5, 6), (1, 3, 4, 6),           # 字母号 x
            (), (3, 6),                     # 空方 + 减号
            (3, 4, 5, 6), (1, 2),           # 数号 + 2
            (5, 6), (1, 3, 4, 6),           # 字母号 x
            (), (3, 6),                     # 空方 + 减号
            (3, 4, 5, 6), (1, 4),           # 数号 + 3
        ]
        self.assertEqual(cells, expected)
        self.assertEqual(len(cells), 19)
        self.assertAlmostEqual(tactile_braille.width("y=x·x-2x-3"), 128.4, places=6)

    def test_unsupported_symbols_fail_closed(self) -> None:
        # 括号、乘方号、函数号没有可核对的公开点表，必须报错而不是猜。
        for text in ["x^2", "(x+1)", "x²"]:
            with self.assertRaises(cnbraille.BrailleError, msg=text):
                cnbraille.encode_text(text)
            self.assertFalse(tactile_braille.safe_text(text), text)

    def test_dot_diameter_follows_the_maths_code(self) -> None:
        # A pure-ASCII equation still belongs to the Chinese plate, so it must
        # use the Chinese 1.6 mm dots and 6.9 mm cell pitch, not UEB.
        self.assertTrue(tactile_braille.uses_chinese_code("y=x·x-2x-3"))
        self.assertAlmostEqual(
            tactile_braille.dot_diameter("y=x·x-2x-3"),
            cnbraille.DOT_DIAMETER_MM,
            places=6,
        )


class MathLabelTests(unittest.TestCase):
    def test_square_is_spelled_as_a_product(self) -> None:
        expression = parse_expression("x^2-2x-3")
        self.assertEqual(_math_label(expression.node), "x·x-2x-3")

    def test_coefficients_are_printed_without_sign_noise(self) -> None:
        expression = parse_expression("2*x^2+-2*x+-3")
        self.assertEqual(_math_label(expression.node), "2·x·x-2·x-3")

    def test_leading_one_disappears(self) -> None:
        self.assertEqual(_math_label(parse_expression("1*x^2").node), "x·x")

    def test_division_uses_the_division_sign(self) -> None:
        self.assertEqual(_math_label(parse_expression("x/2").node), "x÷2")

    def test_brackets_and_functions_are_refused(self) -> None:
        for text in ["(x+1)*(x-2)", "sin(x)", "abs(x)", "x^5", "x^0.5", "-(x+1)"]:
            with self.assertRaises(MathLabelError, msg=text):
                _math_label(parse_expression(text).node)


if __name__ == "__main__":
    unittest.main()