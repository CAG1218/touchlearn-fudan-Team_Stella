from __future__ import annotations

import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from tests import EXAMPLES  # noqa: F401  (ensures src is on sys.path)

from touchlearn.agent import audio


class SpeechEngineTests(unittest.TestCase):
    def test_windows_prefers_sapi_and_keeps_wav(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(
            audio, "_is_windows", return_value=True
        ), patch.object(audio, "synthesize_windows_tts", return_value=(True, "sapi ok")) as sapi, patch.object(
            audio, "synthesize_edge_tts", return_value=(True, "edge ok")
        ) as edge:
            ok, message, name = audio.synthesize_speech("你好", directory)
        self.assertTrue(ok)
        self.assertEqual(message, "sapi ok")
        self.assertEqual(name, "narration.wav")
        sapi.assert_called_once()
        edge.assert_not_called()

    def test_windows_falls_back_to_edge_when_sapi_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(
            audio, "_is_windows", return_value=True
        ), patch.object(
            audio, "synthesize_windows_tts", return_value=(False, "no voice pack")
        ), patch.object(audio, "synthesize_edge_tts", return_value=(True, "edge ok")) as edge:
            ok, message, name = audio.synthesize_speech("你好", directory)
        self.assertTrue(ok)
        self.assertEqual(message, "edge ok")
        self.assertEqual(name, "narration.mp3")
        edge.assert_called_once()

    def test_linux_uses_edge_without_touching_sapi(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(
            audio, "_is_windows", return_value=False
        ), patch.object(audio, "synthesize_windows_tts") as sapi, patch.object(
            audio, "synthesize_edge_tts", return_value=(True, "edge ok")
        ):
            ok, _, name = audio.synthesize_speech("你好", directory)
        self.assertTrue(ok)
        self.assertEqual(name, "narration.mp3")
        sapi.assert_not_called()

    def test_engine_env_can_force_edge_on_windows(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"TOUCHLEARN_TTS_ENGINE": "edge"}
        ), patch.object(audio, "_is_windows", return_value=True), patch.object(
            audio, "synthesize_windows_tts"
        ) as sapi, patch.object(audio, "synthesize_edge_tts", return_value=(True, "edge ok")):
            ok, _, name = audio.synthesize_speech("你好", directory)
        self.assertTrue(ok)
        self.assertEqual(name, "narration.mp3")
        sapi.assert_not_called()

    def test_failure_message_mentions_both_engines(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(
            audio, "_is_windows", return_value=True
        ), patch.object(
            audio, "synthesize_windows_tts", return_value=(False, "no voice pack")
        ), patch.object(audio, "synthesize_edge_tts", return_value=(False, "network blocked")):
            ok, message, name = audio.synthesize_speech("你好", directory)
        self.assertFalse(ok)
        self.assertIsNone(name)
        self.assertIn("no voice pack", message)
        self.assertIn("network blocked", message)

    def test_edge_synthesis_writes_mp3_and_reports_the_voice(self) -> None:
        class FakeCommunicate:
            def __init__(self, text: str, voice: str, rate: str = "+0%") -> None:
                self.voice = voice

            async def save(self, path: str) -> None:
                Path(path).write_bytes(b"ID3" + b"\x00" * 4096)

        fake = types.SimpleNamespace(Communicate=FakeCommunicate)
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            sys.modules, {"edge_tts": fake}
        ):
            target = Path(directory) / "narration.mp3"
            ok, message = audio.synthesize_edge_tts("你好", target, voice="zh-CN-YunxiNeural")
            self.assertTrue(ok, message)
            self.assertTrue(target.is_file())
            self.assertIn("zh-CN-YunxiNeural", message)

    def test_edge_failure_removes_partial_file(self) -> None:
        class BrokenCommunicate:
            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            async def save(self, path: str) -> None:
                Path(path).write_bytes(b"partial")
                raise RuntimeError("socket closed")

        fake = types.SimpleNamespace(Communicate=BrokenCommunicate)
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            sys.modules, {"edge_tts": fake}
        ), patch.object(audio.asyncio, "sleep", new=AsyncMock()):
            target = Path(directory) / "narration.mp3"
            ok, message = audio.synthesize_edge_tts("你好", target)
            self.assertFalse(ok)
            self.assertIn("socket closed", message)
            self.assertFalse(target.exists())
