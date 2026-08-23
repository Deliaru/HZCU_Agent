import asyncio
import dataclasses
import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, Field, ValidationError

from hzcu_agent.config import Settings
from hzcu_agent.ingestion.catalog import SourceRegistry
from hzcu_agent.models import utc_now


class CampusAccessError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class _SidecarSessionResponse(BaseModel):
    session_handle: str = Field(min_length=32, max_length=512)
    subject: str = Field(min_length=1, max_length=160)
    capability: str
    expires_at: datetime


class _SidecarImage(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    data_url: str = Field(
        min_length=32,
        max_length=4_500_000,
        pattern=r"^data:image/png;base64,",
    )


class _SidecarEvidence(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    publisher: str = Field(min_length=1, max_length=200)
    canonical_url: str = Field(min_length=1, max_length=4096)
    excerpt: str = Field(min_length=1, max_length=4000)
    source_id: str = Field(min_length=3, max_length=120)
    published_at: datetime | None = None
    observed_at: datetime
    images: list[_SidecarImage] = Field(default_factory=list, max_length=3)


class _SidecarSearchTrace(BaseModel):
    attempted_source_ids: list[str] = Field(default_factory=list, max_length=100)
    waves: int = Field(default=0, ge=0)
    exhausted: bool = False
    candidate_count: int = Field(default=0, ge=0)
    hydrated_candidate_count: int = Field(default=0, ge=0)
    per_query_result_counts: dict[str, int] = Field(default_factory=dict)


class _SidecarQueryResponse(BaseModel):
    capability: str
    evidence: list[_SidecarEvidence] = Field(max_length=20)
    search_trace: _SidecarSearchTrace | None = None


@dataclass(frozen=True)
class PreparedCampusAccess:
    subject: str
    session_handle: str
    expires_at: datetime


@dataclass(frozen=True)
class CampusAccessStatus:
    mode: str
    credential_handoff_available: bool
    expires_at: datetime | None = None


@dataclass(frozen=True)
class CampusNoticeEvidence:
    title: str
    publisher: str
    canonical_url: str
    excerpt: str
    source_id: str
    published_at: datetime | None
    observed_at: datetime
    images: tuple["CampusNoticeImage", ...] = ()


@dataclass(frozen=True)
class CampusNoticeImage:
    title: str
    data_url: str


@dataclass(frozen=True)
class CampusNoticeSearchOutcome:
    evidence: tuple[CampusNoticeEvidence, ...]
    attempted_source_ids: tuple[str, ...]
    waves: int
    exhausted: bool
    candidate_count: int
    hydrated_candidate_count: int
    per_query_result_counts: dict[str, int] = dataclasses.field(default_factory=dict)

    def __iter__(self):
        return iter(self.evidence)

    def __getitem__(self, index: int) -> CampusNoticeEvidence:
        return self.evidence[index]

    def __len__(self) -> int:
        return len(self.evidence)


@dataclass
class _BoundLease:
    session_handle: str
    expires_at: datetime


class CampusAccessBroker:
    """Select direct campus routing or an opaque, read-only VPN sidecar lease.

    Passwords are forwarded once to the approved sidecar and are not retained.
    The central API stores only the sidecar's opaque capability handle in memory.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        registry: SourceRegistry,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._registry = registry
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(settings.vpn_sidecar_timeout_seconds, connect=8.0),
            follow_redirects=False,
            trust_env=False,
            headers={"User-Agent": "HZCU-Campus-Agent/0.4 (read-only campus access broker)"},
        )
        self._leases: dict[str, _BoundLease] = {}
        self._lease_lock = asyncio.Lock()
        self._direct_probe_lock = asyncio.Lock()
        self._direct_probe_at = 0.0
        self._direct_probe_result = False
        self._attempts: dict[str, deque[float]] = defaultdict(deque)
        self._attempt_lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return self._settings.campus_query_route != "disabled"

    @property
    def credential_handoff_available(self) -> bool:
        return self._settings.credential_vpn_enabled

    async def close(self) -> None:
        async with self._lease_lock:
            leases = list(self._leases.values())
            self._leases.clear()
        await asyncio.gather(
            *(self._delete_sidecar_session(lease.session_handle) for lease in leases),
            return_exceptions=True,
        )
        if self._owns_client:
            await self._client.aclose()

    async def check_credential_attempt(self, username: str, client_key: str) -> None:
        key = self._attempt_key(username, client_key)
        now = time.monotonic()
        async with self._attempt_lock:
            attempts = self._attempts[key]
            while attempts and now - attempts[0] >= 600:
                attempts.popleft()
            if len(attempts) >= 5:
                raise CampusAccessError(
                    "CREDENTIAL_RATE_LIMITED",
                    "登录尝试过于频繁，请十分钟后再试。",
                    retryable=True,
                )
            attempts.append(now)

    async def clear_credential_attempts(self, username: str, client_key: str) -> None:
        key = self._attempt_key(username, client_key)
        async with self._attempt_lock:
            self._attempts.pop(key, None)

    async def prepare_vpn_access(
        self,
        *,
        username: str,
        password: str,
    ) -> PreparedCampusAccess:
        if not self._settings.credential_vpn_enabled:
            raise CampusAccessError(
                "VPN_CREDENTIAL_HANDOFF_DISABLED",
                "当前部署没有启用校外 VPN 凭据通道。",
            )
        endpoint = f"{self._settings.vpn_sidecar_base_url}/v1/sessions"
        try:
            response = await self._client.post(
                endpoint,
                headers=self._sidecar_headers(),
                json={
                    "username": username,
                    "password": password,
                    "capability": "campus_notice.read",
                    "ttl_minutes": self._settings.vpn_session_minutes,
                },
            )
        except httpx.TransportError as exc:
            raise CampusAccessError(
                "VPN_SIDECAR_UNAVAILABLE",
                "校外只读查询通道暂时不可用。",
                retryable=True,
            ) from exc
        if response.status_code in {401, 403}:
            raise CampusAccessError(
                "CAMPUS_CREDENTIALS_REJECTED",
                "统一身份认证未通过，请检查账号或密码。",
            )
        if response.status_code >= 400:
            raise CampusAccessError(
                "VPN_SESSION_FAILED",
                "校外只读查询会话建立失败，请稍后重试。",
                retryable=response.status_code >= 500,
            )
        try:
            payload = _SidecarSessionResponse.model_validate(response.json())
        except (ValueError, ValidationError) as exc:
            raise CampusAccessError(
                "VPN_SIDECAR_RESPONSE_INVALID",
                "校外只读查询通道返回了异常结果。",
            ) from exc
        if payload.capability != "campus_notice.read":
            raise CampusAccessError(
                "VPN_CAPABILITY_INVALID",
                "校外通道没有授予通知只读能力。",
            )
        expires_at = _aware_utc(payload.expires_at)
        if expires_at <= utc_now():
            raise CampusAccessError(
                "VPN_SESSION_EXPIRED",
                "校外只读查询会话已经失效。",
            )
        return PreparedCampusAccess(
            subject=payload.subject,
            session_handle=payload.session_handle,
            expires_at=expires_at,
        )

    async def bind(self, user_id: str, prepared: PreparedCampusAccess) -> None:
        old_lease: _BoundLease | None = None
        async with self._lease_lock:
            old_lease = self._leases.get(user_id)
            self._leases[user_id] = _BoundLease(
                session_handle=prepared.session_handle,
                expires_at=prepared.expires_at,
            )
        if old_lease is not None:
            await self._delete_sidecar_session(old_lease.session_handle)

    async def abort(self, prepared: PreparedCampusAccess) -> None:
        await self._delete_sidecar_session(prepared.session_handle)

    async def release(self, user_id: str | None) -> None:
        if user_id is None:
            return
        async with self._lease_lock:
            lease = self._leases.pop(user_id, None)
        if lease is not None:
            await self._delete_sidecar_session(lease.session_handle)

    async def status(self, user_id: str | None) -> CampusAccessStatus:
        direct = await self._direct_available()
        if direct:
            return CampusAccessStatus(
                mode="direct",
                credential_handoff_available=self.credential_handoff_available,
            )
        lease = await self._active_lease(user_id)
        if lease is not None:
            return CampusAccessStatus(
                mode="vpn",
                credential_handoff_available=self.credential_handoff_available,
                expires_at=lease.expires_at,
            )
        return CampusAccessStatus(
            mode="unavailable",
            credential_handoff_available=self.credential_handoff_available,
        )

    async def query_vpn(
        self,
        *,
        user_id: str,
        queries: list[str],
        limit: int,
    ) -> CampusNoticeSearchOutcome:
        batch = [item for item in dict.fromkeys(queries) if item][:4]
        if not batch:
            raise CampusAccessError(
                "VPN_QUERY_EMPTY",
                "校外通知查询不能为空。",
            )
        lease = await self._active_lease(user_id)
        if lease is None:
            raise CampusAccessError(
                "VPN_SESSION_REQUIRED",
                "请先建立校外只读查询会话。",
            )
        endpoint = f"{self._settings.vpn_sidecar_base_url}/v1/notices/search"
        try:
            response = await self._client.post(
                endpoint,
                headers=self._sidecar_headers(),
                json={
                    "session_handle": lease.session_handle,
                    # Send both shapes: a sidecar that predates batching ignores
                    # "queries" and still serves the first, highest-priority query.
                    "query": batch[0],
                    "queries": batch,
                    "limit": limit,
                    "capability": "campus_notice.read",
                },
            )
        except httpx.TransportError as exc:
            raise CampusAccessError(
                "VPN_QUERY_UNAVAILABLE",
                "校外通知查询暂时不可用。",
                retryable=True,
            ) from exc
        if response.status_code in {401, 403, 410}:
            await self.release(user_id)
            raise CampusAccessError(
                "VPN_SESSION_EXPIRED",
                "校外只读查询会话已经失效，请重新认证。",
            )
        if response.status_code >= 400:
            raise CampusAccessError(
                "VPN_QUERY_FAILED",
                "校外通知查询失败，请稍后重试。",
                retryable=response.status_code >= 500,
            )
        try:
            payload = _SidecarQueryResponse.model_validate(response.json())
        except (ValueError, ValidationError) as exc:
            raise CampusAccessError(
                "VPN_SIDECAR_RESPONSE_INVALID",
                "校外只读查询通道返回了异常结果。",
            ) from exc
        if payload.capability != "campus_notice.read":
            raise CampusAccessError(
                "VPN_CAPABILITY_INVALID",
                "校外通道返回了越界能力。",
            )

        accepted: list[CampusNoticeEvidence] = []
        batch_limit = min(limit * len(batch), 16)
        for item in payload.evidence[:batch_limit]:
            source = self._registry.get(item.source_id)
            if (
                source is None
                or source.visibility not in {"public", "campus"}
                or not self._registry.accepts_detail_url(
                    item.source_id,
                    item.canonical_url,
                )
            ):
                continue
            accepted.append(
                CampusNoticeEvidence(
                    title=item.title,
                    publisher=source.name,
                    canonical_url=item.canonical_url,
                    excerpt=item.excerpt,
                    source_id=item.source_id,
                    published_at=(_aware_utc(item.published_at) if item.published_at else None),
                    observed_at=utc_now(),
                    images=tuple(
                        CampusNoticeImage(
                            title=image.title,
                            data_url=image.data_url,
                        )
                        for image in item.images
                    ),
                )
            )
        trace = payload.search_trace or _SidecarSearchTrace()
        return CampusNoticeSearchOutcome(
            evidence=tuple(accepted),
            attempted_source_ids=tuple(trace.attempted_source_ids),
            waves=trace.waves,
            exhausted=trace.exhausted,
            candidate_count=trace.candidate_count,
            hydrated_candidate_count=trace.hydrated_candidate_count,
            per_query_result_counts={
                query: trace.per_query_result_counts.get(query, 0) for query in batch
            },
        )

    async def _direct_available(self) -> bool:
        if self._settings.campus_query_route not in {"direct", "auto"}:
            return False
        now = time.monotonic()
        if now - self._direct_probe_at < self._settings.campus_direct_probe_seconds:
            return self._direct_probe_result
        async with self._direct_probe_lock:
            now = time.monotonic()
            if now - self._direct_probe_at < self._settings.campus_direct_probe_seconds:
                return self._direct_probe_result
            try:
                response = await self._client.get(
                    self._settings.campus_direct_probe_url,
                    headers={"Range": "bytes=0-1023"},
                )
                available = response.status_code < 500
            except httpx.TransportError:
                available = False
            self._direct_probe_at = time.monotonic()
            self._direct_probe_result = available
            return available

    async def _active_lease(self, user_id: str | None) -> _BoundLease | None:
        if user_id is None:
            return None
        async with self._lease_lock:
            lease = self._leases.get(user_id)
            if lease is None:
                return None
            if lease.expires_at <= utc_now():
                self._leases.pop(user_id, None)
                expired_handle = lease.session_handle
            else:
                return lease
        await self._delete_sidecar_session(expired_handle)
        return None

    async def _delete_sidecar_session(self, handle: str) -> None:
        if not self._settings.credential_vpn_enabled:
            return
        try:
            await self._client.request(
                "DELETE",
                f"{self._settings.vpn_sidecar_base_url}/v1/sessions",
                headers=self._sidecar_headers(),
                json={"session_handle": handle},
            )
        except httpx.TransportError:
            return

    def _sidecar_headers(self) -> dict[str, str]:
        token = self._settings.vpn_sidecar_api_token
        if token is None:
            raise CampusAccessError(
                "VPN_SIDECAR_NOT_CONFIGURED",
                "校外只读查询通道尚未配置。",
            )
        return {
            "Authorization": f"Bearer {token.get_secret_value()}",
            "Accept": "application/json",
        }

    def _attempt_key(self, username: str, client_key: str) -> str:
        secret = self._settings.auth_session_secret
        key = secret.get_secret_value().encode() if secret is not None else b"disabled"
        return hmac.new(
            key,
            f"{username.casefold()}:{client_key}".encode(),
            hashlib.sha256,
        ).hexdigest()


def new_login_challenge() -> str:
    return secrets.token_urlsafe(32)


def valid_login_challenge(
    *,
    submitted: str,
    cookie: str | None,
) -> bool:
    return (
        bool(cookie)
        and 16 <= len(submitted) <= 256
        and len(cookie) <= 256
        and secrets.compare_digest(submitted, cookie)
    )


def same_web_origin(origin: str | None, web_app_url: str) -> bool:
    if not origin:
        return False
    supplied = urlsplit(origin)
    configured = urlsplit(web_app_url)
    return (
        supplied.scheme == configured.scheme
        and supplied.netloc == configured.netloc
        and supplied.path in {"", "/"}
    )


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
