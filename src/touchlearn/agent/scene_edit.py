"""Scene preview, visual-edit and image-recognition services.

The browser never computes tactile geometry.  It sends semantic operations
(move this component, close this switch, apply this sentence) and receives a
fresh, deterministically compiled scene plus an SVG.  That keeps the editor and
the printed plate in lockstep even when a drag lands at an awkward angle.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence

from .circuit import (
    CircuitError,
    CircuitInstructionError,
    apply_op,
    circuit_from_scene,
    circuit_to_scene,
    component_payload,
    default_circuit,
    normalize_circuit,
    parse_circuit_instruction,
)
from .llm import chat_json, llm_status, resolve_selection, vision_provider_status
from .scene import SceneError, normalize_scene, scene_has_wires, scene_summary
from .scene_svg import scene_to_svg


class SceneEditError(ValueError):
    """Raised when a requested scene edit cannot be applied safely."""


CIRCUIT_EDIT_SYSTEM = (
    "你是触觉教材的电路示意图编辑器。你只修改电路 JSON，不生成其他内容。"
    "只输出一个 JSON 对象，不要 Markdown，不要解释。"
    "保留用户没有提到的元件、标签和阅读顺序；不要编造图片或文字里没有的元件。"
)

CIRCUIT_EDIT_PROMPT = """请根据用户指令修改下面的串联电路模型。

当前电路 JSON：
{circuit}

用户指令：
{instruction}

输出结构：
{{
  "supported": true,
  "message": "一句中文说明改了什么",
  "circuit": {{
    "title": "ELECTRIC CIRCUIT",
    "title_zh": "串联电路",
    "loop": [15, 25, 85, 85],
    "components": [
      {{"id": "battery", "type": "battery", "side": "bottom", "offset": 0.5, "state": ""}}
    ],
    "notes": ["一句触摸说明"]
  }}
}}

规则：
1. type 只能是 battery、switch、resistor、lamp、ammeter、voltmeter、motor。
2. side 只能是 top、right、bottom、left；offset 是 0 到 1 的位置，0.5 表示该边中点。
3. 开关的 state 只能是 open 或 closed；其他元件 state 留空。
4. 不要输出导线：导线由编译器根据元件位置自动生成。
5. 只改用户明确要求的内容；指令无法执行时输出 {{"supported": false, "reason": "原因"}}。
"""

CIRCUIT_RECOGNIZE_SYSTEM = (
    "你是触觉教材的电路图识别助手。你只判断图片里是否有可辨认的电路元件，并把它们转成结构化 JSON。"
    "只输出一个 JSON 对象，不要 Markdown，不要解释。看不清的元件不要猜。"
)

CIRCUIT_RECOGNIZE_PROMPT = """请识别这张图片。

如果图片是电路原理图、接线图或教材里的电路示意图，输出：
{{
  "supported": true,
  "is_circuit": true,
  "confidence": 0.0,
  "message": "一句中文说明识别依据",
  "circuit": {{
    "title": "ELECTRIC CIRCUIT",
    "title_zh": "串联电路",
    "loop": [15, 25, 85, 85],
    "components": [
      {{"id": "battery", "type": "battery", "side": "bottom", "offset": 0.5, "state": ""}},
      {{"id": "switch", "type": "switch", "side": "left", "offset": 0.5, "state": "open"}}
    ],
    "notes": ["一句中文触摸说明"]
  }},
  "questions": ["需要用户确认的地方"]
}}

如果图片不是电路图，输出：
{{"supported": true, "is_circuit": false, "confidence": 0.0, "reason": "一句中文说明"}}

如果图片模糊到无法判断，输出：
{{"supported": false, "reason": "一句中文说明"}}

规则：
1. type 只能是 battery、switch、resistor、lamp、ammeter、voltmeter、motor。
2. side 只能是 top、right、bottom、left，表示元件在回路上的位置；offset 0 到 1，0.5 是边中点。
3. 串联电路请把元件沿四条边排开；开关的 state 用 open 或 closed，其他元件 state 留空。
4. 不要输出导线坐标；编译器会自动生成闭合导线。
5. 看不清的元件、文字和数值不要编造，放进 questions。
"""


def _preview_from_circuit(
    circuit: dict[str, Any],
    *,
    warnings: Sequence[str] = (),
    source: str = "manual",
    changes: Sequence[str] = (),
    provider: str = "",
    model: str = "",
) -> dict[str, Any]:
    compiled = circuit_to_scene(circuit)
    scene, scene_warnings = normalize_scene(compiled)
    all_warnings = [str(item) for item in warnings if str(item).strip()]
    all_warnings.extend(scene_warnings)
    return {
        "ok": True,
        "kind": "circuit",
        "editor": "circuit",
        "circuit": circuit,
        "scene": scene,
        "summary": scene_summary(scene),
        "svg": scene_to_svg(scene, show_names=True),
        "svg_plain": scene_to_svg(scene, show_names=False),
        "warnings": all_warnings,
        "changes": list(changes),
        "source": source,
        "provider": provider,
        "model": model,
        "library": component_payload(),
    }


def _preview_from_scene(scene: dict[str, Any], *, warnings: Sequence[str] = ()) -> dict[str, Any]:
    normalized, scene_warnings = normalize_scene(scene)
    all_warnings = [str(item) for item in warnings if str(item).strip()]
    all_warnings.extend(scene_warnings)
    # Only a scene that actually draws wires is promoted to the circuit editor;
    # two labelled boxes alone must not be reinterpreted as a loop.
    recovered = circuit_from_scene(normalized) if scene_has_wires(normalized) else None
    if recovered is not None:
        try:
            circuit, circuit_warnings = normalize_circuit(recovered)
            return _preview_from_circuit(
                circuit,
                warnings=[*all_warnings, *circuit_warnings],
                source="scene",
            )
        except CircuitError:
            pass
    return {
        "ok": True,
        "kind": "scene",
        "editor": "scene",
        "circuit": None,
        "scene": normalized,
        "summary": scene_summary(normalized),
        "svg": scene_to_svg(normalized, show_names=True),
        "svg_plain": scene_to_svg(normalized, show_names=False),
        "warnings": all_warnings,
        "changes": [],
        "source": "scene",
        "provider": "",
        "model": "",
        "library": component_payload(),
    }


def _reject_empty_circuit(raw: Any, message: str = "电路至少需要 1 个元件；请从元件面板添加，或恢复默认电路。") -> None:
    """An explicit circuit must have components; only a missing circuit defaults.

    ``normalize_circuit`` deliberately falls back to the built-in circuit for
    incomplete model output, but silently replacing a user's emptied editor
    with four surprise components would be worse than an error.  The same rule
    applies to LLM edits and image recognition: an empty component list means
    the model failed, not that the default circuit is the answer.
    """
    source = raw.get("circuit") if isinstance(raw, dict) and isinstance(raw.get("circuit"), dict) else raw
    if not isinstance(source, dict):
        return
    components = source.get("components")
    if not isinstance(components, list) or not components:
        raise SceneEditError(message)


def preview_scene(payload: dict[str, Any]) -> dict[str, Any]:
    """Preview either a semantic circuit or a generic scene."""
    circuit_raw = payload.get("circuit")
    if isinstance(circuit_raw, dict):
        _reject_empty_circuit(circuit_raw)
        try:
            circuit, warnings = normalize_circuit(circuit_raw)
        except CircuitError as error:
            raise SceneEditError(str(error)) from error
        return _preview_from_circuit(circuit, warnings=warnings, source="circuit")
    scene_raw = payload.get("scene")
    if isinstance(scene_raw, dict):
        try:
            return _preview_from_scene(scene_raw)
        except SceneError as error:
            raise SceneEditError(str(error)) from error
    circuit, warnings = normalize_circuit(default_circuit())
    return _preview_from_circuit(circuit, warnings=warnings, source="default")


def _llm_edit_circuit(
    circuit: dict[str, Any],
    instruction: str,
    selection: dict[str, Any],
) -> tuple[dict[str, Any], list[str], str, str, str]:
    if not llm_status().get("configured"):
        raise SceneEditError(
            "这句话没有匹配到离线可执行的电路修改，且当前实例没有配置文本大模型。"
            "可以试试：把开关闭合、把灯泡换成电阻、加一个电流表、删除电阻、把回路放大。"
        )
    prompt = CIRCUIT_EDIT_PROMPT.format(
        circuit=json.dumps(circuit, ensure_ascii=False, indent=2),
        instruction=instruction[:1200],
    )
    result = chat_json(
        CIRCUIT_EDIT_SYSTEM,
        prompt,
        kind="text",
        max_tokens=4096,
        model=selection.get("model") or None,
        effort=selection.get("effort") or None,
    )
    if not result.ok:
        raise SceneEditError(f"模型没能完成这次修改：{result.message}")
    data = result.data if isinstance(result.data, dict) else None
    if data is None:
        raise SceneEditError("模型返回的不是 JSON 对象。")
    if data.get("supported") is False:
        raise SceneEditError(str(data.get("reason") or data.get("message") or "模型判断这条指令无法执行。"))
    raw = data.get("circuit") if isinstance(data.get("circuit"), dict) else data
    _reject_empty_circuit(
        raw,
        "模型没有返回任何电路元件，修改未应用。请把要求说得更具体，或手动在元件面板里调整。",
    )
    try:
        edited, warnings = normalize_circuit(raw)
    except CircuitError as error:
        raise SceneEditError(f"模型返回的电路结构无法使用：{error}") from error
    message = str(data.get("message") or "模型已按你的描述修改电路。").strip()[:180]
    return edited, warnings, message, result.provider, result.model


def edit_scene(payload: dict[str, Any]) -> dict[str, Any]:
    """Apply one semantic operation or a natural-language instruction."""
    circuit_raw = payload.get("circuit")
    _reject_empty_circuit(circuit_raw)
    try:
        circuit, warnings = normalize_circuit(circuit_raw if isinstance(circuit_raw, dict) else default_circuit())
    except CircuitError as error:
        raise SceneEditError(str(error)) from error

    changes: list[str] = []
    op = payload.get("op")
    if isinstance(op, dict):
        try:
            circuit, change, extra = apply_op(circuit, op)
        except CircuitError as error:
            raise SceneEditError(str(error)) from error
        changes.append(change)
        warnings.extend(extra)

    instruction = str(payload.get("instruction") or "").strip()
    source = "rules" if instruction else "manual"
    provider = ""
    model = ""
    if instruction:
        try:
            circuit, instruction_warnings, message = parse_circuit_instruction(circuit, instruction)
            warnings.extend(instruction_warnings)
            changes.append(message)
        except CircuitInstructionError as error:
            selection = resolve_selection(
                str(payload.get("model") or "").strip() or None,
                str(payload.get("effort") or "").strip().lower() or None,
            )
            circuit, model_warnings, message, provider, model = _llm_edit_circuit(
                circuit,
                instruction,
                selection,
            )
            warnings.extend(model_warnings)
            changes.append(message)
            source = "model"

    return _preview_from_circuit(
        circuit,
        warnings=warnings,
        source=source,
        changes=changes,
        provider=provider,
        model=model,
    )


def _recognition_not_circuit(
    message: str,
    *,
    provider: str = "",
    model: str = "",
    questions: Sequence[str] = (),
) -> dict[str, Any]:
    return {
        "ok": True,
        "kind": "scene",
        "editor": "none",
        "message": message,
        "provider": provider,
        "model": model,
        "questions": [str(item)[:180] for item in questions if str(item).strip()][:4],
    }


def recognize_circuit_image(
    paths: Sequence[str | Path],
    *,
    text: str = "",
    model: str | None = None,
    effort: str | None = None,
) -> dict[str, Any]:
    """Recognize a circuit diagram from an uploaded image.

    Only circuit schematics enter the visual editor.  A photo of, say, a plant
    cell is explicitly reported as ``kind: scene`` so the caller can route it
    through the normal scene-generation path instead of pretending it is a
    circuit.  A model that answers in prose or returns an empty component list
    is treated the same way: an honest "not recognised" beats silently loading
    the built-in four-component circuit.
    """
    images = [Path(path) for path in paths if Path(path).is_file()]
    if not images:
        raise SceneEditError("没有收到可读取的图片。")
    status = vision_provider_status()
    if not status.get("configured"):
        raise SceneEditError(
            "当前实例没有配置图像理解模型，无法识别电路图。"
            "可以在设置里配置 TOUCHLEARN_VLM_PROVIDER / TOUCHLEARN_VLM_MODEL 后重试，"
            "或直接用左侧元件面板手动搭建电路。"
        )
    selection = resolve_selection(model, effort)
    prompt = CIRCUIT_RECOGNIZE_PROMPT
    if text.strip():
        prompt += f"\n\n用户补充说明：{text.strip()[:800]}"
    result = chat_json(
        CIRCUIT_RECOGNIZE_SYSTEM,
        prompt,
        images=images[:1],
        kind="vision",
        max_tokens=4096,
        model=(selection.get("vision_model") or None),
        effort=selection.get("effort") or None,
    )
    if not result.ok:
        if "JSON" in result.message or "截断" in result.message:
            return _recognition_not_circuit(
                "图像模型这次没有返回可解析的电路结构；请换一张更清晰的电路图，或手动搭建电路。",
                provider=result.provider,
                model=result.model,
            )
        raise SceneEditError(f"读图失败：{result.message}")
    data = result.data if isinstance(result.data, dict) else None
    if data is None:
        return _recognition_not_circuit(
            "图像模型这次没有返回可解析的电路结构；请换一张更清晰的电路图，或手动搭建电路。",
            provider=result.provider,
            model=result.model,
        )
    if data.get("supported") is False:
        return _recognition_not_circuit(
            str(data.get("reason") or "这张图片无法识别。"),
            provider=result.provider,
            model=result.model,
            questions=data.get("questions") if isinstance(data.get("questions"), list) else (),
        )
    if data.get("is_circuit") is False:
        return _recognition_not_circuit(
            str(data.get("reason") or "这张图片不是电路图。"),
            provider=result.provider,
            model=result.model,
            questions=data.get("questions") if isinstance(data.get("questions"), list) else (),
        )
    circuit_raw = data.get("circuit")
    if not isinstance(circuit_raw, dict):
        return _recognition_not_circuit(
            "这张图片没有返回可用的电路结构；请换一张更清晰的电路图，或手动搭建电路。",
            provider=result.provider,
            model=result.model,
        )
    components = circuit_raw.get("components")
    if not isinstance(components, list) or not components:
        return _recognition_not_circuit(
            "这张图片没有识别出任何电路元件；请换一张更清晰的电路图，或手动搭建电路。",
            provider=result.provider,
            model=result.model,
            questions=data.get("questions") if isinstance(data.get("questions"), list) else (),
        )
    try:
        circuit, warnings = normalize_circuit(circuit_raw)
    except CircuitError as error:
        return _recognition_not_circuit(
            f"识别出的电路结构无法使用：{error}",
            provider=result.provider,
            model=result.model,
        )
    preview = _preview_from_circuit(
        circuit,
        warnings=warnings,
        source="image",
        changes=[f"已从图片识别出 {len(circuit['components'])} 个元件。"],
        provider=result.provider,
        model=result.model,
    )
    preview["message"] = str(data.get("message") or "已识别为电路图。")
    preview["confidence"] = data.get("confidence", 0.6)
    questions = data.get("questions")
    if isinstance(questions, list):
        preview["questions"] = [str(item)[:180] for item in questions if str(item).strip()][:4]
    return preview
