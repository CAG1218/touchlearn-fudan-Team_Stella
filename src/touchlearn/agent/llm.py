"""Provider-neutral access to OpenAI-compatible chat models.

The agent uses large models for *understanding* only: classification, reading
an expression from a description or image, and turning a described diagram
into a normalized scene spec.  Geometry and compliance stay deterministic.

Every provider below speaks the OpenAI ``/chat/completions`` protocol, so one
transport covers hosted APIs and local servers.  Keys are read from the
environment (or a local ``.env``); nothing is hard-coded and no key is ever
returned by the status endpoints.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Sequence
from urllib.parse import urlparse

DEFAULT_TIMEOUT = 75.0
MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_RESPONSE_BYTES = 4 * 1024 * 1024

# ``base_url`` is a base, not a full endpoint.  The transport appends the
# OpenAI-compatible path.  ``vision_model`` is empty when the provider has no
# public vision model in this preset.
PRESETS: dict[str, dict[str, Any]] = {
    "openai": {
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "vision_model": "gpt-4o-mini",
        "requires_key": True,
    },
    "deepseek": {
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-v4-pro",
        "vision_model": "deepseek-flash",
        # Long structured JSON (scene specs) is both faster and more detailed
        # on Flash; Pro stays the default for reasoning-heavy text tasks.
        "scene_model": "deepseek-flash",
        "requires_key": True,
        # Reasoning models spend output budget on hidden tokens before writing
        # the answer.  ``low`` keeps structured JSON tasks fast and leaves most
        # of ``max_tokens`` for the actual output.
        "default_effort": "low",
        # Scene specs are long JSON, not a reasoning puzzle.  Turning thinking
        # off avoids burning the output budget and cut latency from ~28 s to
        # ~5 s in local benchmarks.
        "scene_effort": "off",
    },
    "tokenhub": {
        "label": "腾讯云 TokenHub",
        "base_url": "https://tokenhub.tencentmaas.com/v1",
        # Flash is the fastest model on TokenHub that still classified and
        # extracted expressions correctly (3.5-4.4 s vs 12-20 s for Kimi).
        # ``kimi-k3.1-pro`` remains available via TOUCHLEARN_LLM_MODEL when
        # maximum writing quality matters more than latency.
        "model": "deepseek-v4.1-flash",
        "vision_model": "deepseek/deepseek-flash",
        "scene_model": "deepseek-v4.1-flash",
        "requires_key": True,
        # TokenHub passes reasoning_effort through to the underlying model.
        "default_effort": "low",
        "scene_effort": "off",
    },
    "dashscope": {
        "label": "阿里云百炼 / 通义千问",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
        "vision_model": "qwen-vl-max",
        "requires_key": True,
    },
    "moonshot": {
        "label": "月之暗面 / Kimi",
        "base_url": "https://api.moonshot.cn/v1",
        "model": "moonshot-v1-8k",
        "vision_model": "moonshot-v1-8k-vision-preview",
        "requires_key": True,
    },
    "zhipu": {
        "label": "智谱 GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4-flash",
        "vision_model": "glm-4v-flash",
        "requires_key": True,
    },
    "ollama": {
        "label": "本地 Ollama / vLLM",
        "base_url": "http://127.0.0.1:11434/v1",
        "model": "qwen2.5:7b",
        "vision_model": "qwen2.5vl:7b",
        "requires_key": False,
    },
    "custom": {
        "label": "自定义 OpenAI 兼容接口",
        "base_url": "",
        "model": "",
        "vision_model": "",
        "requires_key": False,
    },
}

# Curated, per-provider model lists offered in the UI.  A model is listed only
# after it has been exercised against the provider from this codebase.  The
# environment default is inserted automatically when it is missing, so custom
# endpoints still expose exactly one selectable model instead of none.
MODEL_CATALOG: dict[str, list[dict[str, Any]]] = {
    "tokenhub": [
        {
            "id": "deepseek-v4.1-flash",
            "label": "DeepSeek-V4.1-Flash",
            "short": "V4.1 Flash",
            "tag": "快速",
            "note": "分类、抽取解析式和场景生成都在数秒内完成",
            "vision": False,
        },
        {
            "id": "deepseek-v4-pro",
            "label": "DeepSeek-V4-Pro",
            "short": "V4 Pro",
            "tag": "推理",
            "note": "复杂描述和长推理更稳，响应更慢",
            "vision": False,
        },
        {
            "id": "glm-5.3",
            "label": "GLM-5.3",
            "short": "GLM-5.3",
            "tag": "通用",
            "note": "中文表达稳定，适合教材场景",
            "vision": False,
        },
        {
            "id": "glm-5.3-flash",
            "label": "GLM-5.3-Flash",
            "short": "GLM Flash",
            "tag": "快速",
            "note": "轻量分类与抽取优先",
            "vision": False,
        },
        {
            "id": "kimi-k3.1-pro",
            "label": "Kimi-K3.1-Pro",
            "short": "Kimi K3.1",
            "tag": "长文本",
            "note": "长描述理解强，响应偏慢",
            "vision": False,
        },
        {
            "id": "minimax-m3",
            "label": "MiniMax-M3",
            "short": "MiniMax M3",
            "tag": "通用",
            "note": "通用对话与结构化输出",
            "vision": False,
        },
        {
            "id": "hy4-preview",
            "label": "混元 Hy4 Preview",
            "short": "Hy4 Preview",
            "tag": "预览",
            "note": "腾讯混元预览模型",
            "vision": False,
        },
        {
            "id": "deepseek/deepseek-flash",
            "label": "DeepSeek Flash（读图）",
            "short": "DeepSeek Flash",
            "tag": "读图",
            "note": "图片识别与图片转场景",
            "vision": True,
        },
        {
            "id": "hy-vision-2.0-instruct",
            "label": "混元 Vision 2.0（读图）",
            "short": "Hy Vision 2.0",
            "tag": "读图",
            "note": "备用视觉模型",
            "vision": True,
        },
    ],
    "deepseek": [
        {
            "id": "deepseek-v4-pro",
            "label": "DeepSeek-V4-Pro",
            "short": "V4 Pro",
            "tag": "推理",
            "note": "复杂描述和长推理更稳",
            "vision": False,
        },
        {
            "id": "deepseek-flash",
            "label": "DeepSeek-Flash",
            "short": "DeepSeek Flash",
            "tag": "快速 · 读图",
            "note": "快速分类、场景 JSON 与图片识别",
            "vision": True,
        },
    ],
}

EFFORT_OPTIONS: tuple[dict[str, str], ...] = (
    {"id": "low", "label": "低", "note": "响应更快，适合大多数分类与结构化任务"},
    {"id": "medium", "label": "中", "note": "在速度与推理之间取平衡"},
    {"id": "high", "label": "高", "note": "复杂描述更稳，响应更慢"},
)

MIME_OVERRIDES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".gif": "image/gif",
}


@dataclass(frozen=True)
class ProviderConfig:
    provider: str
    label: str
    endpoint: str
    model: str
    api_key: str = ""
    timeout: float = DEFAULT_TIMEOUT
    source: str = "env"

    @property
    def local(self) -> bool:
        host = (urlparse(self.endpoint).hostname or "").lower()
        return host in {"localhost", "127.0.0.1", "::1", "0.0.0.0"} or host.endswith(".local")


@dataclass
class LLMResult:
    ok: bool
    provider: str = ""
    model: str = ""
    message: str = ""
    data: Any = None
    text: str = ""
    latency_ms: int = 0
    usage: dict[str, Any] | None = None
    finish_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "provider": self.provider,
            "model": self.model,
            "message": self.message,
            "latency_ms": self.latency_ms,
            "usage": self.usage or {},
            "finish_reason": self.finish_reason,
            "has_data": self.data is not None,
        }


_ENV_LOADED = False


def _load_env_file(path: Path) -> None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:]
        name, _, value = line.partition("=")
        name = name.strip()
        value = value.strip()
        if not name or name in os.environ:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ[name] = value


def load_env() -> None:
    """Load a local ``.env`` once, without overriding real environment vars."""
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    _ENV_LOADED = True
    candidates: list[Path] = []
    explicit = os.environ.get("TOUCHLEARN_ENV_FILE", "").strip()
    if explicit:
        candidates.append(Path(explicit))
    candidates.append(Path.cwd() / ".env")
    candidates.append(Path(__file__).resolve().parents[3] / ".env")
    seen: set[Path] = set()
    for path in candidates:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.is_file():
            _load_env_file(resolved)


def _env(name: str) -> str:
    load_env()
    return os.environ.get(name, "").strip()


def _preset(name: str) -> dict[str, Any]:
    return PRESETS.get(name, PRESETS["custom"])


def _chat_endpoint(base_url: str) -> str:
    value = base_url.strip().rstrip("/")
    if not value:
        return ""
    if value.endswith("/chat/completions"):
        return value
    parsed = urlparse(value)
    if not parsed.path or parsed.path == "/":
        return value + "/v1/chat/completions"
    if value.endswith("/v1"):
        return value + "/chat/completions"
    return value + "/chat/completions"


def _timeout() -> float:
    raw = _env("TOUCHLEARN_LLM_TIMEOUT")
    try:
        value = float(raw) if raw else DEFAULT_TIMEOUT
    except ValueError:
        value = DEFAULT_TIMEOUT
    return max(5.0, min(300.0, value))


def _opener() -> urllib.request.OpenerDirector:
    """Build an opener that ignores stale system proxies unless one is configured.

    Windows often has a Clash/V2Ray proxy in the registry.  When that helper is
    not running, ``urllib`` waits for a dead ``127.0.0.1:7890`` and every model
    call times out, while ``curl`` keeps working because it ignores the registry.
    ``TOUCHLEARN_LLM_PROXY`` opts back into a proxy explicitly.
    """
    proxy = _env("TOUCHLEARN_LLM_PROXY")
    if proxy:
        handler: urllib.request.BaseHandler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
    else:
        handler = urllib.request.ProxyHandler({})
    return urllib.request.build_opener(handler)


# Providers with an explicit reasoning-mode switch.  Sending ``reasoning_effort``
# to an API that does not understand it would turn a valid request into a 400,
# so the field is only added for these providers; everyone else gets a clean
# OpenAI-compatible payload.
REASONING_PROVIDERS = {"deepseek", "tokenhub"}
EFFORT_LEVELS = {item["id"] for item in EFFORT_OPTIONS}

# Kimi K3.1 rejects ``temperature: 0`` (and some builds reject the field
# entirely).  The provider default is used instead.
NO_TEMPERATURE_PREFIXES = ("kimi-k3.1",)


def _effort(provider: str, requested: str | None = None) -> str:
    """Resolve the reasoning effort: ``""`` (provider default), low/high/max, or off."""
    raw = (requested or "").strip().lower()
    if not raw:
        raw = _env("TOUCHLEARN_LLM_EFFORT").lower()
    if not raw:
        raw = str(_preset(provider).get("default_effort") or "").lower()
    if raw in {"off", "none", "disabled", "false", "0"}:
        return "off"
    if raw in EFFORT_LEVELS:
        return raw
    return ""


def _config_from_env(kind: str) -> ProviderConfig | None:
    load_env()
    if kind == "vision":
        provider = _env("TOUCHLEARN_VLM_PROVIDER")
        base_url = _env("TOUCHLEARN_VLM_URL")
        api_key = _env("TOUCHLEARN_VLM_KEY")
        model = _env("TOUCHLEARN_VLM_MODEL")
        explicit = bool(provider or base_url or model)
        if not explicit:
            # A text provider that ships a vision model can serve both roles.
            text = _config_from_env("text")
            if text is None:
                return None
            preset = _preset(text.provider)
            vision_model = str(preset.get("vision_model") or "")
            if not vision_model:
                return None
            return ProviderConfig(
                provider=text.provider,
                label=text.label,
                endpoint=text.endpoint,
                model=vision_model,
                api_key=text.api_key,
                timeout=text.timeout,
                source=text.source,
            )
        provider = provider or _env("TOUCHLEARN_LLM_PROVIDER") or ("custom" if base_url else "openai")
    else:
        provider = _env("TOUCHLEARN_LLM_PROVIDER")
        base_url = _env("TOUCHLEARN_LLM_URL")
        api_key = _env("TOUCHLEARN_LLM_KEY")
        model = _env("TOUCHLEARN_LLM_MODEL")
        if not (provider or base_url or model):
            return None
        provider = provider or ("custom" if base_url else "openai")

    # A provider that serves both roles should not force the same secret to be
    # entered twice.  Only reuse the text key when the provider names match.
    if not api_key and provider == (_env("TOUCHLEARN_LLM_PROVIDER") or ""):
        api_key = _env("TOUCHLEARN_LLM_KEY")

    preset = _preset(provider)
    base_url = base_url or str(preset.get("base_url") or "")
    if not model:
        model = str(preset.get("vision_model" if kind == "vision" else "model") or "")
    if not base_url or not model:
        return None
    if preset.get("requires_key") and not api_key:
        return None
    return ProviderConfig(
        provider=provider,
        label=str(preset.get("label") or provider),
        endpoint=_chat_endpoint(base_url),
        model=model,
        api_key=api_key,
        timeout=_timeout(),
    )


def resolve_config(kind: str = "text") -> ProviderConfig | None:
    return _config_from_env("vision" if kind == "vision" else "text")


def scene_model() -> str:
    """Optional faster model for long structured JSON (scene specs).

    ``TOUCHLEARN_SCENE_MODEL`` wins; otherwise the preset may name one.  An
    empty result means "use the normal text model".
    """
    explicit = _env("TOUCHLEARN_SCENE_MODEL")
    if explicit:
        return explicit
    provider = _env("TOUCHLEARN_LLM_PROVIDER")
    return str(_preset(provider).get("scene_model") or "")


def scene_effort() -> str:
    """Reasoning effort for long structured JSON (scene specs).

    ``TOUCHLEARN_SCENE_EFFORT`` wins; otherwise the preset may name one.  Scene
    JSON does not benefit from hidden reasoning, and some providers count those
    tokens against ``max_tokens``, which truncates the scene.  An empty result
    falls back to the normal text effort.
    """
    explicit = _env("TOUCHLEARN_SCENE_EFFORT")
    if explicit:
        return explicit
    provider = _env("TOUCHLEARN_LLM_PROVIDER")
    return str(_preset(provider).get("scene_effort") or "")


def _default_ui_effort(provider: str) -> str:
    """Initial thinking strength shown in the picker.

    The picker exposes low/medium/high.  A server configured with ``off`` still
    gets a usable default instead of an invisible value; scene generation keeps
    its own fast-effort handling and automatically falls back when a long JSON
    answer is truncated.
    """
    raw = (_env("TOUCHLEARN_LLM_EFFORT") or str(_preset(provider).get("default_effort") or "")).strip().lower()
    return raw if raw in EFFORT_LEVELS else "low"


def model_catalog() -> dict[str, Any]:
    """Return the models this instance can offer, plus the initial default.

    Only curated models are selectable.  The configured environment model is
    always inserted so a custom/unknown provider still has one valid choice.
    """
    load_env()
    config = resolve_config("text")
    provider = config.provider if config else ""
    default_model = config.model if config else ""
    entries = [dict(item) for item in MODEL_CATALOG.get(provider, [])]
    if default_model and not any(str(item.get("id")) == default_model for item in entries):
        entries.insert(
            0,
            {
                "id": default_model,
                "label": default_model,
                "short": default_model,
                "tag": "",
                "note": "由服务端环境变量指定的模型",
                "vision": False,
            },
        )
    for item in entries:
        item["id"] = str(item.get("id") or "")
        item["label"] = str(item.get("label") or item["id"])
        item["short"] = str(item.get("short") or item["label"])
        item["tag"] = str(item.get("tag") or "")
        item["note"] = str(item.get("note") or "")
        item["vision"] = bool(item.get("vision"))
        item["default"] = item["id"] == default_model

    vision_config = resolve_config("vision")
    same_provider = bool(vision_config and vision_config.provider == provider)
    default_vision_model = vision_config.model if same_provider else ""
    return {
        "configured": bool(config),
        "provider": provider,
        "label": config.label if config else "",
        "default": {
            "model": default_model,
            "effort": _default_ui_effort(provider),
            "vision_model": default_vision_model,
        },
        "models": entries,
        "efforts": [dict(item) for item in EFFORT_OPTIONS],
        "supports_effort": provider in REASONING_PROVIDERS,
        "note": (
            "模型由当前服务商提供；切换只影响下一次请求。"
            if entries
            else "当前实例没有配置可用模型，正在使用离线规则与模板。"
        ),
    }


def resolve_selection(model: str | None = None, effort: str | None = None) -> dict[str, Any]:
    """Validate a per-request model/effort choice.

    Unknown model names are rejected instead of being forwarded to the
    provider, so the browser cannot turn this endpoint into an arbitrary model
    proxy.  ``effort`` accepts the three picker levels and the internal
    ``off`` value used by long-JSON fallbacks.
    """
    catalog = model_catalog()
    entries = {str(item["id"]): item for item in catalog["models"]}
    requested_model = (model or "").strip()
    if requested_model:
        if not entries:
            raise ValueError("当前实例没有配置可用的大模型，无法切换模型。")
        entry = entries.get(requested_model)
        if entry is None:
            options = "、".join(item["label"] for item in catalog["models"][:8])
            raise ValueError(f"不支持所选模型：{requested_model}。可选：{options}")
    else:
        entry = entries.get(str(catalog["default"].get("model") or ""))
        if entry is None and entries:
            entry = next(iter(entries.values()))

    requested_effort = (effort or "").strip().lower()
    if requested_effort:
        if requested_effort in {"off", "none", "disabled", "false", "0"}:
            resolved_effort = "off"
        elif requested_effort in EFFORT_LEVELS:
            resolved_effort = requested_effort
        else:
            raise ValueError("思考强度只能是 low、medium 或 high。")
    else:
        resolved_effort = str(catalog["default"].get("effort") or "")

    if entry is None:
        return {
            "provider": "",
            "label": "",
            "model": "",
            "effort": resolved_effort,
            "vision_model": "",
            "vision_selected": False,
        }
    vision_model = (
        str(entry["id"])
        if entry.get("vision")
        else str(catalog["default"].get("vision_model") or "")
    )
    return {
        "provider": str(catalog.get("provider") or ""),
        "label": str(entry.get("label") or entry["id"]),
        "model": str(entry["id"]),
        "effort": resolved_effort,
        "vision_model": vision_model,
        "vision_selected": bool(entry.get("vision")),
    }


def _public_endpoint(endpoint: str) -> str:
    parsed = urlparse(endpoint)
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return host


def _missing_message(kind: str) -> str:
    prefix = "TOUCHLEARN_VLM" if kind == "vision" else "TOUCHLEARN_LLM"
    provider = _env(prefix + "_PROVIDER") or _env("TOUCHLEARN_LLM_PROVIDER")
    base_url = _env(prefix + "_URL") or _env("TOUCHLEARN_LLM_URL")
    model = _env(prefix + "_MODEL")
    api_key = _env(prefix + "_KEY") or _env("TOUCHLEARN_LLM_KEY")
    if provider or base_url or model:
        preset = _preset(provider or ("custom" if base_url else "openai"))
        if preset.get("requires_key") and not api_key:
            return f"已选择 {preset['label']}，但缺少 {prefix}_KEY。"
        if not (base_url or preset.get("base_url")):
            return f"缺少接口地址 {prefix}_URL。"
        if not (model or preset.get("vision_model" if kind == "vision" else "model")):
            return f"缺少模型名 {prefix}_MODEL。"
    return f"未配置接口。设置 {prefix}_URL 与 {prefix}_MODEL（或 {prefix}_PROVIDER 使用预置）后启用。"


def provider_status(kind: str = "text") -> dict[str, Any]:
    load_env()
    prefix = "TOUCHLEARN_VLM" if kind == "vision" else "TOUCHLEARN_LLM"
    config = resolve_config(kind)
    if config is None:
        return {
            "configured": False,
            "kind": "vision" if kind == "vision" else "text",
            "provider": _env(prefix + "_PROVIDER") or _env("TOUCHLEARN_LLM_PROVIDER") or "none",
            "label": "",
            "model": _env(prefix + "_MODEL"),
            "endpoint_host": "",
            "has_key": bool(_env(prefix + "_KEY") or _env("TOUCHLEARN_LLM_KEY")),
            "local": False,
            "timeout": _timeout(),
            "effort": "",
            "mode": "offline",
            "message": _missing_message(kind),
        }
    return {
        "configured": True,
        "kind": "vision" if kind == "vision" else "text",
        "provider": config.provider,
        "label": config.label,
        "model": config.model,
        "endpoint_host": _public_endpoint(config.endpoint),
        "has_key": bool(config.api_key),
        "local": config.local,
        "timeout": config.timeout,
        "effort": _effort(config.provider) or "default",
        "mode": "remote-vlm" if kind == "vision" else "remote-llm",
        "message": "接口已配置。",
    }


def llm_status() -> dict[str, Any]:
    return provider_status("text")


def vision_provider_status() -> dict[str, Any]:
    return provider_status("vision")


def provider_presets() -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for name, preset in PRESETS.items():
        result.append(
            {
                "id": name,
                "label": preset["label"],
                "base_url": preset["base_url"],
                "model": preset["model"],
                "vision_model": preset["vision_model"],
                "scene_model": preset.get("scene_model", ""),
                "requires_key": preset["requires_key"],
            }
        )
    return result


def providers_payload() -> dict[str, Any]:
    return {
        "llm": provider_status("text"),
        "vision": provider_status("vision"),
        "presets": provider_presets(),
        "models": model_catalog(),
        "environment": {
            "text": ["TOUCHLEARN_LLM_PROVIDER", "TOUCHLEARN_LLM_URL", "TOUCHLEARN_LLM_KEY", "TOUCHLEARN_LLM_MODEL"],
            "vision": ["TOUCHLEARN_VLM_PROVIDER", "TOUCHLEARN_VLM_URL", "TOUCHLEARN_VLM_KEY", "TOUCHLEARN_VLM_MODEL"],
            "shared": [
                "TOUCHLEARN_LLM_TIMEOUT",
                "TOUCHLEARN_LLM_EFFORT",
                "TOUCHLEARN_SCENE_MODEL",
                "TOUCHLEARN_SCENE_EFFORT",
                "TOUCHLEARN_LLM_PROXY",
                "TOUCHLEARN_ENV_FILE",
            ],
        },
        "privacy": "密钥只从环境变量或本机 .env 读取，不写入作业目录，也不会通过 API 返回。",
    }


def _image_data_url(path: Path) -> tuple[str, str]:
    mime = MIME_OVERRIDES.get(path.suffix.lower()) or (mimetypes.guess_type(path.name)[0] or "")
    if not mime.startswith("image/"):
        raise ValueError(f"不是可发送的位图：{path.name}（PDF 请先导出 PNG）")
    if path.stat().st_size > MAX_IMAGE_BYTES:
        raise ValueError(f"图片超过 {MAX_IMAGE_BYTES // (1024 * 1024)} MB：{path.name}")
    return mime, f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


def _messages(system: str, user: str, images: Sequence[str | Path]) -> list[dict[str, Any]]:
    content: Any = user
    if images:
        parts: list[dict[str, Any]] = [{"type": "text", "text": user}]
        for raw in images:
            path = Path(raw)
            if not path.is_file():
                raise FileNotFoundError(f"图片不存在：{path}")
            _mime, data_url = _image_data_url(path)
            parts.append({"type": "image_url", "image_url": {"url": data_url}})
        content = parts
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": content},
    ]


def _error_detail(body: bytes) -> str:
    text = body.decode("utf-8", errors="replace").strip()
    if not text:
        return "empty response"
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return text[:300]
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error)[:300]
        if isinstance(error, str):
            return error[:300]
        if payload.get("message"):
            return str(payload["message"])[:300]
    return text[:300]


def chat(
    messages: list[dict[str, Any]],
    *,
    kind: str = "text",
    config: ProviderConfig | None = None,
    temperature: float = 0.0,
    max_tokens: int = 1800,
    timeout: float | None = None,
    effort: str | None = None,
    model: str | None = None,
) -> LLMResult:
    """Send one chat completion request and return a structured result."""
    config = config or resolve_config(kind)
    if config is None:
        return LLMResult(False, message="未配置大模型接口。", provider="offline")
    if model:
        config = replace(config, model=model)
    payload = {
        "model": config.model,
        "messages": messages,
        "max_tokens": max(64, int(max_tokens)),
    }
    if not any(config.model.startswith(prefix) for prefix in NO_TEMPERATURE_PREFIXES):
        payload["temperature"] = max(0.0, min(2.0, temperature))
    reasoning = _effort(config.provider, effort)
    if config.provider in REASONING_PROVIDERS:
        if reasoning == "off":
            if config.provider == "deepseek":
                payload["thinking"] = {"type": "disabled"}
            else:
                # TokenHub's GLM/Kimi/MiniMax branches reject ``thinking`` but
                # understand the OpenAI-compatible ``reasoning_effort`` field.
                payload["reasoning_effort"] = "none"
        elif reasoning:
            payload["reasoning_effort"] = reasoning
    request = urllib.request.Request(
        config.endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            **({"Authorization": f"Bearer {config.api_key}"} if config.api_key else {}),
        },
        method="POST",
    )
    started = time.monotonic()
    try:
        with _opener().open(request, timeout=timeout or config.timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        return LLMResult(
            False,
            provider=config.provider,
            model=config.model,
            message=f"接口返回 HTTP {error.code}：{_error_detail(error.read())}",
            latency_ms=int((time.monotonic() - started) * 1000),
        )
    except urllib.error.URLError as error:
        return LLMResult(
            False,
            provider=config.provider,
            model=config.model,
            message=f"无法连接接口：{error.reason}",
            latency_ms=int((time.monotonic() - started) * 1000),
        )
    except (TimeoutError, OSError) as error:
        return LLMResult(
            False,
            provider=config.provider,
            model=config.model,
            message=f"接口调用失败：{error}",
            latency_ms=int((time.monotonic() - started) * 1000),
        )
    latency = int((time.monotonic() - started) * 1000)
    if len(raw) > MAX_RESPONSE_BYTES:
        return LLMResult(False, provider=config.provider, model=config.model, message="接口响应过大。", latency_ms=latency)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return LLMResult(False, provider=config.provider, model=config.model, message="接口返回的不是 JSON。", latency_ms=latency)

    choices = data.get("choices") or []
    if not choices:
        return LLMResult(
            False,
            provider=config.provider,
            model=config.model,
            message="接口没有返回候选结果。",
            latency_ms=latency,
        )
    choice = choices[0] if isinstance(choices[0], dict) else {}
    finish_reason = str(choice.get("finish_reason") or "")
    message = choice.get("message") or {}
    content = message.get("content", "")
    if isinstance(content, list):
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    text = str(content or "").strip()
    if not text:
        detail = "接口返回为空。"
        if finish_reason == "length":
            detail = (
                "模型在写出正文前耗尽了 max_tokens（finish_reason=length），"
                "通常是推理过程占满了输出预算。请提高 max_tokens 或降低推理强度。"
            )
        return LLMResult(
            False,
            provider=config.provider,
            model=config.model,
            message=detail,
            latency_ms=latency,
            usage=data.get("usage"),
            finish_reason=finish_reason,
        )
    return LLMResult(
        True,
        provider=config.provider,
        model=config.model,
        message="ok",
        text=text,
        latency_ms=latency,
        usage=data.get("usage"),
        finish_reason=finish_reason,
    )


def chat_text(
    system: str,
    user: str,
    *,
    images: Sequence[str | Path] = (),
    kind: str = "text",
    temperature: float = 0.0,
    max_tokens: int = 1800,
    timeout: float | None = None,
    effort: str | None = None,
    model: str | None = None,
) -> LLMResult:
    try:
        messages = _messages(system, user, images)
    except (ValueError, OSError) as error:
        return LLMResult(False, message=str(error), provider="local")
    return chat(
        messages,
        kind=kind,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
        effort=effort,
        model=model,
    )


def extract_json(text: str) -> Any:
    """Extract the first JSON object/array from model output."""
    cleaned = text.strip()
    cleaned = cleaned.removeprefix("```json").removeprefix("```JSON").removeprefix("```").strip()
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3].strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    start = -1
    opening = ""
    for index, character in enumerate(cleaned):
        if character in "{[":
            start = index
            opening = character
            break
    if start < 0:
        return None
    closing = "}" if opening == "{" else "]"
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(cleaned)):
        character = cleaned[index]
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character == opening:
            depth += 1
        elif character == closing:
            depth -= 1
            if depth == 0:
                snippet = cleaned[start : index + 1]
                try:
                    return json.loads(snippet)
                except json.JSONDecodeError:
                    return None
    return None


def chat_json(
    system: str,
    user: str,
    *,
    images: Sequence[str | Path] = (),
    kind: str = "text",
    temperature: float = 0.0,
    max_tokens: int = 1800,
    timeout: float | None = None,
    effort: str | None = None,
    model: str | None = None,
) -> LLMResult:
    """Chat and parse a JSON object/array; keeps the raw text for diagnostics."""
    result = chat_text(
        system,
        user,
        images=images,
        kind=kind,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
        effort=effort,
        model=model,
    )
    if not result.ok:
        return result
    data = extract_json(result.text)
    if data is None:
        detail = "模型没有返回可解析的 JSON。"
        if result.finish_reason == "length":
            detail = "模型输出被 max_tokens 截断，未能形成完整 JSON；请提高 max_tokens 或降低推理强度。"
        return LLMResult(
            False,
            provider=result.provider,
            model=result.model,
            message=detail,
            text=result.text,
            latency_ms=result.latency_ms,
            usage=result.usage,
            finish_reason=result.finish_reason,
        )
    result.data = data
    return result
