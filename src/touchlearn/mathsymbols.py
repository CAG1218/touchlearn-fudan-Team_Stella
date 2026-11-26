"""GB/T 18028-2010 cells for the maths symbols that appear on a plate.

Only symbols that were checked against the standard text are listed here.  An
unknown character raises :class:`MathSymbolError`, so a label is never printed
with a guessed cell; callers either shorten the label or spell it in Chinese.

4.1 requires a 数号 (3456) in front of *every* Arabic digit.  The all-digit
exception (two 数号 at the start of a whole line or page) does not apply to a
mixed label, so ``12`` really is six cells here.  Latin letters take the
小写字母号 (56) or 大写字母号 (6) that 5.2.1 defines.

Spacing follows the usage rules quoted with the operator table: a blank cell
before an operation sign, none before the marks the rule keeps tight (不等号,
点乘号, 小数点), and no blank between a sign and the number it signs when it
sits right after an equal sign.
"""

from __future__ import annotations

from dataclasses import dataclass

# 5.1.1 / 5.1.4
NUMBER_SIGN: tuple[int, ...] = (3, 4, 5, 6)
# 5.2.1 大写拉丁字母号 (6), 小写拉丁字母号 (56)
UPPER_LETTER_SIGN: tuple[int, ...] = (6,)
LOWER_LETTER_SIGN: tuple[int, ...] = (5, 6)

DIGITS: dict[str, tuple[int, ...]] = {
    "1": (1,),
    "2": (1, 2),
    "3": (1, 4),
    "4": (1, 4, 5),
    "5": (1, 5),
    "6": (1, 2, 4),
    "7": (1, 2, 4, 5),
    "8": (1, 2, 5),
    "9": (2, 4),
    "0": (2, 4, 5),
}

# 附录 C 盲文英语字母（与拉丁字母号配合使用）
LETTERS: dict[str, tuple[int, ...]] = {
    "a": (1,),
    "b": (1, 2),
    "c": (1, 4),
    "d": (1, 4, 5),
    "e": (1, 5),
    "f": (1, 2, 4),
    "g": (1, 2, 4, 5),
    "h": (1, 2, 5),
    "i": (2, 4),
    "j": (2, 4, 5),
    "k": (1, 3),
    "l": (1, 2, 3),
    "m": (1, 3, 4),
    "n": (1, 3, 4, 5),
    "o": (1, 3, 5),
    "p": (1, 2, 3, 4),
    "q": (1, 2, 3, 4, 5),
    "r": (1, 2, 3, 5),
    "s": (2, 3, 4),
    "t": (2, 3, 4, 5),
    "u": (1, 3, 6),
    "v": (1, 2, 3, 6),
    "w": (2, 4, 5, 6),
    "x": (1, 3, 4, 6),
    "y": (1, 3, 4, 5, 6),
    "z": (1, 3, 5, 6),
}

# Characters that belong to a maths run instead of the prose stream.  "·" is
# deliberately absent: in prose it is still the 间隔号, and only a run that
# starts with an ASCII character may contain it (see cnbraille._units).
MATH_RUN_CHARS = frozenset("×÷−≠≈≤≥±")


class MathSymbolError(ValueError):
    """Raised when a printed symbol has no checked GB/T 18028 cell."""


@dataclass(frozen=True)
class Symbol:
    text: str
    cells: tuple[tuple[int, ...], ...]
    space_before: bool
    name: str


# Operator cells.  "+" is 235 in the Chinese maths code; the UEB cell (346) is
# a different dot pattern and must not be reused here.
_OPERATORS: dict[str, tuple[tuple[tuple[int, ...], ...], bool, str]] = {
    "+": (((2, 3, 5),), True, "加号"),
    "-": (((3, 6),), True, "减号"),
    "−": (((3, 6),), True, "减号"),
    "×": (((2, 3, 6),), True, "乘号"),
    "*": (((2, 3, 6),), True, "乘号"),
    "÷": (((2, 5, 6),), True, "除号"),
    "/": (((2, 5, 6),), True, "除号"),
    "=": (((2, 3, 5, 6),), True, "等号"),
    "≠": (((4,), (2, 3, 5, 6)), False, "不等号"),
    "≈": (((2, 6), (2, 6)), True, "约等于号"),
    ">": (((1, 3, 5),), True, "大于号"),
    "<": (((2, 4, 6),), True, "小于号"),
    "≥": (((1, 3, 5), (2, 3, 5, 6)), True, "大于等于号"),
    "≤": (((2, 4, 6), (2, 3, 5, 6)), True, "小于等于号"),
    "±": (((2, 3, 5), (3, 6)), True, "正负号"),
    ".": (((2,),), False, "小数点"),
    "·": (((3,),), False, "点乘号"),
}

# After an equal sign a leading +/− is a sign, not an operation; the rule keeps
# it tight against the number that follows.
_SIGN_AFTER = {"=", "≈"}


def operator(symbol: str) -> Symbol:
    try:
        cells, space_before, name = _OPERATORS[symbol]
    except KeyError:
        raise MathSymbolError(f"未收录数学符号：{symbol}") from None
    return Symbol(symbol, cells, space_before, name)


def is_operator(symbol: str) -> bool:
    return symbol in _OPERATORS


def _digit_symbol(digits: str) -> Symbol:
    cells: list[tuple[int, ...]] = []
    for digit in digits:
        cells.append(NUMBER_SIGN)
        cells.append(DIGITS[digit])
    return Symbol(digits, tuple(cells), False, "数")


def _letter_symbol(word: str) -> Symbol:
    if word.isupper():
        cells = (UPPER_LETTER_SIGN,) + tuple(LETTERS[letter] for letter in word.lower())
    elif word.islower():
        cells = (LOWER_LETTER_SIGN,) + tuple(LETTERS[letter] for letter in word)
    else:
        raise MathSymbolError(f"大小写混写的字母串未定写法：{word}")
    return Symbol(word, cells, False, "字母")


def tokens(run: str) -> list[Symbol]:
    """Split an ASCII maths run into printable GB/T 18028 symbols."""
    result: list[Symbol] = []
    index = 0
    while index < len(run):
        character = run[index]
        # ``str.isdigit`` is true for characters such as "²" that have no cell
        # here, so membership in the checked table is the only safe test.
        if character in DIGITS:
            end = index
            while end < len(run) and run[end] in DIGITS:
                end += 1
            symbol = _digit_symbol(run[index:end])
            index = end
        elif character.isascii() and character.isalpha():
            end = index
            while end < len(run) and run[end].isascii() and run[end].isalpha():
                end += 1
            symbol = _letter_symbol(run[index:end])
            index = end
        else:
            symbol = operator(character)
            index += 1

        if not result:
            symbol = Symbol(symbol.text, symbol.cells, False, symbol.name)
        elif symbol.text in {"+", "-", "−", "±"} and result[-1].text in _SIGN_AFTER:
            symbol = Symbol(symbol.text, symbol.cells, False, symbol.name)
        result.append(symbol)
    return result