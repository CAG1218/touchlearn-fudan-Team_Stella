"""Tests for the passwordless account store."""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from touchlearn.agent.accounts import (
    AuthError,
    AuthSettings,
    AccountStore,
    normalize_email,
    normalize_identity,
    normalize_phone,
)


class IdentityNormalizationTests(unittest.TestCase):
    def test_phone_accepts_common_prefixes(self) -> None:
        for raw in ("13800138000", "+86 138-0013-8000", "0086 138 0013 8000", "86 13800138000"):
            with self.subTest(raw=raw):
                self.assertEqual(normalize_phone(raw), "13800138000")

    def test_phone_rejects_invalid(self) -> None:
        for raw in ("12345", "12800138000", "1380013800a", ""):
            with self.subTest(raw=raw):
                with self.assertRaises(AuthError) as ctx:
                    normalize_phone(raw)
                self.assertEqual(ctx.exception.code, "invalid_phone")

    def test_email_is_lowercased_and_checked(self) -> None:
        self.assertEqual(normalize_email("  User.Name@Example.COM "), "user.name@example.com")
        for raw in ("a@b", "a@@b.com", "a..b@c.com", "x" * 250 + "@a.com"):
            with self.subTest(raw=raw):
                with self.assertRaises(AuthError) as ctx:
                    normalize_email(raw)
                self.assertEqual(ctx.exception.code, "invalid_email")

    def test_unknown_kind_is_rejected(self) -> None:
        with self.assertRaises(AuthError) as ctx:
            normalize_identity("wechat", "abc")
        self.assertEqual(ctx.exception.code, "invalid_kind")

    def test_explicit_dev_mode_flag_wins_over_env(self) -> None:
        forced = AuthSettings.from_env(
            Path("unused.sqlite3"),
            dev_mode=True,
            env={"TOUCHLEARN_AUTH_DEV_MODE": "false"},
        )
        self.assertTrue(forced.dev_mode)
        disabled = AuthSettings.from_env(
            Path("unused.sqlite3"),
            dev_mode=None,
            env={"TOUCHLEARN_AUTH_DEV_MODE": "false"},
        )
        self.assertFalse(disabled.dev_mode)


class AccountStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "accounts.sqlite3"

    def store(self, **overrides) -> AccountStore:
        options = {"resend_after": 0}
        options.update(overrides)
        settings = AuthSettings(
            db_path=self.db_path,
            dev_mode=True,
            **options,
        )
        return AccountStore(settings)

    def request(self, store: AccountStore, kind: str, value: str, ip: str = "") -> dict:
        with contextlib.redirect_stdout(io.StringIO()):
            return store.request_code(kind, value, ip=ip)

    def login(self, store: AccountStore, kind: str, value: str, code: str) -> dict:
        with contextlib.redirect_stdout(io.StringIO()):
            return store.login(kind, value, code)

    def test_first_login_registers_and_second_login_reuses_account(self) -> None:
        store = self.store()
        first = self.request(store, "phone", "13800138000")
        self.assertTrue(first["dev_code"])
        session = self.login(store, "phone", "13800138000", first["dev_code"])
        user_id = session["user"]["id"]
        self.assertEqual(session["user"]["display_name"], "用户8000")
        self.assertEqual(len(session["user"]["identities"]), 1)
        self.assertFalse(session["user"]["can_unlink"])
        self.assertEqual(store.user_for_token(session["token"])["id"], user_id)

        second = self.request(store, "phone", "13800138000")
        again = self.login(store, "phone", "13800138000", second["dev_code"])
        self.assertEqual(again["user"]["id"], user_id)

    def test_logout_invalidates_session(self) -> None:
        store = self.store()
        code = self.request(store, "email", "a@example.com")["dev_code"]
        session = self.login(store, "email", "a@example.com", code)
        store.logout(session["token"])
        self.assertIsNone(store.user_for_token(session["token"]))

    def test_wrong_code_is_counted_and_locked(self) -> None:
        store = self.store()
        code = self.request(store, "email", "a@example.com")["dev_code"]
        wrong = "000000" if code != "000000" else "111111"
        for attempt in range(5):
            with self.subTest(attempt=attempt):
                with self.assertRaises(AuthError) as ctx:
                    self.login(store, "email", "a@example.com", wrong)
                self.assertEqual(ctx.exception.code, "code_invalid")
        with self.assertRaises(AuthError) as ctx:
            self.login(store, "email", "a@example.com", code)
        self.assertEqual(ctx.exception.code, "code_locked")

    def test_code_cannot_be_reused(self) -> None:
        store = self.store()
        code = self.request(store, "email", "a@example.com")["dev_code"]
        self.login(store, "email", "a@example.com", code)
        with self.assertRaises(AuthError) as ctx:
            self.login(store, "email", "a@example.com", code)
        self.assertEqual(ctx.exception.code, "code_missing")

    def test_expired_code_is_rejected(self) -> None:
        store = self.store(code_ttl=0)
        code = self.request(store, "email", "a@example.com")["dev_code"]
        with self.assertRaises(AuthError) as ctx:
            self.login(store, "email", "a@example.com", code)
        self.assertEqual(ctx.exception.code, "code_expired")

    def test_resend_cooldown(self) -> None:
        store = self.store(resend_after=60)
        self.request(store, "phone", "13800138000")
        with self.assertRaises(AuthError) as ctx:
            self.request(store, "phone", "13800138000")
        self.assertEqual(ctx.exception.code, "resend_cooldown")
        self.assertGreaterEqual(ctx.exception.retry_after or 0, 1)

    def test_per_identity_hourly_limit(self) -> None:
        store = self.store(max_codes_per_hour=2)
        self.request(store, "phone", "13800138000")
        self.request(store, "phone", "13800138000")
        with self.assertRaises(AuthError) as ctx:
            self.request(store, "phone", "13800138000")
        self.assertEqual(ctx.exception.code, "rate_limited")

    def test_per_ip_hourly_limit(self) -> None:
        store = self.store(max_codes_per_ip_hour=2)
        self.request(store, "email", "a@example.com", ip="1.2.3.4")
        self.request(store, "email", "b@example.com", ip="1.2.3.4")
        with self.assertRaises(AuthError) as ctx:
            self.request(store, "email", "c@example.com", ip="1.2.3.4")
        self.assertEqual(ctx.exception.code, "ip_rate_limited")

    def test_expired_session_is_invalid(self) -> None:
        store = self.store(session_ttl=0)
        code = self.request(store, "email", "a@example.com")["dev_code"]
        session = self.login(store, "email", "a@example.com", code)
        self.assertIsNone(store.user_for_token(session["token"]))

    def test_phone_and_email_can_share_one_account(self) -> None:
        store = self.store()
        code = self.request(store, "phone", "13800138000")["dev_code"]
        session = self.login(store, "phone", "13800138000", code)
        user_id = session["user"]["id"]

        # request_link_code needs a user; call it directly with the same helper.
        with contextlib.redirect_stdout(io.StringIO()):
            link = store.request_link_code(user_id, "email", "a@example.com")
        merged = store.link_identity(user_id, "email", "a@example.com", link["dev_code"])
        self.assertEqual(merged["id"], user_id)
        self.assertEqual({item["kind"] for item in merged["identities"]}, {"phone", "email"})
        self.assertTrue(merged["can_unlink"])

        email_code = self.request(store, "email", "a@example.com")["dev_code"]
        email_session = self.login(store, "email", "a@example.com", email_code)
        self.assertEqual(email_session["user"]["id"], user_id)

    def test_identity_cannot_belong_to_two_accounts(self) -> None:
        store = self.store()
        code = self.request(store, "phone", "13800138000")["dev_code"]
        first = self.login(store, "phone", "13800138000", code)

        code = self.request(store, "phone", "13900139000")["dev_code"]
        second = self.login(store, "phone", "13900139000", code)
        with contextlib.redirect_stdout(io.StringIO()):
            link = store.request_link_code(second["user"]["id"], "email", "shared@example.com")
        store.link_identity(second["user"]["id"], "email", "shared@example.com", link["dev_code"])

        other = self.login(store, "email", "shared@example.com", self.request(store, "email", "shared@example.com")["dev_code"])
        self.assertEqual(other["user"]["id"], second["user"]["id"])
        self.assertNotEqual(other["user"]["id"], first["user"]["id"])

    def test_link_rejects_identity_owned_by_another_user(self) -> None:
        store = self.store()
        code = self.request(store, "email", "a@example.com")["dev_code"]
        owner = self.login(store, "email", "a@example.com", code)

        code = self.request(store, "phone", "13800138000")["dev_code"]
        other = self.login(store, "phone", "13800138000", code)
        with self.assertRaises(AuthError) as ctx:
            with contextlib.redirect_stdout(io.StringIO()):
                store.request_link_code(other["user"]["id"], "email", "a@example.com")
        self.assertEqual(ctx.exception.code, "identity_taken")
        self.assertNotEqual(owner["user"]["id"], other["user"]["id"])

    def test_second_identity_of_same_kind_is_rejected(self) -> None:
        store = self.store()
        code = self.request(store, "email", "a@example.com")["dev_code"]
        session = self.login(store, "email", "a@example.com", code)
        with self.assertRaises(AuthError) as ctx:
            with contextlib.redirect_stdout(io.StringIO()):
                store.request_link_code(session["user"]["id"], "email", "b@example.com")
        self.assertEqual(ctx.exception.code, "kind_exists")

    def test_last_identity_cannot_be_unlinked(self) -> None:
        store = self.store()
        code = self.request(store, "phone", "13800138000")["dev_code"]
        session = self.login(store, "phone", "13800138000", code)
        identity_id = session["user"]["identities"][0]["id"]
        with self.assertRaises(AuthError) as ctx:
            store.unlink_identity(session["user"]["id"], identity_id)
        self.assertEqual(ctx.exception.code, "last_identity")

    def test_unlink_keeps_remaining_login_method(self) -> None:
        store = self.store()
        code = self.request(store, "phone", "13800138000")["dev_code"]
        session = self.login(store, "phone", "13800138000", code)
        user_id = session["user"]["id"]
        with contextlib.redirect_stdout(io.StringIO()):
            link = store.request_link_code(user_id, "email", "a@example.com")
        store.link_identity(user_id, "email", "a@example.com", link["dev_code"])
        phone_identity = next(
            item for item in store.current_user(user_id)["identities"] if item["kind"] == "phone"
        )
        remaining = store.unlink_identity(user_id, phone_identity["id"])
        self.assertEqual([item["kind"] for item in remaining["identities"]], ["email"])
        self.assertFalse(remaining["can_unlink"])

    def test_profile_name_is_trimmed_and_validated(self) -> None:
        store = self.store()
        code = self.request(store, "email", "a@example.com")["dev_code"]
        session = self.login(store, "email", "a@example.com", code)
        updated = store.update_profile(session["user"]["id"], "  触见   同学  ")
        self.assertEqual(updated["display_name"], "触见 同学")
        for name in ("", "   ", "x" * 25):
            with self.subTest(name=name):
                with self.assertRaises(AuthError) as ctx:
                    store.update_profile(session["user"]["id"], name)
                self.assertEqual(ctx.exception.code, "invalid_name")

    def test_without_channel_and_without_dev_mode_login_is_unavailable(self) -> None:
        store = AccountStore(AuthSettings(db_path=self.db_path, dev_mode=False))
        config = store.public_config()
        self.assertFalse(config["phone"])
        self.assertFalse(config["email"])
        with self.assertRaises(AuthError) as ctx:
            store.request_code("email", "a@example.com")
        self.assertEqual(ctx.exception.code, "channel_unavailable")


if __name__ == "__main__":
    unittest.main()