"""Chinese Common Braille (GF 0019-2018) encoding and cell geometry.

The 2018 scheme keeps the 1995 sound symbols but marks tone with a separate
cell after the initial and final, then applies a set of tone-omission rules.
This module implements the sound tables, the spelling rules and the level-2
tone-omission rules that matter for short labels.  It never guesses a reading:
a character outside the bundled GB2312 pinyin table raises
:class:`BrailleError` instead of silently producing an unrelated syllable.

The bundled table contains the most common reading of each GB2312 character.
Polyphonic characters in a label should be overridden by the caller when the
context requires another reading.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable

DATA_FILE = Path(__file__).with_name("data") / "pinyin.json"

# Dot numbering:
#   1 4
#   2 5
#   3 6
INITIALS: dict[str, tuple[int, ...]] = {
    "b": (1, 2),
    "p": (1, 2, 3, 4),
    "m": (1, 3, 4),
    "f": (1, 2, 4),
    "d": (1, 4, 5),
    "t": (2, 3, 4, 5),
    "n": (1, 3, 4, 5),
    "l": (1, 2, 3),
    "g": (1, 2, 4, 5),
    "k": (1, 3),
    "h": (1, 2, 5),
    "j": (1, 2, 4, 5),
    "q": (1, 3),
    "x": (1, 2, 5),
    "zh": (3, 4),
    "ch": (1, 2, 3, 4, 5),
    "sh": (1, 5, 6),
    "r": (2, 4, 5),
    "z": (1, 3, 5, 6),
    "c": (1, 4),
    "s": (2, 3, 4),
}

FINALS: dict[str, tuple[int, ...]] = {
    "a": (3, 5),
    "o": (2, 6),
    "e": (2, 6),
    "i": (2, 4),
    "u": (1, 3, 6),
    "ü": (3, 4, 6),
    "er": (1, 2, 3, 5),
    "ai": (2, 4, 6),
    "ao": (2, 3, 5),
    "ei": (2, 3, 4, 6),
    "ou": (1, 2, 3, 5, 6),
    "ia": (1, 2, 4, 6),
    "iao": (3, 4, 5),
    "ie": (1, 5),
    "iou": (1, 2, 5, 6),
    "ua": (1, 2, 3, 4, 5, 6),
    "uai": (1, 3, 4, 5, 6),
    "uei": (2, 4, 5, 6),
    "uo": (1, 3, 5),
    "üe": (2, 3, 4, 5, 6),
    "an": (1, 2, 3, 6),
    "ang": (2, 3, 6),
    "en": (3, 5, 6),
    "eng": (3, 4, 5, 6),
    "ian": (1, 4, 6),
    "iang": (1, 3, 4, 6),
    "in": (1, 2, 6),
    "ing": (1, 6),
    "uan": (1, 2, 4, 5, 6),
    "uang": (2, 3, 5, 6),
    "uen": (2, 5),
    "ong": (2, 5, 6),
    "ueng": (2, 5, 6),
    "üan": (1, 2, 3, 4, 6),
    "ün": (4, 5, 6),
    "iong": (1, 4, 5, 6),
}

TONES: dict[int, tuple[int, ...]] = {
    1: (1,),       # 阴平
    2: (2,),       # 阳平
    3: (3,),       # 上声
    4: (2, 3),     # 去声
    5: (),         # 轻声不设符号
    0: (),
}

# GF 0019 10.2 tone omission.  The value is the tone that may be omitted for
# syllables beginning with that initial.
OMIT_TONE: dict[str, int] = {
    "f": 1,
    "p": 2,
    "m": 2,
    "t": 2,
    "n": 2,
    "h": 2,
    "q": 2,
    "ch": 2,
    "r": 2,
    "c": 2,
    "b": 4,
    "d": 4,
    "l": 4,
    "g": 4,
    "k": 4,
    "j": 4,
    "x": 4,
    "zh": 4,
    "sh": 4,
    "z": 4,
    "s": 4,
}

# 10.2.5 / 10.2.6: explicit exceptions that keep or change the omitted tone.
KEEP_TONE = {"tou2", "le4", "zi4", "e4", "yi4", "er4", "wo4", "ye4", "you4", "zai4"}
OMIT_SPECIAL = {"yi1", "er2", "wo3", "ye3", "you3"}

# 9.2: zhi/chi/shi/ri/zi/ci/si omit the final i.
SYLLABIC_CONSONANTS = {"zh", "ch", "sh", "r", "z", "c", "s"}

# Orthographic y/w are not separate initials in the 2018 scheme; the underlying
# final stands alone (9.3 韵母可自成音节).
Y_FINALS = {
    "yi": "i",
    "ya": "ia",
    "ye": "ie",
    "yao": "iao",
    "you": "iou",
    "yan": "ian",
    "yin": "in",
    "yang": "iang",
    "ying": "ing",
    "yong": "iong",
    "yu": "ü",
    "yue": "üe",
    "yuan": "üan",
    "yun": "ün",
}
W_FINALS = {
    "wu": "u",
    "wa": "ua",
    "wo": "uo",
    "wai": "uai",
    "wei": "uei",
    "wan": "uan",
    "wen": "uen",
    "wang": "uang",
    "weng": "ueng",
}

INITIAL_ORDER = sorted(INITIALS, key=len, reverse=True)

DOT_DIAMETER_MM = 1.6
DOT_SPACING_MM = 2.6
CELL_SPACING_MM = 6.4
CELL_WIDTH_MM = DOT_SPACING_MM + DOT_DIAMETER_MM
CELL_HEIGHT_MM = 5.2 + DOT_DIAMETER_MM
SYLLABLE_GAP_MM = 1.8


class BrailleError(ValueError):
    """Raised when a character has no verified pinyin reading."""


@dataclass(frozen=True)
class Syllable:
    text: str
    initial: str
    final: str
    tone: int
    cells: tuple[tuple[int, ...], ...]


@lru_cache(maxsize=1)
def pinyin_table() -> dict[str, str]:
    try:
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _split_tone(syllable: str) -> tuple[str, int]:
    if syllable and syllable[-1].isdigit():
        tone = int(syllable[-1])
        return syllable[:-1], (tone if 1 <= tone <= 5 else 5)
    return syllable, 5


def _initial_of(syllable: str) -> str:
    for candidate in INITIAL_ORDER:
        if syllable.startswith(candidate):
            return candidate
    return ""


def _canonical_final(initial: str, final: str) -> str:
    # The bundled table stores ü as ASCII "v" (lv4, nv3, lve4); the braille
    # tables below are keyed by the real finals (ü, üe, üan, ün).
    if final.startswith("v"):
        final = "ü" + final[1:]
    if initial in {"j", "q", "x"}:
        if final.startswith("ü"):
            return final
        if final == "u":
            return "ü"
        if final.startswith("u"):
            return "ü" + final[1:]
    if final == "iu":
        return "iou"
    if final == "ui":
        return "uei"
    if final == "un":
        return "uen"
    return final


def analyze_syllable(syllable: str) -> tuple[str, str, int]:
    """Return ``(initial, final, tone)`` for a pinyin syllable.

    ``syllable`` uses the TONE3 form produced by pypinyin (``zhong1``); a
    missing tone digit means the neutral tone.
    """
    body, tone = _split_tone(syllable.strip().lower())
    if not body:
        return "", "", tone
    if body in {"a", "o", "e", "ai", "ei", "ao", "ou", "an", "en", "ang", "eng", "er"}:
        return "", body, tone
    if body in Y_FINALS:
        return "", Y_FINALS[body], tone
    if body in W_FINALS:
        return "", W_FINALS[body], tone
    initial = _initial_of(body)
    if not initial:
        return "", body, tone
    final = body[len(initial) :]
    if initial in SYLLABIC_CONSONANTS and final == "i":
        final = ""
    elif final == "i" and initial in {"b", "p", "m", "d", "t", "n", "l", "j", "q", "x"}:
        final = "i"
    final = _canonical_final(initial, final)
    return initial, final, tone


def tone_is_omitted(syllable: str, tone: int, *, full_tone: bool = False) -> bool:
    if full_tone or tone in {0, 5}:
        return tone in {0, 5}
    body, _ = _split_tone(syllable.strip().lower())
    initial, final, _ = analyze_syllable(syllable)
    key = f"{body}{tone}" if tone in {1, 2, 3, 4} else body
    if key in KEEP_TONE:
        return False
    if key in OMIT_SPECIAL:
        return True
    if initial == "" and final == "o":
        return True
    if initial == "" and final:
        return tone == 4
    return OMIT_TONE.get(initial) == tone


def encode_syllable(syllable: str, *, full_tone: bool = False) -> Syllable:
    initial, final, tone = analyze_syllable(syllable)
    if initial and initial not in INITIALS:
        raise BrailleError(f"未收录的声母：{syllable}")
    if final and final not in FINALS:
        raise BrailleError(f"未收录的韵母：{syllable}")
    cells: list[tuple[int, ...]] = []
    if initial:
        cells.append(INITIALS[initial])
    if final:
        cells.append(FINALS[final])
    if not cells:
        raise BrailleError(f"空音节：{syllable}")
    if tone in TONES and not tone_is_omitted(syllable, tone, full_tone=full_tone):
        cells.append(TONES[tone])
    return Syllable(text=syllable, initial=initial, final=final, tone=tone, cells=tuple(cells))


def _digits_and_ascii(character: str) -> list[tuple[int, ...]]:
    from . import braille

    return braille.encode(character)


def encode_text(text: str, *, full_tone: bool = False) -> list[tuple[int, ...]]:
    """Encode a Chinese label, keeping ASCII runs in UEB.

    Spaces separate words; a space is one empty cell.  Unknown Chinese
    characters raise :class:`BrailleError` so a caller can fall back to an
    English label instead of printing an unrelated syllable.
    """
    table = pinyin_table()
    cells: list[tuple[int, ...]] = []
    ascii_run: list[str] = []

    def flush_ascii() -> None:
        if ascii_run:
            cells.extend(_digits_and_ascii("".join(ascii_run)))
            ascii_run.clear()

    for character in text:
        if character == " ":
            flush_ascii()
            cells.append(())
            continue
        if "\u4e00" <= character <= "\u9fff":
            flush_ascii()
            reading = table.get(character)
            if not reading:
                raise BrailleError(f"拼音表未收录字符：{character}")
            cells.extend(encode_syllable(reading, full_tone=full_tone).cells)
            continue
        if character.isascii():
            ascii_run.append(character)
            continue
        flush_ascii()
        if character in {"，", "、"}:
            cells.append((5,))
        elif character in {"。", "．"}:
            cells.append((5, 6))
        elif character == "：":
            cells.append((2, 5))
        elif character == "；":
            cells.append((2, 3))
        elif character == "（":
            cells.append((1, 2, 3, 5, 6))
        elif character == "）":
            cells.append((2, 3, 4, 5, 6))
        elif character == "·":
            cells.append((3,))
        else:
            raise BrailleError(f"未收录标点：{character}")
    flush_ascii()
    return cells


def _syllable_groups(text: str) -> list[list[tuple[int, ...]]]:
    """Split encoded cells into per-syllable groups for tactile spacing."""
    table = pinyin_table()
    groups: list[list[tuple[int, ...]]] = []
    ascii_run: list[str] = []
    for character in text:
        if "\u4e00" <= character <= "\u9fff":
            if ascii_run:
                groups.append(_digits_and_ascii("".join(ascii_run)))
                ascii_run.clear()
            reading = table.get(character)
            if not reading:
                raise BrailleError(f"拼音表未收录字符：{character}")
            groups.append(list(encode_syllable(reading).cells))
        elif character.isascii():
            ascii_run.append(character)
        else:
            if ascii_run:
                groups.append(_digits_and_ascii("".join(ascii_run)))
                ascii_run.clear()
            groups.append(list(encode_text(character)))
    if ascii_run:
        groups.append(_digits_and_ascii("".join(ascii_run)))
    return [group for group in groups if group]


def width(text: str, *, syllable_gap_mm: float = 0.0) -> float:
    groups = _syllable_groups(text)
    if not groups:
        return 0.0
    cell_count = sum(len(group) for group in groups)
    gaps = max(0, len(groups) - 1) * syllable_gap_mm
    return CELL_SPACING_MM * (cell_count - 1) + CELL_WIDTH_MM + gaps


def height() -> float:
    return CELL_HEIGHT_MM


def dot_centers(
    text: str,
    x: float,
    y: float,
    *,
    syllable_gap_mm: float = SYLLABLE_GAP_MM,
    dot_spacing_mm: float = DOT_SPACING_MM,
) -> list[tuple[float, float]]:
    """Return dot centres for a label whose top-left corner is ``x, y``."""
    groups = _syllable_groups(text)
    radius = DOT_DIAMETER_MM / 2.0
    centres: list[tuple[float, float]] = []
    cursor = x
    for group in groups:
        for cell in group:
            cell_x = cursor + radius
            cell_y = y + radius
            for dot in cell:
                column = 1 if dot >= 4 else 0
                row = (dot - 1) % 3
                centres.append((cell_x + column * dot_spacing_mm, cell_y + row * dot_spacing_mm))
            cursor += CELL_SPACING_MM
        cursor += syllable_gap_mm
    return centres


def bbox(text: str, x: float, y: float) -> tuple[float, float, float, float]:
    return (x, y, width(text, syllable_gap_mm=SYLLABLE_GAP_MM), height())


def readings(text: str) -> list[str]:
    table = pinyin_table()
    result: list[str] = []
    for character in text:
        if "\u4e00" <= character <= "\u9fff":
            reading = table.get(character)
            if not reading:
                raise BrailleError(f"拼音表未收录字符：{character}")
            result.append(reading)
    return result


def describe(text: str) -> list[dict[str, object]]:
    """Debug helper: show the reading and dot cells for each syllable."""
    table = pinyin_table()
    rows: list[dict[str, object]] = []
    for character in text:
        if "\u4e00" <= character <= "\u9fff":
            reading = table.get(character)
            if not reading:
                raise BrailleError(f"拼音表未收录字符：{character}")
            syllable = encode_syllable(reading)
            rows.append(
                {
                    "char": character,
                    "pinyin": reading,
                    "initial": syllable.initial,
                    "final": syllable.final,
                    "tone": syllable.tone,
                    "cells": [list(cell) for cell in syllable.cells],
                }
            )
    return rows


def describe_text(text: str) -> list[dict[str, object]]:
    """Show every printed cell, including digits, punctuation and spaces.

    ``describe`` only covers Chinese characters, which is enough for a
    title but not for a numbered legend such as ``1 翅膀``.  This helper is
    what the print-instructions decoder uses, so a sighted reader can match
    every dot group back to a syllable instead of guessing.
    """
    table = pinyin_table()
    rows: list[dict[str, object]] = []
    ascii_run: list[str] = []

    def flush_ascii() -> None:
        if not ascii_run:
            return
        chunk = "".join(ascii_run)
        rows.append(
            {
                "char": chunk,
                "kind": "ascii",
                "pinyin": "",
                "cells": [list(cell) for cell in _digits_and_ascii(chunk)],
            }
        )
        ascii_run.clear()

    for character in text:
        if character == " ":
            flush_ascii()
            rows.append({"char": "空格", "kind": "space", "pinyin": "", "cells": [[]]})
        elif "\u4e00" <= character <= "\u9fff":
            flush_ascii()
            reading = table.get(character)
            if not reading:
                raise BrailleError(f"拼音表未收录字符：{character}")
            syllable = encode_syllable(reading)
            rows.append(
                {
                    "char": character,
                    "kind": "han",
                    "pinyin": reading,
                    "initial": syllable.initial,
                    "final": syllable.final,
                    "tone": syllable.tone,
                    "cells": [list(cell) for cell in syllable.cells],
                }
            )
        elif character.isascii():
            ascii_run.append(character)
        else:
            flush_ascii()
            rows.append(
                {
                    "char": character,
                    "kind": "punct",
                    "pinyin": "",
                    "cells": [list(cell) for cell in encode_text(character)],
                }
            )
    flush_ascii()
    return rows
