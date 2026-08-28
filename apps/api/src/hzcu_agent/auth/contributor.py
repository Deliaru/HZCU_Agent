from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from hzcu_agent.config import Settings
from hzcu_agent.db import Database
from hzcu_agent.models import (
    AuthSession,
    CampusUser,
    LocalContributorCredential,
    SecurityAuditEvent,
    new_id,
    utc_now,
)
from hzcu_agent.observability import request_id_context
from hzcu_agent.text_safety import clean_product_text

_PASSWORD_SCHEME = "scrypt"
_SCRYPT_N = 1 << 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_MAX_MEMORY = 64 * 1024 * 1024
_MIN_PASSWORD_LENGTH = 6
_MAX_PASSWORD_LENGTH = 256


class ContributorAuthenticationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ContributorStatus:
    contributor_id: str
    username: str
    public_name: str
    unit: str | None
    status: str
    created_at: object
    updated_at: object
    last_login_at: object | None


class LocalContributorAuthenticator:
    """Manage administrator-created answerer accounts using the same scrypt policy."""

    def __init__(self, *, settings: Settings, database: Database) -> None:
        self._settings = settings
        self._database = database
        self._attempts: dict[str, deque[float]] = defaultdict(deque)
        self._attempt_lock = asyncio.Lock()
        self._dummy_hash = _hash_password(secrets.token_urlsafe(24))

    async def authenticate(self, *, username: str, password: str, client_key: str) -> str:
        normalized = _normalize_username(username)
        _validate_password(password)
        attempt_key = hashlib.sha256(f"{normalized}\0{client_key}".encode()).hexdigest()
        await self._check_attempt(attempt_key)
        async with self._database.session_factory() as session:
            credential = await session.scalar(
                select(LocalContributorCredential).where(
                    LocalContributorCredential.username == normalized
                )
            )
            if credential is None:
                _verify_password(password, self._dummy_hash)
                raise ContributorAuthenticationError(
                    "CONTRIBUTOR_CREDENTIALS_INVALID", "贡献者账号或密码不正确。"
                )
            accepted = _verify_password(password, credential.password_hash)
            if not accepted:
                raise ContributorAuthenticationError(
                    "CONTRIBUTOR_CREDENTIALS_INVALID", "贡献者账号或密码不正确。"
                )
            if credential.status != "active":
                raise ContributorAuthenticationError("CONTRIBUTOR_DISABLED", "该贡献者账号已停用。")
            user = await session.get(CampusUser, credential.user_id)
            if user is None or user.status != "active":
                raise ContributorAuthenticationError("CONTRIBUTOR_DISABLED", "该贡献者账号已停用。")
            now = utc_now()
            credential.last_login_at = now
            credential.updated_at = now
            user.last_login_at = now
            await session.commit()
        await self._clear_attempts(attempt_key)
        return normalized

    async def create(
        self,
        *,
        username: str,
        password: str,
        public_name: str,
        unit: str | None,
        subject_hash: str,
    ) -> LocalContributorCredential:
        normalized = _normalize_username(username)
        _validate_password(password)
        clean_name = clean_product_text(public_name).strip()
        if not clean_name or len(clean_name) > 120:
            raise ContributorAuthenticationError("CONTRIBUTOR_NAME_INVALID", "公开展示名不能为空。")
        clean_unit_value = clean_product_text(unit).strip() if unit else ""
        clean_unit = clean_unit_value[:200] if clean_unit_value else None
        now = utc_now()
        async with self._database.session_factory() as session:
            exists = await session.scalar(
                select(LocalContributorCredential).where(
                    LocalContributorCredential.username == normalized
                )
            )
            if exists is not None:
                raise ContributorAuthenticationError(
                    "CONTRIBUTOR_USERNAME_TAKEN", "该贡献者登录名已经存在。"
                )
            user = CampusUser(
                id=new_id("usr"),
                identity_provider="local_contributor",
                subject_hash=subject_hash,
                subject_hint=f"贡献者 ····{normalized[-4:]}",
                access_scopes=["public"],
                role="contributor",
                status="active",
                created_at=now,
                last_login_at=now,
            )
            credential = LocalContributorCredential(
                id=new_id("contrib"),
                user_id=user.id,
                username=normalized,
                public_name=clean_name,
                unit=clean_unit,
                password_hash=_hash_password(password),
                status="active",
                created_at=now,
                updated_at=now,
            )
            session.add(user)
            await session.flush()
            session.add(credential)
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise ContributorAuthenticationError(
                    "CONTRIBUTOR_USERNAME_TAKEN", "该贡献者登录名已经存在。"
                ) from exc
            return credential

    async def update(
        self,
        credential_id: str,
        *,
        public_name: str | None = None,
        unit: str | None = None,
        status: str | None = None,
        password: str | None = None,
    ) -> LocalContributorCredential:
        async with self._database.session_factory() as session:
            credential = await session.get(LocalContributorCredential, credential_id)
            if credential is None:
                raise ContributorAuthenticationError("CONTRIBUTOR_NOT_FOUND", "贡献者账号不存在。")
            if public_name is not None:
                clean_name = clean_product_text(public_name).strip()
                if not clean_name:
                    raise ContributorAuthenticationError(
                        "CONTRIBUTOR_NAME_INVALID", "公开展示名不能为空。"
                    )
                credential.public_name = clean_name[:120]
            if unit is not None:
                clean_unit = clean_product_text(unit).strip()
                credential.unit = clean_unit[:200] or None
            if status is not None:
                credential.status = status
                user = await session.get(CampusUser, credential.user_id)
                if user is not None:
                    user.status = "active" if status == "active" else "disabled"
                    if status == "disabled":
                        active_sessions = list(
                            (
                                await session.scalars(
                                    select(AuthSession).where(
                                        AuthSession.user_id == user.id,
                                        AuthSession.revoked_at.is_(None),
                                    )
                                )
                            ).all()
                        )
                        now = utc_now()
                        for auth_session in active_sessions:
                            auth_session.revoked_at = now
            if password is not None:
                _validate_password(password)
                credential.password_hash = _hash_password(password)
            credential.updated_at = utc_now()
            await session.commit()
            return credential

    async def _check_attempt(self, key: str) -> None:
        now = time.monotonic()
        async with self._attempt_lock:
            attempts = self._attempts[key]
            while attempts and now - attempts[0] >= 600:
                attempts.popleft()
            if len(attempts) >= 5:
                raise ContributorAuthenticationError(
                    "CONTRIBUTOR_RATE_LIMITED", "登录尝试过于频繁，请十分钟后再试。"
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
        raise ContributorAuthenticationError(
            "CONTRIBUTOR_USERNAME_INVALID", "贡献者账号格式不正确。"
        )
    return normalized


def _validate_password(password: str) -> None:
    if not _MIN_PASSWORD_LENGTH <= len(password) <= _MAX_PASSWORD_LENGTH:
        raise ContributorAuthenticationError(
            "CONTRIBUTOR_PASSWORD_INVALID", "贡献者密码长度应为 6 至 256 个字符。"
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


async def audit_contributor_change(
    database: Database,
    *,
    actor_user_id: str,
    event_type: str,
    contributor_id: str,
) -> None:
    async with database.session_factory() as session:
        session.add(
            SecurityAuditEvent(
                id=new_id("audit"),
                actor_user_id=actor_user_id,
                event_type=event_type,
                outcome="succeeded",
                request_id=request_id_context.get(),
                event_metadata={"contributor_id": contributor_id},
                occurred_at=utc_now(),
            )
        )
        await session.commit()
