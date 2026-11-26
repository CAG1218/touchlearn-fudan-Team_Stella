"""Per-plate Chinese braille decoder sheet.

The sheet ships with every tactile plate so a sighted teacher, a print shop or
an evaluator can match each printed cell back to its syllable and dot numbers
without guessing.  It is the "逐字对照表" required by the review feedback: the
plate stays a tactile object, and the paper sheet documents exactly what was
encoded.
"""

from __future__ import annotations

from pathlib import Path

from . import cnbraille
from .layout import LayoutResult


def _dot_order(cells: list[list[int]]) -> str:
    if not cells:
        return "空"
    return " / ".join("".join(str(dot) for dot in cell) or "空" for cell in cells)


def build_braille_map(layout: LayoutResult) -> str:
    diagram = layout.diagram
    title = (getattr(diagram, "title_zh", "") or diagram.title or diagram.id).strip()
    lines = [
        "# 中文盲文逐字对照表",
        "",
        f"- 图：{title}（`{diagram.id}`）",
        "- 编码：GF 0019-2018《国家通用盲文方案》，按声母 + 韵母 + 声调三格拼写",
        "- 数学：数字、拉丁字母与运算符号按 GB/T 18028-2010，数字前加数号，字母前加字母号",
        "- 点序：`1 4` / `2 5` / `3 6`；单元格内按点号升序排列",
        "- 分词：词内连写，词与词之间空一方（表中记作「分词空方」）",
        "- 说明：同一段文本只列一次；「简写」一行是 GF 0019 §11 的定字简写",
        "",
        "> 注意：中文盲文的每一方是「声母 / 韵母 / 声调」符号，不是英文字母。",
        "> 用拉丁字母盲文表逐格去读会得到无意义的字母串，请按本表的读音列对照。",
        "",
    ]

    seen: set[str] = set()
    for feature in layout.features:
        for primitive in feature.primitives:
            if primitive.kind != "braille" or not primitive.text:
                continue
            text = primitive.text
            if text in seen:
                continue
            seen.add(text)
            lines.append(f"## {feature.kind} / {feature.id}")
            lines.append("")
            lines.append(f"文本：{text}")
            lines.append("")
            lines.append("| 内容 | 类别 | 读音 | 点序 | 说明 |")
            lines.append("|---|---|---|---|---|")
            try:
                rows = cnbraille.describe_text(text)
            except cnbraille.BrailleError as error:
                lines.append(f"| {text} | 无法编码 | - | - | {error} |")
                lines.append("")
                continue
            for row in rows:
                kind = row.get("kind") or "han"
                reading = row.get("pinyin") or row.get("symbol") or "-"
                note = "简写" if row.get("shorthand") else "-"
                lines.append(
                    f"| {row['char']} | {kind} | {reading} | {_dot_order(row['cells'])} | {note} |"
                )
            lines.append("")

    lines.append("")
    lines.append("对照表由 `touchlearn.braille_map` 依据实际点阵生成，不是手工抄写。")
    lines.append("")
    return "\n".join(lines)


def write_braille_map(layout: LayoutResult, path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(build_braille_map(layout), encoding="utf-8")
    return target