"""Safe parsing, evaluation and sampling of one-variable expressions.

The agent never calls ``eval`` on user input.  Expressions are tokenised and
parsed into a small tuple AST, then evaluated against a whitelist of maths
functions.  Sampling and key-point extraction are deterministic, so the same
expression always produces the same tactile plate.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Iterable, Sequence

Number = float
Node = tuple


class ExpressionError(ValueError):
    """The expression is not valid or uses an unsupported construct."""


class MathDomainError(ValueError):
    """The expression is undefined at this point."""


_TOKEN_RE = re.compile(
    r"""
    \s*(?:
      (?P<number>(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)
    | (?P<name>[A-Za-z_][A-Za-z_0-9]*)
    | (?P<op>\*\*|[+\-*/^(),])
    )
    """,
    re.VERBOSE,
)

_CONSTANTS = {"pi": math.pi, "e": math.e}

_FUNCTIONS: dict[str, tuple[int, object]] = {
    "sin": (1, math.sin),
    "cos": (1, math.cos),
    "tan": (1, math.tan),
    "asin": (1, math.asin),
    "acos": (1, math.acos),
    "atan": (1, math.atan),
    "sqrt": (1, math.sqrt),
    "abs": (1, abs),
    "exp": (1, math.exp),
    "ln": (1, math.log),
    "log": (1, math.log),
    "log10": (1, math.log10),
    "floor": (1, math.floor),
    "ceil": (1, math.ceil),
    "round": (1, round),
    "sign": (1, lambda value: 1.0 if value > 0 else (-1.0 if value < 0 else 0.0)),
    "min": (2, min),
    "max": (2, max),
    "pow": (2, math.pow),
}


def _normalise(text: str) -> str:
    return (
        text.strip()
        .replace("π", "pi")
        .replace("−", "-")
        .replace("–", "-")
        .replace("×", "*")
        .replace("÷", "/")
        .replace("²", "^2")
        .replace("³", "^3")
    )


def _tokenize(text: str) -> list[tuple[str, str]]:
    source = _normalise(text)
    if not source:
        raise ExpressionError("表达式为空")
    tokens: list[tuple[str, str]] = []
    position = 0
    while position < len(source):
        match = _TOKEN_RE.match(source, position)
        if not match:
            remainder = source[position:].strip()
            if not remainder:
                break
            raise ExpressionError(f"无法识别的位置: {remainder[:12]!r}")
        position = match.end()
        kind = match.lastgroup or ""
        value = match.group(kind)
        if kind == "op" and value == "**":
            value = "^"
        tokens.append((kind, value))
    tokens.append(("eof", ""))
    return tokens


class _Parser:
    def __init__(self, tokens: Sequence[tuple[str, str]]) -> None:
        self.tokens = list(tokens)
        self.index = 0

    def peek(self) -> tuple[str, str]:
        return self.tokens[self.index]

    def take(self, kind: str | None = None, value: str | None = None) -> tuple[str, str]:
        token = self.peek()
        if kind is not None and token[0] != kind:
            raise ExpressionError(f"期望 {kind}，得到 {token[1]!r}")
        if value is not None and token[1] != value:
            raise ExpressionError(f"期望 {value!r}，得到 {token[1]!r}")
        self.index += 1
        return token

    def parse(self) -> Node:
        node = self.expression()
        if self.peek()[0] != "eof":
            raise ExpressionError(f"多余的内容: {self.peek()[1]!r}")
        return node

    def expression(self) -> Node:
        node = self.term()
        while self.peek()[0] == "op" and self.peek()[1] in {"+", "-"}:
            operator = self.take("op")[1]
            node = ("bin", operator, node, self.term())
        return node

    def term(self) -> Node:
        node = self.unary()
        while True:
            kind, value = self.peek()
            if kind == "op" and value in {"*", "/"}:
                self.take("op")
                node = ("bin", value, node, self.unary())
                continue
            # Implicit multiplication: 2x, 3sin(x), 2(x+1), (x+1)(x-1)
            if kind in {"number", "name"} or (kind == "op" and value == "("):
                node = ("bin", "*", node, self.unary())
                continue
            break
        return node

    def unary(self) -> Node:
        if self.peek()[0] == "op" and self.peek()[1] in {"+", "-"}:
            operator = self.take("op")[1]
            return ("unary", operator, self.unary())
        return self.power()

    def power(self) -> Node:
        node = self.primary()
        if self.peek()[0] == "op" and self.peek()[1] == "^":
            self.take("op")
            node = ("bin", "^", node, self.unary())
        return node

    def primary(self) -> Node:
        kind, value = self.peek()
        if kind == "number":
            self.take("number")
            try:
                return ("num", float(value))
            except ValueError as exc:
                raise ExpressionError(f"无效数字: {value!r}") from exc
        if kind == "name":
            self.take("name")
            lowered = value.lower()
            if lowered == "x":
                return ("var",)
            if lowered in _CONSTANTS:
                return ("num", float(_CONSTANTS[lowered]))
            if lowered in _FUNCTIONS:
                arity, _ = _FUNCTIONS[lowered]
                if not (self.peek()[0] == "op" and self.peek()[1] == "("):
                    raise ExpressionError(f"函数 {lowered} 缺少括号")
                self.take("op", "(")
                arguments = [self.expression()]
                while self.peek()[0] == "op" and self.peek()[1] == ",":
                    self.take("op", ",")
                    arguments.append(self.expression())
                self.take("op", ")")
                if len(arguments) != arity:
                    raise ExpressionError(f"函数 {lowered} 需要 {arity} 个参数，得到 {len(arguments)} 个")
                return ("call", lowered, tuple(arguments))
            raise ExpressionError(f"不支持的名称: {value!r}（只支持 x、pi、e 和白名单函数）")
        if kind == "op" and value == "(":
            self.take("op", "(")
            node = self.expression()
            self.take("op", ")")
            return node
        raise ExpressionError(f"表达式不完整，遇到 {value!r}")


def _evaluate(node: Node, x: float) -> float:
    kind = node[0]
    if kind == "num":
        return float(node[1])
    if kind == "var":
        return x
    if kind == "unary":
        value = _evaluate(node[2], x)
        return -value if node[1] == "-" else value
    if kind == "bin":
        operator = node[1]
        left = _evaluate(node[2], x)
        right = _evaluate(node[3], x)
        try:
            if operator == "+":
                return left + right
            if operator == "-":
                return left - right
            if operator == "*":
                return left * right
            if operator == "/":
                if abs(right) < 1e-15:
                    raise MathDomainError("除以零")
                return left / right
            if operator == "^":
                if left < 0 and not float(right).is_integer():
                    raise MathDomainError("负数的非整数次幂")
                return left**right
        except (OverflowError, ValueError) as exc:
            raise MathDomainError(str(exc)) from exc
        raise ExpressionError(f"未知运算符: {operator}")
    if kind == "call":
        name = node[1]
        arguments = [_evaluate(argument, x) for argument in node[2]]
        _, function = _FUNCTIONS[name]
        try:
            if name == "sqrt" and arguments[0] < 0:
                raise MathDomainError("负数开平方")
            if name in {"ln", "log", "log10"} and arguments[0] <= 0:
                raise MathDomainError("对数定义域错误")
            if name == "tan" and abs(math.cos(arguments[0])) < 1e-12:
                raise MathDomainError("正切无定义")
            result = function(*arguments)  # type: ignore[operator]
        except (OverflowError, ValueError) as exc:
            raise MathDomainError(str(exc)) from exc
        if isinstance(result, complex):
            raise MathDomainError("复数结果")
        result = float(result)
        if not math.isfinite(result):
            raise MathDomainError("非有限值")
        return result
    raise ExpressionError(f"未知节点: {kind}")


def _safe_evaluate(node: Node, x: float) -> float | None:
    try:
        value = _evaluate(node, x)
    except (MathDomainError, OverflowError, ValueError, ZeroDivisionError):
        return None
    if not math.isfinite(value):
        return None
    return value


def _percentile(sorted_values: Sequence[float], fraction: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = fraction * (len(sorted_values) - 1)
    lower = int(math.floor(position))
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = position - lower
    return sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight


@dataclass(frozen=True)
class KeyPoint:
    kind: str
    x: float
    y: float
    label: str

    def to_dict(self) -> dict[str, object]:
        return {"kind": self.kind, "x": round(self.x, 4), "y": round(self.y, 4), "label": self.label}


@dataclass(frozen=True)
class Expression:
    source: str
    node: Node
    canonical: str

    def evaluate(self, x: float) -> float | None:
        return _safe_evaluate(self.node, x)

    def sample(self, x_min: float, x_max: float, count: int = 600) -> list[list[tuple[float, float]]]:
        """Sample the curve and split it at undefined points or jumps."""
        if x_max <= x_min:
            raise ExpressionError("x 的上界必须大于下界")
        count = max(40, min(int(count), 4000))
        raw: list[tuple[float, float | None]] = []
        for index in range(count + 1):
            x = x_min + (x_max - x_min) * index / count
            raw.append((x, self.evaluate(x)))
        finite = [value for _, value in raw if value is not None]
        span = (max(finite) - min(finite)) if finite else 1.0
        jump_limit = max(span * 0.6, 20.0)

        segments: list[list[tuple[float, float]]] = []
        current: list[tuple[float, float]] = []
        previous: tuple[float, float | None] | None = None
        for x, value in raw:
            if value is None:
                if len(current) >= 2:
                    segments.append(current)
                current = []
                previous = None
                continue
            if previous is not None and previous[1] is not None:
                jump = abs(value - previous[1])
                if jump > jump_limit and abs(value) > span:
                    if len(current) >= 2:
                        segments.append(current)
                    current = []
            current.append((x, value))
            previous = (x, value)
        if len(current) >= 2:
            segments.append(current)
        return segments

    def auto_y_range(self, x_min: float, x_max: float, pad: float = 0.12) -> tuple[float, float]:
        segments = self.sample(x_min, x_max, 500)
        values = sorted(y for segment in segments for _, y in segment)
        if not values:
            return (-1.0, 1.0)
        low = _percentile(values, 0.04)
        high = _percentile(values, 0.96)
        if high - low < 1e-6:
            high = low + 1.0
        span = high - low
        if span < 2.0:
            centre = (high + low) / 2.0
            low = centre - 1.0
            high = centre + 1.0
            span = 2.0
        return (low - span * pad, high + span * pad)

    def suggest_window(self, default_min: float = -5.0, default_max: float = 5.0) -> tuple[float, float, float, float]:
        """Pick a readable window that keeps the interesting shape on the plate.

        When the curve has extrema, frame them together with the roots; a
        monotone curve or a rational function falls back to the default domain
        and a percentile-based range.  This is a layout hint, not a claim about
        the true domain of the expression.
        """
        points = self.key_points(default_min, default_max, max_points=6)
        has_extrema = any(point.kind in {"minimum", "maximum"} for point in points)
        if has_extrema and points:
            xs = [point.x for point in points]
            low, high = min(xs), max(xs)
            span = max(high - low, 2.0)
            low -= span * 0.35
            high += span * 0.35
        else:
            low, high = default_min, default_max
        ys = [point.y for point in points if math.isfinite(point.y)]
        if ys:
            y_low, y_high = min(ys), max(ys)
            y_span = max(y_high - y_low, 2.0)
            y_low -= y_span * 0.30
            y_high += y_span * 0.30
        else:
            y_low, y_high = self.auto_y_range(low, high)
        return (round(low, 3), round(high, 3), round(y_low, 3), round(y_high, 3))

    def key_points(
        self,
        x_min: float,
        x_max: float,
        y_min: float | None = None,
        y_max: float | None = None,
        max_points: int = 6,
    ) -> list[KeyPoint]:
        roots: list[float] = []
        samples = 1000
        previous_x = x_min
        previous_y = self.evaluate(previous_x)
        for index in range(1, samples + 1):
            x = x_min + (x_max - x_min) * index / samples
            y = self.evaluate(x)
            if previous_y is not None and y is not None:
                if abs(previous_y) < 1e-9:
                    roots.append(previous_x)
                elif previous_y * y < 0:
                    roots.append(self._bisect(previous_x, x))
            previous_x, previous_y = x, y
        roots = _dedupe(roots, tolerance=max((x_max - x_min) * 1e-4, 1e-6))

        extrema: list[KeyPoint] = []
        h = max((x_max - x_min) / 3000.0, 1e-7)
        previous_derivative = self._derivative(x_min + h, h)
        for index in range(1, samples + 1):
            x = x_min + (x_max - x_min) * index / samples
            if x + h > x_max:
                break
            derivative = self._derivative(x, h)
            if previous_derivative is None or derivative is None:
                previous_derivative = derivative
                continue
            if previous_derivative == 0 or previous_derivative * derivative < 0:
                point = self._refine_extremum(x - (x_max - x_min) / samples, x, h)
                if point is not None:
                    kind = "minimum" if derivative > previous_derivative else "maximum"
                    extrema.append(KeyPoint(kind, point[0], point[1], ""))
            previous_derivative = derivative
        extrema = _dedupe_key_points(extrema, tolerance=max((x_max - x_min) * 1e-4, 1e-6))

        # A tangent root is also an extremum (for example x^2 at the origin).
        # Prefer the extremum because it carries more shape information.
        tolerance = max((x_max - x_min) * 1e-4, 1e-6)
        points: list[KeyPoint] = []
        for x in roots:
            y = self.evaluate(x)
            if y is not None:
                points.append(KeyPoint("root", x, y, ""))
        for point in extrema:
            near = next((item for item in points if abs(item.x - point.x) <= tolerance), None)
            if near is not None:
                points.remove(near)
            points.append(point)
        if x_min <= 0.0 <= x_max:
            y = self.evaluate(0.0)
            if y is not None and all(abs(point.x) > tolerance for point in points):
                points.append(KeyPoint("y_intercept", 0.0, y, ""))

        if y_min is not None and y_max is not None:
            points = [point for point in points if y_min - 1e-6 <= point.y <= y_max + 1e-6]

        priority = {"root": 0, "minimum": 1, "maximum": 1, "y_intercept": 2}
        points.sort(key=lambda point: (priority.get(point.kind, 3), point.x))
        points = points[:max_points]
        points.sort(key=lambda point: point.x)
        labelled: list[KeyPoint] = []
        counts = {"root": 0, "minimum": 0, "maximum": 0, "y_intercept": 0}
        for point in points:
            counts[point.kind] = counts.get(point.kind, 0) + 1
            prefix = {"root": "R", "minimum": "MIN", "maximum": "MAX", "y_intercept": "Y"}.get(point.kind, "P")
            labelled.append(KeyPoint(point.kind, point.x, point.y, f"{prefix}{counts[point.kind]}"))
        return labelled

    def _derivative(self, x: float, h: float) -> float | None:
        left = self.evaluate(x - h)
        right = self.evaluate(x + h)
        if left is None or right is None:
            return None
        value = (right - left) / (2.0 * h)
        return value if math.isfinite(value) else None

    def _bisect(self, left: float, right: float) -> float:
        left_value = self.evaluate(left)
        for _ in range(48):
            middle = (left + right) / 2.0
            middle_value = self.evaluate(middle)
            if middle_value is None:
                return middle
            if abs(middle_value) < 1e-10:
                return middle
            if left_value is not None and left_value * middle_value <= 0:
                right = middle
            else:
                left = middle
                left_value = middle_value
        return (left + right) / 2.0

    def _refine_extremum(self, left: float, right: float, h: float) -> tuple[float, float] | None:
        # Golden-section search on the sampled bracket; works for either sign.
        golden = (math.sqrt(5.0) - 1.0) / 2.0
        a, b = left, right
        c = b - golden * (b - a)
        d = a + golden * (b - a)
        fc = self.evaluate(c)
        fd = self.evaluate(d)
        if fc is None or fd is None:
            return None
        # Decide whether this bracket is a minimum or maximum by sampling.
        fa = self.evaluate(a)
        fb = self.evaluate(b)
        if fa is None or fb is None:
            return None
        is_minimum = fc <= fa and fc <= fb
        for _ in range(60):
            if is_minimum:
                if fc < fd:
                    b, d, fd = d, c, fc
                    c = b - golden * (b - a)
                    fc = self.evaluate(c)
                else:
                    a, c, fc = c, d, fd
                    d = a + golden * (b - a)
                    fd = self.evaluate(d)
            else:
                if fc > fd:
                    b, d, fd = d, c, fc
                    c = b - golden * (b - a)
                    fc = self.evaluate(c)
                else:
                    a, c, fc = c, d, fd
                    d = a + golden * (b - a)
                    fd = self.evaluate(d)
            if fc is None or fd is None:
                return None
            if abs(b - a) < 1e-9:
                break
        x = (a + b) / 2.0
        y = self.evaluate(x)
        if y is None:
            return None
        return (x, y)

    def to_dict(self) -> dict[str, object]:
        return {"source": self.source, "canonical": self.canonical}


def _dedupe(values: Iterable[float], tolerance: float) -> list[float]:
    result: list[float] = []
    for value in sorted(values):
        if not result or abs(value - result[-1]) > tolerance:
            result.append(value)
    return result


def _dedupe_key_points(points: Iterable[KeyPoint], tolerance: float) -> list[KeyPoint]:
    result: list[KeyPoint] = []
    for point in sorted(points, key=lambda item: item.x):
        if not result or abs(point.x - result[-1].x) > tolerance:
            result.append(point)
    return result


def _canonical_display(text: str) -> str:
    cleaned = _normalise(text).upper().replace("**", "^").replace("*", "")
    cleaned = re.sub(r"\s+", "", cleaned)
    cleaned = cleaned.replace("PI", "PI")
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789+-./^()= ")
    return "".join(character if character in allowed else "?" for character in cleaned)


def parse_expression(text: str) -> Expression:
    source = text.strip()
    if not source:
        raise ExpressionError("表达式为空")
    tokens = _tokenize(source)
    node = _Parser(tokens).parse()
    canonical = _canonical_display(source)
    if len(canonical) > 28:
        canonical = canonical[:27] + "?"
    return Expression(source=source, node=node, canonical=canonical)


def expression_from_coefficients(a: float, b: float, c: float) -> Expression:
    return parse_expression(f"{a}*x^2+{b}*x+{c}")


def describe_key_points(points: Sequence[KeyPoint]) -> str:
    if not points:
        return "没有自动标记关键点"
    parts = []
    labels = {"root": "零点", "minimum": "极小值", "maximum": "极大值", "y_intercept": "纵轴截距"}
    for point in points:
        parts.append(f"{point.label} 是{labels.get(point.kind, '关键点')}，x 约 {point.x:.2f}，y 约 {point.y:.2f}")
    return "；".join(parts)
