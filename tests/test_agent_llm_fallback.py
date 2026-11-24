from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from tests import EXAMPLES  # noqa: F401  (ensures src is on sys.path)

from touchlearn.agent import llm


def _config(provider: str, label: str, endpoint: str, model: str) -> llm.ProviderConfig:
    return llm.ProviderConfig(provider=provider, label=label, endpoint=endpoint, model=model)


class LocalFallbackTests(unittest.TestCase):
    def test_cloud_failure_falls_back_to_local(self) -> None:
        calls: list[object] = []

        def fake_once(messages, *, kind="text", config=None, **kwargs):
            calls.append(config)
            if len(calls) == 1:
                return llm.LLMResult(False, provider="tokenhub", model="stub-cloud", message="接口返回 HTTP 500")
            return llm.LLMResult(True, provider="custom", model="qwen2.5-3b", text='{"ok": true}', message="ok")

        fallback = _config("custom", "本地兜底（llama.cpp）", "http://127.0.0.1:8080/v1/chat/completions", "qwen2.5-3b")
        cloud = _config("tokenhub", "腾讯云 TokenHub", "https://tokenhub.example/v1/chat/completions", "stub-cloud")
        with patch.object(llm, "_chat_once", side_effect=fake_once), patch.object(
            llm, "_local_fallback_config", return_value=fallback
        ), patch.object(llm, "resolve_config", return_value=cloud):
            result = llm.chat([{"role": "user", "content": "hi"}])

        self.assertTrue(result.ok)
        self.assertIn("已回落本地模型", result.message)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1].label, "本地兜底（llama.cpp）")

    def test_no_fallback_when_disabled(self) -> None:
        def fake_once(messages, *, kind="text", config=None, **kwargs):
            return llm.LLMResult(False, provider="tokenhub", model="stub-cloud", message="无法连接接口")

        cloud = _config("tokenhub", "腾讯云 TokenHub", "https://tokenhub.example/v1/chat/completions", "stub-cloud")
        with patch.object(llm, "_chat_once", side_effect=fake_once), patch.object(
            llm, "_local_fallback_config", return_value=None
        ), patch.object(llm, "resolve_config", return_value=cloud):
            result = llm.chat([{"role": "user", "content": "hi"}])

        self.assertFalse(result.ok)
        self.assertNotIn("已回落", result.message)

    def test_local_primary_is_not_retried(self) -> None:
        def fake_once(messages, *, kind="text", config=None, **kwargs):
            return llm.LLMResult(False, provider="custom", model="qwen2.5-3b", message="本地服务不可用")

        local = _config("custom", "本地", "http://127.0.0.1:8080/v1/chat/completions", "qwen2.5-3b")
        with patch.object(llm, "_chat_once", side_effect=fake_once) as once, patch.object(
            llm, "_local_fallback_config", return_value=local
        ), patch.object(llm, "resolve_config", return_value=local):
            result = llm.chat([{"role": "user", "content": "hi"}])

        self.assertFalse(result.ok)
        self.assertEqual(once.call_count, 1)

    def test_vision_never_uses_text_fallback(self) -> None:
        with patch.dict(os.environ, {"TOUCHLEARN_LOCAL_FALLBACK": "1"}, clear=False):
            self.assertIsNone(llm._local_fallback_config("vision"))


if __name__ == "__main__":
    unittest.main()
