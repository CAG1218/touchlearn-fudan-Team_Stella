"""Request routing and light-weight semantic extraction.

The router decides which deterministic pipeline should handle a request.  A
configured language model may classify free-form text or an image; when no
model is configured, a conservative keyword heuristic keeps the offline demo
usable.  The router never invents measurements: it only chooses a path and,
for functions, proposes an expression that the user still has to confirm.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from .llm import chat_json, llm_status
from .expression import ExpressionError, parse_expression

SUBJECTS = {"function", "robot", "mesh", "scene", "unsupported"}

ROBOT_WORDS = {
    "机械臂",
    "机器人",
    "机械手",
    "关节",
    "连杆",
    "自由度",
    "夹爪",
    "吸盘",
    "舵机",
    "运动链",
    "robot",
    "robotic arm",
    "dof",
    "gripper",
    "kinematic",
}
FUNCTION_WORDS = {
    "函数",
    "函数图像",
    "二次函数",
    "一次函数",
    "反比例函数",
    "三角函数",
    "曲线",
    "抛物线",
    "正弦",
    "余弦",
    "正切",
    "指数函数",
    "对数函数",
    "根号",
    "解析式",
    "方程",
    "坐标图",
    "function",
    "parabola",
    "sine",
    "cosine",
    "graph",
}
SCENE_WORDS = {
    "示意图",
    "触觉图",
    "教材",
    "课程",
    "几何",
    "三角形",
    "四边形",
    "圆",
    "电路",
    "电池",
    "电阻",
    "开关",
    "灯泡",
    "串联",
    "并联",
    "流程",
    "步骤",
    "判断",
    "框图",
    "算法",
    "坐标",
    "象限",
    "地图",
    "路线",
    "导航",
    "校园",
    "路口",
    "细胞",
    "细胞核",
    "线粒体",
    "生物",
    "化学",
    "分子",
    "原子",
    "实验",
    "装置",
    "图表",
    "柱状图",
    "条形图",
    "统计",
    "数据",
    "太阳系",
    "行星",
    "结构",
    "部件",
    "原理",
    "过程",
    "流程",
    "图例",
    "示意",
    "diagram",
    "circuit",
    "flowchart",
    "geometry",
    "map",
    "cell",
    "chart",
}
UNSUPPORTED_WORDS = {
    "写诗",
    "写一首",
    "写文章",
    "作文",
    "翻译",
    "聊天",
    "讲个笑话",
    "写代码",
    "编程",
    "发邮件",
    "手机壳",
    "钥匙扣",
    "手办",
    "人像",
    "照片修图",
    "海报设计",
    "logo设计",
    "生成一个三维物体",
    "任意三维物体",
}
EXPRESSION_RE = re.compile(r"(?:y|f\s*\(\s*x\s*\))\s*=\s*([0-9xX+\-*/^(). a-zA-Z]+)")
FUNCTION_CHARS_RE = re.compile(r"^[0-9xX+\-*/^(). a-zA-Zπ]+$")


@dataclass
class RouteDecision:
    subject: str
    confidence: float
    reason: str
    provider: str = "heuristic"
    notes: list[str] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "confidence": round(self.confidence, 3),
            "reason": self.reason,
            "provider": self.provider,
            "notes": self.notes,
            "questions": self.questions,
        }


def _score(text: str, words: set[str]) -> int:
    lowered = text.lower()
    return sum(1 for word in words if word.lower() in lowered)


def _file_summary(files: Sequence[dict[str, Any]], images: Sequence[str | Path], has_mesh: bool) -> str:
    parts: list[str] = []
    if files:
        names = [str(item.get("name") or "") for item in files if isinstance(item, dict)]
        parts.append(f"{len(files)} 个附件：" + "，".join(names[:6]))
    elif images:
        parts.append(f"{len(images)} 张图片")
    if has_mesh:
        parts.append("包含 STL/OBJ 三维文件")
    return "；".join(parts) or "无附件"


CLASSIFY_SYSTEM = (
    "你是触觉教材智能体的路由器。你只判断请求应该走哪条确定性管线，并输出严格 JSON。"
    "不要编造内容，不要把无法可靠转成触觉几何的请求硬塞进某个类别。"
)

CLASSIFY_PROMPT = """请判断用户请求最适合的处理路径。

可选路径：
- function：单变量函数图像、解析式、曲线图；几何由表达式计算。
- robot：机械臂/串联机器人，有基座、连杆、关节、末端执行器；可生成三维模型或浮雕。
- mesh：用户已经上传 STL/OBJ，要求缩放、检查或转浮雕。
- scene：其他二维示意图，例如几何图形、电路、流程图、坐标系、地图路线、柱状图、细胞结构、实验装置。系统会把结构排成 A5 触觉板，但不能还原照片细节。
- unsupported：无法可靠转成触觉 STL 的请求，例如写文章、翻译、聊天、照片美化、凭空生成任意三维物体、设计手机壳等。

用户输入：
{text}

附件信息：{files}
是否已有解析式：{expression}
是否已有机械臂参数：{spec}
是否已有三维文件：{mesh}

只输出 JSON：
{{"subject":"function|robot|mesh|scene|unsupported","confidence":0.0,"reason":"一句中文理由","questions":["需要用户确认的问题"],"notes":["给用户的说明"]}}
规则：
1. 有 STL/OBJ 且没有别的强意图时选 mesh。
2. 有明确解析式时选 function；有明确机械臂结构时选 robot。
3. 二维教材示意图选 scene，即使描述不完整；在 questions 中列出缺失信息。
4. 只有确实无法转成线条/形状/文字/盲文的请求才选 unsupported。
5. 不要声称你看到了附件内容；你只能根据文件名和用户文字判断。
"""


def _llm_classify(
    text: str,
    *,
    files: Sequence[dict[str, Any]],
    images: Sequence[str | Path],
    has_expression: bool,
    has_spec: bool,
    has_mesh: bool,
) -> RouteDecision | None:
    status = llm_status()
    if not status.get("configured"):
        return None
    prompt = CLASSIFY_PROMPT.format(
        text=(text or "（无文字，只有附件）")[:3000],
        files=_file_summary(files, images, has_mesh),
        expression="是" if has_expression else "否",
        spec="是" if has_spec else "否",
        mesh="是" if has_mesh else "否",
    )
    result = chat_json(
        CLASSIFY_SYSTEM,
        prompt,
        images=images if images else (),
        kind="vision" if images else "text",
        max_tokens=900,
    )
    if not result.ok or not isinstance(result.data, dict):
        return None
    subject = str(result.data.get("subject") or "").strip().lower()
    if subject not in SUBJECTS:
        return None
    try:
        confidence = max(0.0, min(1.0, float(result.data.get("confidence", 0.5))))
    except (TypeError, ValueError):
        confidence = 0.5
    questions = [str(item).strip()[:180] for item in (result.data.get("questions") or []) if isinstance(item, str) and item.strip()][:5]
    notes = [str(item).strip()[:180] for item in (result.data.get("notes") or []) if isinstance(item, str) and item.strip()][:5]
    return RouteDecision(
        subject=subject,
        confidence=confidence,
        reason=str(result.data.get("reason") or "模型分类").strip()[:240],
        provider=f"llm:{result.provider}",
        notes=notes,
        questions=questions,
    )


def _heuristic(
    text: str,
    *,
    has_images: bool,
    has_expression: bool,
    has_spec: bool,
    has_mesh: bool,
) -> RouteDecision:
    stripped = (text or "").strip()
    lowered = stripped.lower()
    if has_expression:
        return RouteDecision("function", 0.98, "检测到显式函数解析式。", "heuristic")
    if has_spec:
        return RouteDecision("robot", 0.95, "检测到机械臂参数。", "heuristic")
    if has_mesh and not stripped:
        return RouteDecision("mesh", 0.98, "检测到 STL/OBJ 文件。", "heuristic")

    unsupported = _score(stripped, UNSUPPORTED_WORDS)
    robot = _score(stripped, ROBOT_WORDS)
    function = _score(stripped, FUNCTION_WORDS)
    scene = _score(stripped, SCENE_WORDS)
    if "机械臂" in stripped or "机器人" in stripped or "robotic" in lowered or "robot" in lowered:
        robot += 2
    if "函数" in stripped or "解析式" in stripped:
        function += 2
    if re.search(r"\b(?:sin|cos|tan|sqrt|log|abs|exp|ln)\s*\(", lowered) or EXPRESSION_RE.search(stripped):
        function += 2
    if unsupported and unsupported >= max(robot, function, scene):
        return RouteDecision(
            "unsupported",
            0.72,
            "请求更像写作、聊天或照片处理，无法可靠转成触觉几何。",
            "heuristic",
            notes=["当前系统支持函数图像、机械臂、STL/OBJ，以及可用线条表达的二维示意图。"],
            questions=["如果你要的是二维示意图，请说明图中的部件、连接关系和需要标注的文字。"],
        )
    if robot >= 1 and robot >= function and robot >= scene:
        return RouteDecision("robot", min(0.95, 0.62 + robot * 0.08), "描述中出现机械臂、关节或连杆等结构词。", "heuristic")
    if function >= 1 and function >= scene and function >= robot:
        return RouteDecision("function", min(0.94, 0.62 + function * 0.08), "描述中出现函数、曲线或解析式等数学词。", "heuristic")
    if has_mesh:
        return RouteDecision("mesh", 0.85, "检测到三维文件，按三维模型处理。", "heuristic")
    if scene >= 1:
        return RouteDecision("scene", min(0.88, 0.55 + scene * 0.07), "描述可用二维形状、线条和标注表达。", "heuristic")
    if has_images:
        return RouteDecision(
            "scene",
            0.45,
            "收到图片但没有足够文字说明，先按二维示意图处理。",
            "heuristic",
            questions=["请说明图片中最需要触摸的结构，以及哪些文字必须保留。"],
        )
    if len(stripped) >= 4:
        return RouteDecision(
            "scene",
            0.42,
            "没有命中专用管线，先按通用二维示意图处理。",
            "heuristic",
            questions=["请确认图中的部件、连接关系和阅读顺序；离线模式对复杂图只能做简化重建。"],
        )
    return RouteDecision(
        "unsupported",
        0.5,
        "信息不足，无法判断要生成的触觉内容。",
        "heuristic",
        questions=["请描述要触摸的对象，或上传图片、STL/OBJ、函数解析式。"],
    )


def classify_request(
    text: str,
    *,
    files: Sequence[dict[str, Any]] = (),
    images: Sequence[str | Path] = (),
    has_expression: bool = False,
    has_spec: bool = False,
    has_mesh: bool = False,
    use_llm: bool = True,
) -> RouteDecision:
    heuristic = _heuristic(
        text,
        has_images=bool(images),
        has_expression=has_expression,
        has_spec=has_spec,
        has_mesh=has_mesh,
    )
    if not use_llm:
        return heuristic
    model = _llm_classify(
        text,
        files=files,
        images=images,
        has_expression=has_expression,
        has_spec=has_spec,
        has_mesh=has_mesh,
    )
    if model is None:
        return heuristic
    # Hard evidence wins over a low-confidence model guess.
    if model.confidence < 0.5 and heuristic.confidence >= 0.8:
        heuristic.notes.append(f"模型分类置信度较低（{model.confidence:.2f}），采用规则判断。")
        return heuristic
    if model.subject != heuristic.subject and model.confidence < 0.62 and heuristic.confidence >= 0.9:
        heuristic.notes.append(f"模型建议 {model.subject}，但强规则证据优先。")
        return heuristic
    if model.subject == "unsupported" and heuristic.subject in {"function", "robot", "mesh"} and heuristic.confidence >= 0.9:
        heuristic.notes.append("显式输入足以确定专用管线，未采用模型的 unsupported 判断。")
        return heuristic
    return model


# ---------------------------------------------------------------------------
# Function extraction
# ---------------------------------------------------------------------------

FUNCTION_SYSTEM = (
    "你是数学函数读图与文本解析助手。只输出 JSON 对象，不要 Markdown。"
    "只提取用户明确给出的信息；看不清或没有给出的窗口必须省略，不能猜测。"
)

FUNCTION_PROMPT = """从下面的需求中提取一个单变量函数。

用户需求：
{text}

输出 JSON：
{{"expression":"用 x 表示的单变量表达式，^ 表示幂，函数名限 sin/cos/tan/sqrt/log/abs/exp/ln","x_min":可选数字,"x_max":可选数字,"notes":["依据"],"questions":["需要确认的问题"],"confidence":0.0}}

规则：
1. 只使用用户明确提到的函数；如果只说了“正弦曲线”，可写 sin(x)，并在 questions 中请用户确认振幅和周期。
2. 不确定的系数不要编造；表达式留空。
3. 如果用户给了定义域，填入 x_min/x_max；否则省略。
4. 不要输出 y_min/y_max，纵轴由几何引擎自动计算。
"""


def _heuristic_function(text: str) -> dict[str, Any] | None:
    match = EXPRESSION_RE.search(text)
    if match:
        candidate = match.group(1).strip().rstrip("。，,;；")
        if candidate and FUNCTION_CHARS_RE.match(candidate):
            return {"expression": candidate, "confidence": 0.82, "notes": ["从文本中提取到显式解析式。"]}
    lowered = text.lower()
    if "正弦" in text or "sine" in lowered:
        return {"expression": "sin(x)", "confidence": 0.42, "notes": ["文本只说明是正弦曲线，振幅和周期需要确认。"]}
    if "余弦" in text or "cosine" in lowered:
        return {"expression": "cos(x)", "confidence": 0.42, "notes": ["文本只说明是余弦曲线，振幅和周期需要确认。"]}
    if "抛物线" in text or "parabola" in lowered:
        return {"expression": "x^2", "confidence": 0.38, "notes": ["文本只说明是抛物线，系数和开口方向需要确认。"]}
    return None


def extract_function(text: str, *, images: Sequence[str | Path] = ()) -> dict[str, Any]:
    """Return a candidate expression and optional domain; never a final draft."""
    heuristic = _heuristic_function(text)
    status = llm_status()
    if not status.get("configured"):
        if heuristic is None:
            return {
                "ok": False,
                "message": "未配置大模型，且文本中没有可解析的表达式。",
                "confidence": 0.0,
                "expression": "",
            }
        result = dict(heuristic)
        result["ok"] = True
        result["provider"] = "heuristic"
        result["questions"] = ["该表达式是按关键词推断的，请核对系数、振幅和定义域。"]
        return result

    prompt = FUNCTION_PROMPT.format(text=text[:3000])
    result = chat_json(
        FUNCTION_SYSTEM,
        prompt,
        images=images if images else (),
        kind="vision" if images else "text",
        max_tokens=1000,
    )
    if not result.ok or not isinstance(result.data, dict):
        if heuristic is not None:
            value = dict(heuristic)
            value["ok"] = True
            value["provider"] = "heuristic"
            value["message"] = result.message
            value["questions"] = ["大模型不可用；表达式按关键词推断，请核对。"]
            return value
        return {"ok": False, "message": result.message, "confidence": 0.0, "expression": ""}

    data = result.data
    expression = str(data.get("expression") or "").strip()
    if not expression:
        if heuristic is not None:
            value = dict(heuristic)
            value["ok"] = True
            value["provider"] = f"llm:{result.provider}"
            value["message"] = "模型没有给出可靠表达式，使用关键词候选。"
            value["questions"] = ["请确认候选表达式是否与你的需求一致。"]
            return value
        return {"ok": False, "message": "模型没有给出可靠表达式，请手动填写。", "confidence": 0.0, "expression": ""}
    try:
        parsed = parse_expression(expression)
    except ExpressionError as error:
        return {"ok": False, "message": f"模型给出的表达式无法解析：{error}", "confidence": 0.0, "expression": expression}

    confidence = data.get("confidence", 0.6)
    try:
        confidence = max(0.0, min(1.0, float(confidence)))
    except (TypeError, ValueError):
        confidence = 0.6
    payload: dict[str, Any] = {
        "ok": True,
        "expression": parsed.canonical,
        "confidence": confidence,
        "provider": f"llm:{result.provider}",
        "model": result.model,
        "notes": [str(item).strip()[:180] for item in (data.get("notes") or []) if isinstance(item, str) and item.strip()][:4],
        "questions": [str(item).strip()[:180] for item in (data.get("questions") or []) if isinstance(item, str) and item.strip()][:4],
    }
    for key in ("x_min", "x_max"):
        value = data.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            payload[key] = float(value)
    return payload
