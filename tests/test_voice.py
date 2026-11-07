from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen

from tests import EXAMPLES  # noqa: F401  (ensures src is on sys.path)

from touchlearn.agent.accounts import AccountStore, AuthSettings
from touchlearn.agent.server import AppState, Server
from touchlearn.agent.voice import (
    MAX_UTTERANCE,
    MODE_IDS,
    PROMPTS,
    interpret,
    prompts_payload,
)


def turn(stage: str, text: str, goal: str = "", modes: list[str] | None = None) -> dict:
    return interpret({"stage": stage, "text": text, "context": {"goal": goal, "modes": modes or []}})


class VoiceDialogTests(unittest.TestCase):
    def test_greeting_accepts_yes(self) -> None:
        for said in ("开启", "需要开启", "要开", "好的"):
            result = turn("greet", said)
            self.assertEqual(result["stage"], "goal", said)
            self.assertEqual(result["intent"], "enable", said)
            self.assertTrue(result["listen"], said)

    def test_greeting_accepts_no(self) -> None:
        for said in ("不用", "不要开启", "不开启", "不需要", "关闭"):
            result = turn("greet", said)
            self.assertEqual(result["stage"], "done", said)
            self.assertEqual(result["intent"], "disable", said)
            self.assertFalse(result["listen"], said)

    def test_greeting_question_is_asked_again(self) -> None:
        result = turn("greet", "要不要开启")
        self.assertEqual(result["stage"], "greet")
        self.assertEqual(result["intent"], "unclear")
        self.assertIn("盲人语音模式", result["say"])

    def test_greeting_keeps_a_goal_given_together_with_yes(self) -> None:
        result = turn("greet", "我想做一个六轴机械臂的触觉教材")
        self.assertEqual(result["intent"], "enable")
        self.assertEqual(result["stage"], "detail")
        self.assertEqual(result["modes"], ["tactile"])
        self.assertEqual(result["goal"], "六轴机械臂的触觉教材")
        self.assertTrue(result["say"].startswith(PROMPTS["enabled_short"]))

    def test_greeting_strips_acknowledgement_before_goal(self) -> None:
        result = turn("greet", "开启，做一个六轴机械臂的触觉教材")
        self.assertEqual(result["goal"], "六轴机械臂的触觉教材")
        self.assertEqual(result["modes"], ["tactile"])

    def test_goal_without_mode_asks_mode(self) -> None:
        result = turn("goal", "太阳系")
        self.assertEqual(result["stage"], "mode")
        self.assertEqual(result["goal"], "太阳系")
        self.assertEqual(result["modes"], [])

    def test_goal_with_mode_asks_detail(self) -> None:
        result = turn("goal", "六轴机械臂的触觉板")
        self.assertEqual(result["stage"], "detail")
        self.assertEqual(result["modes"], ["tactile"])

    def test_goal_with_photo_mode_goes_to_confirmation(self) -> None:
        result = turn("goal", "用照片重建这个齿轮")
        self.assertEqual(result["stage"], "confirm")
        self.assertEqual(result["modes"], ["reconstruct"])
        self.assertIn("照片", result["say"])

    def test_empty_goal_asks_again(self) -> None:
        result = turn("goal", "   ")
        self.assertEqual(result["stage"], "goal")
        self.assertEqual(result["intent"], "unclear")
        self.assertEqual(result["say"], PROMPTS["ask_goal"])

    def test_mode_both_expands_to_two_printable_modes(self) -> None:
        result = turn("mode", "都要")
        self.assertEqual(result["stage"], "detail")
        self.assertEqual(result["modes"], ["tactile", "model3d"])

    def test_mode_merges_with_existing_choice(self) -> None:
        result = turn("mode", "三维模型", modes=["tactile"])
        self.assertEqual(result["modes"], ["tactile", "model3d"])

    def test_mode_unclear_with_existing_choice_confirms(self) -> None:
        result = turn("mode", "随便", goal="齿轮", modes=["tactile"])
        self.assertEqual(result["stage"], "confirm")
        self.assertIn("齿轮", result["say"])

    def test_mode_unclear_without_choice_asks_again(self) -> None:
        result = turn("mode", "随便", goal="齿轮")
        self.assertEqual(result["stage"], "mode")
        self.assertEqual(result["say"], PROMPTS["ask_mode_again"])

    def test_detail_text_is_appended_to_the_goal(self) -> None:
        result = turn("detail", "一米高，突出关节", goal="六轴机械臂", modes=["model3d"])
        self.assertEqual(result["stage"], "confirm")
        self.assertEqual(result["goal"], "六轴机械臂；一米高,突出关节")
        self.assertIn("六轴机械臂；一米高", result["say"])

    def test_detail_can_be_skipped(self) -> None:
        for said in ("没有了", "不用了", "就这样"):
            result = turn("detail", said, goal="齿轮", modes=["tactile"])
            self.assertEqual(result["stage"], "confirm", said)
            self.assertEqual(result["intent"], "detail_skip", said)
            self.assertEqual(result["goal"], "齿轮", said)

    def test_confirm_submits(self) -> None:
        result = turn("confirm", "开始", goal="齿轮", modes=["tactile"])
        self.assertEqual(result["stage"], "done")
        self.assertEqual(result["intent"], "confirm")
        self.assertTrue(result["ready"])
        self.assertFalse(result["listen"])
        self.assertEqual(result["say"], PROMPTS["submit"])

    def test_confirm_without_choice_defaults_to_tactile(self) -> None:
        result = turn("confirm", "确认", goal="齿轮")
        self.assertTrue(result["ready"])
        self.assertEqual(result["modes"], ["tactile"])

    def test_confirm_modify_returns_to_detail(self) -> None:
        result = turn("confirm", "修改", goal="齿轮", modes=["tactile"])
        self.assertEqual(result["stage"], "detail")
        self.assertEqual(result["say"], PROMPTS["modify"])
        self.assertEqual(result["goal"], "齿轮")

    def test_confirm_cancel_resets(self) -> None:
        result = turn("confirm", "取消", goal="齿轮", modes=["tactile"])
        self.assertEqual(result["stage"], "goal")
        self.assertEqual(result["intent"], "cancel")
        self.assertEqual(result["goal"], "")
        self.assertEqual(result["modes"], [])

    def test_confirm_free_text_adds_a_requirement(self) -> None:
        result = turn("confirm", "再加一个底座", goal="齿轮", modes=["model3d"])
        self.assertEqual(result["stage"], "confirm")
        self.assertEqual(result["goal"], "齿轮；再加一个底座")

    def test_confirm_empty_repeats_the_recap(self) -> None:
        result = turn("confirm", "", goal="齿轮", modes=["tactile"])
        self.assertEqual(result["stage"], "confirm")
        self.assertEqual(result["intent"], "unclear")
        self.assertIn("齿轮", result["say"])

    def test_repeat_works_from_every_stage(self) -> None:
        for stage in ("greet", "goal", "mode", "detail", "confirm"):
            result = turn(stage, "再说一遍", goal="齿轮", modes=["tactile"])
            self.assertEqual(result["intent"], "repeat", stage)
            self.assertTrue(result["say"].startswith(PROMPTS["repeat_prefix"]), stage)

    def test_help_works_from_every_stage(self) -> None:
        for stage in ("greet", "goal", "mode", "detail", "confirm"):
            result = turn(stage, "帮助")
            self.assertEqual(result["intent"], "help", stage)
            self.assertEqual(result["say"], PROMPTS["help"], stage)

    def test_long_sentence_is_not_mistaken_for_help(self) -> None:
        result = turn("goal", "帮助我把六轴机械臂做成可打印的触觉教材")
        self.assertNotEqual(result["intent"], "help")

    def test_stop_turns_voice_off_from_every_stage(self) -> None:
        for stage in ("greet", "goal", "mode", "detail", "confirm"):
            result = turn(stage, "关闭语音", goal="齿轮")
            self.assertEqual(result["stage"], "done", stage)
            self.assertEqual(result["intent"], "disable", stage)
            self.assertFalse(result["listen"], stage)

    def test_restart_clears_the_dialog(self) -> None:
        result = turn("confirm", "重新开始", goal="齿轮", modes=["tactile"])
        self.assertEqual(result["stage"], "goal")
        self.assertEqual(result["intent"], "restart")
        self.assertEqual(result["goal"], "")

    def test_unknown_stage_falls_back_to_greeting(self) -> None:
        result = turn("nonsense", "随便说一句")
        self.assertEqual(result["stage"], "greet")
        self.assertEqual(result["intent"], "unclear")

    def test_context_modes_are_sanitized(self) -> None:
        result = turn("confirm", "开始", goal="齿轮", modes=["bogus", "model3d", "model3d"])
        self.assertEqual(result["modes"], ["model3d"])

    def test_long_utterance_is_truncated(self) -> None:
        result = turn("goal", "机" * 1000)
        self.assertLessEqual(len(result["goal"]), MAX_UTTERANCE)
        self.assertLessEqual(len(result["say"]), MAX_UTTERANCE + 200)

    def test_full_width_and_punctuation_are_folded(self) -> None:
        result = turn("greet", "开启！")
        self.assertEqual(result["intent"], "enable")
        result = turn("mode", "３Ｄ模型")
        self.assertEqual(result["modes"], ["model3d"])

    def test_prompts_payload_shape(self) -> None:
        payload = prompts_payload()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["stages"], ["greet", "goal", "mode", "detail", "confirm"])
        self.assertEqual([mode["id"] for mode in payload["modes"]], list(MODE_IDS))
        self.assertIn("盲人语音模式", payload["prompts"]["greeting"])
        self.assertTrue(payload["commands"])


class VoiceApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        state = AppState(Path(cls.temp.name), "http://127.0.0.1:8765")
        cls.server = Server(("127.0.0.1", 0), state)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.temp.cleanup()

    def get_json(self, path: str) -> dict:
        with urlopen(f"{self.base}{path}", timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))

    def post(self, path: str, payload: object) -> dict:
        request = Request(
            f"{self.base}{path}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_prompts_endpoint(self) -> None:
        payload = self.get_json("/api/voice/prompts")
        self.assertTrue(payload["ok"])
        self.assertEqual([mode["id"] for mode in payload["modes"]], list(MODE_IDS))
        self.assertIn("confirm_now", payload["prompts"])

    def test_interpret_endpoint(self) -> None:
        payload = self.post(
            "/api/voice/interpret",
            {"stage": "greet", "text": "需要开启", "context": {"goal": "", "modes": []}},
        )
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["stage"], "goal")
        self.assertEqual(payload["intent"], "enable")

    def test_interpret_defaults_to_greeting(self) -> None:
        payload = self.post("/api/voice/interpret", {})
        self.assertEqual(payload["stage"], "greet")
        self.assertEqual(payload["intent"], "unclear")

    def test_interpret_rejects_non_string_text(self) -> None:
        request = Request(
            f"{self.base}/api/voice/interpret",
            data=json.dumps({"stage": "greet", "text": 123}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(HTTPError) as caught:
            urlopen(request, timeout=10)
        self.assertEqual(caught.exception.code, 400)

    def test_interpret_rejects_oversized_text(self) -> None:
        request = Request(
            f"{self.base}/api/voice/interpret",
            data=json.dumps({"stage": "goal", "text": "机" * (MAX_UTTERANCE * 8 + 1)}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(HTTPError) as caught:
            urlopen(request, timeout=10)
        self.assertEqual(caught.exception.code, 413)


class VoicePreLoginTests(unittest.TestCase):
    """A blind user must reach the spoken dialog before signing in."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        auth = AccountStore(
            AuthSettings(
                db_path=Path(cls.temp.name) / "accounts.sqlite3",
                dev_mode=True,
                resend_after=0,
            )
        )
        state = AppState(Path(cls.temp.name), auth=auth)
        cls.server = Server(("127.0.0.1", 0), state)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.opener = build_opener(HTTPCookieProcessor(CookieJar()))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.temp.cleanup()

    def test_prompts_are_public(self) -> None:
        with self.opener.open(f"{self.base}/api/voice/prompts", timeout=10) as response:
            self.assertEqual(response.status, 200)
            payload = json.loads(response.read().decode("utf-8"))
        self.assertTrue(payload["ok"])

    def test_interpret_is_public(self) -> None:
        request = Request(
            f"{self.base}/api/voice/interpret",
            data=json.dumps({"stage": "greet", "text": "开启"}, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.opener.open(request, timeout=10) as response:
            self.assertEqual(response.status, 200)
            payload = json.loads(response.read().decode("utf-8"))
        self.assertEqual(payload["intent"], "enable")

    def test_other_api_still_requires_login(self) -> None:
        with self.assertRaises(HTTPError) as caught:
            self.opener.open(f"{self.base}/api/providers", timeout=10)
        self.assertEqual(caught.exception.code, 401)


if __name__ == "__main__":
    unittest.main()
