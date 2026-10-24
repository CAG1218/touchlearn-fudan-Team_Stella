"""Narration scripts for the QR-code audio track.

v0 writes a deterministic script.  TTS is an optional production step so the
repository never depends on a cloud voice service to reproduce a plate.
"""

from __future__ import annotations

from pathlib import Path

from .model import Diagram


def build_script(diagram: Diagram) -> str:
    audio = diagram.audio
    lines: list[str] = [
        "触见 TouchLearn 音频脚本",
        f"案例：{diagram.title_zh}",
        "",
        "【开场】",
        str(audio.get("intro", "")).strip(),
        "",
    ]

    steps = audio.get("steps", [])
    for index, step in enumerate(steps, start=1):
        lines.append(f"【第 {index} 步】{step}")
        lines.append("[停顿 1 秒]")

    outro = str(audio.get("outro", "")).strip()
    if outro:
        lines.extend(["", "【结束】", outro])

    lines.extend(
        [
            "",
            "【制作说明】",
            "语速约每秒 4 到 5 个汉字，句间停顿 0.8 到 1.2 秒。",
            "背景只保留轻微纸张或打印环境声，不使用有版权争议的音乐。",
            "导出 MP3 后与二维码一起做三台手机播放测试。",
            "",
        ]
    )
    return "\n".join(lines)


def write_script(diagram: Diagram, path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(build_script(diagram), encoding="utf-8")
    return target
