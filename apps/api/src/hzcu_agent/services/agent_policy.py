from __future__ import annotations

import asyncio
import base64
import contextvars
import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select

from hzcu_agent.config import Settings
from hzcu_agent.db import Database
from hzcu_agent.models import AgentRuntimePolicy, AgentTask, AgentUsageCounter, new_id, utc_now

DEFAULT_POLICY: dict[str, Any] = {
    "mode": "observe",
    "subject_window_limit": 5,
    "subject_window_seconds": 1800,
    "subject_daily_limit": 15,
    "max_running_per_subject": 1,
    "max_queued_per_subject": 1,
    "global_queue_limit": 30,
    "queue_timeout_seconds": 300,
    "agent_concurrency": 4,
    "model_concurrency": 4,
    "global_daily_task_limit": 300,
    "global_daily_model_call_limit": 1500,
    "per_task_model_call_limit": 8,
    "max_message_length": 1500,
    "scope_policy": "balanced",
    "timezone": "Asia/Shanghai",
    "turnstile_enabled": False,
    "turnstile_site_key": None,
    "verification_lease_hours": 24,
    "ip_new_subjects_per_hour": 60,
}

POLICY_MODES = frozenset({"observe", "enforce", "paused"})
SCOPE_POLICIES = frozenset({"balanced", "strict"})
GLOBAL_SCOPE_KEY = "__global__"

current_agent_task_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_agent_task_id",
    default=None,
)


class AgentPolicyConfigError(RuntimeError):
    pass


class AgentModelBudgetExceeded(RuntimeError):
    def __init__(self, code: str = "AGENT_MODEL_BUDGET_EXHAUSTED") -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class AgentPolicySnapshot:
    mode: str = "observe"
    subject_window_limit: int = 5
    subject_window_seconds: int = 1800
    subject_daily_limit: int = 15
    max_running_per_subject: int = 1
    max_queued_per_subject: int = 1
    global_queue_limit: int = 30
    queue_timeout_seconds: int = 300
    agent_concurrency: int = 4
    model_concurrency: int = 4
    global_daily_task_limit: int = 300
    global_daily_model_call_limit: int = 1500
    per_task_model_call_limit: int = 8
    max_message_length: int = 1500
    scope_policy: str = "balanced"
    timezone: str = "Asia/Shanghai"
    turnstile_enabled: bool = False
    turnstile_site_key: str | None = None
    verification_lease_hours: int = 24
    ip_new_subjects_per_hour: int = 60
    turnstile_secret_configured: bool = False
    turnstile_secret_hint: str | None = None
    updated_at: datetime | None = None

    @property
    def turnstile_configured(self) -> bool:
        return bool(self.turnstile_site_key and self.turnstile_secret_configured)


class AgentPolicyService:
    """Own runtime Agent policy, dynamic gates and daily model-call accounting."""

    record_id = "primary"

    def __init__(self, *, settings: Settings, database: Database) -> None:
        self._settings = settings
        self._database = database
        self._snapshot = AgentPolicySnapshot()
        self._turnstile_secret: str | None = None
        self._config_lock = asyncio.Lock()
        self._usage_lock = asyncio.Lock()
        self._task_condition = asyncio.Condition()
        self._model_condition = asyncio.Condition()
        self._active_tasks = 0
        self._active_model_calls = 0
        self._fernet = self._build_fernet()

    async def initialize(self) -> None:
        async with self._database.session_factory() as session:
            row = await session.get(AgentRuntimePolicy, self.record_id)
            if row is None:
                now = utc_now()
                row = AgentRuntimePolicy(
                    id=self.record_id,
                    **DEFAULT_POLICY,
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
                await session.commit()
            snapshot, secret = self._snapshot_from_row(row)
        async with self._config_lock:
            self._snapshot = snapshot
            self._turnstile_secret = secret

    def snapshot(self) -> AgentPolicySnapshot:
        return self._snapshot

    def turnstile_secret(self) -> str | None:
        return self._turnstile_secret

    def security_hmac_key(self) -> bytes:
        """Return the process-local key used for short-lived security hashes."""

        secret = self._settings.auth_session_secret or self._settings.model_config_secret
        if secret is None:
            return b"hzcu-agent-ip-hmac"
        return secret.get_secret_value().encode("utf-8")

    @property
    def counter_lock(self) -> asyncio.Lock:
        """Serialize daily counter mutations across admission and model gates.

        Admission holds this lock until its task transaction commits, so a
        concurrent model-call reservation cannot race the initial counter row
        creation or overwrite a sibling counter update.
        """

        return self._usage_lock

    def local_date(self, value: datetime | None = None) -> str:
        current = value or utc_now()
        try:
            timezone = ZoneInfo(self.snapshot().timezone)
        except ZoneInfoNotFoundError:
            timezone = ZoneInfo("Asia/Shanghai")
        return current.astimezone(timezone).date().isoformat()

    def next_local_midnight(self, value: datetime | None = None) -> datetime:
        current = value or utc_now()
        try:
            timezone = ZoneInfo(self.snapshot().timezone)
        except ZoneInfoNotFoundError:
            timezone = ZoneInfo("Asia/Shanghai")
        local = current.astimezone(timezone)
        next_day = local.date().fromordinal(local.date().toordinal() + 1)
        return datetime(
            next_day.year,
            next_day.month,
            next_day.day,
            tzinfo=timezone,
        ).astimezone(UTC)

    async def update(self, values: dict[str, Any], *, actor_user_id: str) -> AgentPolicySnapshot:
        normalized = self._validate_values(values)
        async with self._config_lock:
            async with self._database.session_factory() as session:
                row = await session.get(AgentRuntimePolicy, self.record_id)
                if row is None:
                    now = utc_now()
                    row = AgentRuntimePolicy(
                        id=self.record_id,
                        **DEFAULT_POLICY,
                        created_at=now,
                        updated_at=now,
                    )
                    session.add(row)
                    await session.flush()
                for key, value in normalized.items():
                    if key == "turnstile_secret":
                        if value:
                            if self._fernet is None:
                                raise AgentPolicyConfigError(
                                    "保存 Turnstile secret 需要服务器会话加密密钥。"
                                )
                            row.encrypted_turnstile_secret = self._fernet.encrypt(
                                value.encode("utf-8")
                            ).decode("ascii")
                            row.turnstile_secret_hint = f"••••{value[-4:]}"
                        continue
                    setattr(row, key, value)
                row.updated_by_user_id = actor_user_id
                row.updated_at = utc_now()
                await session.commit()
                snapshot, secret = self._snapshot_from_row(row)
            self._snapshot = snapshot
            if "turnstile_secret" in normalized and normalized["turnstile_secret"]:
                self._turnstile_secret = normalized["turnstile_secret"]
            else:
                self._turnstile_secret = secret
            async with self._task_condition:
                self._task_condition.notify_all()
            async with self._model_condition:
                self._model_condition.notify_all()
            return snapshot

    def public_dict(self, *, include_secret_state: bool = True) -> dict[str, Any]:
        snapshot = self.snapshot()
        data = {
            key: getattr(snapshot, key)
            for key in DEFAULT_POLICY
            if key not in {"turnstile_site_key"}
        }
        data["turnstile_site_key"] = snapshot.turnstile_site_key
        if include_secret_state:
            data["turnstile_secret_configured"] = snapshot.turnstile_secret_configured
            data["turnstile_secret_hint"] = snapshot.turnstile_secret_hint
        data["updated_at"] = snapshot.updated_at
        return data

    def task_slot(self):
        return _PolicySlot(self, kind="task")

    async def model_call(
        self,
        role: str,
        operation: Callable[[], Awaitable[Any]],
    ) -> Any:
        del role  # role is retained at call sites for diagnostics and future metrics.
        async with self._model_condition:
            while self._active_model_calls >= max(1, self.snapshot().model_concurrency):
                await self._model_condition.wait()
            self._active_model_calls += 1
        try:
            await self._reserve_model_call()
            return await operation()
        finally:
            async with self._model_condition:
                self._active_model_calls = max(0, self._active_model_calls - 1)
                self._model_condition.notify_all()

    async def _reserve_model_call(self) -> None:
        async with self._usage_lock:
            snapshot = self.snapshot()
            task_id = current_agent_task_id.get()
            now = utc_now()
            bucket = self.local_date(now)
            async with self._database.session_factory() as session:
                global_counter = await self._get_counter(
                    session,
                    scope_key=GLOBAL_SCOPE_KEY,
                    bucket_date=bucket,
                    now=now,
                )
                task = await session.get(AgentTask, task_id) if task_id else None
                if snapshot.mode != "observe":
                    if global_counter.model_call_count >= snapshot.global_daily_model_call_limit:
                        await self.record_rejection(
                            session,
                            code="AGENT_MODEL_BUDGET_EXHAUSTED",
                            now=now,
                        )
                        await session.commit()
                        raise AgentModelBudgetExceeded()
                    if (
                        task is not None
                        and task.model_call_count >= snapshot.per_task_model_call_limit
                    ):
                        await self.record_rejection(
                            session,
                            code="AGENT_MODEL_BUDGET_EXHAUSTED",
                            now=now,
                        )
                        await session.commit()
                        raise AgentModelBudgetExceeded()
                global_counter.model_call_count += 1
                global_counter.updated_at = now
                if task is not None:
                    task.model_call_count += 1
                    task.updated_at = now
                await session.commit()

    async def usage(self, *, subject_key: str | None = None) -> dict[str, int]:
        bucket = self.local_date()
        async with self._database.session_factory() as session:
            global_counter = await session.scalar(
                select(AgentUsageCounter).where(
                    AgentUsageCounter.scope_key == GLOBAL_SCOPE_KEY,
                    AgentUsageCounter.bucket_date == bucket,
                )
            )
            subject_counter = None
            if subject_key:
                subject_counter = await session.scalar(
                    select(AgentUsageCounter).where(
                        AgentUsageCounter.scope_key == f"subject:{subject_key}",
                        AgentUsageCounter.bucket_date == bucket,
                    )
                )
        return {
            "global_tasks": int(global_counter.task_count if global_counter else 0),
            "global_model_calls": int(global_counter.model_call_count if global_counter else 0),
            "subject_tasks": int(subject_counter.task_count if subject_counter else 0),
        }

    async def rejection_counts(self) -> dict[str, int]:
        """Return today's aggregate admission rejection counts by code.

        Rejections are stored in the same daily counter table under a code-only
        scope key.  No subject, IP, message or other user identifier is kept.
        """

        bucket = self.local_date()
        async with self._database.session_factory() as session:
            rows = list(
                (
                    await session.scalars(
                        select(AgentUsageCounter).where(
                            AgentUsageCounter.scope_key.like("rejection:%"),
                            AgentUsageCounter.bucket_date == bucket,
                        )
                    )
                ).all()
            )
        return {
            row.scope_key.removeprefix("rejection:"): int(row.task_count)
            for row in rows
            if row.scope_key.startswith("rejection:")
        }

    async def record_rejection(
        self,
        session,
        *,
        code: str,
        bucket_date: str | None = None,
        now: datetime | None = None,
    ) -> None:
        """Add one code-only rejection to the caller's controlled transaction."""

        occurred_at = now or utc_now()
        counter = await self._get_counter(
            session,
            scope_key=f"rejection:{code}",
            bucket_date=bucket_date or self.local_date(occurred_at),
            now=occurred_at,
        )
        counter.task_count += 1
        counter.updated_at = occurred_at

    async def acquire_task_slot(self) -> None:
        async with self._task_condition:
            while self._active_tasks >= max(1, self.snapshot().agent_concurrency):
                await self._task_condition.wait()
            self._active_tasks += 1

    async def release_task_slot(self) -> None:
        async with self._task_condition:
            self._active_tasks = max(0, self._active_tasks - 1)
            self._task_condition.notify_all()

    async def _get_counter(self, session, *, scope_key: str, bucket_date: str, now: datetime):
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

    def _snapshot_from_row(
        self,
        row: AgentRuntimePolicy,
    ) -> tuple[AgentPolicySnapshot, str | None]:
        secret: str | None = None
        if row.encrypted_turnstile_secret:
            if self._fernet is None:
                raise AgentPolicyConfigError("读取 Turnstile secret 需要服务器会话加密密钥。")
            try:
                secret = self._fernet.decrypt(
                    row.encrypted_turnstile_secret.encode("ascii")
                ).decode("utf-8")
            except (InvalidToken, UnicodeError, ValueError) as exc:
                raise AgentPolicyConfigError("Turnstile secret 无法解密。") from exc
        return (
            AgentPolicySnapshot(
                mode=row.mode,
                subject_window_limit=row.subject_window_limit,
                subject_window_seconds=row.subject_window_seconds,
                subject_daily_limit=row.subject_daily_limit,
                max_running_per_subject=row.max_running_per_subject,
                max_queued_per_subject=row.max_queued_per_subject,
                global_queue_limit=row.global_queue_limit,
                queue_timeout_seconds=row.queue_timeout_seconds,
                agent_concurrency=row.agent_concurrency,
                model_concurrency=row.model_concurrency,
                global_daily_task_limit=row.global_daily_task_limit,
                global_daily_model_call_limit=row.global_daily_model_call_limit,
                per_task_model_call_limit=row.per_task_model_call_limit,
                max_message_length=row.max_message_length,
                scope_policy=row.scope_policy,
                timezone=row.timezone,
                turnstile_enabled=row.turnstile_enabled,
                turnstile_site_key=row.turnstile_site_key,
                verification_lease_hours=row.verification_lease_hours,
                ip_new_subjects_per_hour=row.ip_new_subjects_per_hour,
                turnstile_secret_configured=secret is not None,
                turnstile_secret_hint=row.turnstile_secret_hint,
                updated_at=row.updated_at,
            ),
            secret,
        )

    def _validate_values(self, values: dict[str, Any]) -> dict[str, Any]:
        allowed = set(DEFAULT_POLICY) | {"turnstile_secret"}
        unknown = set(values) - allowed
        if unknown:
            raise AgentPolicyConfigError(f"未知 Agent 策略字段: {', '.join(sorted(unknown))}")
        result = dict(values)
        if "mode" in result and result["mode"] not in POLICY_MODES:
            raise AgentPolicyConfigError("Agent 模式必须是 observe、enforce 或 paused。")
        if "scope_policy" in result and result["scope_policy"] not in SCOPE_POLICIES:
            raise AgentPolicyConfigError("用途策略必须是 balanced 或 strict。")
        if "timezone" in result:
            try:
                ZoneInfo(str(result["timezone"]))
            except ZoneInfoNotFoundError as exc:
                raise AgentPolicyConfigError("日界线必须是有效 IANA 时区。") from exc
        ranges = {
            "subject_window_limit": (1, 100),
            "subject_window_seconds": (1, 86_400),
            "subject_daily_limit": (1, 1_000),
            "max_running_per_subject": (1, 4),
            "max_queued_per_subject": (0, 8),
            "global_queue_limit": (1, 10_000),
            "queue_timeout_seconds": (1, 86_400),
            "agent_concurrency": (1, 64),
            "model_concurrency": (1, 64),
            "global_daily_task_limit": (1, 100_000),
            "global_daily_model_call_limit": (1, 1_000_000),
            "per_task_model_call_limit": (1, 100),
            "max_message_length": (1, 20_000),
            "verification_lease_hours": (1, 168),
            "ip_new_subjects_per_hour": (1, 10_000),
        }
        for key, (minimum, maximum) in ranges.items():
            if key not in result:
                continue
            value = result[key]
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or not minimum <= value <= maximum
            ):
                raise AgentPolicyConfigError(f"{key} 必须在 {minimum} 到 {maximum} 之间。")
        if "turnstile_enabled" in result and not isinstance(result["turnstile_enabled"], bool):
            raise AgentPolicyConfigError("turnstile_enabled 必须是布尔值。")
        if "turnstile_site_key" in result:
            value = result["turnstile_site_key"]
            if value is not None and (
                not isinstance(value, str) or not 1 <= len(value.strip()) <= 256
            ):
                raise AgentPolicyConfigError("Turnstile sitekey 格式不正确。")
            result["turnstile_site_key"] = value.strip() if isinstance(value, str) else value
        if "turnstile_secret" in result:
            value = result["turnstile_secret"]
            if value is not None:
                if not isinstance(value, str) or not 1 <= len(value.strip()) <= 4096:
                    raise AgentPolicyConfigError("Turnstile secret 格式不正确。")
                result["turnstile_secret"] = value.strip()
        return result

    def _build_fernet(self) -> Fernet | None:
        secret = self._settings.effective_model_config_secret
        if secret is None:
            return None
        digest = hashlib.sha256(secret.get_secret_value().encode("utf-8")).digest()
        return Fernet(base64.urlsafe_b64encode(digest))


class _PolicySlot:
    def __init__(self, service: AgentPolicyService, *, kind: str) -> None:
        self._service = service
        self._kind = kind

    async def __aenter__(self):
        if self._kind == "task":
            await self._service.acquire_task_slot()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self._kind == "task":
            await self._service.release_task_slot()
        return False


def set_current_agent_task(task_id: str | None):
    return current_agent_task_id.set(task_id)


def reset_current_agent_task(token) -> None:
    current_agent_task_id.reset(token)
