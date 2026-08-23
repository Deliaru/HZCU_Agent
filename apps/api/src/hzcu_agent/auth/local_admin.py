import asyncio
import base64
import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque
from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError

from hzcu_agent.config import Settings
from hzcu_agent.db import Database
from hzcu_agent.models import LocalAdminCredential, SecurityAuditEvent, new_id, utc_now
from hzcu_agent.observability import request_id_context

_PASSWORD_SCHEME = "scrypt"
_SCRYPT_N = 1 << 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_MAX_MEMORY = 64 * 1024 * 1024
_MIN_PASSWORD_LENGTH = 6
_MAX_PASSWORD_LENGTH = 256


class LocalAdminAuthenticationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class LocalAdminStatus:
    enabled: bool
    configured: bool
    setup_available: bool


class LocalAdminAuthenticator:
    """Own and verify the single password-backed server administrator."""

    def __init__(self, *, settings: Settings, database: Database) -> None:
        self._settings = settings
        self._database = database
        self._attempts: dict[str, deque[float]] = defaultdict(deque)
        self._attempt_lock = asyncio.Lock()
        self._dummy_hash = _hash_password(secrets.token_urlsafe(24))

    async def status(self) -> LocalAdminStatus:
        configured = False
        if self._settings.local_admin_enabled:
            async with self._database.session_factory() as session:
                configured = await session.get(LocalAdminCredential, "primary") is not None
        return LocalAdminStatus(
            enabled=self._settings.local_admin_enabled,
            configured=configured,
            setup_available=(self._settings.local_admin_setup_allowed and not configured),
        )

    async def setup(self, *, username: str, password: str) -> str:
        self._require_enabled()
        if not self._settings.local_admin_setup_allowed:
            raise LocalAdminAuthenticationError(
                "LOCAL_ADMIN_SETUP_DISABLED",
                "当前运行环境不允许从网页初始化后台管理员。",
            )
        normalized = _normalize_username(username)
        _validate_password(password)
        if self._settings.has_admin_cas_subjects and not self._settings.is_admin_cas_subject(
            normalized
        ):
            raise LocalAdminAuthenticationError(
                "LOCAL_ADMIN_SUBJECT_NOT_ALLOWED",
                "该账号不是服务器中已配置的初始管理员。",
            )

        now = utc_now()
        async with self._database.session_factory() as session:
            if await session.get(LocalAdminCredential, "primary") is not None:
                raise LocalAdminAuthenticationError(
                    "LOCAL_ADMIN_ALREADY_CONFIGURED",
                    "后台管理员已经完成初始化，请直接登录。",
                )
            credential = LocalAdminCredential(
                id="primary",
                username=normalized,
                subject_hint=_subject_hint(normalized),
                password_hash=_hash_password(password),
                created_at=now,
                updated_at=now,
            )
            session.add(credential)
            session.add(
                SecurityAuditEvent(
                    id=new_id("audit"),
                    event_type="local_admin.setup",
                    outcome="succeeded",
                    request_id=request_id_context.get(),
                    event_metadata={"provider": "local_admin"},
                    occurred_at=now,
                )
            )
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise LocalAdminAuthenticationError(
                    "LOCAL_ADMIN_ALREADY_CONFIGURED",
                    "后台管理员已经完成初始化，请直接登录。",
                ) from exc
        return normalized

    async def authenticate(self, *, username: str, password: str, client_key: str) -> str:
        self._require_enabled()
        normalized = _normalize_username(username)
        _validate_password(password)
        attempt_key = hashlib.sha256(f"{normalized}\0{client_key}".encode()).hexdigest()
        await self._check_attempt(attempt_key)

        async with self._database.session_factory() as session:
            credential = await session.get(LocalAdminCredential, "primary")
            username_matches = credential is not None and hmac.compare_digest(
                credential.username,
                normalized,
            )
            if username_matches:
                accepted = _verify_password(password, credential.password_hash)
            else:
                _verify_password(password, self._dummy_hash)
                accepted = False
            if not accepted:
                raise LocalAdminAuthenticationError(
                    "LOCAL_ADMIN_CREDENTIALS_INVALID",
                    "管理员账号或密码不正确。",
                )
            credential.last_login_at = utc_now()
            credential.updated_at = credential.last_login_at
            await session.commit()

        await self._clear_attempts(attempt_key)
        return normalized

    def _require_enabled(self) -> None:
        if not self._settings.local_admin_enabled:
            raise LocalAdminAuthenticationError(
                "LOCAL_ADMIN_DISABLED",
                "本地后台管理员登录未启用。",
            )

    async def _check_attempt(self, key: str) -> None:
        now = time.monotonic()
        async with self._attempt_lock:
            attempts = self._attempts[key]
            while attempts and now - attempts[0] >= 600:
                attempts.popleft()
            if len(attempts) >= 5:
                raise LocalAdminAuthenticationError(
                    "LOCAL_ADMIN_RATE_LIMITED",
                    "登录尝试过于频繁，请十分钟后再试。",
                )
            attempts.append(now)

    async def _clear_attempts(self, key: str) -> None:
        async with self._attempt_lock:
            self._attempts.pop(key, None)


def _normalize_username(username: str) -> str:
    normalized = username.strip()
    if not 1 <= len(normalized) <= 160 or any(
        character.isspace() or ord(character) < 32 for character in normalized
    ):
        raise LocalAdminAuthenticationError(
            "LOCAL_ADMIN_USERNAME_INVALID",
            "管理员账号格式不正确。",
        )
    return normalized


def _validate_password(password: str) -> None:
    if not _MIN_PASSWORD_LENGTH <= len(password) <= _MAX_PASSWORD_LENGTH:
        raise LocalAdminAuthenticationError(
            "LOCAL_ADMIN_PASSWORD_INVALID",
            f"管理员密码长度应为 {_MIN_PASSWORD_LENGTH} 至 {_MAX_PASSWORD_LENGTH} 个字符。",
        )


def _hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        maxmem=_SCRYPT_MAX_MEMORY,
    )
    return "$".join(
        (
            _PASSWORD_SCHEME,
            str(_SCRYPT_N),
            str(_SCRYPT_R),
            str(_SCRYPT_P),
            _encode(salt),
            _encode(digest),
        )
    )


def _verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, n, r, p, salt, expected = encoded.split("$", 5)
        if scheme != _PASSWORD_SCHEME:
            return False
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=_decode(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            maxmem=_SCRYPT_MAX_MEMORY,
        )
        return hmac.compare_digest(digest, _decode(expected))
    except (ValueError, TypeError):
        return False


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _subject_hint(subject: str) -> str:
    visible = subject[-4:] if len(subject) > 4 else subject
    return f"••••{visible}"
