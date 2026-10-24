"""Narration scripts and local Windows speech synthesis.

The audio track is part of the product, not an afterthought: every plate and
model gets a script that mirrors the physical exploration order.  Windows SAPI
is used because it ships with the target machine and needs no API key.  If a
voice is missing, the script is still written and the manifest records the
failure; the geometry build never fails because of TTS.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Sequence

from ..model import Diagram

DEFAULT_VOICE = "Microsoft Huihui Desktop"


def _number(value: float) -> str:
    if abs(value - round(value)) < 1e-6:
        return str(int(round(value)))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def spoken_expression(text: str) -> str:
    """A conservative spoken form for simple textbook expressions."""
    value = text.strip()
    if value.lower().startswith("y="):
        value = value[2:]
    value = value.replace("**", "^")
    value = value.replace("*", " 乘 ")
    value = value.replace("/", " 除以 ")
    value = value.replace("+", " 加 ")
    value = value.replace("-", " 减 ")
    value = value.replace("^2", " 的平方 ")
    value = value.replace("^3", " 的立方 ")
    value = value.replace("^", " 的 ")
    value = value.replace("(", " 括号 ")
    value = value.replace(")", " 括号 ")
    value = value.replace("=", " 等于 ")
    return re.sub(r"\s+", " ", value).strip()


def _point_name(kind: str) -> str:
    return {
        "root": "与横轴的交点",
        "minimum": "最低点，也就是顶点",
        "maximum": "最高点，也就是顶点",
        "y_intercept": "与纵轴的交点",
    }.get(kind, "关键点")


def function_narration(
    expression: str,
    x_min: float,
    x_max: float,
    y_min: float,
    y_max: float,
    key_points: Sequence[Any],
    letters: Sequence[str],
    *,
    title_zh: str = "函数图像",
) -> dict[str, Any]:
    intro = f"这是一张{title_zh}的触觉图。图幅左上角有缺口，缺口方向是上方。"
    steps = [
        "先摸左上角的缺口，确认图的方向。",
        "找到两条最粗的直线，横的是 x 轴，竖的是 y 轴，交点是原点。",
    ]
    if key_points:
        steps.append("按字母顺序摸关键点：")
        for point, letter in zip(key_points, letters):
            steps.append(
                f"字母 {letter} 是{_point_name(point.kind)}，坐标是 x 等于 {_number(point.x)}，y 等于 {_number(point.y)}。"
            )
    steps.append("沿粗曲线从左向右慢慢滑动，再滑回来，感受曲线的弯曲方向和开口。")
    steps.append(
        f"横轴范围是 {_number(x_min)} 到 {_number(x_max)}，纵轴范围是 {_number(y_min)} 到 {_number(y_max)}。"
    )
    steps.append("图幅下方的短横线是比例尺，旁边标着一个单位对应的长度。")
    outro = f"解析式是 y 等于 {spoken_expression(expression)}。音频只描述读图顺序，不替代数学课上的讲解。"
    return {"intro": intro, "steps": steps, "outro": outro}


def robot_relief_narration(spec: Any) -> dict[str, Any]:
    dof = spec.dof
    intro = f"这是一张 {dof} 轴机械臂的触觉示意图。图幅左上角有缺口，缺口方向是上方。"
    steps = [
        "先摸左上角的缺口，确认图的方向。",
        "从底部宽大的基座开始，基座标着数字 0。",
        "沿最粗的连杆向上，摸到数字 1 到 6 表示的关节，字母 A 到 F 表示连杆。",
        "每个关节旁边都有一个数字，数字越大，离底座越远。",
        "摸到右上角的末端执行器，标着字母 T。夹爪形状像两片张开的钳口。",
        "最后从基座沿粗线一路摸到夹爪，这就是从底座到末端的运动链。",
    ]
    outro = "这张图只表达关节顺序和连接关系，不表示真实尺寸和姿态。"
    return {"intro": intro, "steps": steps, "outro": outro}


def robot_volume_narration(spec: Any, size_mm: Sequence[float]) -> dict[str, Any]:
    intro = f"这是一个按比例缩小的 {spec.dof} 轴机械臂立体模型。"
    steps = [
        "模型底部是一块方形底板，正面刻着英文 ROBOT ARM 和对应的盲文。",
        "底板右侧有一块平整的方形区域，用来贴二维码，扫码可以播放这段讲解。",
        "从底板中央的圆形基座开始，向上摸到第一个关节。",
        "沿连杆继续向上，依次经过六个关节，每个关节都是横向的圆柱。",
        "最上面是末端夹爪，可以摸到两片张开的钳口。",
        f"模型整体尺寸约为 {_number(size_mm[0])} 乘 {_number(size_mm[1])} 乘 {_number(size_mm[2])} 毫米。",
    ]
    outro = "这是展示模型，不是可编程的真实机械臂；打印时建议在关节和夹爪下方开启树状支撑。"
    return {"intro": intro, "steps": steps, "outro": outro}


def mesh_narration(meta: dict[str, Any], mode: str) -> dict[str, Any]:
    if mode == "silhouette_relief":
        intro = "这是一张由上传的三维模型生成的轮廓浮雕触觉板。图幅左上角有缺口，缺口方向是上方。"
        steps = [
            "先摸左上角的缺口，确认图的方向。",
            "沿凸起的轮廓从一侧慢慢滑到另一侧，感受物体的外形和主要凸出部分。",
            "轮廓内部是平的，表示原模型在这个方向上被投影压缩。",
            "右上角的方框是二维码预留区，可以贴上音频二维码。",
            "图幅上方是标题 3D RELIEF，下方边缘是板厚。",
        ]
        outro = "轮廓浮雕只保留外形，不保留内部结构、标签和隐藏特征。"
    else:
        intro = "这是一个由上传文件生成的三维缩放模型。"
        steps = [
            "模型已经按目标高度等比缩放，并平放在打印平台上。",
            "打印前请在切片软件中检查摆放方向、支撑和最小壁厚。",
            "如果模型包含细小结构，建议使用较小的层高和较低速度。",
        ]
        outro = "缩放和居中参数已写入清单文件，可复现本次结果。"
    return {"intro": intro, "steps": steps, "outro": outro}


def narration_text(audio: dict[str, Any]) -> str:
    parts = [str(audio.get("intro", ""))]
    parts.extend(str(step) for step in audio.get("steps", []))
    parts.append(str(audio.get("outro", "")))
    return "\n".join(part for part in parts if part)


def audio_from_diagram(diagram: Diagram) -> dict[str, Any]:
    return dict(diagram.audio)


def _powershell_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def synthesize_windows_tts(
    text: str,
    wav_path: str | Path,
    *,
    voice: str = DEFAULT_VOICE,
    rate: int = 0,
) -> tuple[bool, str]:
    """Render ``text`` to a WAV file with Windows SAPI.

    Returns ``(ok, message)``.  The caller keeps the script either way.
    """
    wav = Path(wav_path)
    wav.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="touchlearn-tts-") as temp_dir:
        text_path = Path(temp_dir) / "script.txt"
        text_path.write_text(text, encoding="utf-8")
        script = f"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Speech
$text = Get-Content -Raw -Encoding UTF8 {_powershell_quote(str(text_path))}
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {{ $synth.SelectVoice({_powershell_quote(voice)}) }} catch {{ }}
$synth.Rate = {int(rate)}
$synth.Volume = 100
$synth.SetOutputToWaveFile({_powershell_quote(str(wav))})
$synth.Speak($text)
$synth.Dispose()
"""
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=180,
            )
        except (OSError, subprocess.SubprocessError) as error:
            return False, f"TTS 调用失败：{error}"
    if result.returncode != 0:
        return False, f"TTS 退出码 {result.returncode}：{result.stderr.strip()[:300]}"
    if not wav.exists() or wav.stat().st_size < 1024:
        return False, "TTS 没有生成有效的 WAV 文件（可能缺少中文语音包）"
    return True, f"已生成 {wav.name}，{wav.stat().st_size // 1024} KB"


def available_voices() -> list[str]:
    script = """
Add-Type -AssemblyName System.Speech
(New-Object System.Speech.Synthesis.SpeechSynthesizer).GetInstalledVoices() |
  ForEach-Object { $_.VoiceInfo.Name }
"""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]
