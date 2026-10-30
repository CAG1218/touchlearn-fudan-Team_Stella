"""Passwordless accounts for TouchLearn.

Every account is identified by one or more verified contact addresses: a
mainland China mobile number or an e-mail address.  The first successful
verification creates the account automatically, later verifications from the
same address log into it, and a signed-in user may attach the other kind of
address so that both can be used.

The implementation only uses the standard library.  Verification codes are
stored as HMAC-SHA256 digests, sessions are stored as SHA-256 digests, and all
delivery channels (SMTP, Aliyun SMS) are optional: deployments that have not
configured a channel can run in development mode, where the code is printed to
the server console and returned to the browser for testing.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import smtplib
import sqlite3
import ssl
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote
from urllib.request import urlopen

SESSION_COOKIE = "touchlearn_session"
LEGACY_ACCESS_COOKIE = "touchlearn_access"

PHONE_RE = re.compile(r"^1[3-9]\d{9}$")
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9](?:[A-Za-z0-9.\-]{0,188}[A-Za-z0-9])?\.[A-Za-z]{2,24}$")

KIND_PHONE = "phone"
KIND_EMAIL = "email"

PURPOSE_LOGIN = "login"
PURPOSE_LINK = "link"

MAX_CODE_ATTEMPTS = 5
CODE_LENGTH = 6


class AuthError(Exception):
    """An error that can be shown to the browser as-is."""

    def __init__(
        self,
        status: int,
        message: str,
        code: str = "auth_error",
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code
        self.retry_after = retry_after


def normalize_phone(raw: str) -> str:
    value = re.sub(r"[\s\-()（）]", "", str(raw or ""))
    if value.startswith("+"):
        value = value[1:]
    if value.startswith("0086"):
        value = value[4:]
    elif value.startswith("86") and len(value) == 13:
        value = value[2:]
    if not PHONE_RE.match(value):
        raise AuthError(400, "请输入 11 位中国大陆手机号。", "invalid_phone")
    return value


def normalize_email(raw: str) -> str:
    value = str(raw or "").strip().lower()
    if len(value) > 254 or not EMAIL_RE.match(value) or ".." in value:
        raise AuthError(400, "请输入有效的邮箱地址。", "invalid_email")
    return value


def normalize_identity(kind: str, value: str) -> tuple[str, str]:
    if kind == KIND_PHONE:
        return KIND_PHONE, normalize_phone(value)
    if kind == KIND_EMAIL:
        return KIND_EMAIL, normalize_email(value)
    raise AuthError(400, "登录方式只能是手机号或邮箱。", "invalid_kind")


def mask_value(kind: str, value: str) -> str:
    if kind == KIND_PHONE:
        return f"{value[:3]}****{value[-4:]}" if len(value) == 11 else value
    local, _, domain = value.partition("@")
    if len(local) <= 1:
        return f"*@{domain}"
    return f"{local[0]}***@{domain}"


def _default_name(kind: str, value: str) -> str:
    if kind == KIND_PHONE:
        return f"用户{value[-4:]}"
    local = value.split("@", 1)[0]
    cleaned = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]", "", local)[:20]
    return cleaned or "新用户"


@dataclass
class AuthSettings:
    db_path: Path
    secret: str = ""
    dev_mode: bool | None = None
    code_ttl: int = 600
    resend_after: int = 60
    session_ttl: int = 30 * 86400
    max_codes_per_hour: int = 8
    max_codes_per_ip_hour: int = 40
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_tls: str = "ssl"
    sms_provider: str = ""
    aliyun_key_id: str = ""
    aliyun_key_secret: str = ""
    aliyun_sign_name: str = ""
    aliyun_template_code: str = ""
    aliyun_region: str = "cn-hangzhou"

    @property
    def email_ready(self) -> bool:
        return bool(self.smtp_host and (self.smtp_from or self.smtp_user))

    @property
    def phone_ready(self) -> bool:
        return bool(
            self.sms_provider == "aliyun"
            and self.aliyun_key_id
            and self.aliyun_key_secret
            and self.aliyun_sign_name
            and self.aliyun_template_code
        )

    @classmethod
    def from_env(
        cls,
        db_path: Path,
        *,
        dev_mode: bool | None = None,
        env: dict[str, str] | None = None,
    ) -> "AuthSettings":
        source = os.environ if env is None else env

        def text(name: str, default: str = "") -> str:
            return str(source.get(name, default) or "").strip()

        def flag(name: str) -> bool | None:
            raw = text(name).lower()
            if raw in {"1", "true", "yes", "on"}:
                return True
            if raw in {"0", "false", "no", "off"}:
                return False
            return None

        def number(name: str, default: int) -> int:
            raw = text(name)
            try:
                value = int(raw)
            except ValueError:
                return default
            return value if value > 0 else default

        explicit = flag("TOUCHLEARN_AUTH_DEV_MODE")
        if dev_mode is None and explicit is not None:
            dev_mode = explicit

        tls = text("TOUCHLEARN_SMTP_TLS", "ssl").lower()
        if tls not in {"ssl", "starttls", "none"}:
            tls = "ssl"
        port = number("TOUCHLEARN_SMTP_PORT", 465 if tls == "ssl" else 587)

        return cls(
            db_path=db_path,
            secret=text("TOUCHLEARN_AUTH_SECRET"),
            dev_mode=dev_mode,
            code_ttl=number("TOUCHLEARN_AUTH_CODE_TTL", 600),
            resend_after=number("TOUCHLEARN_AUTH_RESEND_AFTER", 60),
            session_ttl=number("TOUCHLEARN_AUTH_SESSION_DAYS", 30) * 86400,
            max_codes_per_hour=number("TOUCHLEARN_AUTH_CODES_PER_HOUR", 8),
            max_codes_per_ip_hour=number("TOUCHLEARN_AUTH_CODES_PER_IP_HOUR", 40),
            smtp_host=text("TOUCHLEARN_SMTP_HOST"),
            smtp_port=port,
            smtp_user=text("TOUCHLEARN_SMTP_USER"),
            smtp_password=text("TOUCHLEARN_SMTP_PASSWORD"),
            smtp_from=text("TOUCHLEARN_SMTP_FROM"),
            smtp_tls=tls,
            sms_provider=text("TOUCHLEARN_SMS_PROVIDER").lower(),
            aliyun_key_id=text("TOUCHLEARN_ALIYUN_SMS_KEY_ID"),
            aliyun_key_secret=text("TOUCHLEARN_ALIYUN_SMS_KEY_SECRET"),
            aliyun_sign_name=text("TOUCHLEARN_ALIYUN_SMS_SIGN_NAME"),
            aliyun_template_code=text("TOUCHLEARN_ALIYUN_SMS_TEMPLATE_CODE"),
            aliyun_region=text("TOUCHLEARN_ALIYUN_SMS_REGION", "cn-hangzhou") or "cn-hangzhou",
        )


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS identities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('phone', 'email')),
    value TEXT NOT NULL,
    verified_at REAL NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE (kind, value)
);
CREATE INDEX IF NOT EXISTS idx_identities_user ON identities(user_id);

CREATE TABLE IF NOT EXISTS verification_codes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    value TEXT NOT NULL,
    purpose TEXT NOT NULL,
    user_id TEXT NOT NULL DEFAULT '',
    salt TEXT NOT NULL,
    code_hash TEXT NOT NULL,
    expires_at REAL NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    consumed_at REAL,
    created_at REAL NOT NULL,
    ip TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_codes_lookup
    ON verification_codes(kind, value, purpose, created_at DESC);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    last_seen_at REAL NOT NULL,
    ip TEXT NOT NULL DEFAULT '',
    user_agent TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expiry ON sessions(expires_at);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@dataclass
class _CodeRow:
    id: int
    user_id: str
    salt: str
    code_hash: str
    expires_at: float
    attempts: int
    created_at: float


class AccountStore:
    """SQLite-backed passwordless account store."""

    def __init__(self, settings: AuthSettings) -> None:
        self.settings = settings
        self.db_path = Path(settings.db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_lock = threading.Lock()
        self._secret = ""
        with self._init_lock:
            with self._db() as conn:
                conn.executescript(SCHEMA)
                try:
                    conn.execute("PRAGMA journal_mode = WAL")
                except sqlite3.OperationalError:  # pragma: no cover - exotic filesystems
                    pass
            self._secret = self._load_secret()

    # ------------------------------------------------------------------
    # Storage helpers
    # ------------------------------------------------------------------

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _load_secret(self) -> str:
        if self.settings.secret:
            return self.settings.secret
        with self._db() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = 'auth_secret'").fetchone()
            if row is not None:
                return str(row["value"])
            generated = secrets.token_hex(32)
            conn.execute(
                "INSERT OR IGNORE INTO meta (key, value) VALUES ('auth_secret', ?)",
                (generated,),
            )
            row = conn.execute("SELECT value FROM meta WHERE key = 'auth_secret'").fetchone()
            return str(row["value"]) if row is not None else generated

    def _hash_code(self, kind: str, value: str, purpose: str, salt: str, code: str) -> str:
        message = "\0".join([kind, value, purpose, salt, code]).encode("utf-8")
        return hmac.new(self._secret.encode("utf-8"), message, hashlib.sha256).hexdigest()

    @staticmethod
    def _hash_token(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def _now() -> float:
        return time.time()

    # ------------------------------------------------------------------
    # Public configuration
    # ------------------------------------------------------------------

    @property
    def dev_mode(self) -> bool:
        return bool(self.settings.dev_mode)

    def methods(self) -> dict[str, bool]:
        if self.dev_mode:
            return {KIND_PHONE: True, KIND_EMAIL: True}
        return {KIND_PHONE: self.settings.phone_ready, KIND_EMAIL: self.settings.email_ready}

    def public_config(self) -> dict[str, Any]:
        methods = self.methods()
        return {
            "phone": methods[KIND_PHONE],
            "email": methods[KIND_EMAIL],
            "dev_mode": self.dev_mode,
            "code_ttl": self.settings.code_ttl,
            "resend_after": self.settings.resend_after,
            "session_days": max(1, self.settings.session_ttl // 86400),
        }

    # ------------------------------------------------------------------
    # Verification codes
    # ------------------------------------------------------------------

    def _channel_ready(self, kind: str) -> bool:
        if self.dev_mode:
            return True
        return self.settings.phone_ready if kind == KIND_PHONE else self.settings.email_ready

    def _require_channel(self, kind: str) -> None:
        if self._channel_ready(kind):
            return
        label = "手机号" if kind == KIND_PHONE else "邮箱"
        raise AuthError(503, f"{label}登录暂未开通，请联系部署者配置验证码通道。", "channel_unavailable")

    def _check_send_limits(self, conn: sqlite3.Connection, kind: str, value: str, purpose: str, ip: str) -> None:
        now = self._now()
        hour_ago = now - 3600
        latest = conn.execute(
            "SELECT created_at FROM verification_codes "
            "WHERE kind = ? AND value = ? AND purpose = ? ORDER BY created_at DESC LIMIT 1",
            (kind, value, purpose),
        ).fetchone()
        if latest is not None:
            elapsed = now - float(latest["created_at"])
            if elapsed < self.settings.resend_after:
                raise AuthError(
                    429,
                    f"发送太频繁，请 {int(self.settings.resend_after - elapsed) + 1} 秒后再试。",
                    "resend_cooldown",
                    retry_after=int(self.settings.resend_after - elapsed) + 1,
                )
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM verification_codes "
            "WHERE kind = ? AND value = ? AND purpose = ? AND created_at > ?",
            (kind, value, purpose, hour_ago),
        ).fetchone()
        if int(count["n"]) >= self.settings.max_codes_per_hour:
            raise AuthError(429, "该手机号或邮箱获取验证码次数过多，请 1 小时后再试。", "rate_limited", retry_after=3600)
        if ip:
            ip_count = conn.execute(
                "SELECT COUNT(*) AS n FROM verification_codes WHERE ip = ? AND created_at > ?",
                (ip, hour_ago),
            ).fetchone()
            if int(ip_count["n"]) >= self.settings.max_codes_per_ip_hour:
                raise AuthError(429, "当前网络获取验证码次数过多，请 1 小时后再试。", "ip_rate_limited", retry_after=3600)

    def _issue_code(
        self,
        conn: sqlite3.Connection,
        kind: str,
        value: str,
        purpose: str,
        *,
        user_id: str = "",
        ip: str = "",
    ) -> tuple[int, str]:
        self._require_channel(kind)
        self._check_send_limits(conn, kind, value, purpose, ip)
        code = f"{secrets.randbelow(10 ** CODE_LENGTH):0{CODE_LENGTH}d}"
        salt = secrets.token_hex(8)
        now = self._now()
        cursor = conn.execute(
            "INSERT INTO verification_codes "
            "(kind, value, purpose, user_id, salt, code_hash, expires_at, created_at, ip) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                kind,
                value,
                purpose,
                user_id,
                salt,
                self._hash_code(kind, value, purpose, salt, code),
                now + self.settings.code_ttl,
                now,
                ip,
            ),
        )
        conn.execute(
            "DELETE FROM verification_codes WHERE created_at < ?",
            (now - 7 * 86400,),
        )
        return int(cursor.lastrowid or 0), code

    def _latest_code(
        self,
        conn: sqlite3.Connection,
        kind: str,
        value: str,
        purpose: str,
        *,
        user_id: str = "",
    ) -> _CodeRow | None:
        sql = (
            "SELECT id, user_id, salt, code_hash, expires_at, attempts, created_at "
            "FROM verification_codes WHERE kind = ? AND value = ? AND purpose = ? "
            "AND consumed_at IS NULL"
        )
        params: list[Any] = [kind, value, purpose]
        if user_id:
            sql += " AND user_id = ?"
            params.append(user_id)
        sql += " ORDER BY created_at DESC LIMIT 1"
        row = conn.execute(sql, params).fetchone()
        if row is None:
            return None
        return _CodeRow(
            id=int(row["id"]),
            user_id=str(row["user_id"]),
            salt=str(row["salt"]),
            code_hash=str(row["code_hash"]),
            expires_at=float(row["expires_at"]),
            attempts=int(row["attempts"]),
            created_at=float(row["created_at"]),
        )

    def _verify_code(
        self,
        conn: sqlite3.Connection,
        kind: str,
        value: str,
        purpose: str,
        code: str,
        *,
        user_id: str = "",
    ) -> None:
        code = str(code or "").strip()
        if not re.fullmatch(r"\d{6}", code):
            raise AuthError(400, "请输入 6 位数字验证码。", "invalid_code")
        row = self._latest_code(conn, kind, value, purpose, user_id=user_id)
        if row is None:
            raise AuthError(400, "请先获取验证码。", "code_missing")
        if row.expires_at < self._now():
            raise AuthError(400, "验证码已过期，请重新获取。", "code_expired")
        if row.attempts >= MAX_CODE_ATTEMPTS:
            raise AuthError(429, "验证码错误次数过多，请重新获取。", "code_locked")
        expected = self._hash_code(kind, value, purpose, row.salt, code)
        if not hmac.compare_digest(expected, row.code_hash):
            conn.execute(
                "UPDATE verification_codes SET attempts = attempts + 1 WHERE id = ?",
                (row.id,),
            )
            # Keep the attempt counter even though this call raises.
            conn.commit()
            raise AuthError(401, "验证码不正确。", "code_invalid")
        cursor = conn.execute(
            "UPDATE verification_codes SET consumed_at = ? WHERE id = ? AND consumed_at IS NULL",
            (self._now(), row.id),
        )
        if cursor.rowcount != 1:  # pragma: no cover - race between two logins
            raise AuthError(400, "验证码已使用，请重新获取。", "code_used")

    def _deliver(self, kind: str, value: str, code: str) -> None:
        if self.dev_mode:
            print(f"[auth] 开发模式验证码 {kind}={value}: {code}", flush=True)
            return
        if kind == KIND_EMAIL:
            self._send_email(value, code)
        else:
            self._send_sms(value, code)

    # ------------------------------------------------------------------
    # Delivery channels
    # ------------------------------------------------------------------

    def _send_email(self, recipient: str, code: str) -> None:
        settings = self.settings
        if not settings.email_ready:
            raise AuthError(503, "邮件通道未配置。", "channel_unavailable")
        message = EmailMessage()
        message["Subject"] = "TouchLearn 登录验证码"
        message["From"] = settings.smtp_from or settings.smtp_user
        message["To"] = recipient
        minutes = max(1, settings.code_ttl // 60)
        message.set_content(
            f"你的 TouchLearn 验证码是 {code}，{minutes} 分钟内有效。\n\n"
            "验证码只能使用一次，请勿转发给他人。如果不是你本人操作，请忽略这封邮件。"
        )
        context = ssl.create_default_context()
        try:
            if settings.smtp_tls == "ssl":
                client: smtplib.SMTP = smtplib.SMTP_SSL(
                    settings.smtp_host, settings.smtp_port, timeout=20, context=context
                )
            else:
                client = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20)
            with client:
                if settings.smtp_tls == "starttls":
                    client.ehlo()
                    client.starttls(context=context)
                    client.ehlo()
                if settings.smtp_user:
                    client.login(settings.smtp_user, settings.smtp_password)
                client.send_message(message)
        except (OSError, smtplib.SMTPException) as error:
            raise AuthError(502, f"验证码邮件发送失败：{error}", "delivery_failed") from error

    def _send_sms(self, phone: str, code: str) -> None:
        settings = self.settings
        if not settings.phone_ready:
            raise AuthError(503, "短信通道未配置。", "channel_unavailable")
        params: dict[str, str] = {
            "AccessKeyId": settings.aliyun_key_id,
            "Action": "SendSms",
            "Format": "JSON",
            "PhoneNumbers": phone,
            "RegionId": settings.aliyun_region,
            "SignName": settings.aliyun_sign_name,
            "SignatureMethod": "HMAC-SHA1",
            "SignatureNonce": uuid.uuid4().hex,
            "SignatureVersion": "1.0",
            "TemplateCode": settings.aliyun_template_code,
            "TemplateParam": json.dumps({"code": code}, ensure_ascii=False, separators=(",", ":")),
            "Timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "Version": "2017-05-25",
        }
        canonical = "&".join(
            f"{quote(str(key), safe='~')}={quote(str(value), safe='~')}"
            for key, value in sorted(params.items())
        )
        string_to_sign = f"GET&%2F&{quote(canonical, safe='~')}"
        digest = hmac.new(
            f"{settings.aliyun_key_secret}&".encode("utf-8"),
            string_to_sign.encode("utf-8"),
            hashlib.sha1,
        ).digest()
        signature = base64.b64encode(digest).decode("ascii")
        url = (
            "https://dysmsapi.aliyuncs.com/?%s&Signature=%s"
            % (canonical, quote(signature, safe="~"))
        )
        try:
            with urlopen(url, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (OSError, ValueError) as error:
            raise AuthError(502, f"短信发送失败：{error}", "delivery_failed") from error
        if str(payload.get("Code") or "") != "OK":
            raise AuthError(502, f"短信发送失败：{payload.get('Message') or payload.get('Code')}", "delivery_failed")

    # ------------------------------------------------------------------
    # Login and registration
    # ------------------------------------------------------------------

    def request_code(self, kind: str, value: str, *, ip: str = "") -> dict[str, Any]:
        """Send a login code.  An unknown address is allowed: it registers."""
        kind, value = normalize_identity(kind, value)
        with self._db() as conn:
            code_id, code = self._issue_code(conn, kind, value, PURPOSE_LOGIN, ip=ip)
        try:
            self._deliver(kind, value, code)
        except AuthError:
            with self._db() as conn:
                conn.execute("DELETE FROM verification_codes WHERE id = ?", (code_id,))
            raise
        result: dict[str, Any] = {
            "ok": True,
            "sent": True,
            "kind": kind,
            "masked": mask_value(kind, value),
            "expires_in": self.settings.code_ttl,
            "resend_after": self.settings.resend_after,
        }
        if self.dev_mode:
            result["dev_code"] = code
        return result

    def login(self, kind: str, value: str, code: str, *, ip: str = "", user_agent: str = "") -> dict[str, Any]:
        kind, value = normalize_identity(kind, value)
        with self._db() as conn:
            self._verify_code(conn, kind, value, PURPOSE_LOGIN, code)
            now = self._now()
            identity = conn.execute(
                "SELECT user_id FROM identities WHERE kind = ? AND value = ?",
                (kind, value),
            ).fetchone()
            if identity is not None:
                user_id = str(identity["user_id"])
            else:
                user_id = uuid.uuid4().hex
                display_name = _default_name(kind, value)
                conn.execute(
                    "INSERT INTO users (id, display_name, created_at, updated_at) VALUES (?, ?, ?, ?)",
                    (user_id, display_name, now, now),
                )
                inserted = conn.execute(
                    "INSERT OR IGNORE INTO identities (user_id, kind, value, verified_at, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (user_id, kind, value, now, now),
                )
                if inserted.rowcount == 0:
                    # Another request registered the same address first.
                    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
                    owner = conn.execute(
                        "SELECT user_id FROM identities WHERE kind = ? AND value = ?",
                        (kind, value),
                    ).fetchone()
                    if owner is None:  # pragma: no cover - defensive
                        raise AuthError(500, "账号创建失败，请重试。", "register_failed")
                    user_id = str(owner["user_id"])
            token, expires_at = self._create_session(conn, user_id, ip=ip, user_agent=user_agent)
            user = self._user_payload(conn, user_id)
        return {"user": user, "token": token, "expires_at": expires_at}

    def _create_session(
        self,
        conn: sqlite3.Connection,
        user_id: str,
        *,
        ip: str = "",
        user_agent: str = "",
    ) -> tuple[str, float]:
        token = secrets.token_urlsafe(32)
        now = self._now()
        expires_at = now + self.settings.session_ttl
        conn.execute(
            "INSERT INTO sessions (token_hash, user_id, created_at, expires_at, last_seen_at, ip, user_agent) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (self._hash_token(token), user_id, now, expires_at, now, ip, user_agent[:200]),
        )
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
        return token, expires_at

    def user_for_token(self, token: str) -> dict[str, Any] | None:
        if not token:
            return None
        token_hash = self._hash_token(token)
        with self._db() as conn:
            row = conn.execute(
                "SELECT user_id, last_seen_at FROM sessions WHERE token_hash = ? AND expires_at > ?",
                (token_hash, self._now()),
            ).fetchone()
            if row is None:
                return None
            if self._now() - float(row["last_seen_at"]) > 300:
                conn.execute(
                    "UPDATE sessions SET last_seen_at = ? WHERE token_hash = ?",
                    (self._now(), token_hash),
                )
            try:
                return self._user_payload(conn, str(row["user_id"]))
            except AuthError:
                return None

    def logout(self, token: str) -> None:
        if not token:
            return
        with self._db() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (self._hash_token(token),))

    def _user_payload(self, conn: sqlite3.Connection, user_id: str) -> dict[str, Any]:
        user = conn.execute(
            "SELECT id, display_name, created_at, updated_at FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        if user is None:
            raise AuthError(401, "账号不存在。", "user_missing")
        rows = conn.execute(
            "SELECT id, kind, value, verified_at FROM identities WHERE user_id = ? ORDER BY created_at ASC",
            (user_id,),
        ).fetchall()
        identities = [
            {
                "id": int(row["id"]),
                "kind": str(row["kind"]),
                "masked": mask_value(str(row["kind"]), str(row["value"])),
                "verified_at": float(row["verified_at"]),
            }
            for row in rows
        ]
        return {
            "id": str(user["id"]),
            "display_name": str(user["display_name"]),
            "created_at": float(user["created_at"]),
            "identities": identities,
            "can_unlink": len(identities) > 1,
        }

    def current_user(self, user_id: str) -> dict[str, Any]:
        with self._db() as conn:
            return self._user_payload(conn, user_id)

    # ------------------------------------------------------------------
    # Linking a second identity
    # ------------------------------------------------------------------

    def request_link_code(self, user_id: str, kind: str, value: str, *, ip: str = "") -> dict[str, Any]:
        kind, value = normalize_identity(kind, value)
        with self._db() as conn:
            user = conn.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
            if user is None:
                raise AuthError(401, "请先登录。", "login_required")
            existing = conn.execute(
                "SELECT user_id FROM identities WHERE kind = ? AND value = ?",
                (kind, value),
            ).fetchone()
            if existing is not None:
                if str(existing["user_id"]) == user_id:
                    raise AuthError(409, "该手机号或邮箱已经绑定到当前账号。", "already_linked")
                raise AuthError(409, "该手机号或邮箱已经绑定到其他账号。", "identity_taken")
            same_kind = conn.execute(
                "SELECT id FROM identities WHERE user_id = ? AND kind = ?",
                (user_id, kind),
            ).fetchone()
            if same_kind is not None:
                label = "手机号" if kind == KIND_PHONE else "邮箱"
                raise AuthError(409, f"当前账号已绑定{label}，请先解绑后再绑定新的{label}。", "kind_exists")
            code_id, code = self._issue_code(conn, kind, value, PURPOSE_LINK, user_id=user_id, ip=ip)
        try:
            self._deliver(kind, value, code)
        except AuthError:
            with self._db() as conn:
                conn.execute("DELETE FROM verification_codes WHERE id = ?", (code_id,))
            raise
        result: dict[str, Any] = {
            "ok": True,
            "sent": True,
            "kind": kind,
            "masked": mask_value(kind, value),
            "expires_in": self.settings.code_ttl,
            "resend_after": self.settings.resend_after,
        }
        if self.dev_mode:
            result["dev_code"] = code
        return result

    def link_identity(self, user_id: str, kind: str, value: str, code: str) -> dict[str, Any]:
        kind, value = normalize_identity(kind, value)
        with self._db() as conn:
            self._verify_code(conn, kind, value, PURPOSE_LINK, code, user_id=user_id)
            existing = conn.execute(
                "SELECT user_id FROM identities WHERE kind = ? AND value = ?",
                (kind, value),
            ).fetchone()
            if existing is not None:
                if str(existing["user_id"]) == user_id:
                    return self._user_payload(conn, user_id)
                raise AuthError(409, "该手机号或邮箱已经绑定到其他账号。", "identity_taken")
            same_kind = conn.execute(
                "SELECT id FROM identities WHERE user_id = ? AND kind = ?",
                (user_id, kind),
            ).fetchone()
            if same_kind is not None:
                label = "手机号" if kind == KIND_PHONE else "邮箱"
                raise AuthError(409, f"当前账号已绑定{label}，请先解绑。", "kind_exists")
            now = self._now()
            conn.execute(
                "INSERT INTO identities (user_id, kind, value, verified_at, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (user_id, kind, value, now, now),
            )
            conn.execute("UPDATE users SET updated_at = ? WHERE id = ?", (now, user_id))
            return self._user_payload(conn, user_id)

    def unlink_identity(self, user_id: str, identity_id: int) -> dict[str, Any]:
        with self._db() as conn:
            row = conn.execute(
                "SELECT id, kind FROM identities WHERE id = ? AND user_id = ?",
                (int(identity_id), user_id),
            ).fetchone()
            if row is None:
                raise AuthError(404, "绑定关系不存在。", "identity_missing")
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM identities WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if int(count["n"]) <= 1:
                raise AuthError(409, "至少保留一种登录方式，不能全部解绑。", "last_identity")
            conn.execute("DELETE FROM identities WHERE id = ?", (int(identity_id),))
            conn.execute("UPDATE users SET updated_at = ? WHERE id = ?", (self._now(), user_id))
            return self._user_payload(conn, user_id)

    def update_profile(self, user_id: str, display_name: str) -> dict[str, Any]:
        name = re.sub(r"\s+", " ", str(display_name or "")).strip()
        if not name:
            raise AuthError(400, "昵称不能为空。", "invalid_name")
        if len(name) > 24:
            raise AuthError(400, "昵称最多 24 个字符。", "invalid_name")
        with self._db() as conn:
            row = conn.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
            if row is None:
                raise AuthError(401, "请先登录。", "login_required")
            conn.execute(
                "UPDATE users SET display_name = ?, updated_at = ? WHERE id = ?",
                (name, self._now(), user_id),
            )
            return self._user_payload(conn, user_id)