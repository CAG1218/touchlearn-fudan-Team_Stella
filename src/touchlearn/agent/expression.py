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

# Users paste equations in several common forms: ``y=x^2``, ``y = x^2``,
# ``f(x)=sin(x)``.  The geometry engine only needs the right-hand side.
_FUNCTION_PREFIX_RE = re.compile(
    r"^\s*(?:y|[fgh]\s*\(\s*x\s*\))\s*=\s*",
    re.IGNORECASE,
)

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


def strip_function_prefix(text: str) -> str:
    """Drop ``y=`` / ``f(x)=`` so the parser only sees the right-hand side."""
    cleaned = str(text or "").strip()
    match = _FUNCTION_PREFIX_RE.match(cleaned)
    return cleaned[match.end() :].strip() if match else cleaned


def _normalise(text: str) -> str:
    return (
        strip_function_prefix(text)
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
        values = [y for segment in segments for _, y in segment]
        if not values:
            return (-1.0, 1.0)
        # Percentiles keep a single asymptote from flattening the whole plate,
        # but a root or extremum exactly at the edge must never be clipped.
        key_values = [
            point.y
            for point in self.key_points(x_min, x_max, max_points=16)
            if math.isfinite(point.y)
        ]
        values.extend(key_values)
        values.sort()
        low = _percentile(values, 0.04)
        high = _percentile(values, 0.96)
        if key_values:
            low = min(low, min(key_values))
            high = max(high, max(key_values))
        if high - low < 1e-6:
            high = low + 1.0
        span = high - low
        if span < 2.0:
            centre = (high + low) / 2.0
            low = centre - 1.0
            high = centre + 1.0
            span = 2.0
        return (low - span * pad, high + span * pad)

    def _defined_x_extent(
        self, x_min: float, x_max: float, samples: int = 400
    ) -> tuple[float, float] | None:
        """Return the first and last sampled x where the expression is defined.

        This is only used to frame functions such as ``sqrt(x)`` or ``ln(x)``
        whose useful domain starts at a boundary.  Rational functions that are
        defined on both sides of a pole still return the full interval.
        """
        if x_max <= x_min:
            return None
        first: float | None = None
        last: float | None = None
        for index in range(samples + 1):
            x = x_min + (x_max - x_min) * index / samples
            if self.evaluate(x) is None:
                continue
            if first is None:
                first = x
            last = x
        if first is None or last is None or last - first <= 1e-9:
            return None
        return first, last

    def suggest_window(self, default_min: float = -5.0, default_max: float = 5.0) -> tuple[float, float, float, float]:
        """Pick a readable window that keeps the interesting shape on the plate.

        Extrema and roots are framed together.  When the default domain has no
        feature (for example ``(x-10)^2``), progressively wider domains are
        scanned so the plateau is not silently cropped.  A monotone curve or a
        rational function falls back to the default domain.  This is a layout
        hint, not a claim about the true domain of the expression.
        """
        points = self.key_points(default_min, default_max, max_points=16)
        features = [point for point in points if point.kind in {"root", "minimum", "maximum"}]
        if not features:
            best_features: list[KeyPoint] = []
            best_points = points
            for span in (10.0, 20.0, 40.0):
                wide_points = self.key_points(-span, span, max_points=16)
                wide_features = [
                    point for point in wide_points if point.kind in {"root", "minimum", "maximum"}
                ]
                if len(wide_features) > len(best_features):
                    best_features = wide_features
                    best_points = wide_points
            points = best_points
            features = best_features
        else:
            # A lone vertex in the middle of the default domain can belong to a
            # wider shape (x^4-100x^2 hides its two minima outside [-5, 5]).
            # Look further only when the visible features are clustered, and
            # take the first wider span that adds extrema.  Periodic curves
            # already fill the window, so they are left alone.
            extrema = [point for point in features if point.kind in {"minimum", "maximum"}]
            visible_span = max(point.x for point in features) - min(point.x for point in features)
            if extrema and visible_span < (default_max - default_min) * 0.25:
                best_extrema = len(extrema)
                for span in (10.0, 20.0, 40.0):
                    wide_points = self.key_points(-span, span, max_points=16)
                    wide_extrema = [
                        point for point in wide_points if point.kind in {"minimum", "maximum"}
                    ]
                    if len(wide_extrema) > best_extrema:
                        points = wide_points
                        features = [
                            point
                            for point in wide_points
                            if point.kind in {"root", "minimum", "maximum"}
                        ]
                        break

        if features:
            xs = [point.x for point in features]
            low, high = min(xs), max(xs)
            # A feature cluster can be very narrow (a vertex and a nearby
            # tangent root).  Keep at least a 4-unit window so the curvature
            # is visible instead of zooming into a nearly flat fragment.
            has_extrema = any(point.kind in {"minimum", "maximum"} for point in features)
            if has_extrema and high - low < 4.0:
                center = (low + high) / 2.0
                low, high = center - 2.0, center + 2.0
            else:
                # Keep a margin, but a wide one on a steep curve lets the
                # tails dominate the auto y-range and squash the real shape.
                pad = max((high - low) * 0.1, 0.5) if has_extrema else max((high - low) * 0.5, 3.0)
                low -= pad
                high += pad
        else:
            low, high = default_min, default_max

        # Functions with a one-sided domain (sqrt, ln) otherwise waste half of
        # the plate on an undefined region.  Trim only when a meaningful slice
        # at an edge is undefined, so 1/x and tan(x) keep both branches.
        if high > low:
            extent = self._defined_x_extent(low, high)
            if extent is not None:
                first, last = extent
                span = high - low
                step = span / 400.0
                if first - low > span * 0.08:
                    low = max(low, first - step)
                if high - last > span * 0.08:
                    high = min(high, last + step)
                if high <= low:
                    low, high = default_min, default_max

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
        segments = self.sample(x_min, x_max, samples)
        magnitudes = sorted(
            abs(y) for segment in segments for _, y in segment if math.isfinite(y)
        )
        # A pole looks like a sign change or a derivative flip to a naive
        # scanner.  Ten times the 90th percentile of the sampled magnitudes is
        # a scale-aware ceiling; anything above it is a discontinuity artefact,
        # not a point a fingertip could read on the plate.
        y_scale = _percentile(magnitudes, 0.9) if magnitudes else 1.0
        root_limit = max(1e-6, y_scale * 1e-6)
        point_limit = max(100.0, y_scale * 10.0)
        # A curve that underflows to zero (exp(-x^2)) can stay below this
        # tolerance for hundreds of samples.  Those samples are a flat tail,
        # not hundreds of separate roots.
        zero_tolerance = max(1e-9, y_scale * 1e-9)
        touch_limit = zero_tolerance * 10.0

        for segment in segments:
            index = 0
            while index < len(segment):
                x, y = segment[index]
                if abs(y) <= zero_tolerance:
                    start = index
                    while index + 1 < len(segment) and abs(segment[index + 1][1]) <= zero_tolerance:
                        index += 1
                    end = index
                    run_length = end - start + 1
                    before = segment[start - 1] if start > 0 else None
                    after = segment[end + 1] if end + 1 < len(segment) else None
                    # Only a short dip through zero is a root.  A long run is
                    # either a constant stretch or floating-point underflow,
                    # and a tail that runs into the window edge has no
                    # crossing at all.
                    if (
                        run_length <= 2
                        and before is not None
                        and after is not None
                        and abs(before[1]) > touch_limit
                        and abs(after[1]) > touch_limit
                    ):
                        roots.append((segment[start][0] + segment[end][0]) / 2.0)
                    index += 1
                    continue
                if index + 1 < len(segment):
                    next_y = segment[index + 1][1]
                    if y * next_y < 0:
                        candidate = self._bisect(x, segment[index + 1][0])
                        candidate_y = self.evaluate(candidate)
                        if candidate_y is not None and abs(candidate_y) <= root_limit:
                            roots.append(candidate)
                index += 1
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
            if previous_derivative * derivative < 0 or (
                (previous_derivative == 0) != (derivative == 0)
            ):
                step = (x_max - x_min) / samples
                # When the derivative is exactly zero on a sample (a vertex
                # landing on the grid), the bracket must reach back past that
                # sample.  Otherwise the search only sees one shoulder.
                left = x - 2.0 * step if previous_derivative == 0 else x - step
                kind = "minimum" if derivative > previous_derivative else "maximum"
                point = self._refine_extremum(left, x, h, kind)
                if (
                    point is not None
                    and abs(point[1]) <= point_limit
                    and self._is_smooth_extremum(point[0], point[1], step, kind)
                ):
                    extrema.append(KeyPoint(kind, point[0], point[1], ""))
            previous_derivative = derivative
        extrema = _dedupe_key_points(extrema, tolerance=max((x_max - x_min) * 1e-4, 1e-6))

        # A tangent root is also an extremum (for example x^2 at the origin).
        # Prefer the extremum because it carries more shape information.
        x_span = max(x_max - x_min, 1e-9)
        y_span = (y_max - y_min) if y_min is not None and y_max is not None else None
        points: list[KeyPoint] = []
        for x in roots:
            y = self.evaluate(x)
            if y is not None:
                points.append(KeyPoint("root", x, y, ""))
        for point in extrema:
            near = next((item for item in points if _same_place(item, point, x_span, y_span)), None)
            if near is not None:
                points.remove(near)
            points.append(point)
        if x_min <= 0.0 <= x_max:
            y = self.evaluate(0.0)
            if y is not None:
                intercept = KeyPoint("y_intercept", 0.0, y, "")
                if not any(_same_place(item, intercept, x_span, y_span) for item in points):
                    points.append(intercept)

        points = [point for point in points if math.isfinite(point.y) and abs(point.y) <= point_limit]
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

    def _refine_extremum(self, left: float, right: float, h: float, kind: str) -> tuple[float, float] | None:
        # Golden-section search on the sampled bracket; the caller already
        # knows whether the derivative flipped up or down.
        golden = (math.sqrt(5.0) - 1.0) / 2.0
        a, b = left, right
        c = b - golden * (b - a)
        d = a + golden * (b - a)
        fc = self.evaluate(c)
        fd = self.evaluate(d)
        if fc is None or fd is None:
            return None
        is_minimum = kind == "minimum"
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

    def _is_smooth_extremum(self, x: float, y: float, probe: float, kind: str) -> bool:
        """Reject poles and flat treads that a derivative sign flip produced.

        A real minimum must sit at or below both neighbours and a real maximum
        at or above both.  At a pole shoulder one side is always closer to the
        pole, so it breaks this dominance test even though the finite
        difference changes sign there.  The gap must also shrink as the probe
        shrinks; that rejects a same-sign pole such as ``1/x^2`` where both
        neighbours are below the refined value.  A constant stretch has no gap
        and is not an extremum either.
        """
        probe = max(probe, 1e-9)
        scale = max(abs(y), 1.0)
        floor = scale * 1e-9
        gaps: list[float] = []
        for step in (probe, probe / 2.0, probe / 4.0):
            left = self.evaluate(x - step)
            right = self.evaluate(x + step)
            if left is None or right is None:
                return False
            if kind == "minimum":
                if left < y - floor or right < y - floor:
                    return False
            else:
                if left > y + floor or right > y + floor:
                    return False
            gaps.append(max(abs(left - y), abs(right - y)))
        if gaps[0] <= floor:
            return False
        for outer_gap, inner_gap in zip(gaps, gaps[1:]):
            if inner_gap > outer_gap:
                return False
            # A jump discontinuity looks flat on one side and then collapses
            # to zero as the probe shrinks; a smooth extremum shrinks by a
            # steady factor (x^2: 1/4, abs(x): 1/2, x^4: 1/16).
            if inner_gap < outer_gap * 1e-6:
                return False
        return gaps[-1] <= max(gaps[0] * 0.5, floor)

    def display(self, limit: int = 42) -> str:
        """Shorten only the plate label; the full expression stays in metadata."""
        text = self.canonical
        if len(text) <= limit:
            return text
        return text[: max(1, limit - 1)] + "…"

    def to_dict(self) -> dict[str, object]:
        return {"source": self.source, "canonical": self.canonical}


def _dedupe(values: Iterable[float], tolerance: float) -> list[float]:
    result: list[float] = []
    for value in sorted(values):
        if not result or abs(value - result[-1]) > tolerance:
            result.append(value)
    return result


def _same_place(
    first: KeyPoint,
    second: KeyPoint,
    x_span: float,
    y_span: float | None,
    relative: float = 0.02,
) -> bool:
    """True when two key points would land on the same tactile marker.

    Numerical refinement leaves a vertex a few thousandths of a unit away from
    the origin, which used to produce two labels on one spot.  The threshold is
    a fraction of the plotted window, so it scales with the drawing instead of
    the expression's units.
    """
    if abs(first.x - second.x) > x_span * relative:
        return False
    scale = y_span if y_span and y_span > 0 else max(abs(first.y), abs(second.y), 1.0)
    return abs(first.y - second.y) <= scale * relative


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
    source = strip_function_prefix(text)
    if not source:
        raise ExpressionError("表达式为空")
    if len(source) > 400:
        raise ExpressionError("表达式过长（最多 400 个字符）")
    tokens = _tokenize(source)
    node = _Parser(tokens).parse()
    return Expression(source=source, node=node, canonical=_canonical_display(source))


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
