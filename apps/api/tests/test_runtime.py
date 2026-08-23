from types import SimpleNamespace

import pytest

from hzcu_agent.models import new_id, utc_now
from hzcu_agent.runtime import TaskEventBroker
from hzcu_agent.schemas import Evidence, ToolResult
from hzcu_agent.services.coordinator import AgentCoordinator


@pytest.mark.asyncio
async def test_broker_replays_history_and_stops_after_terminal_event() -> None:
    broker = TaskEventBroker()
    await broker.publish("task_1", "task.accepted", {"step": 1})
    await broker.publish("task_1", "thinking.started", {"step": 2})
    await broker.publish("task_1", "answer.completed", {"step": 3})

    events = [event async for event in broker.subscribe("task_1", after_sequence=1)]

    assert [event.sequence for event in events] == [2, 3]
    assert [event.event for event in events] == ["thinking.started", "answer.completed"]


@pytest.mark.asyncio
async def test_tool_completed_event_preserves_evidence_provenance() -> None:
    broker = TaskEventBroker()
    coordinator = object.__new__(AgentCoordinator)
    coordinator._broker = broker
    observed_at = utc_now()
    evidence = Evidence(
        evidence_id=new_id("ev"),
        title="校历通知",
        publisher="浙大城市学院教务处",
        canonical_url="https://www.hzcu.edu.cn/calendar",
        observed_at=observed_at,
        excerpt="全校按校历安排教学活动。",
        source_id="hzcu-jwc",
        authority_level="official",
        audience_scopes=["public", "campus"],
        effective_from=observed_at,
        retrieval_mode="memory",
    )
    result = ToolResult(
        tool="search_campus_memory",
        status="ok",
        evidence=[evidence],
        trace_id="trace_runtime",
    )

    await coordinator._publish_tool_completed(
        task_id="task_runtime",
        step=SimpleNamespace(id="step_runtime", tool="search_campus_memory"),
        result=result,
        new_evidence=[evidence],
    )
    subscription = broker.subscribe("task_runtime")
    event = await anext(subscription)
    await subscription.aclose()

    assert event.event == "tool.completed"
    assert event.data["evidence"][0]["authority_level"] == "official"
    assert event.data["evidence"][0]["audience_scopes"] == ["public", "campus"]
    assert event.data["evidence"][0]["retrieval_mode"] == "memory"
