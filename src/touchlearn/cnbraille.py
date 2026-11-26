"""Chinese Common Braille (GF 0019-2018) encoding and cell geometry.

The 2018 scheme keeps the 1995 sound symbols but marks tone with a separate
cell after the initial and final, then applies a set of tone-omission rules.
This module implements the sound tables, the spelling rules, the level-2
tone-omission rules and the §11 定字简写 that matter for short labels.  It never
guesses a reading: a character outside the bundled GB2312 pinyin table raises
:class:`BrailleError` instead of silently producing an unrelated syllable.

The bundled table contains the most common reading of each GB2312 character.
That default is wrong for some domain words (对称轴 was read as ``cheng1``,
夹爪 as ``zhao3``), so labels are segmented against a phrase lexicon before the
per-character table is used.  The same segmentation drives GF 0019 分词连写:
syllables inside one word are written without extra gaps, while two adjacent
words are separated by one blank cell.

Maths runs are not spelled as pinyin.  GB/T 18028-2010 supplies the 数号 and
operator cells through :mod:`touchlearn.mathsymbols`; a symbol that is not in
that checked table raises :class:`mathsymbols.MathSymbolError`, so a plate
never carries a made-up dot pattern for ``²`` or a bracket.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable

from . import mathsymbols

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
# GF 0019 10.2.1–10.2.6 exceptions: these syllables keep the tone that the
# ordinary rule would drop.  The zero-initial syllables (yi/er/wo/ye/you) only
# have their *listed* tone omitted (see OMIT_SPECIAL); their other tones still
# follow the 韵母自成音节省去声 rule, so e.g. 二 èr is a single cell.
KEEP_TONE = {"tou2", "le4", "zi4", "e4", "wen4"}
OMIT_SPECIAL = {"yi1", "er2", "wo3", "ye3", "you3"}
# 10.2.8 需要区分音义时保调：规则原例“再”。 在 zài stays unmarked, so this
# has to be keyed by the character rather than by the syllable alone.
KEEP_TONE_CHARS = {"再": "zai4"}

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
    # Longest match wins, so a phrase that spans two words would silently
    # remove the 分词空方 between them.  二次函数图像 and 六轴机械臂 are kept
    # as word pairs (二次函数 / 图像, 六轴 / 机械臂) for that reason.
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
    "六轴": ("liu4", "zhou2"),
    "机械臂": ("ji1", "xie4", "bi4"),
    "关节": ("guan1", "jie2"),
    "连杆": ("lian2", "gan1"),
    "基座": ("ji1", "zuo4"),
    "底座": ("di3", "zuo4"),
    "夹爪": ("jia1", "zhua3"),
    "末端": ("mo4", "duan1"),
    "执行器": ("zhi2", "xing2", "qi4"),
    "自由度": ("zi4", "you2", "du4"),
    "三视图": ("san1", "shi4", "tu2"),
    "照片": ("zhao4", "pian4"),
    "重建": ("chong2", "jian4"),
    "触觉": ("chu4", "jue2"),
    "触摸": ("chu4", "mo1"),
    "学习": ("xue2", "xi2"),
    "盲文": ("mang2", "wen2"),
    "中文": ("zhong1", "wen2"),
    "毫米": ("hao2", "mi3"),
    "单位": ("dan1", "wei4"),
    "比例": ("bi3", "li4"),
    "方向": ("fang1", "xiang4"),
    "太阳系": ("tai4", "yang2", "xi4"),
    "站立": ("zhan4", "li4"),
    "小狗": ("xiao3", "gou3"),
    "小鸟": ("xiao3", "niao3"),
    "翅膀": ("chi4", "bang3"),
    "头部": ("tou2", "bu4"),
    "手臂": ("shou3", "bi4"),
    "尾巴": ("wei3", "ba5"),
    "触见": ("chu4", "jian4"),
    # 几何与函数板的常用词。 既是分词单位，也避免“平方”这类词在长句里
    # 被拆成一个没有停顿的音节串。
    "平方": ("ping2", "fang1"),
    "立方": ("li4", "fang1"),
    "绝对值": ("jue2", "dui4", "zhi2"),
    "三角形": ("san1", "jiao3", "xing2"),
    "长方形": ("chang2", "fang1", "xing2"),
    "正方形": ("zheng4", "fang1", "xing2"),
    "梯形": ("ti1", "xing2"),
    "半径": ("ban4", "jing4"),
    "直径": ("zhi2", "jing4"),
    "周长": ("zhou1", "chang2"),
    "面积": ("mian4", "ji1"),
    "体积": ("ti3", "ji1"),
    "角度": ("jiao3", "du4"),
    "垂直": ("chui2", "zhi2"),
    "平行": ("ping2", "xing2"),
    "相交": ("xiang1", "jiao1"),
    "斜率": ("xie2", "lv4"),
    "凸起": ("tu1", "qi3"),
    "凹陷": ("ao1", "xian4"),
    "平面": ("ping2", "mian4"),
    "立体": ("li4", "ti3"),
    "距离": ("ju4", "li2"),
    "间距": ("jian1", "ju4"),
    "厚度": ("hou4", "du4"),
    "长度": ("chang2", "du4"),
    "宽度": ("kuan1", "du4"),
    "高度": ("gao1", "du4"),
    "刻度": ("ke4", "du4"),
    "网格": ("wang3", "ge2"),
    "箭头": ("jian4", "tou2"),
    "标注": ("biao1", "zhu4"),
    "步骤": ("bu4", "zhou4"),
    "练习": ("lian4", "xi4"),
    "题目": ("ti2", "mu4"),
    "答案": ("da2", "an4"),
    "提示": ("ti2", "shi4"),
    "观察": ("guan1", "cha2"),
    # 场景模板、图例与页脚实际会写上的词。 词典收录一个词，就少一次“整段
    # 连续汉字按一个词连写”的回退；这些条目同时也是多音字的读音校正。
    "其它": ("qi2", "ta1"),
    "标记": ("biao1", "ji4"),
    "抛物线": ("pao1", "wu4", "xian4"),
    "视图": ("shi4", "tu2"),
    "侧视图": ("ce4", "shi4", "tu2"),
    "第一": ("di4", "yi1"),
    "第二": ("di4", "er4"),
    "第三": ("di4", "san1"),
    "第四": ("di4", "si4"),
    "读取": ("du2", "qu3"),
    "输入": ("shu1", "ru4"),
    "输出": ("shu1", "chu1"),
    "判断": ("pan4", "duan4"),
    "开始": ("kai1", "shi3"),
    "结束": ("jie2", "shu4"),
    "连接": ("lian2", "jie1"),
    "象限": ("xiang4", "xian4"),
    "路线": ("lu4", "xian4"),
    "路口": ("lu4", "kou3"),
    "学校": ("xue2", "xiao4"),
    "公园": ("gong1", "yuan2"),
    "柱子": ("zhu4", "zi5"),
    "编号": ("bian1", "hao4"),
    "类别": ("lei4", "bie2"),
    "坐标轴": ("zuo4", "biao1", "zhou2"),
    "直角": ("zhi2", "jiao3"),
    "直角三角形": ("zhi2", "jiao3", "san1", "jiao3", "xing2"),
    "边长": ("bian1", "chang2"),
    "示意": ("shi4", "yi4"),
    "说明": ("shuo1", "ming2"),
    "标题": ("biao1", "ti2"),
    "图例": ("tu2", "li4"),
    "数字": ("shu4", "zi4"),
    "字母": ("zi4", "mu3"),
    "符号": ("fu2", "hao4"),
    "圆点": ("yuan2", "dian3"),
    "起点": ("qi3", "dian3"),
    "终点": ("zhong1", "dian3"),
    "中心": ("zhong1", "xin1"),
    "边缘": ("bian1", "yuan2"),
    "顶部": ("ding3", "bu4"),
    "底部": ("di3", "bu4"),
    "左侧": ("zuo3", "ce4"),
    "右侧": ("you4", "ce4"),
    "上方": ("shang4", "fang1"),
    "下方": ("xia4", "fang1"),
    "内部": ("nei4", "bu4"),
    "外部": ("wai4", "bu4"),
    "之间": ("zhi1", "jian1"),
    "位置": ("wei4", "zhi4"),
    "图形": ("tu2", "xing2"),
    "形状": ("xing2", "zhuang4"),
    "结构": ("jie2", "gou4"),
    "部分": ("bu4", "fen5"),
    "元件": ("yuan2", "jian4"),
    "顺序": ("shun4", "xu4"),
    "电池": ("dian4", "chi2"),
    "电路": ("dian4", "lu4"),
    "电流": ("dian4", "liu2"),
    "电压": ("dian4", "ya1"),
    "电阻": ("dian4", "zu3"),
    "开关": ("kai1", "guan1"),
    "导线": ("dao3", "xian4"),
    "正极": ("zheng4", "ji2"),
    "负极": ("fu4", "ji2"),
    "灯泡": ("deng1", "pao4"),
    "液泡": ("ye4", "pao4"),
    "细胞膜": ("xi4", "bao1", "mo2"),
    "细胞核": ("xi4", "bao1", "he2"),
    "线粒体": ("xian4", "li4", "ti3"),
    "太阳": ("tai4", "yang2"),
    "地球": ("di4", "qiu2"),
    "月球": ("yue4", "qiu2"),
    "行星": ("xing2", "xing1"),
    "轨道": ("gui3", "dao4"),
    "同心": ("tong2", "xin1"),
    "转动": ("zhuan4", "dong4"),
    "屏幕": ("ping2", "mu4"),
    "按钮": ("an4", "niu3"),
    "手指": ("shou3", "zhi3"),
    "阅读": ("yue4", "du2"),
    # 虚词表会把“的”单独分写；这些词里的“的”是词内成分，必须整词匹配。
    "目的": ("mu4", "di4"),
    "的确": ("di2", "que4"),
    "有的": ("you3", "de5"),
    "别的": ("bie2", "de5"),
    "似的": ("shi4", "de5"),
}

# Single-character corrections that are unambiguous in tactile textbook labels.
CHAR_FIXES: dict[str, str] = {
    "爪": "zhua3",
}

# GF 0019 11.1 定字简写.  These six characters are printed with a fixed cell
# (or cell pair) instead of their full 声母 + 韵母 + 声调 spelling:
#   的 d=(145)  么 m=(134)  你 n=(1345)
#   他 t=(2345)  她 t+1点=(2345)(1)  它 4点+t=(4)(2345)
SHORTHAND: dict[str, tuple[tuple[int, ...], ...]] = {
    "的": ((1, 4, 5),),
    "么": ((1, 3, 4),),
    "你": ((1, 3, 4, 5),),
    "他": ((2, 3, 4, 5),),
    "她": ((2, 3, 4, 5), (1,)),
    "它": ((4,), (2, 3, 4, 5)),
}

# 11.2: these five drop the shorthand when the next syllable is 连写 in the
# same word and is 韵母自成音节 (zero initial).  “她” is not on that list.
SHORTHAND_BLOCKED_BEFORE_ZERO_FINAL = frozenset({"的", "么", "你", "他", "它"})

# The contracted cell stands for the character in its reading.  的 in 的确
# (díquè) or 目的 (mùdì) is a different morpheme and keeps its full spelling;
# so does anyone reading the character with an unexpected tone base.
SHORTHAND_READINGS: dict[str, str] = {
    "的": "de",
    "么": "me",
    "你": "ni",
    "他": "ta",
    "她": "ta",
    "它": "ta",
}

# 12.2 / 6.1.9.1: 结构助词分写.  Without this split the fallback segmenter glues
# 的 onto the run before it, and the label loses its 分词空方.  Only 的 is
# listed for now: 地/得 need a much larger lexicon before splitting is safe
# (地球、得到 …), so they stay inside the word they are written in.
PARTICLES = frozenset({"的"})

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


def tone_is_omitted(
    syllable: str,
    tone: int,
    *,
    full_tone: bool = False,
    keep_tone: bool = False,
    character: str | None = None,
) -> bool:
    if full_tone:
        return tone in {0, 5}
    if keep_tone:
        # GF 0019 3.2.5: a 声母自成音节 that is followed, inside the same
        # word, by a 韵母自成音节 keeps its tone (慈爱, 自 + 由).
        return tone in {0, 5}
    if tone in {0, 5}:
        return True
    body, _ = _split_tone(syllable.strip().lower())
    initial, final, _ = analyze_syllable(syllable)
    key = f"{body}{tone}" if tone in {1, 2, 3, 4} else body
    if character is not None and KEEP_TONE_CHARS.get(character) == key:
        return False
    if key in KEEP_TONE:
        return False
    if key in OMIT_SPECIAL:
        return True
    if initial == "" and final == "o":
        return True
    if initial == "" and final:
        return tone == 4
    return OMIT_TONE.get(initial) == tone


def encode_syllable(
    syllable: str,
    *,
    full_tone: bool = False,
    keep_tone: bool = False,
    character: str | None = None,
) -> Syllable:
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
    if tone in TONES and not tone_is_omitted(
        syllable, tone, full_tone=full_tone, keep_tone=keep_tone, character=character
    ):
        cells.append(TONES[tone])
    return Syllable(text=syllable, initial=initial, final=final, tone=tone, cells=tuple(cells))


# GF 0019 §8 中文标点。  Values are cell *sequences*, because many Chinese
# marks need two or three cells (。= ⠐⠆, ？= ⠐⠄, ！= ⠰⠂).  Using the UEB
# English table here produced the wrong dots for every mark except the comma.
PUNCTUATION_CELLS: dict[str, tuple[tuple[int, ...], ...]] = {
    "。": ((5,), (2, 3)),
    "．": ((5,), (2, 3)),
    "，": ((5,),),
    "、": ((4,),),
    "：": ((3, 6),),
    "；": ((5, 6),),
    "（": ((5, 6), (3,)),
    "）": ((6,), (2, 3)),
    "！": ((5, 6), (2,)),
    "？": ((5,), (3,)),
    "“": ((4, 5),),
    "”": ((4, 5),),
    "‘": ((4, 5), (4, 5)),
    "’": ((4, 5), (4, 5)),
    "【": ((5, 6), (2, 3)),
    "】": ((5, 6), (2, 3)),
    "《": ((5,), (3, 6)),
    "》": ((3, 6), (2,)),
    "…": ((5,), (5,), (5,)),
    "—": ((6,), (3, 6)),
    "－": ((3, 6),),
    "·": ((6,), (3,)),
}

# Doubled marks are written once, not twice (GF 0019 §8).
MULTI_PUNCTUATION: dict[str, tuple[tuple[int, ...], ...]] = {
    "——": ((6,), (3, 6)),
    "……": ((5,), (5,), (5,)),
}


def _punct_cells(mark: str) -> tuple[tuple[int, ...], ...]:
    return MULTI_PUNCTUATION.get(mark) or PUNCTUATION_CELLS[mark]


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


def _is_math_atom(character: str) -> bool:
    """ASCII text and the GB/T 18028 operator signs belong to a maths run."""
    return character.isascii() and not character.isspace()


def _units(text: str) -> list[tuple[str, object]]:
    """Split ``text`` into word / maths / punctuation / space units.

    A Chinese word is either a lexicon phrase or a maximal run of characters
    that neither start another phrase nor are a 结构助词.  This deliberately
    under-segments rather than inserting a space between every character,
    which is what the old implementation did and which broke GF 0019 分词连写.
    Each word carries its per-character readings so the dots, the width and the
    printed decode sheet are all derived from the same segmentation.

    A maths run starts with an ASCII character and continues through ASCII and
    the GB/T 18028 operator signs.  The 间隔号 ``·`` is only pulled in when it
    sits between two maths characters; ``卡尔·马克思`` stays prose.
    """
    table = pinyin_table()
    units: list[tuple[str, object]] = []
    index = 0
    length = len(text)
    while index < length:
        character = text[index]
        if character == " ":
            units.append(("space", None))
            index += 1
            continue
        if _is_han(character):
            word = _phrase_at(text, index)
            if word:
                readings = PHRASES[word]
                if len(readings) != len(word):
                    raise BrailleError(f"词条音节数与字数不一致：{word}")
                units.append(("word", list(zip(word, readings))))
                index += len(word)
                continue
            if character in PARTICLES:
                units.append(("word", [(character, _reading_of(character, table))]))
                index += 1
                continue
            end = index + 1
            while (
                end < length
                and _is_han(text[end])
                and _phrase_at(text, end) is None
                and text[end] not in PARTICLES
            ):
                end += 1
            run = text[index:end]
            units.append(("word", [(item, _reading_of(item, table)) for item in run]))
            index = end
            continue
        if _is_math_atom(character):
            end = index
            while end < length:
                current = text[end]
                if _is_math_atom(current) or current in mathsymbols.MATH_RUN_CHARS:
                    end += 1
                    continue
                if (
                    current == "·"
                    and end + 1 < length
                    and (_is_math_atom(text[end + 1]) or text[end + 1] in mathsymbols.MATH_RUN_CHARS)
                ):
                    end += 1
                    continue
                break
            units.append(("math", text[index:end]))
            index = end
            continue
        pair = text[index : index + 2]
        if pair in MULTI_PUNCTUATION:
            units.append(("punct", pair))
            index += 2
            continue
        if character not in PUNCTUATION_CELLS:
            raise BrailleError(f"未收录标点：{character}")
        units.append(("punct", character))
        index += 1
    return units


@dataclass(frozen=True)
class Piece:
    """One printed cell group plus the row a sighted reader sees in the sheet.

    ``encode_text``, :func:`width` and :func:`describe_text` all flatten the
    same list of pieces, so the dots on the plate, the millimetres reserved for
    them and the decode sheet cannot drift apart.
    """

    kind: str  # han | math | punct | space | blank
    text: str
    cells: tuple[tuple[int, ...], ...]
    pinyin: str = ""
    initial: str = ""
    final: str = ""
    tone: int = 0
    shorthand: bool = False
    symbol: str = ""


def _word_pieces(items: list[tuple[str, str]], *, full_tone: bool = False) -> list[Piece]:
    """Encode one 连写 word: §11 shorthand, §11.2 blocking, 3.2.5 keep-tone."""
    parsed = [analyze_syllable(reading) for _, reading in items]
    pieces: list[Piece] = []
    for position, (character, reading) in enumerate(items):
        initial, final, tone = parsed[position]
        keep_tone = False
        if position + 1 < len(parsed):
            next_initial, _, _ = parsed[position + 1]
            if initial in SYLLABIC_CONSONANTS and final == "" and next_initial == "":
                keep_tone = True
        contracted: tuple[tuple[int, ...], ...] | None = None
        body, _ = _split_tone(reading.strip().lower())
        if not full_tone and SHORTHAND_READINGS.get(character) == body:
            blocked = (
                character in SHORTHAND_BLOCKED_BEFORE_ZERO_FINAL
                and position + 1 < len(parsed)
                and parsed[position + 1][0] == ""
            )
            if not blocked:
                contracted = SHORTHAND[character]
        if contracted is not None:
            pieces.append(
                Piece(
                    "han",
                    character,
                    contracted,
                    pinyin=reading,
                    initial=initial,
                    final=final,
                    tone=tone,
                    shorthand=True,
                )
            )
            continue
        syllable = encode_syllable(
            reading, full_tone=full_tone, keep_tone=keep_tone, character=character
        )
        pieces.append(
            Piece(
                "han",
                character,
                syllable.cells,
                pinyin=reading,
                initial=syllable.initial,
                final=syllable.final,
                tone=syllable.tone,
            )
        )
    return pieces


def _math_pieces(run: str) -> list[Piece]:
    """Turn one maths run into GB/T 18028 cells, failing closed."""
    try:
        symbols = mathsymbols.tokens(run)
    except mathsymbols.MathSymbolError as error:
        raise BrailleError(f"数学符号无法按 GB/T 18028 编码：{error}") from error
    pieces: list[Piece] = []
    for symbol in symbols:
        if symbol.space_before:
            pieces.append(Piece("blank", "空方", ((),), symbol="间隔"))
        pieces.append(Piece("math", symbol.text, symbol.cells, symbol=symbol.name))
    return pieces


def _pieces(text: str, *, full_tone: bool = False) -> list[Piece]:
    pieces: list[Piece] = []
    previous_word = False
    for kind, payload in _units(text):
        if kind == "space":
            pieces.append(Piece("space", "空格", ((),)))
            previous_word = False
            continue
        if kind == "word":
            if previous_word:
                pieces.append(Piece("blank", "分词空方", ((),)))
            pieces.extend(_word_pieces(list(payload), full_tone=full_tone))
            previous_word = True
            continue
        if kind == "math":
            pieces.extend(_math_pieces(str(payload)))
            previous_word = False
            continue
        pieces.append(Piece("punct", str(payload), _punct_cells(str(payload))))
        previous_word = False
    return pieces


def _layout_cells(text: str, *, full_tone: bool = False) -> list[tuple[int, ...]]:
    return [cell for piece in _pieces(text, full_tone=full_tone) for cell in piece.cells]


def encode_text(text: str, *, full_tone: bool = False) -> list[tuple[int, ...]]:
    """Encode a Chinese label, including its maths and punctuation cells.

    Spaces separate words; a space is one empty cell.  ``full_tone=True``
    spells every syllable out, which also disables the §11 shorthand.  Unknown
    characters and maths symbols outside the checked tables raise
    :class:`BrailleError` so a caller can shorten the label instead of printing
    an unrelated syllable.
    """
    return _layout_cells(text, full_tone=full_tone)


def _syllable_groups(text: str) -> list[list[tuple[int, ...]]]:
    """Backward-compatible word groups (each group is one 词)."""
    return [
        [cell for piece in _word_pieces(list(payload)) for cell in piece.cells]
        for kind, payload in _units(text)
        if kind == "word"
    ]


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


def unverified_words(text: str, *, minimum: int = 4) -> list[str]:
    """Return fallback word runs that the bundled lexicon did not cover.

    A run this long is likely more than one word, but without a full
    dictionary the segmenter cannot break it: it writes the characters with no
    internal blank cell.  The readings stay correct; the 分词空方 may not.  The
    caller reports the run instead of pretending the blank cells are verified.
    """
    found: list[str] = []
    for kind, payload in _units(text):
        if kind != "word":
            continue
        word = "".join(character for character, _ in payload)
        if len(word) >= minimum and word not in PHRASES:
            found.append(word)
    return found


def _reading_at(text: str, index: int) -> str:
    table = pinyin_table()
    word = _phrase_at(text, index)
    if word:
        # The caller walks character by character; return the reading for the
        # first syllable of the phrase so the rows stay aligned.
        return PHRASES[word][0]
    return _reading_of(text[index], table)


def describe(text: str) -> list[dict[str, object]]:
    """Debug helper: show the reading and dot cells for each Chinese syllable."""
    rows: list[dict[str, object]] = []
    for piece in _pieces(text):
        if piece.kind != "han":
            continue
        rows.append(
            {
                "char": piece.text,
                "pinyin": piece.pinyin,
                "initial": piece.initial,
                "final": piece.final,
                "tone": piece.tone,
                "shorthand": piece.shorthand,
                "cells": [list(cell) for cell in piece.cells],
            }
        )
    return rows


def describe_text(text: str, *, full_tone: bool = False) -> list[dict[str, object]]:
    """Show every printed cell: syllables, maths symbols, punctuation, blanks.

    The rows come from the same :func:`_pieces` pipeline that
    :func:`encode_text` uses, so the decode sheet and the physical dots always
    agree: one row per syllable or maths symbol, a ``分词空方`` row wherever a
    blank separates two words, and an ``空方`` row for the blank cells that
    GB/T 18028 puts before an operation sign.  ``shorthand`` marks the §11
    single-cell forms and ``symbol`` names the maths sign printed in that row.
    """
    rows: list[dict[str, object]] = []
    for piece in _pieces(text, full_tone=full_tone):
        row: dict[str, object] = {
            "char": piece.text,
            "kind": piece.kind,
            "pinyin": piece.pinyin,
            "cells": [list(cell) for cell in piece.cells],
        }
        if piece.kind == "han":
            row["initial"] = piece.initial
            row["final"] = piece.final
            row["tone"] = piece.tone
            row["shorthand"] = piece.shorthand
        elif piece.kind == "math":
            row["symbol"] = piece.symbol
        elif piece.kind == "blank":
            row["symbol"] = piece.symbol
        rows.append(row)
    return rows
    return rows
