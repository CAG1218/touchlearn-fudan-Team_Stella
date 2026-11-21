"""Chinese Common Braille (GF 0019-2018) encoding and cell geometry.

The 2018 scheme keeps the 1995 sound symbols but marks tone with a separate
cell after the initial and final, then applies a set of tone-omission rules.
This module implements the sound tables, the spelling rules and the level-2
tone-omission rules that matter for short labels.  It never guesses a reading:
a character outside the bundled GB2312 pinyin table raises
:class:`BrailleError` instead of silently producing an unrelated syllable.

The bundled table contains the most common reading of each GB2312 character.
That default is wrong for some domain words (对称轴 was read as ``cheng1``,
夹爪 as ``zhao3``), so labels are segmented against a small phrase lexicon
before the per-character table is used.  The same segmentation drives
GF 0019 分词连写: syllables inside one word are written without extra gaps,
while two adjacent words are separated by one blank cell.
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
# 6.4 mm pitch leaves a 2.2 mm clear gap between cells.  FDM printers with a
# 0.4 mm nozzle can bridge that gap at 0.2 mm layers and merge neighbouring
# cells, so the pitch carries 0.5 mm of extra clearance.  Dot spacing inside a
# cell is unchanged because it is the parameter the standards actually cover.
CELL_SPACING_MM = 6.9
CELL_WIDTH_MM = DOT_SPACING_MM + DOT_DIAMETER_MM
CELL_HEIGHT_MM = 5.2 + DOT_DIAMETER_MM
# Kept for callers that still pass it explicitly.  GF 0019 uses a blank cell
# between words, so no millimetre gap is added by default.
SYLLABLE_GAP_MM = 0.0

# Phrase-level readings.  Longest match wins.  Besides fixing polyphones, each
# entry is one word for 分词连写 and therefore gets no internal blank cell.
PHRASES: dict[str, tuple[str, ...]] = {
    "二次函数图像": ("er4", "ci4", "han2", "shu4", "tu2", "xiang4"),
    "二次函数": ("er4", "ci4", "han2", "shu4"),
    "函数图像": ("han2", "shu4", "tu2", "xiang4"),
    "函数": ("han2", "shu4"),
    "图像": ("tu2", "xiang4"),
    "对称轴": ("dui4", "chen4", "zhou2"),
    "对称": ("dui4", "chen4"),
    "横轴": ("heng2", "zhou2"),
    "纵轴": ("zong4", "zhou2"),
    "原点": ("yuan2", "dian3"),
    "顶点": ("ding3", "dian3"),
    "零点": ("ling2", "dian3"),
    "交点": ("jiao1", "dian3"),
    "截距": ("jie2", "ju4"),
    "极小值": ("ji2", "xiao3", "zhi2"),
    "极大值": ("ji2", "da4", "zhi2"),
    "定义域": ("ding4", "yi4", "yu4"),
    "值域": ("zhi2", "yu4"),
    "曲线": ("qu1", "xian4"),
    "横坐标": ("heng2", "zuo4", "biao1"),
    "纵坐标": ("zong4", "zuo4", "biao1"),
    "坐标": ("zuo4", "biao1"),
    "六轴机械臂": ("liu4", "zhou2", "ji1", "xie4", "bi4"),
    "机械臂": ("ji1", "xie4", "bi4"),
    "关节": ("guan1", "jie2"),
    "连杆": ("lian2", "gan1"),
    "基座": ("ji1", "zuo4"),
    "底座": ("di3", "zuo4"),
    "夹爪": ("jia1", "zhua3"),
    "末端执行器": ("mo4", "duan1", "zhi2", "xing2", "qi4"),
    "末端": ("mo4", "duan1"),
    "执行器": ("zhi2", "xing2", "qi4"),
    "自由度": ("zi4", "you2", "du4"),
    "三视图": ("san1", "shi4", "tu2"),
    "照片重建": ("zhao4", "pian4", "chong2", "jian4"),
    "触觉": ("chu4", "jue2"),
    "盲文": ("mang2", "wen2"),
    "中文盲文": ("zhong1", "wen2", "mang2", "wen2"),
    "毫米": ("hao2", "mi3"),
    "比例": ("bi3", "li4"),
    "方向": ("fang1", "xiang4"),
    "太阳系": ("tai4", "yang2", "xi4"),
    "站立小狗": ("zhan4", "li4", "xiao3", "gou3"),
    "小狗": ("xiao3", "gou3"),
    "小鸟": ("xiao3", "niao3"),
    "翅膀": ("chi4", "bang3"),
    "头部": ("tou2", "bu4"),
    "手臂": ("shou3", "bi4"),
    "尾巴": ("wei3", "ba5"),
    "触见": ("chu4", "jian4"),
}

# Single-character corrections that are unambiguous in tactile textbook labels.
CHAR_FIXES: dict[str, str] = {
    "爪": "zhua3",
}

MAX_PHRASE = max(len(key) for key in PHRASES)


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


PUNCTUATION_CELLS: dict[str, tuple[int, ...]] = {
    "，": (5,),
    "、": (5,),
    "。": (5, 6),
    "．": (5, 6),
    "：": (2, 5),
    "；": (2, 3),
    "（": (1, 2, 3, 5, 6),
    "）": (2, 3, 4, 5, 6),
    "·": (3,),
    "！": (2, 3, 5),
    "？": (2, 6),
}


def _is_han(character: str) -> bool:
    return "\u4e00" <= character <= "\u9fff"


def _phrase_at(text: str, index: int) -> str | None:
    for length in range(min(MAX_PHRASE, len(text) - index), 1, -1):
        candidate = text[index : index + length]
        if candidate in PHRASES:
            return candidate
    return None


def _reading_of(character: str, table: dict[str, str]) -> str:
    reading = CHAR_FIXES.get(character) or table.get(character)
    if not reading:
        raise BrailleError(f"拼音表未收录字符：{character}")
    return reading


def _tokens(text: str) -> list[tuple[str, list[tuple[int, ...]]]]:
    """Split ``text`` into word / ASCII / punctuation / space tokens.

    A Chinese word is either a lexicon phrase or a maximal run of characters
    that do not start another phrase.  This deliberately under-segments rather
    than inserting a space between every character, which is what the old
    implementation did and which broke GF 0019 分词连写.
    """
    table = pinyin_table()
    tokens: list[tuple[str, list[tuple[int, ...]]]] = []
    index = 0
    length = len(text)
    while index < length:
        character = text[index]
        if character == " ":
            tokens.append(("space", [()]))
            index += 1
            continue
        if _is_han(character):
            word = _phrase_at(text, index)
            if word:
                cells: list[tuple[int, ...]] = []
                readings = PHRASES[word]
                if len(readings) != len(word):
                    raise BrailleError(f"词条音节数与字数不一致：{word}")
                for reading in readings:
                    cells.extend(encode_syllable(reading).cells)
                tokens.append(("word", cells))
                index += len(word)
                continue
            end = index + 1
            while end < length and _is_han(text[end]) and _phrase_at(text, end) is None:
                end += 1
            cells = []
            for item in text[index:end]:
                cells.extend(encode_syllable(_reading_of(item, table)).cells)
            tokens.append(("word", cells))
            index = end
            continue
        if character.isascii():
            end = index
            while end < length and text[end].isascii() and text[end] != " ":
                end += 1
            tokens.append(("ascii", _digits_and_ascii(text[index:end])))
            index = end
            continue
        cell = PUNCTUATION_CELLS.get(character)
        if cell is None:
            raise BrailleError(f"未收录标点：{character}")
        tokens.append(("punct", [cell]))
        index += 1
    return tokens


def _layout_cells(text: str) -> list[tuple[int, ...]]:
    """Flatten tokens into physical cells, inserting one blank between words."""
    cells: list[tuple[int, ...]] = []
    previous_word = False
    for kind, group in _tokens(text):
        if kind == "space":
            cells.append(())
            previous_word = False
            continue
        if kind == "word":
            if previous_word:
                cells.append(())
            cells.extend(group)
            previous_word = True
            continue
        cells.extend(group)
        previous_word = False
    return cells


def encode_text(text: str, *, full_tone: bool = False) -> list[tuple[int, ...]]:
    """Encode a Chinese label, keeping ASCII runs in UEB.

    Spaces separate words; a space is one empty cell.  Unknown Chinese
    characters raise :class:`BrailleError` so a caller can fall back to an
    English label instead of printing an unrelated syllable.
    """
    if full_tone:
        # Re-encode Chinese characters with the tone cells forced on.  The
        # phrase lexicon stores TONE3 readings, so expanding them again keeps
        # word segmentation intact.
        cells: list[tuple[int, ...]] = []
        table = pinyin_table()
        index = 0
        while index < len(text):
            character = text[index]
            if _is_han(character):
                word = _phrase_at(text, index)
                if word:
                    for reading in PHRASES[word]:
                        cells.extend(encode_syllable(reading, full_tone=True).cells)
                    index += len(word)
                    continue
                cells.extend(encode_syllable(_reading_of(character, table), full_tone=True).cells)
                index += 1
                continue
            if character.isascii():
                end = index
                while end < len(text) and text[end].isascii() and text[end] != " ":
                    end += 1
                cells.extend(_digits_and_ascii(text[index:end]))
                index = end
                continue
            if character == " ":
                cells.append(())
            else:
                cell = PUNCTUATION_CELLS.get(character)
                if cell is None:
                    raise BrailleError(f"未收录标点：{character}")
                cells.append(cell)
            index += 1
        return cells
    return _layout_cells(text)


def _syllable_groups(text: str) -> list[list[tuple[int, ...]]]:
    """Backward-compatible word groups (each group is one 词)."""
    return [list(group) for kind, group in _tokens(text) if kind == "word" and group]


def width(text: str, *, syllable_gap_mm: float = 0.0) -> float:
    cells = _layout_cells(text)
    if not cells:
        return 0.0
    gaps = 0.0
    if syllable_gap_mm:
        gaps = max(0, len(_syllable_groups(text)) - 1) * syllable_gap_mm
    return CELL_SPACING_MM * (len(cells) - 1) + CELL_WIDTH_MM + gaps


def height() -> float:
    return CELL_HEIGHT_MM


def dot_centers(
    text: str,
    x: float,
    y: float,
    *,
    syllable_gap_mm: float = 0.0,
    dot_spacing_mm: float = DOT_SPACING_MM,
) -> list[tuple[float, float]]:
    """Return dot centres for a label whose top-left corner is ``x, y``."""
    cells = _layout_cells(text)
    radius = DOT_DIAMETER_MM / 2.0
    centres: list[tuple[float, float]] = []
    cursor = x
    for cell in cells:
        cell_x = cursor + radius
        cell_y = y + radius
        for dot in cell:
            column = 1 if dot >= 4 else 0
            row = (dot - 1) % 3
            centres.append((cell_x + column * dot_spacing_mm, cell_y + row * dot_spacing_mm))
        cursor += CELL_SPACING_MM
    return centres


def bbox(text: str, x: float, y: float) -> tuple[float, float, float, float]:
    return (x, y, width(text), height())


def _char_readings(text: str) -> list[str]:
    table = pinyin_table()
    result: list[str] = []
    index = 0
    while index < len(text):
        character = text[index]
        if _is_han(character):
            word = _phrase_at(text, index)
            if word:
                result.extend(PHRASES[word])
                index += len(word)
                continue
            result.append(_reading_of(character, table))
        index += 1
    return result


def readings(text: str) -> list[str]:
    return _char_readings(text)


def _reading_at(text: str, index: int) -> str:
    table = pinyin_table()
    word = _phrase_at(text, index)
    if word:
        # The caller walks character by character; return the reading for the
        # first syllable of the phrase so the rows stay aligned.
        return PHRASES[word][0]
    return _reading_of(text[index], table)


def describe(text: str) -> list[dict[str, object]]:
    """Debug helper: show the reading and dot cells for each syllable."""
    table = pinyin_table()
    rows: list[dict[str, object]] = []
    phrase_offset = 0
    phrase: str | None = None
    for index, character in enumerate(text):
        if not _is_han(character):
            continue
        if phrase is None or phrase_offset >= len(phrase):
            phrase = _phrase_at(text, index)
            phrase_offset = 0
        if phrase:
            reading = PHRASES[phrase][phrase_offset]
            phrase_offset += 1
            if phrase_offset >= len(phrase):
                phrase = None
        else:
            reading = _reading_of(character, table)
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
    phrase: str | None = None
    phrase_offset = 0

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

    for index, character in enumerate(text):
        if character == " ":
            flush_ascii()
            rows.append({"char": "空格", "kind": "space", "pinyin": "", "cells": [[]]})
            phrase = None
        elif _is_han(character):
            flush_ascii()
            if phrase is None or phrase_offset >= len(phrase):
                phrase = _phrase_at(text, index)
                phrase_offset = 0
            if phrase:
                reading = PHRASES[phrase][phrase_offset]
                phrase_offset += 1
                if phrase_offset >= len(phrase):
                    phrase = None
            else:
                reading = _reading_of(character, table)
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
            phrase = None
            cell = PUNCTUATION_CELLS.get(character)
            if cell is None:
                raise BrailleError(f"未收录标点：{character}")
            rows.append(
                {
                    "char": character,
                    "kind": "punct",
                    "pinyin": "",
                    "cells": [list(cell)],
                }
            )
    flush_ascii()
    return rows
