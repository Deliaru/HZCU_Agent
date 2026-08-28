from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta

from sqlalchemy import func, select, update

from hzcu_agent.db import Database
from hzcu_agent.models import AgentTask, utc_now
from hzcu_agent.runtime import TaskEventBroker
from hzcu_agent.services.agent_admission import AgentAdmissionService
from hzcu_agent.services.agent_policy import AgentPolicyService
from hzcu_agent.services.coordinator import AgentCoordinator

logger = logging.getLogger(__name__)


class AgentTaskScheduler:
    """Database-backed FIFO dispatcher for queued Agent tasks.

    The process keeps only running coroutines in memory.  Queue membership,
    deadlines and recovery survive a normal service restart.
    """

    def __init__(
        self,
        *,
        database: Database,
        coordinator: AgentCoordinator,
        broker: TaskEventBroker,
        policy: AgentPolicyService,
        admission: AgentAdmissionService | None = None,
        background_tasks: dict[str, asyncio.Task[object]],
    ) -> None:
        self._database = database
        self._coordinator = coordinator
        self._broker = broker
        self._policy = policy
        self._admission = admission
        self._background_tasks = background_tasks
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._loop_task: asyncio.Task[None] | None = None
        self._active_subjects: dict[str, int] = {}
        self._lock = asyncio.Lock()
        self._last_cleanup = 0.0

    def start(self) -> None:
        if self._loop_task is None or self._loop_task.done():
            self._stop.clear()
            self._last_cleanup = time.monotonic()
            self._loop_task = asyncio.create_task(self._dispatch_loop(), name="agent-scheduler")

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._loop_task is not None:
            await asyncio.gather(self._loop_task, return_exceptions=True)
            self._loop_task = None

    async def enqueue(self, task_id: str) -> None:
        await self._broker.ensure(task_id)
        async with self._database.session_factory() as session:
            task = await session.get(AgentTask, task_id)
            position = await self._queue_position(session, task) if task is not None else 0
        await self._broker.publish(
            task_id,
            "task.accepted",
            {
                "task_id": task_id,
                "conversation_id": task.conversation_id if task is not None else None,
                "queue_position": position,
            },
        )
        self._wake.set()

    async def _dispatch_loop(self) -> None:
        while not self._stop.is_set():
            now = time.monotonic()
            if now - self._last_cleanup >= 900:
                try:
                    if self._admission is not None:
                        await self._admission.cleanup()
                except Exception:
                    logger.exception(
                        "Agent admission cleanup failed",
                        extra={"event": "agent.admission.cleanup_failed"},
                    )
                self._last_cleanup = now
            # A transient SQLite/connection error must not terminate the
            # scheduler task and strand durable queued rows until a process
            # restart.  The next tick retries both expiry and dispatch while
            # keeping the in-memory running set untouched.
            try:
                await self._expire_queued()
            except Exception:
                logger.exception(
                    "Agent queue expiry failed",
                    extra={"event": "agent.scheduler.expiry_failed"},
                )
            try:
                await self._dispatch_available()
            except Exception:
                logger.exception(
                    "Agent queue dispatch failed",
                    extra={"event": "agent.scheduler.dispatch_failed"},
                )
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=0.2)
            except TimeoutError:
                pass
            self._wake.clear()

    async def _dispatch_available(self) -> None:
        snapshot = self._policy.snapshot()
        async with self._lock:
            capacity = max(0, snapshot.agent_concurrency - len(self._background_tasks))
            if capacity <= 0:
                return
            async with self._database.session_factory() as session:
                persisted_running_rows = list(
                    (
                        await session.execute(
                            select(
                                AgentTask.requested_by_subject_id,
                                func.count(AgentTask.id),
                            )
                            .where(AgentTask.status == "running")
                            .group_by(AgentTask.requested_by_subject_id)
                        )
                    ).all()
                )
                queued = list(
                    (
                        await session.scalars(
                            select(AgentTask)
                            .where(AgentTask.status == "queued")
                            .order_by(AgentTask.created_at.asc(), AgentTask.id.asc())
                            .limit(max(1, snapshot.global_queue_limit))
                        )
                    ).all()
                )
            # Close the read session before launching coordinators.  A queued
            # task may immediately update its row to ``running``; keeping the
            # scheduler's SQLite read transaction open would unnecessarily
            # hold a shared lock over that write.
            persisted_running = {
                (subject_id or ""): int(count or 0)
                for subject_id, count in persisted_running_rows
            }
            for task in queued:
                if capacity <= 0:
                    break
                subject_key = task.requested_by_subject_id or f"task:{task.id}"
                active_for_subject = max(
                    self._active_subjects.get(subject_key, 0),
                    persisted_running.get(subject_key, 0),
                )
                if active_for_subject >= snapshot.max_running_per_subject:
                    continue
                self._active_subjects[subject_key] = active_for_subject + 1
                running = asyncio.create_task(
                    self._run_one(task.id, subject_key),
                    name=f"agent:{task.id}",
                )
                self._background_tasks[task.id] = running
                running.add_done_callback(
                    lambda completed, task_id=task.id: self._background_tasks.pop(
                        task_id,
                        None,
                    )
                )
                capacity -= 1

    async def _run_one(self, task_id: str, subject_key: str) -> None:
        try:
            await self._coordinator.run(task_id)
        finally:
            active_for_subject = self._active_subjects.get(subject_key, 1) - 1
            if active_for_subject > 0:
                self._active_subjects[subject_key] = active_for_subject
            else:
                self._active_subjects.pop(subject_key, None)
            self._wake.set()

    async def _expire_queued(self) -> None:
        now = utc_now()
        snapshot = self._policy.snapshot()
        fallback_deadline = now - timedelta(seconds=snapshot.queue_timeout_seconds)
        async with self._database.session_factory() as session:
            rows = list(
                (
                    await session.scalars(
                        select(AgentTask).where(
                            AgentTask.status == "queued",
                            (
                                (AgentTask.queue_deadline_at.is_not(None))
                                & (AgentTask.queue_deadline_at <= now)
                            )
                            | (
                                AgentTask.queue_deadline_at.is_(None)
                                & (AgentTask.created_at <= fallback_deadline)
                            ),
                        )
                    )
                ).all()
            )
            if not rows:
                return
            task_ids = [task.id for task in rows]
            await session.execute(
                update(AgentTask)
                .where(
                    AgentTask.id.in_(task_ids),
                    AgentTask.status == "queued",
                )
                .values(
                    status="failed",
                    error_code="QUEUE_TIMEOUT",
                    updated_at=now,
                )
            )
            await session.commit()
            rows = list(
                (
                    await session.scalars(
                        select(AgentTask).where(
                            AgentTask.id.in_(task_ids),
                            AgentTask.status == "failed",
                            AgentTask.error_code == "QUEUE_TIMEOUT",
                        )
                    )
                ).all()
            )
        for task in rows:
            await self._broker.ensure(task.id)
            await self._broker.publish(
                task.id,
                "task.failed",
                {
                    "task_id": task.id,
                    "error_code": "QUEUE_TIMEOUT",
                    "message": "排队等待超时，请稍后重新提交。",
                },
            )

    async def _queue_position(self, session, task: AgentTask | None) -> int:
        if task is None or task.status != "queued":
            return 0
        earlier = await session.scalar(
            select(AgentTask.id)
            .where(
                AgentTask.status == "queued",
                (AgentTask.created_at < task.created_at)
                | ((AgentTask.created_at == task.created_at) & (AgentTask.id < task.id)),
            )
            .order_by(AgentTask.created_at.desc(), AgentTask.id.desc())
            .limit(31)
        )
        if earlier is None:
            return 0
        from sqlalchemy import func

        count = await session.scalar(
            select(func.count(AgentTask.id)).where(
                AgentTask.status == "queued",
                (AgentTask.created_at < task.created_at)
                | ((AgentTask.created_at == task.created_at) & (AgentTask.id < task.id)),
            )
        )
        return int(count or 0)
