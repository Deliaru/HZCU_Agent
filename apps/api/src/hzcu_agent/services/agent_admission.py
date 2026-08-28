from __future__ import annotations

import asyncio
import hashlib
import hmac
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from fastapi import Request
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from hzcu_agent.auth.service import RequestPrincipal
from hzcu_agent.db import Database
from hzcu_agent.models import (
    AgentAdmissionEvent,
    AgentTask,
    AgentUsageCounter,
    AgentVerificationEvent,
    VisitorSession,
    new_id,
    utc_now,
)
from hzcu_agent.services.agent_policy import (
    GLOBAL_SCOPE_KEY,
    AgentPolicyService,
)

TERMINAL_TASK_STATES = frozenset({"completed", "failed", "canceled"})


class AgentAdmissionError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 429,
        retry_after: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retry_after = retry_after
        self.details = details or {}


@dataclass(frozen=True, slots=True)
class AdmissionResult:
    queue_deadline_at: datetime
    window_remaining: int | None
    daily_remaining: int | None
    _lease: _AdmissionLease | None = None

    async def release(self) -> None:
        """Release the admission transaction lease after the task is committed."""

        if self._lease is not None:
            await self._lease.release()


class _AdmissionLease:
    def __init__(self, lock: asyncio.Lock, counter_lock: asyncio.Lock) -> None:
        self._lock = lock
        self._counter_lock = counter_lock
        self._released = False

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._counter_lock.release()
        self._lock.release()


class AgentAdmissionService:
    """Serialize Agent admission and keep all public quotas inside this app."""

    def __init__(self, *, database: Database, policy: AgentPolicyService) -> None:
        self._database = database
        self._policy = policy
        self._lock = asyncio.Lock()

    async def admit(
        self,
        session: AsyncSession,
        *,
        request: Request,
        principal: RequestPrincipal,
        task_id: str,
        request_kind: str,
        hold_lease: bool = False,
    ) -> AdmissionResult:
        subject_key = principal.product_subject_id
        if not subject_key:
            raise AgentAdmissionError(
                "PRODUCT_IDENTITY_UNAVAILABLE",
                "当前无法建立匿名设备身份，请刷新页面后重试。",
                status_code=503,
            )
        await self._lock.acquire()
        counter_lock = self._policy.counter_lock
        counter_acquired = False
        try:
            await counter_lock.acquire()
            counter_acquired = True
            now = utc_now()
            snapshot = self._policy.snapshot()
            is_admin = principal.authenticated and principal.role == "admin"
            is_visitor = principal.role == "visitor" and not principal.authenticated

            if snapshot.mode == "paused" and not is_admin:
                await self._record_rejection(session, "PUBLIC_AGENT_PAUSED", now)
                raise AgentAdmissionError(
                    "PUBLIC_AGENT_PAUSED",
                    "公众提问暂时暂停，请稍后再试。",
                    status_code=503,
                )

            if (
                snapshot.mode == "enforce"
                and is_visitor
                and snapshot.turnstile_enabled
                and not await self._visitor_verified(session, principal, now)
            ):
                if not snapshot.turnstile_configured:
                    await self._record_rejection(session, "TURNSTILE_NOT_CONFIGURED", now)
                    raise AgentAdmissionError(
                        "TURNSTILE_NOT_CONFIGURED",
                        "人机验证尚未完成服务器配置，请联系管理员。",
                        status_code=503,
                    )
                await self._record_rejection(session, "HUMAN_VERIFICATION_REQUIRED", now)
                raise AgentAdmissionError(
                    "HUMAN_VERIFICATION_REQUIRED",
                    "请先完成一次人机验证后继续提问。",
                    status_code=403,
                    details={"verification_required": True},
                )

            active = int(
                await session.scalar(
                    select(func.count(AgentTask.id)).where(
                        AgentTask.requested_by_subject_id == subject_key,
                        AgentTask.status == "running",
                    )
                )
                or 0
            )
            queued = int(
                await session.scalar(
                    select(func.count(AgentTask.id)).where(
                        AgentTask.requested_by_subject_id == subject_key,
                        AgentTask.status == "queued",
                    )
                )
                or 0
            )
            global_queued = int(
                await session.scalar(
                    select(func.count(AgentTask.id)).where(AgentTask.status == "queued")
                )
                or 0
            )
            bucket = self._policy.local_date(now)
            subject_counter = await self._get_counter(
                session,
                scope_key=f"subject:{subject_key}",
                bucket_date=bucket,
                now=now,
            )
            global_counter = await self._get_counter(
                session,
                scope_key=GLOBAL_SCOPE_KEY,
                bucket_date=bucket,
                now=now,
            )
            window_start = now - timedelta(seconds=snapshot.subject_window_seconds)
            rolling_count = int(
                await session.scalar(
                    select(func.count(AgentAdmissionEvent.id)).where(
                        AgentAdmissionEvent.subject_key == subject_key,
                        AgentAdmissionEvent.occurred_at > window_start,
                    )
                )
                or 0
            )
            window_remaining = max(0, snapshot.subject_window_limit - rolling_count - 1)
            daily_remaining = max(0, snapshot.subject_daily_limit - subject_counter.task_count - 1)
            reasons: list[tuple[str, str, int | None, dict[str, Any]]] = []
            if not is_admin:
                # A subject may keep one task running and one task waiting by
                # default.  The running limit is enforced by the scheduler;
                # admission only rejects when the waiting allowance is full
                # (or when queueing is disabled while all running slots are
                # occupied).
                queue_full = (
                    queued >= snapshot.max_queued_per_subject
                    if snapshot.max_queued_per_subject > 0
                    else active >= snapshot.max_running_per_subject
                )
                if queue_full:
                    reasons.append(
                        (
                            "SUBJECT_QUEUE_FULL",
                            "当前主体已有任务正在运行或排队，请等待完成后再提交。",
                            snapshot.queue_timeout_seconds,
                            {
                                "running": active,
                                "queued": queued,
                                "running_limit": snapshot.max_running_per_subject,
                                "queued_limit": snapshot.max_queued_per_subject,
                            },
                        )
                    )
            if global_queued >= snapshot.global_queue_limit:
                reasons.append(
                    (
                        "GLOBAL_QUEUE_FULL",
                        "公共队列已满，请稍后再试。",
                        snapshot.queue_timeout_seconds,
                        {"queued": global_queued, "limit": snapshot.global_queue_limit},
                    )
                )
            if not is_admin and is_visitor:
                if rolling_count >= snapshot.subject_window_limit:
                    reset = await self._rolling_reset(session, subject_key, window_start)
                    reasons.append(
                        (
                            "SUBJECT_RATE_LIMITED",
                            "本设备提问过于频繁，请稍后再试。",
                            reset,
                            {"window_seconds": snapshot.subject_window_seconds},
                        )
                    )
                if subject_counter.task_count >= snapshot.subject_daily_limit:
                    reasons.append(
                        (
                            "SUBJECT_DAILY_LIMITED",
                            "本设备今日试用额度已用完，请明天再试。",
                            int((self._policy.next_local_midnight(now) - now).total_seconds()),
                            {"daily_limit": snapshot.subject_daily_limit},
                        )
                    )
            if global_counter.task_count >= snapshot.global_daily_task_limit:
                reasons.append(
                    (
                        "AGENT_DAILY_TASK_BUDGET_EXHAUSTED",
                        "今日公共试用额度已用完，请明天再试。",
                        int((self._policy.next_local_midnight(now) - now).total_seconds()),
                        {"daily_limit": snapshot.global_daily_task_limit},
                    )
                )

            queue_deadline = now + timedelta(seconds=snapshot.queue_timeout_seconds)
            if reasons:
                code, message, retry_after, details = reasons[0]
                should_reject = snapshot.mode == "enforce" or (
                    is_admin and snapshot.mode == "paused"
                )
                # Observe mode deliberately keeps accepting the task but
                # records the reason that enforce mode would have returned.
                await self._record_rejection(session, code, now, commit=should_reject)
                if should_reject:
                    raise AgentAdmissionError(
                        code,
                        message,
                        status_code=429 if code != "AGENT_DAILY_TASK_BUDGET_EXHAUSTED" else 503,
                        retry_after=max(1, retry_after) if retry_after is not None else None,
                        details=details,
                    )

            event = AgentAdmissionEvent(
                id=new_id("admit"),
                subject_key=subject_key,
                task_id=task_id,
                request_kind=request_kind,
                occurred_at=now,
            )
            session.add(event)
            subject_counter.task_count += 1
            subject_counter.updated_at = now
            global_counter.task_count += 1
            global_counter.updated_at = now
            # Flush before returning so SQLite takes the write lock while this
            # admission lease is still held.  Route handlers request a lease,
            # then commit the event, counter and task together before releasing
            # it; direct service callers retain the historical auto-release
            # behavior by leaving ``hold_lease`` false.
            await session.flush()
            lease = _AdmissionLease(self._lock, counter_lock) if hold_lease else None
            if lease is None:
                counter_lock.release()
                self._lock.release()
            return AdmissionResult(
                queue_deadline_at=queue_deadline,
                window_remaining=window_remaining,
                daily_remaining=daily_remaining,
                _lease=lease,
            )
        except BaseException:
            if counter_acquired:
                counter_lock.release()
            self._lock.release()
            raise

    async def access(self, *, principal: RequestPrincipal) -> dict[str, Any]:
        subject_key = principal.product_subject_id
        snapshot = self._policy.snapshot()
        now = utc_now()
        if not subject_key:
            return {
                "mode": snapshot.mode,
                "turnstile_enabled": snapshot.turnstile_enabled,
                "turnstile_site_key": snapshot.turnstile_site_key,
                "verification_required": False,
                "window_remaining": None,
                "daily_remaining": None,
                "window_reset_at": None,
                "daily_reset_at": self._policy.next_local_midnight(now),
                "running": 0,
                "queued": 0,
            }
        async with self._database.session_factory() as session:
            window_start = now - timedelta(seconds=snapshot.subject_window_seconds)
            used_window = int(
                await session.scalar(
                    select(func.count(AgentAdmissionEvent.id)).where(
                        AgentAdmissionEvent.subject_key == subject_key,
                        AgentAdmissionEvent.occurred_at > window_start,
                    )
                )
                or 0
            )
            counter = await session.scalar(
                select(AgentUsageCounter).where(
                    AgentUsageCounter.scope_key == f"subject:{subject_key}",
                    AgentUsageCounter.bucket_date == self._policy.local_date(now),
                )
            )
            running = int(
                await session.scalar(
                    select(func.count(AgentTask.id)).where(
                        AgentTask.requested_by_subject_id == subject_key,
                        AgentTask.status == "running",
                    )
                )
                or 0
            )
            queued = int(
                await session.scalar(
                    select(func.count(AgentTask.id)).where(
                        AgentTask.requested_by_subject_id == subject_key,
                        AgentTask.status == "queued",
                    )
                )
                or 0
            )
            earliest = await session.scalar(
                select(AgentAdmissionEvent.occurred_at)
                .where(
                    AgentAdmissionEvent.subject_key == subject_key,
                    AgentAdmissionEvent.occurred_at > window_start,
                )
                .order_by(AgentAdmissionEvent.occurred_at.asc())
                .limit(1)
            )
            verified_until = None
            if principal.role == "visitor" and principal.session_id:
                visitor = await session.get(VisitorSession, principal.session_id)
                verified_until = visitor.verified_until if visitor else None
        return {
            "mode": snapshot.mode,
            "turnstile_enabled": snapshot.turnstile_enabled,
            "turnstile_site_key": snapshot.turnstile_site_key,
            "verification_required": bool(
                snapshot.mode == "enforce"
                and snapshot.turnstile_enabled
                and principal.role == "visitor"
                and (verified_until is None or _aware(verified_until) <= now)
            ),
            "window_remaining": (
                None
                if principal.role != "visitor" or principal.authenticated
                else max(0, snapshot.subject_window_limit - used_window)
            ),
            "daily_remaining": (
                None
                if principal.role != "visitor" or principal.authenticated
                else max(
                    0,
                    snapshot.subject_daily_limit
                    - int(counter.task_count if counter else 0),
                )
            ),
            "window_reset_at": (
                (_aware(earliest) + timedelta(seconds=snapshot.subject_window_seconds))
                if earliest is not None
                else None
            ),
            "daily_reset_at": self._policy.next_local_midnight(now),
            "running": running,
            "queued": queued,
        }

    async def verify_turnstile(
        self,
        *,
        request: Request,
        principal: RequestPrincipal,
        token: str,
    ) -> datetime:
        snapshot = self._policy.snapshot()
        if (
            principal.role != "visitor"
            or not principal.product_subject_id
            or not principal.session_id
        ):
            raise AgentAdmissionError(
                "HUMAN_VERIFICATION_NOT_APPLICABLE",
                "当前身份不需要匿名人机验证。",
                status_code=400,
            )
        if not snapshot.turnstile_enabled:
            raise AgentAdmissionError(
                "TURNSTILE_DISABLED",
                "当前部署未启用人机验证。",
                status_code=409,
            )
        secret = self._policy.turnstile_secret()
        if not snapshot.turnstile_site_key or not secret:
            raise AgentAdmissionError(
                "TURNSTILE_NOT_CONFIGURED",
                "人机验证尚未完成服务器配置，请联系管理员。",
                status_code=503,
            )
        token = token.strip()
        if not token or len(token) > 4096:
            raise AgentAdmissionError(
                "HUMAN_VERIFICATION_FAILED",
                "人机验证凭证格式不正确，请重新验证。",
                status_code=400,
            )
        now = utc_now()
        ip_hmac = self._ip_hmac(request)
        async with self._lock:
            async with self._database.session_factory() as session:
                cutoff = now - timedelta(hours=1)
                recent = int(
                    await session.scalar(
                        select(func.count(func.distinct(AgentVerificationEvent.subject_key))).where(
                            AgentVerificationEvent.ip_hmac == ip_hmac,
                            AgentVerificationEvent.outcome == "succeeded",
                            AgentVerificationEvent.occurred_at >= cutoff,
                        )
                    )
                    or 0
                )
                existing = await session.get(VisitorSession, principal.session_id)
                if existing is None:
                    raise AgentAdmissionError(
                        "PRODUCT_IDENTITY_UNAVAILABLE",
                        "匿名设备身份已失效，请刷新页面后重试。",
                        status_code=503,
                    )
                if recent >= snapshot.ip_new_subjects_per_hour and existing.verified_until is None:
                    session.add(
                        AgentVerificationEvent(
                            id=new_id("verify"),
                            ip_hmac=ip_hmac,
                            subject_key=principal.product_subject_id,
                            outcome="rate_limited",
                            occurred_at=now,
                        )
                    )
                    await session.commit()
                    raise AgentAdmissionError(
                        "VERIFICATION_RATE_LIMITED",
                        "同一网络的新设备验证次数过多，请稍后再试。",
                        status_code=429,
                        retry_after=3600,
                    )
            try:
                async with httpx.AsyncClient(timeout=8.0, trust_env=False) as client:
                    response = await client.post(
                        "https://challenges.cloudflare.com/turnstile/v0/siteverify",
                        data={"secret": secret, "response": token},
                    )
                    response.raise_for_status()
                    result = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                await self._record_verification(
                    principal.product_subject_id,
                    ip_hmac,
                    "unavailable",
                )
                raise AgentAdmissionError(
                    "TURNSTILE_UNAVAILABLE",
                    "人机验证服务暂时不可用，请稍后重试。",
                    status_code=503,
                    retry_after=30,
                ) from exc
            if not isinstance(result, dict) or result.get("success") is not True:
                await self._record_verification(principal.product_subject_id, ip_hmac, "failed")
                raise AgentAdmissionError(
                    "HUMAN_VERIFICATION_FAILED",
                    "人机验证未通过，请重新验证。",
                    status_code=400,
                )
            verified_until = now + timedelta(hours=snapshot.verification_lease_hours)
            async with self._database.session_factory() as session:
                visitor = await session.get(VisitorSession, principal.session_id)
                if visitor is None:
                    raise AgentAdmissionError(
                        "PRODUCT_IDENTITY_UNAVAILABLE",
                        "匿名设备身份已失效，请刷新页面后重试。",
                        status_code=503,
                    )
                visitor.verified_until = verified_until
                session.add(
                    AgentVerificationEvent(
                        id=new_id("verify"),
                        ip_hmac=ip_hmac,
                        subject_key=principal.product_subject_id,
                        outcome="succeeded",
                        occurred_at=now,
                    )
                )
                await session.commit()
            return verified_until

    async def cleanup(self) -> None:
        now = utc_now()
        cutoff_date = (
            now.astimezone(_policy_timezone(self._policy.snapshot().timezone)).date()
            - timedelta(days=2)
        ).isoformat()
        # Keep cleanup behind the same process-wide admission/counter locks so
        # an expiring event cannot be removed between a quota read and its
        # reservation, and rejection aggregates cannot race a write.
        async with self._lock:
            async with self._policy.counter_lock:
                async with self._database.session_factory() as session:
                    await session.execute(
                        delete(AgentAdmissionEvent).where(
                            AgentAdmissionEvent.occurred_at < now - timedelta(hours=48)
                        )
                    )
                    await session.execute(
                        delete(AgentVerificationEvent).where(
                            AgentVerificationEvent.occurred_at < now - timedelta(hours=24)
                        )
                    )
                    await session.execute(
                        delete(AgentUsageCounter).where(
                            AgentUsageCounter.scope_key.like("rejection:%"),
                            AgentUsageCounter.bucket_date < cutoff_date,
                        )
                    )
                    # Subject counters are only needed for the current local
                    # day.  Retain the rolling admission events separately
                    # for 48 hours, but do not let random anonymous subject
                    # identifiers accumulate in the daily counter table.
                    await session.execute(
                        delete(AgentUsageCounter).where(
                            AgentUsageCounter.scope_key.like("subject:%"),
                            AgentUsageCounter.bucket_date < cutoff_date,
                        )
                    )
                    await session.commit()

    async def _visitor_verified(
        self,
        session: AsyncSession,
        principal: RequestPrincipal,
        now: datetime,
    ) -> bool:
        if not principal.session_id:
            return False
        visitor = await session.get(VisitorSession, principal.session_id)
        return visitor is not None and visitor.verified_until is not None and _aware(
            visitor.verified_until
        ) > now

    async def _get_counter(
        self,
        session: AsyncSession,
        *,
        scope_key: str,
        bucket_date: str,
        now: datetime,
    ) -> AgentUsageCounter:
        counter = await session.scalar(
            select(AgentUsageCounter).where(
                AgentUsageCounter.scope_key == scope_key,
                AgentUsageCounter.bucket_date == bucket_date,
            )
        )
        if counter is None:
            counter = AgentUsageCounter(
                id=new_id("usage"),
                scope_key=scope_key,
                bucket_date=bucket_date,
                task_count=0,
                model_call_count=0,
                updated_at=now,
            )
            session.add(counter)
            await session.flush()
        return counter

    async def _rolling_reset(
        self,
        session: AsyncSession,
        subject_key: str,
        window_start: datetime,
    ) -> int:
        earliest = await session.scalar(
            select(AgentAdmissionEvent.occurred_at)
            .where(
                AgentAdmissionEvent.subject_key == subject_key,
                AgentAdmissionEvent.occurred_at > window_start,
            )
            .order_by(AgentAdmissionEvent.occurred_at.asc())
            .limit(1)
        )
        if earliest is None:
            return 60
        expires_at = _aware(earliest) + timedelta(
            seconds=self._policy.snapshot().subject_window_seconds
        )
        return max(1, int((expires_at - utc_now()).total_seconds()))

    async def _record_verification(self, subject_key: str, ip_hmac: str, outcome: str) -> None:
        async with self._database.session_factory() as session:
            session.add(
                AgentVerificationEvent(
                    id=new_id("verify"),
                    ip_hmac=ip_hmac,
                    subject_key=subject_key,
                    outcome=outcome,
                    occurred_at=utc_now(),
                )
            )
            await session.commit()

    async def _record_rejection(
        self,
        session: AsyncSession,
        code: str,
        now: datetime,
        *,
        commit: bool = True,
    ) -> None:
        """Persist a code-only rejection without turning metrics into a 500."""

        try:
            await self._policy.record_rejection(
                session,
                code=code,
                now=now,
            )
            if commit:
                await session.commit()
        except Exception:
            await session.rollback()

    def _ip_hmac(self, request: Request) -> str:
        forwarded = request.headers.get("x-forwarded-for", "")
        ip = forwarded.split(",", 1)[0].strip() if forwarded else ""
        if not ip and request.client is not None:
            ip = request.client.host
        key = self._policy.security_hmac_key()
        return hmac.new(key, ip.encode("utf-8"), hashlib.sha256).hexdigest()


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _policy_timezone(name: str):
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("Asia/Shanghai")


def admission_http_exception(exc: AgentAdmissionError):
    from fastapi import HTTPException

    headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after is not None else None
    return HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code, "message": exc.message, **exc.details},
        headers=headers,
    )
