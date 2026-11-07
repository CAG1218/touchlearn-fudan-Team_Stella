"""Deterministic voice dialog for blind and low-vision users.

The module owns two things:

* the spoken copy for every dialog step, and
* a rule based intent parser that turns one recognised sentence into the next
  dialog step.

It deliberately contains no model calls and no randomness: the same utterance
and the same context always produce the same answer, which keeps the voice
assistant predictable (and testable) on an offline competition machine.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Final

# A spoken sentence is short; anything longer is a paste, not speech.
MAX_UTTERANCE: Final = 400

# Stages the client persists between turns.
STAGES: Final = ("greet", "goal", "mode", "detail", "confirm")

MODE_IDS: Final = ("tactile", "model3d", "reconstruct")
MODE_NAMES: Final = {
    "tactile": "触觉教材",
    "model3d": "触觉三维模型",
    "reconstruct": "照片级三维重建",
}
MODE_HINTS: Final = {
    "tactile": "适合打印成 A5 到 A3 的平面触觉板",
    "model3d": "带底座和中文盲文标注，可以切片打印",
    "reconstruct": "需要先上传一到四张同一物体的照片",
}

# ---------------------------------------------------------------------------
# Spoken copy
# ---------------------------------------------------------------------------

PROMPTS: Final[dict[str, str]] = {
    "greeting": (
        "您好，这里是触见。需要开启盲人语音模式吗？"
        "开启后，我会把每一步念给您听。说 开启，或者 不用。"
    ),
    "enabled": (
        "好的，语音模式已开启。我会一直听着，您说完一句，我就接着问下一句。"
        "请问您想做什么呢？比如一个六轴机械臂的触觉板，或者太阳系的三维模型。"
    ),
    "enabled_short": "好的，语音模式已开启。",
    "disabled": "好的，语音模式已关闭。需要的时候，点屏幕右下角的语音助手按钮就能重新打开。",
    "ask_goal": "请问您想做什么样的触觉教材，或者三维模型？直接说出对象和用途就好。",
    "ask_mode": (
        "请问您想输出成哪一种？第一，触觉教材，可以打印成平面触觉板。"
        "第二，触觉三维模型，带底座和盲文标注。第三，照片级三维重建，需要上传照片。"
        "也可以说 都要。"
    ),
    "ask_mode_again": "抱歉，我没听清输出方式。可以说 触觉教材、三维模型、照片重建，或者 都要。",
    "ask_detail": "还需要补充尺寸、标签或者要突出的部位吗？可以说 没有了，也可以直接描述。",
    "confirm_now": "确认开始吗？说 开始，或者 修改。",
    "help": (
        "我现在可以帮您做三件事。第一，把一段描述变成可打印的触觉教材。"
        "第二，生成带底座和盲文标注的三维模型。第三，用照片重建实物。"
        "您可以随时说 再说一遍、修改、取消，或者 关闭语音。"
    ),
    "not_understood": "抱歉，我没有听清。可以再说一次，也可以点屏幕上的输入框用键盘输入。",
    "cancel": "好的，这次先不做。想重新开始时，说 重新开始。",
    "modify": "好的，请把新的要求说一遍，我会覆盖上一条。",
    "repeat_prefix": "我再念一遍。",
    "submit": "已经开始生成。这通常需要一到几分钟，完成后我会第一时间念给您听。",
    "photo_next": "照片级三维重建需要您随后上传一到四张同一物体的照片，页面上会有上传按钮。",
    "login_needed": (
        "请先登录。您可以让家人帮忙输入手机号和验证码，登录完成后我会自动继续。"
        "如果浏览器允许，也可以对我说 获取验证码。"
    ),
    "microphone_denied": (
        "浏览器没有拿到麦克风权限。请在地址栏旁边的权限设置里允许麦克风，然后重新开启语音模式。"
        "也可以先用键盘输入，我会把结果念给您听。"
    ),
    "unsupported": (
        "当前浏览器不支持语音识别。语音播报仍然可用，您可以用键盘输入需求，"
        "我会把每一步念给您听。推荐使用最新版的 Chrome 或 Edge。"
    ),
}

COMMANDS: Final = [
    "开启 / 不用：回答是否开启语音模式",
    "触觉教材 / 三维模型 / 照片重建 / 都要：选择输出方式",
    "开始 / 确认：确认并提交",
    "修改 / 重说：重新描述需求",
    "取消 / 不做：放弃当前需求",
    "再说一遍：重复上一条提示",
    "帮助：听一遍功能说明",
    "关闭语音：退出盲人语音模式",
]

# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

_STRIP_CHARS: Final = "，。！？、；：,.!?;:…—～~“”\"'‘’「」『』（）()【】[]《》<>·"
_STRIP_TABLE: Final = {ord(char): None for char in _STRIP_CHARS}
_SPACES: Final = re.compile(r"\s+")
_EDGE_FILLER: Final = re.compile(
    r"^(?:麻烦你|麻烦|请你|请|帮我|帮忙|我想|我想要|我要|想|要|做一个|做个|来一个|来个|生成一个|生成)"
)
_EDGE_ACK: Final = re.compile(
    r"^(?:开启语音模式|不需要开启|需要开启|开启|打开|启用|进入|好的|好|嗯|是的|是|可以|行|对的)"
)


def normalize(text: str) -> str:
    """Fold width variants, drop sentence punctuation and whitespace."""

    folded = unicodedata.normalize("NFKC", str(text or ""))
    folded = folded.translate(_STRIP_TABLE)
    return _SPACES.sub("", folded)


def clean_utterance(text: str) -> str:
    """Keep the user's wording, only trimming edges and collapsing blanks."""

    cleaned = unicodedata.normalize("NFKC", str(text or ""))
    cleaned = _SPACES.sub(" ", cleaned).strip()
    cleaned = cleaned.strip(_STRIP_CHARS + " ")
    return cleaned[:MAX_UTTERANCE]


def _strip_filler(text: str) -> str:
    current = text
    for _ in range(3):
        stripped = _EDGE_FILLER.sub("", current)
        if stripped == current:
            break
        current = stripped
    return current or text


def _strip_ack(text: str) -> str:
    current = text
    for _ in range(3):
        stripped = _EDGE_ACK.sub("", current)
        if stripped == current:
            break
        current = stripped
    return current or text


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

_QUESTION = re.compile(r"(要不要|需不需要|开不开|是不是|可不可以|要不要开)")
_NO = re.compile(
    r"(不用了|不需要了|不用|不要|不需要|不必|不开|别开|别开启|关闭|关掉|退出|取消|算了|不必了|否)"
)
_YES = re.compile(
    r"(需要|要开|开启|打开|启用|进入|开一下|可以|好的|好|行|对的|是的|是|嗯|要用|来吧)"
)

_STOP = re.compile(r"(关闭语音|退出语音|关闭盲人模式|退出盲人模式|不聊了)")
_REPEAT = re.compile(r"(再说一遍|再说一次|重复一遍|重复|没听清|你说什么|再念一遍)")
_HELP = re.compile(r"(帮助|你能做什么|会什么|怎么用|有什么功能)")
_RESTART = re.compile(r"(重新开始|重来|从头开始|清空重来)")

_CONFIRM = re.compile(r"(确认|确定|开始|生成吧|生成|可以|好的|好|没问题|就这样|对的|是的|行|嗯|提交|开始吧)")
_CANCEL = re.compile(r"(取消|不用了|算了|退出|不做了|关闭|先不做|不做)")
_MODIFY = re.compile(r"(不对|不是|改一下|改改|修改|调整|重说|重新说|换个|换成)")

_SKIP_DETAIL = re.compile(r"^(没有了|没了|不用了|不需要|就这样|没什么|没有|可以了|好了|够了)$")

_RECONSTRUCT = re.compile(r"(照片|实拍|拍照|图像重建|扫描实物|照片重建|真实物品)")
_MODEL3D = re.compile(r"(3d模型|三维模型|立体模型|立体|模型|3d打印|打印件|实体)")
_TACTILE = re.compile(r"(触觉教材|触觉板|触觉图|触觉卡片|盲文板|盲文图|平面|浮雕|教材|板书)")
_BOTH = re.compile(r"(都要|都做|全都|全要|三个都|两种都|都选|都行)")


def detect_modes(text: str) -> list[str]:
    """Return the generation modes mentioned in ``text`` (in catalog order)."""

    folded = normalize(text)
    if not folded:
        return []
    if _BOTH.search(folded):
        # Photo reconstruction needs images, so "都要" means the two things a
        # user can ask for by voice alone.
        return ["tactile", "model3d"]
    found: list[str] = []
    if _TACTILE.search(folded):
        found.append("tactile")
    if _MODEL3D.search(folded):
        found.append("model3d")
    if _RECONSTRUCT.search(folded):
        found.append("reconstruct")
    return found


def _polarity(folded: str) -> str:
    """Pick yes or no. When both appear, the one said first wins."""

    yes = _YES.search(folded)
    no = _NO.search(folded)
    if yes and no:
        return "no" if no.start() < yes.start() else "yes"
    if no:
        return "no"
    if yes:
        return "yes"
    return ""


def _sanitize_modes(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    seen: list[str] = []
    for item in value:
        mode = str(item)
        if mode in MODE_IDS and mode not in seen:
            seen.append(mode)
    return [mode for mode in MODE_IDS if mode in seen]


def _merge_modes(current: list[str], extra: list[str]) -> list[str]:
    merged = list(dict.fromkeys([*current, *extra]))
    return [mode for mode in MODE_IDS if mode in merged]


def _mode_sentence(modes: list[str]) -> str:
    if not modes:
        return ""
    parts: list[str] = []
    for mode in modes:
        parts.append(f"{MODE_NAMES[mode]}，{MODE_HINTS[mode]}")
    return "；".join(parts)


def _mode_names(modes: list[str]) -> str:
    return "、".join(MODE_NAMES[mode] for mode in modes)


def _recap(goal: str, modes: list[str]) -> str:
    target = goal or "您刚才说的对象"
    if modes:
        return f"我复述一下。您要做的是：{target}。输出方式是：{_mode_sentence(modes)}。"
    return f"我复述一下。您要做的是：{target}。"


def prompts_payload() -> dict[str, Any]:
    return {
        "ok": True,
        "stages": list(STAGES),
        "modes": [
            {"id": mode, "label": MODE_NAMES[mode], "hint": MODE_HINTS[mode]}
            for mode in MODE_IDS
        ],
        "prompts": PROMPTS,
        "commands": COMMANDS,
    }


# ---------------------------------------------------------------------------
# Dialog
# ---------------------------------------------------------------------------


def _result(
    stage: str,
    intent: str,
    say: str,
    *,
    goal: str = "",
    modes: list[str] | None = None,
    ready: bool = False,
    ask: bool = False,
    confidence: float = 0.9,
) -> dict[str, Any]:
    return {
        "ok": True,
        "stage": stage,
        "intent": intent,
        "say": say,
        "goal": goal,
        "modes": modes or [],
        "ready": ready,
        "listen": stage != "done",
        "ask": ask,
        "confidence": round(confidence, 2),
    }


def _ask_detail(stage: str, intent: str, goal: str, modes: list[str], prefix: str = "") -> dict[str, Any]:
    """Confirm what was heard, then ask the one question that still matters."""

    head = prefix or "好的。"
    if goal:
        head += f"记下了：{goal}。"
    if modes:
        head += f"输出用{_mode_names(modes)}。"
    return _result(
        stage,
        intent,
        head + PROMPTS["ask_detail"],
        goal=goal,
        modes=modes,
        ask=True,
        confidence=0.85,
    )


def _ask_confirm(intent: str, goal: str, modes: list[str], prefix: str = "") -> dict[str, Any]:
    return _result(
        "confirm",
        intent,
        prefix + _recap(goal, modes) + PROMPTS["confirm_now"],
        goal=goal,
        modes=modes,
        ask=True,
        confidence=0.9,
    )


def _greet(folded: str, cleaned: str, goal: str, modes: list[str]) -> dict[str, Any]:
    if _QUESTION.search(folded):
        return _result(
            "greet",
            "unclear",
            PROMPTS["greeting"],
            goal=goal,
            modes=modes,
            ask=True,
            confidence=0.4,
        )
    polarity = _polarity(folded)
    if polarity == "no":
        return _result("done", "disable", PROMPTS["disabled"], confidence=0.95)
    if polarity == "yes":
        # "开启，做一个机械臂的触觉教材" carries both an answer and a goal.
        core = clean_utterance(_strip_ack(cleaned))
        detected = detect_modes(core)
        if detected and len(core) >= 4:
            enabled = _goal(folded, core, goal, modes)
            enabled["intent"] = "enable"
            enabled["say"] = PROMPTS["enabled_short"] + enabled["say"]
            enabled["confidence"] = 0.8
            return enabled
        return _result(
            "goal",
            "enable",
            PROMPTS["enabled"],
            goal=goal,
            modes=modes,
            ask=True,
            confidence=0.95,
        )
    # A user who starts describing the object instead of answering is saying
    # yes with extra information; keep their words and jump forward.
    detected = detect_modes(cleaned)
    if detected and len(cleaned) >= 4:
        enabled = _goal(folded, cleaned, goal, modes)
        enabled["intent"] = "enable"
        enabled["say"] = PROMPTS["enabled_short"] + enabled["say"]
        enabled["confidence"] = 0.7
        return enabled
    return _result(
        "greet",
        "unclear",
        PROMPTS["greeting"],
        goal=goal,
        modes=modes,
        ask=True,
        confidence=0.3,
    )


def _goal(folded: str, cleaned: str, goal: str, modes: list[str]) -> dict[str, Any]:
    if not cleaned:
        return _result("goal", "unclear", PROMPTS["ask_goal"], goal=goal, modes=modes, ask=True, confidence=0.3)
    text = _strip_filler(cleaned)
    detected = detect_modes(text)
    if detected:
        merged = _merge_modes(modes, detected)
        if merged == ["reconstruct"]:
            return _ask_confirm("goal", text, merged, prefix="好的。" + PROMPTS["photo_next"])
        return _ask_detail("detail", "goal", text, merged)
    return _result(
        "mode",
        "goal",
        f"好的，记下了：{text}。" + PROMPTS["ask_mode"],
        goal=text,
        modes=modes,
        ask=True,
        confidence=0.85,
    )


def _mode(folded: str, cleaned: str, goal: str, modes: list[str]) -> dict[str, Any]:
    detected = detect_modes(cleaned)
    if not detected:
        if modes:
            return _ask_confirm("mode", goal, modes, prefix="好的。")
        return _result("mode", "unclear", PROMPTS["ask_mode_again"], goal=goal, modes=modes, ask=True, confidence=0.3)
    merged = _merge_modes(modes, detected)
    if merged == ["reconstruct"]:
        return _ask_confirm("mode", goal, merged, prefix="好的。" + PROMPTS["photo_next"])
    return _ask_detail("detail", "mode", goal, merged, prefix="好的。")


def _detail(folded: str, cleaned: str, goal: str, modes: list[str]) -> dict[str, Any]:
    if not cleaned or _SKIP_DETAIL.search(normalize(cleaned)):
        return _ask_confirm("detail_skip", goal, modes)
    text = _strip_filler(cleaned)
    merged_goal = text if not goal else f"{goal}；{text}"
    detected = detect_modes(text)
    merged_modes = _merge_modes(modes, detected) if detected else modes
    return _ask_confirm("detail", merged_goal, merged_modes)


def _confirm(folded: str, cleaned: str, goal: str, modes: list[str]) -> dict[str, Any]:
    if _CANCEL.search(folded):
        return _result("goal", "cancel", PROMPTS["cancel"], goal="", modes=[], ask=True, confidence=0.9)
    if _MODIFY.search(folded):
        return _result("detail", "modify", PROMPTS["modify"], goal=goal, modes=modes, ask=True, confidence=0.9)
    if _CONFIRM.search(folded):
        modes = modes or ["tactile"]
        return _result(
            "done",
            "confirm",
            PROMPTS["submit"],
            goal=goal,
            modes=modes,
            ready=True,
            ask=False,
            confidence=0.95,
        )
    if not cleaned:
        return _ask_confirm("unclear", goal, modes)
    # Free text at the confirm step is treated as an extra requirement.
    return _detail(folded, cleaned, goal, modes)


def interpret(payload: dict[str, Any] | None) -> dict[str, Any]:
    """Turn one recognised utterance into the next dialog step."""

    payload = payload if isinstance(payload, dict) else {}
    stage = str(payload.get("stage") or "greet")
    if stage not in STAGES and stage not in {"done"}:
        stage = "greet"
    raw = str(payload.get("text") or "")[:MAX_UTTERANCE]
    cleaned = clean_utterance(raw)
    folded = normalize(raw)
    context = payload.get("context") if isinstance(payload.get("context"), dict) else {}
    goal = clean_utterance(context.get("goal") or "")
    modes = _sanitize_modes(context.get("modes"))

    if _STOP.search(folded):
        return _result("done", "disable", PROMPTS["disabled"], goal=goal, modes=modes, confidence=0.95)
    if _RESTART.search(folded):
        return _result("goal", "restart", PROMPTS["ask_goal"], ask=True, confidence=0.9)
    # Short sentences only: "帮我做一个机械臂的触觉教材" must not be read as
    # "help me", and "改正一下尺寸" must not be read as "repeat".
    if len(folded) <= 12 and _REPEAT.search(folded):
        stage_prompt = {
            "greet": PROMPTS["greeting"],
            "goal": PROMPTS["ask_goal"],
            "mode": PROMPTS["ask_mode"],
            "detail": PROMPTS["ask_detail"],
            "confirm": _recap(goal, modes) + PROMPTS["confirm_now"],
            "done": PROMPTS["submit"],
        }.get(stage, PROMPTS["ask_goal"])
        return _result(stage, "repeat", PROMPTS["repeat_prefix"] + stage_prompt, goal=goal, modes=modes, ask=stage != "done")
    if len(folded) <= 12 and _HELP.search(folded):
        return _result(stage, "help", PROMPTS["help"], goal=goal, modes=modes, ask=True)

    if stage == "greet":
        return _greet(folded, cleaned, goal, modes)
    if stage == "goal":
        return _goal(folded, cleaned, goal, modes)
    if stage == "mode":
        return _mode(folded, cleaned, goal, modes)
    if stage == "detail":
        return _detail(folded, cleaned, goal, modes)
    if stage == "confirm":
        return _confirm(folded, cleaned, goal, modes)
    return _result("goal", "restart", PROMPTS["ask_goal"], ask=True, confidence=0.5)
