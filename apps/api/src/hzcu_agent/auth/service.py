import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree

import httpx
from sqlalchemy import select

from hzcu_agent.config import Settings
from hzcu_agent.db import Database
from hzcu_agent.models import (
    AuthSession,
    CampusUser,
    SecurityAuditEvent,
    new_id,
    utc_now,
)
from hzcu_agent.observability import request_id_context

KNOWN_VISIBILITY_SCOPES = frozenset({"public", "campus", "restricted"})
CAS_TICKET_MAX_LENGTH = 1024
CAS_RESPONSE_MAX_BYTES = 128 * 1024


class CasAuthenticationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class RequestPrincipal:
    user_id: str | None
    subject_hint: str | None
    visibility_scopes: frozenset[str]
    identity_provider: str | None = None
    session_id: str | None = None
    csrf_hash: str | None = None
    product_subject_id: str | None = None
    visitor_subject_id: str | None = None
    role: str = "visitor"
    csrf_required: bool = False
    # A visitor subject can exist solely because the browser first opened the
    # app.  Keep this separate from ``visitor_subject_id`` so the UI only asks
    # about merging when there is actual user-created data to move.
    visitor_data_available: bool = False

    @property
    def authenticated(self) -> bool:
        return self.user_id is not None and self.session_id is not None

    @classmethod
    def anonymous(cls) -> "RequestPrincipal":
        return cls(
            user_id=None,
            subject_hint=None,
            visibility_scopes=frozenset({"public"}),
        )


@dataclass(frozen=True)
class LoginStart:
    redirect_url: str
    state: str
    return_to: str
    service_url: str


@dataclass(frozen=True)
class EstablishedSession:
    principal: RequestPrincipal
    session_token: str
    csrf_token: str
    expires_at: datetime
    return_to: str


class AuthService:
    """Validate CAS tickets and issue opaque, revocable application sessions.

    The browser submits credentials only to the campus CAS page. The application
    receives a single-use service ticket and never receives a password or TGC.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        database: Database,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._database = database
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=8.0),
            follow_redirects=False,
            trust_env=False,
            headers={
                "User-Agent": "HZCU-Campus-Agent/0.3 (CAS service-ticket validator)",
                "Accept": "application/xml,text/xml,text/plain",
            },
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def start_login(self, return_to: str | None) -> LoginStart:
        if not self._settings.cas_is_enabled:
            raise CasAuthenticationError("CAS_DISABLED", "校园统一身份认证尚未启用。")
        if not self._settings.cas_service_registered:
            raise CasAuthenticationError(
                "CAS_SERVICE_REGISTRATION_PENDING",
                "应用回调地址尚未在学校 CA 登记。",
            )
        safe_return = self.normalize_return_to(return_to)
        state = secrets.token_urlsafe(32)
        service_url = self._service_url(state, safe_return)
        redirect_url = f"{self._settings.cas_browser_base_url}/login?" + urlencode(
            {"service": service_url}
        )
        return LoginStart(
            redirect_url=redirect_url,
            state=state,
            return_to=safe_return,
            service_url=service_url,
        )

    async def finish_login(
        self,
        *,
        state: str,
        state_cookie: str | None,
        ticket: str,
        return_to: str | None,
    ) -> EstablishedSession:
        safe_return = self.normalize_return_to(return_to)
        if (
            not state_cookie
            or not state
            or len(state) > 256
            or not secrets.compare_digest(state, state_cookie)
        ):
            await self._audit(
                event_type="cas.login",
                outcome="denied",
                metadata={"reason": "state_mismatch"},
            )
            raise CasAuthenticationError("CAS_STATE_INVALID", "登录状态已失效，请重新登录。")
        if (
            not ticket.startswith("ST-")
            or len(ticket) > CAS_TICKET_MAX_LENGTH
            or any(character.isspace() for character in ticket)
        ):
            await self._audit(
                event_type="cas.login",
                outcome="denied",
                metadata={"reason": "ticket_shape_invalid"},
            )
            raise CasAuthenticationError("CAS_TICKET_INVALID", "CA 返回的登录票据无效。")

        service_url = self._service_url(state, safe_return)
        subject = await self._validate_ticket(ticket=ticket, service_url=service_url)
        established = await self.establish_verified_subject(
            subject=subject,
            return_to=safe_return,
            channel="cas_redirect",
        )
        subject = ""
        ticket = ""
        return established

    async def establish_verified_subject(
        self,
        *,
        subject: str,
        return_to: str | None = None,
        channel: str,
    ) -> EstablishedSession:
        validated_subject = _valid_subject(subject)
        if validated_subject is None:
            raise CasAuthenticationError(
                "CAS_SUBJECT_INVALID",
                "学校认证通道返回了异常的校园身份。",
            )
        return await self._establish_subject(
            subject=validated_subject,
            return_to=return_to,
            identity_provider="hzcu_cas",
            role=("admin" if self._settings.is_admin_cas_subject(validated_subject) else "student"),
            event_type="cas.login",
            channel=channel,
            capability="campus_notice.read",
        )

    async def establish_local_admin_subject(
        self,
        *,
        subject: str,
        return_to: str | None = None,
    ) -> EstablishedSession:
        validated_subject = _valid_subject(subject)
        if validated_subject is None:
            raise CasAuthenticationError(
                "LOCAL_ADMIN_SUBJECT_INVALID",
                "后台管理员账号格式不正确。",
            )
        return await self._establish_subject(
            subject=validated_subject,
            return_to=return_to,
            identity_provider="local_admin",
            role="admin",
            event_type="local_admin.login",
            channel="password",
            capability="server_admin",
        )

    async def establish_local_contributor_subject(
        self,
        *,
        subject: str,
        return_to: str | None = None,
    ) -> EstablishedSession:
        validated_subject = _valid_subject(subject)
        if validated_subject is None:
            raise CasAuthenticationError(
                "CONTRIBUTOR_SUBJECT_INVALID",
                "贡献者账号格式不正确。",
            )
        return await self._establish_subject(
            subject=validated_subject,
            return_to=return_to,
            identity_provider="local_contributor",
            role="contributor",
            event_type="contributor.login",
            channel="password",
            capability="community.answer",
        )

    def subject_hash_for(self, subject: str, *, identity_provider: str = "hzcu_cas") -> str:
        validated_subject = _valid_subject(subject)
        if validated_subject is None:
            raise CasAuthenticationError("SUBJECT_INVALID", "身份标识格式不正确。")
        return self._subject_hash(validated_subject, identity_provider=identity_provider)

    async def _establish_subject(
        self,
        *,
        subject: str,
        return_to: str | None,
        identity_provider: str,
        role: str,
        event_type: str,
        channel: str,
        capability: str,
    ) -> EstablishedSession:
        safe_return = self.normalize_return_to(return_to)
        subject_hash = self._subject_hash(subject, identity_provider=identity_provider)
        subject_hint = _subject_hint(subject)
        now = utc_now()
        expires_at = now + timedelta(hours=self._settings.auth_session_hours)
        session_token = secrets.token_urlsafe(48)
        csrf_token = secrets.token_urlsafe(32)

        async with self._database.session_factory() as session:
            user = await session.scalar(
                select(CampusUser).where(
                    CampusUser.identity_provider == identity_provider,
                    CampusUser.subject_hash == subject_hash,
                )
            )
            access_scopes = (
                ["public"]
                if identity_provider == "local_contributor"
                else ["public", "campus"]
            )
            if user is None:
                user = CampusUser(
                    id=new_id("usr"),
                    identity_provider=identity_provider,
                    subject_hash=subject_hash,
                    subject_hint=subject_hint,
                    access_scopes=access_scopes,
                    role=role,
                    status="active",
                    created_at=now,
                    last_login_at=now,
                )
                session.add(user)
            else:
                if user.status != "active":
                    raise CasAuthenticationError(
                        "ACCOUNT_DISABLED",
                        "该校园身份当前不能使用本服务。",
                    )
                user.subject_hint = subject_hint
                user.role = role
                # Contributor sessions must never inherit campus-only visibility
                # from a previously created user row.  Their account is limited
                # to public reading and community answers by design.
                if identity_provider == "local_contributor":
                    user.access_scopes = access_scopes
                user.last_login_at = now

            # These mappers intentionally do not expose ORM relationships.
            # Flush the parent explicitly so SQLite FK enforcement cannot
            # schedule the session/audit inserts ahead of a newly created user.
            await session.flush()
            session_record = AuthSession(
                id=new_id("sess"),
                user_id=user.id,
                token_hash=_token_hash(session_token),
                csrf_hash=_token_hash(csrf_token),
                created_at=now,
                last_seen_at=now,
                expires_at=expires_at,
            )
            session.add(session_record)
            session.add(
                SecurityAuditEvent(
                    id=new_id("audit"),
                    actor_user_id=user.id,
                    event_type=event_type,
                    outcome="succeeded",
                    request_id=request_id_context.get(),
                    event_metadata={
                        "provider": identity_provider,
                        "channel": channel[:40],
                        "capability": capability,
                    },
                    occurred_at=now,
                )
            )
            await session.commit()
            scopes = _normalized_scopes(user.access_scopes)
            principal = RequestPrincipal(
                user_id=user.id,
                subject_hint=user.subject_hint,
                visibility_scopes=scopes,
                identity_provider=user.identity_provider,
                session_id=session_record.id,
                csrf_hash=session_record.csrf_hash,
                role=user.role,
                csrf_required=True,
            )

        return EstablishedSession(
            principal=principal,
            session_token=session_token,
            csrf_token=csrf_token,
            expires_at=expires_at,
            return_to=safe_return,
        )

    async def resolve_principal(self, session_token: str | None) -> RequestPrincipal:
        if not session_token or len(session_token) > 512:
            return RequestPrincipal.anonymous()
        digest = _token_hash(session_token)
        now = utc_now()
        async with self._database.session_factory() as session:
            row = (
                await session.execute(
                    select(AuthSession, CampusUser)
                    .join(CampusUser, AuthSession.user_id == CampusUser.id)
                    .where(AuthSession.token_hash == digest)
                )
            ).one_or_none()
            if row is None:
                return RequestPrincipal.anonymous()
            auth_session, user = row
            expires_at = _aware_utc(auth_session.expires_at)
            if auth_session.revoked_at is not None or expires_at <= now or user.status != "active":
                if auth_session.revoked_at is None:
                    auth_session.revoked_at = now
                    await session.commit()
                return RequestPrincipal.anonymous()
            last_seen_at = _aware_utc(auth_session.last_seen_at)
            if now - last_seen_at >= timedelta(minutes=5):
                auth_session.last_seen_at = now
                await session.commit()
            return RequestPrincipal(
                user_id=user.id,
                subject_hint=user.subject_hint,
                visibility_scopes=_normalized_scopes(user.access_scopes),
                identity_provider=user.identity_provider,
                session_id=auth_session.id,
                csrf_hash=auth_session.csrf_hash,
                role=user.role,
                csrf_required=True,
            )

    async def logout(
        self,
        *,
        principal: RequestPrincipal,
        csrf_header: str | None,
        csrf_cookie: str | None,
    ) -> None:
        self.require_csrf(principal, csrf_header=csrf_header, csrf_cookie=csrf_cookie)
        if principal.session_id is None:
            return
        now = utc_now()
        async with self._database.session_factory() as session:
            auth_session = await session.get(AuthSession, principal.session_id)
            if auth_session is not None and auth_session.revoked_at is None:
                auth_session.revoked_at = now
            session.add(
                SecurityAuditEvent(
                    id=new_id("audit"),
                    actor_user_id=principal.user_id,
                    event_type="session.logout",
                    outcome="succeeded",
                    request_id=request_id_context.get(),
                    event_metadata={},
                    occurred_at=now,
                )
            )
            await session.commit()

    def require_csrf(
        self,
        principal: RequestPrincipal,
        *,
        csrf_header: str | None,
        csrf_cookie: str | None,
    ) -> None:
        if not principal.csrf_required:
            return
        if (
            not principal.csrf_hash
            or not csrf_header
            or not csrf_cookie
            or len(csrf_header) > 512
            or not secrets.compare_digest(csrf_header, csrf_cookie)
            or not secrets.compare_digest(_token_hash(csrf_header), principal.csrf_hash)
        ):
            raise CasAuthenticationError(
                "CSRF_VALIDATION_FAILED",
                "请求安全校验失败，请刷新页面后重试。",
            )

    def normalize_return_to(self, return_to: str | None) -> str:
        candidate = (return_to or self._settings.web_app_url).strip()
        if candidate.startswith("/"):
            candidate = urljoin(f"{self._settings.web_app_url}/", candidate.lstrip("/"))
        if len(candidate) > 2048 or any(char in candidate for char in ("\r", "\n", "\x00")):
            raise CasAuthenticationError("RETURN_TARGET_INVALID", "登录返回地址无效。")
        parsed = urlsplit(candidate)
        allowed = urlsplit(self._settings.web_app_url)
        if (
            parsed.scheme != allowed.scheme
            or parsed.netloc != allowed.netloc
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise CasAuthenticationError("RETURN_TARGET_INVALID", "登录返回地址不在允许范围内。")
        return urlunsplit(
            (
                parsed.scheme,
                parsed.netloc,
                parsed.path or "/",
                parsed.query,
                parsed.fragment,
            )
        )

    def _service_url(self, state: str, return_to: str) -> str:
        callback = f"{self._settings.public_api_base_url}{self._settings.api_prefix}/auth/callback"
        return f"{callback}?{urlencode({'state': state, 'return_to': return_to})}"

    async def _validate_ticket(self, *, ticket: str, service_url: str) -> str:
        endpoint = f"{self._settings.cas_server_base_url}{self._settings.cas_validation_path}"
        try:
            response = await self._client.get(
                endpoint,
                params={"service": service_url, "ticket": ticket},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise CasAuthenticationError(
                "CAS_VALIDATION_UNAVAILABLE",
                "暂时无法向学校 CA 验证登录结果。",
            ) from exc
        if len(response.content) > CAS_RESPONSE_MAX_BYTES:
            raise CasAuthenticationError(
                "CAS_RESPONSE_INVALID",
                "学校 CA 返回了异常的验证响应。",
            )
        subject = _parse_cas_subject(response.content)
        if not subject:
            raise CasAuthenticationError(
                "CAS_AUTHENTICATION_FAILED",
                "学校 CA 未确认本次登录，请重新尝试。",
            )
        return subject

    def _subject_hash(self, subject: str, *, identity_provider: str = "hzcu_cas") -> str:
        secret = self._settings.auth_session_secret
        if secret is None:
            raise CasAuthenticationError("CAS_DISABLED", "校园统一身份认证尚未配置。")
        return hmac.new(
            secret.get_secret_value().encode("utf-8"),
            f"{identity_provider}:{subject}".encode(),
            hashlib.sha256,
        ).hexdigest()

    async def _audit(
        self,
        *,
        event_type: str,
        outcome: str,
        metadata: dict[str, str],
        actor_user_id: str | None = None,
    ) -> None:
        async with self._database.session_factory() as session:
            session.add(
                SecurityAuditEvent(
                    id=new_id("audit"),
                    actor_user_id=actor_user_id,
                    event_type=event_type,
                    outcome=outcome,
                    request_id=request_id_context.get(),
                    event_metadata=metadata,
                    occurred_at=utc_now(),
                )
            )
            await session.commit()


def _parse_cas_subject(content: bytes) -> str | None:
    stripped = content.strip()
    if not stripped:
        return None
    lowered = stripped[:512].lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        return None
    if stripped.startswith(b"yes"):
        lines = stripped.decode("utf-8", errors="replace").splitlines()
        subject = lines[1].strip() if len(lines) > 1 else ""
        return _valid_subject(subject)
    try:
        root = ElementTree.fromstring(stripped)
    except ElementTree.ParseError:
        return None
    success = next(
        (element for element in root.iter() if _local_name(element.tag) == "authenticationSuccess"),
        None,
    )
    if success is None:
        return None
    user_element = next(
        (element for element in success.iter() if _local_name(element.tag) == "user"),
        None,
    )
    return _valid_subject((user_element.text or "").strip()) if user_element is not None else None


def _valid_subject(subject: str) -> str | None:
    if not 1 <= len(subject) <= 160:
        return None
    if any(character.isspace() or ord(character) < 32 for character in subject):
        return None
    return subject


def _subject_hint(subject: str) -> str:
    suffix = subject[-4:] if len(subject) >= 4 else subject
    return f"校园账号 ····{suffix}"


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _normalized_scopes(values: list[str] | None) -> frozenset[str]:
    scopes = {value for value in (values or []) if value in KNOWN_VISIBILITY_SCOPES}
    scopes.add("public")
    return frozenset(scopes)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", maxsplit=1)[-1]


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
