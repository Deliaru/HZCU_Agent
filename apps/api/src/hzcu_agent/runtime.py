import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

TERMINAL_EVENTS = {"answer.completed", "task.failed", "task.needs_input"}


@dataclass(frozen=True)
class RuntimeEvent:
    sequence: int
    event: str
    data: dict[str, Any]


@dataclass
class _TaskChannel:
    history: list[RuntimeEvent] = field(default_factory=list)
    subscribers: set[asyncio.Queue[RuntimeEvent]] = field(default_factory=set)
    terminal: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class TaskEventBroker:
    """Process-local event history and fan-out for the Stage 1 SSE stream."""

    def __init__(self, history_limit: int = 200) -> None:
        self._channels: dict[str, _TaskChannel] = {}
        self._channels_lock = asyncio.Lock()
        self._history_limit = history_limit

    async def ensure(self, task_id: str) -> None:
        await self._get_channel(task_id)

    async def publish(self, task_id: str, event: str, data: dict[str, Any]) -> RuntimeEvent:
        channel = await self._get_channel(task_id)
        async with channel.lock:
            envelope = RuntimeEvent(
                sequence=(channel.history[-1].sequence + 1 if channel.history else 1),
                event=event,
                data=data,
            )
            channel.history.append(envelope)
            if len(channel.history) > self._history_limit:
                channel.history = channel.history[-self._history_limit :]
            if event in TERMINAL_EVENTS:
                channel.terminal = True
            subscribers = tuple(channel.subscribers)

        for queue in subscribers:
            queue.put_nowait(envelope)
        return envelope

    async def subscribe(self, task_id: str, after_sequence: int = 0) -> AsyncIterator[RuntimeEvent]:
        channel = await self._get_channel(task_id)
        queue: asyncio.Queue[RuntimeEvent] = asyncio.Queue()

        async with channel.lock:
            history = [event for event in channel.history if event.sequence > after_sequence]
            terminal = channel.terminal
            if not terminal:
                channel.subscribers.add(queue)

        try:
            for event in history:
                yield event
            if terminal:
                return

            while True:
                event = await queue.get()
                yield event
                if event.event in TERMINAL_EVENTS:
                    return
        finally:
            async with channel.lock:
                channel.subscribers.discard(queue)

    async def _get_channel(self, task_id: str) -> _TaskChannel:
        async with self._channels_lock:
            return self._channels.setdefault(task_id, _TaskChannel())
