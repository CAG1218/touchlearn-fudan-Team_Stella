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
from dataclasses import dataclass
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
        "model": "deepseek-chat",
        "vision_model": "",
        "requires_key": True,
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "provider": self.provider,
            "model": self.model,
            "message": self.message,
            "latency_ms": self.latency_ms,
            "usage": self.usage or {},
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
                "requires_key": preset["requires_key"],
            }
        )
    return result


def providers_payload() -> dict[str, Any]:
    return {
        "llm": provider_status("text"),
        "vision": provider_status("vision"),
        "presets": provider_presets(),
        "environment": {
            "text": ["TOUCHLEARN_LLM_PROVIDER", "TOUCHLEARN_LLM_URL", "TOUCHLEARN_LLM_KEY", "TOUCHLEARN_LLM_MODEL"],
            "vision": ["TOUCHLEARN_VLM_PROVIDER", "TOUCHLEARN_VLM_URL", "TOUCHLEARN_VLM_KEY", "TOUCHLEARN_VLM_MODEL"],
            "shared": ["TOUCHLEARN_LLM_TIMEOUT", "TOUCHLEARN_ENV_FILE"],
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
) -> LLMResult:
    """Send one chat completion request and return a structured result."""
    config = config or resolve_config(kind)
    if config is None:
        return LLMResult(False, message="未配置大模型接口。", provider="offline")
    payload = {
        "model": config.model,
        "messages": messages,
        "temperature": max(0.0, min(2.0, temperature)),
        "max_tokens": max(64, int(max_tokens)),
    }
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
        with urllib.request.urlopen(request, timeout=timeout or config.timeout) as response:
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
    message = choices[0].get("message") or {}
    content = message.get("content", "")
    if isinstance(content, list):
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    text = str(content or "").strip()
    if not text:
        return LLMResult(
            False,
            provider=config.provider,
            model=config.model,
            message="接口返回为空。",
            latency_ms=latency,
            usage=data.get("usage"),
        )
    return LLMResult(
        True,
        provider=config.provider,
        model=config.model,
        message="ok",
        text=text,
        latency_ms=latency,
        usage=data.get("usage"),
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
) -> LLMResult:
    try:
        messages = _messages(system, user, images)
    except (ValueError, OSError) as error:
        return LLMResult(False, message=str(error), provider="local")
    return chat(messages, kind=kind, temperature=temperature, max_tokens=max_tokens, timeout=timeout)


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
    )
    if not result.ok:
        return result
    data = extract_json(result.text)
    if data is None:
        return LLMResult(
            False,
            provider=result.provider,
            model=result.model,
            message="模型没有返回可解析的 JSON。",
            text=result.text,
            latency_ms=result.latency_ms,
            usage=result.usage,
        )
    result.data = data
    return result
