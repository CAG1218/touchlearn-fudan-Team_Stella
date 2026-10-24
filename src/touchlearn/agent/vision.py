"""Optional vision-model adapter for uploaded images and engineering drawings.

The competition build must run offline and must never invent a formula or a
measurement.  This module therefore has two modes:

* no endpoint configured (default): returns ``configured=False`` and the agent
  keeps the image as source evidence, asking the user for an expression or
  parameters;
* an OpenAI-compatible endpoint configured through environment variables:
  sends the image, asks for strict JSON, validates the shape, and returns a
  draft candidate that is still shown to the user for confirmation.

The transport lives in :mod:`touchlearn.agent.llm`; this module only owns the
task prompts and the validation of the returned fields.  No API key is
hard-coded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .llm import LLMResult, chat_json, provider_status


@dataclass
class VisionResult:
    configured: bool
    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    message: str = ""
    provider: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "ok": self.ok,
            "confidence": round(self.confidence, 3),
            "message": self.message,
            "provider": self.provider,
            "data": self.data,
        }


def vision_status() -> dict[str, Any]:
    status = provider_status("vision")
    return {
        "configured": bool(status.get("configured")),
        "model": status.get("model") or "",
        "endpoint_host": status.get("endpoint_host") or "",
        "has_key": bool(status.get("has_key")),
        "mode": "remote-vlm" if status.get("configured") else "offline",
        "provider": status.get("provider") or "offline",
        "label": status.get("label") or "",
        "local": bool(status.get("local")),
        "message": status.get("message") or "",
    }


def _prompt(subject: str, hint: str) -> str:
    common = (
        "你是触觉教材建模助手。只输出一个 JSON 对象，不要 Markdown，不要解释。"
        "看不清或不确定的字段必须省略，不能猜测。"
    )
    if subject == "function":
        return (
            common
            + " 识别图中的函数图像，给出解析式。JSON 字段："
            "expression（字符串，使用 x 为自变量，^ 表示幂，函数名限 sin/cos/tan/sqrt/log/abs/exp/ln），"
            "x_min、x_max（可选数字），notes（字符串数组，说明读图依据），"
            "confidence（0 到 1 的数字）。若无法可靠识别，expression 留空。"
            + (f" 用户补充：{hint}" if hint else "")
        )
    return (
        common
        + " 识别图中的机械臂结构。JSON 字段：description（字符串），"
        "dof（可选整数 3-7），end_effector（可选 gripper/suction/none），"
        "link_lengths_mm（可选数字数组，只有图中标注了尺寸才填写），"
        "base_width_mm、base_height_mm（可选数字，只有标注了才填写），"
        "joint_angles_deg（可选数字数组，表示侧视姿态角），"
        "notes（字符串数组，说明哪些是读图、哪些是假设），confidence（0 到 1）。"
        "绝对不要把像素距离当成毫米尺寸。"
        + (f" 用户补充：{hint}" if hint else "")
    )


def _data_object(result: LLMResult) -> dict[str, Any] | None:
    return result.data if isinstance(result.data, dict) else None


def interpret_image(path: Path, subject: str, *, hint: str = "") -> VisionResult:
    """Ask an optional vision endpoint to read one image."""
    status = vision_status()
    if not status["configured"]:
        return VisionResult(False, False, message=status.get("message") or "未配置视觉模型；图片只作为来源留档。", provider="offline")
    if not path.is_file():
        return VisionResult(True, False, message=f"图片不存在：{path.name}", provider=str(status.get("provider")))
    if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}:
        return VisionResult(True, False, message="该文件不是可发送的位图（PDF 请先导出 PNG）。", provider=str(status.get("provider")))

    result = chat_json(
        "你是严谨的触觉教材读图助手。",
        _prompt(subject, hint),
        images=[path],
        kind="vision",
        max_tokens=1400,
    )
    provider = result.provider or str(status.get("provider"))
    if not result.ok:
        return VisionResult(True, False, message=result.message, provider=provider)
    data = _data_object(result)
    if data is None:
        return VisionResult(True, False, message="视觉模型返回的 JSON 不是对象。", provider=provider)

    confidence = data.get("confidence", 0.0)
    try:
        confidence = max(0.0, min(1.0, float(confidence)))
    except (TypeError, ValueError):
        confidence = 0.0
    return VisionResult(True, True, data=data, confidence=confidence, message="视觉模型已返回候选结构。", provider=provider)
