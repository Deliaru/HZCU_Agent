from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from hzcu_agent.config import Settings
from hzcu_agent.schemas import Evidence, GoalHypothesis, SemanticDossier
from hzcu_agent.services import model_gateway
from hzcu_agent.services.model_gateway import (
    AnthropicModelGateway,
    DemoModelGateway,
    ModelConfigurationError,
    OpenAIModelGateway,
)
from hzcu_agent.services.model_runtime import ModelEndpointConfig
from hzcu_agent.services.performance import (
    AgentPerformanceTrace,
    bind_performance_trace,
    reset_performance_trace,
)


def test_openai_provider_requires_an_api_key() -> None:
    with pytest.raises(ModelConfigurationError):
        OpenAIModelGateway(Settings(model_provider="openai", openai_api_key=None))


@pytest.mark.asyncio
async def test_anthropic_gateway_falls_back_when_successful_output_violates_schema(
    monkeypatch,
) -> None:
    parsed = SemanticDossier(
        goal_hypotheses=[
            GoalHypothesis(
                goal="查询新生入党流程",
                confidence=0.9,
                support=["用户原始问题"],
            )
        ]
    )
    parse_calls: list[dict] = []
    create_calls: list[dict] = []

    class FakeMessages:
        async def parse(self, **kwargs):
            parse_calls.append(kwargs)
            # Mimic a relay accepting output_format but returning its own JSON shape.
            SemanticDossier.model_validate(
                {
                    "entities": [{"type": "action", "value": "加入中国共产党"}],
                }
            )
            raise AssertionError("validation should fail")

        async def create(self, **kwargs):
            create_calls.append(kwargs)
            return SimpleNamespace(
                content=[
                    SimpleNamespace(
                        type="tool_use",
                        name="emit_structured_result",
                        input=parsed.model_dump(mode="json"),
                    )
                ]
            )

    class FakeClient:
        messages = FakeMessages()

        async def close(self):
            return None

    monkeypatch.setattr(model_gateway, "AsyncAnthropic", lambda **kwargs: FakeClient())
    gateway = AnthropicModelGateway(
        ModelEndpointConfig(
            protocol="anthropic_messages",
            api_key="test-only-key",
            base_url="https://relay.example",
            agent_model="agent-model",
            utility_model="utility-model",
            reasoning_effort="medium",
            utility_reasoning_effort="low",
            timeout_seconds=60,
        )
    )

    first = await gateway.understand(
        original_query="我是新生，我想入党，该怎么做？",
        conversation_context=[],
        profile_context={},
        current_time=datetime(2026, 8, 11, tzinfo=UTC),
    )
    second = await gateway.understand(
        original_query="入党申请书交给谁？",
        conversation_context=[],
        profile_context={},
        current_time=datetime(2026, 8, 11, tzinfo=UTC),
    )
    await gateway.close()

    assert first == parsed
    assert second == parsed
    assert len(parse_calls) == 1
    assert len(create_calls) == 2
    assert create_calls[0]["tool_choice"] == {
        "type": "tool",
        "name": "emit_structured_result",
    }
    assert create_calls[0]["tools"][0]["input_schema"] == SemanticDossier.model_json_schema()


@pytest.mark.asyncio
async def test_anthropic_gateway_repairs_text_when_model_ignores_forced_tool(
    monkeypatch,
) -> None:
    parsed = SemanticDossier(
        goal_hypotheses=[
            GoalHypothesis(
                goal="查询新生入党流程",
                confidence=0.9,
                support=["用户原始问题"],
            )
        ]
    )
    create_calls: list[dict] = []

    class FakeMessages:
        async def parse(self, **kwargs):
            SemanticDossier.model_validate({"goal": "wrong shape"})
            raise AssertionError("validation should fail")

        async def create(self, **kwargs):
            create_calls.append(kwargs)
            if len(create_calls) == 1:
                return SimpleNamespace(
                    content=[SimpleNamespace(type="text", text="I cannot call that tool.")]
                )
            return SimpleNamespace(
                content=[
                    SimpleNamespace(
                        type="text",
                        text=f"```json\n{parsed.model_dump_json()}\n```",
                    )
                ]
            )

    class FakeClient:
        messages = FakeMessages()

        async def close(self):
            return None

    monkeypatch.setattr(model_gateway, "AsyncAnthropic", lambda **kwargs: FakeClient())
    gateway = AnthropicModelGateway(
        ModelEndpointConfig(
            protocol="anthropic_messages",
            api_key="test-only-key",
            base_url="https://relay.example",
            agent_model="agent-model",
            utility_model="utility-model",
            reasoning_effort="medium",
            utility_reasoning_effort="low",
            timeout_seconds=60,
        )
    )

    result = await gateway.understand(
        original_query="我是新生，我想入党，该怎么做？",
        conversation_context=[],
        profile_context={},
        current_time=datetime(2026, 8, 11, tzinfo=UTC),
    )
    await gateway.close()

    assert result == parsed
    assert len(create_calls) == 2
    assert create_calls[0]["tool_choice"]["name"] == "emit_structured_result"
    assert "tools" not in create_calls[1]
    assert "Return exactly one JSON object" in create_calls[1]["system"]
    assert "JSON Schema" in create_calls[1]["system"]
    assert create_calls[1]["messages"][-1]["role"] == "user"


@pytest.mark.asyncio
async def test_demo_gateway_truncates_long_supporting_excerpt() -> None:
    observed_at = datetime(2026, 7, 28, tzinfo=UTC)
    evidence = Evidence(
        evidence_id="ev-long-excerpt",
        title="商学院本科学生奖学金评定条件及管理办法",
        publisher="商学院",
        canonical_url="https://sxy.hzcu.edu.cn/example.pdf",
        observed_at=observed_at,
        excerpt="奖学金评定原文" * 80,
        source_id="hzcu-sxy-notices",
        authority_level="official",
        retrieval_mode="memory",
    )

    composition = await DemoModelGateway().compose(
        original_query="商学院本科学生奖学金评定条件",
        dossier=SemanticDossier(
            goal_hypotheses=[
                GoalHypothesis(
                    goal="核验商学院奖学金评定条件",
                    confidence=0.9,
                )
            ]
        ),
        conversation_context=[],
        profile_context={},
        evidence=[evidence],
        tool_errors=[],
        tool_observations=[],
        tool_catalog=[],
        can_research_more=False,
        current_time=observed_at,
    )

    assert composition.answer is not None
    excerpt = composition.answer.claims[0].citations[0].supporting_excerpt
    assert len(excerpt) == 200


@pytest.mark.asyncio
async def test_openai_gateway_uses_responses_structured_parse(monkeypatch) -> None:
    captured: dict = {}
    parsed = SemanticDossier(
        goal_hypotheses=[
            GoalHypothesis(
                goal="了解选课准备",
                confidence=0.9,
                support=["用户原始问题"],
            )
        ]
    )

    class FakeResponses:
        async def parse(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(output_parsed=parsed)

    class FakeClient:
        responses = FakeResponses()

        async def close(self):
            return None

    monkeypatch.setattr(model_gateway, "AsyncOpenAI", lambda **kwargs: FakeClient())
    gateway = OpenAIModelGateway(
        Settings(
            model_provider="openai",
            openai_api_key="test-only-key",
            utility_model="gpt-5.6-terra",
        )
    )
    result = await gateway.understand(
        original_query="选课前我该准备什么？",
        conversation_context=[],
        profile_context={"student_type": "undergraduate"},
        current_time=datetime(2026, 7, 25, tzinfo=UTC),
    )
    await gateway.close()

    assert result is parsed
    assert captured["model"] == "gpt-5.6-terra"
    assert captured["text_format"] is SemanticDossier
    assert captured["store"] is False
    assert captured["reasoning"] == {"effort": "low"}
    assert "选课前我该准备什么" in captured["input"]


@pytest.mark.asyncio
async def test_openai_gateway_recovers_fenced_json_from_parse_error(monkeypatch) -> None:
    parsed = SemanticDossier(
        goal_hypotheses=[
            GoalHypothesis(
                goal="查询专升本课程安排",
                confidence=0.9,
                support=["用户原始问题"],
            )
        ]
    )
    parse_calls = 0
    create_calls = 0

    class FakeResponses:
        async def parse(self, **kwargs):
            nonlocal parse_calls
            parse_calls += 1
            SemanticDossier.model_validate_json(f"```json\n{parsed.model_dump_json()}\n```")
            raise AssertionError("the fenced JSON should raise before this line")

        async def create(self, **kwargs):
            nonlocal create_calls
            create_calls += 1
            raise AssertionError("a valid fenced response should not require a retry")

    class FakeClient:
        responses = FakeResponses()

        async def close(self):
            return None

    monkeypatch.setattr(model_gateway, "AsyncOpenAI", lambda **kwargs: FakeClient())
    gateway = OpenAIModelGateway(
        Settings(
            model_provider="openai",
            openai_api_key="test-only-key",
        )
    )

    result = await gateway.understand(
        original_query="机电专业专升本课程怎么安排？",
        conversation_context=[],
        profile_context={},
        current_time=datetime(2026, 8, 22, tzinfo=UTC),
    )
    await gateway.close()

    assert result == parsed
    assert parse_calls == 1
    assert create_calls == 0


@pytest.mark.asyncio
async def test_openai_gateway_retries_when_structured_response_is_missing(monkeypatch) -> None:
    parsed = SemanticDossier(
        goal_hypotheses=[
            GoalHypothesis(
                goal="查询专升本课程安排",
                confidence=0.9,
                support=["用户原始问题"],
            )
        ]
    )
    parse_calls: list[dict] = []
    create_calls: list[dict] = []

    class FakeResponses:
        async def parse(self, **kwargs):
            parse_calls.append(kwargs)
            return SimpleNamespace(output_parsed=None)

        async def create(self, **kwargs):
            create_calls.append(kwargs)
            return SimpleNamespace(output_text=f"```json\n{parsed.model_dump_json()}\n```")

    class FakeClient:
        responses = FakeResponses()

        async def close(self):
            return None

    monkeypatch.setattr(model_gateway, "AsyncOpenAI", lambda **kwargs: FakeClient())
    gateway = OpenAIModelGateway(
        Settings(
            model_provider="openai",
            openai_api_key="test-only-key",
        )
    )

    result = await gateway.understand(
        original_query="机电专业专升本课程怎么安排？",
        conversation_context=[],
        profile_context={},
        current_time=datetime(2026, 8, 22, tzinfo=UTC),
    )
    await gateway.close()

    assert result == parsed
    assert len(parse_calls) == 1
    assert len(create_calls) == 1
    assert "text_format" not in create_calls[0]
    assert "Return exactly one JSON object" in create_calls[0]["instructions"]
    assert "JSON Schema" in create_calls[0]["instructions"]


@pytest.mark.asyncio
async def test_openai_gateway_retries_relay_overload(monkeypatch) -> None:
    parsed = SemanticDossier(
        goal_hypotheses=[
            GoalHypothesis(
                goal="核验培养方案",
                confidence=0.9,
                support=["用户原始问题"],
            )
        ]
    )
    calls = 0

    class FakeResponses:
        async def parse(self, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("Our servers are currently overloaded. Please try again later.")
            return SimpleNamespace(output_parsed=parsed)

    class FakeClient:
        responses = FakeResponses()

        async def close(self):
            return None

    async def no_wait(_delay: float) -> None:
        return None

    monkeypatch.setattr(model_gateway, "AsyncOpenAI", lambda **kwargs: FakeClient())
    monkeypatch.setattr(model_gateway.asyncio, "sleep", no_wait)
    gateway = OpenAIModelGateway(
        Settings(
            model_provider="openai",
            openai_api_key="test-only-key",
        )
    )

    result = await gateway.understand(
        original_query="智能建造培养方案要求是什么？",
        conversation_context=[],
        profile_context={},
        current_time=datetime(2026, 8, 11, tzinfo=UTC),
    )
    await gateway.close()

    assert result is parsed
    assert calls == 2


@pytest.mark.asyncio
async def test_openai_gateway_streams_to_measure_first_output_event(monkeypatch) -> None:
    parsed = SemanticDossier(
        goal_hypotheses=[
            GoalHypothesis(
                goal="核验校历",
                confidence=0.9,
                support=["用户原始问题"],
            )
        ]
    )

    class FakeStream:
        def __init__(self):
            self._events = iter(
                [
                    SimpleNamespace(type="response.created"),
                    SimpleNamespace(type="response.output_text.delta"),
                    SimpleNamespace(type="response.completed"),
                ]
            )

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return None

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self._events)
            except StopIteration as exc:
                raise StopAsyncIteration from exc

        async def get_final_response(self):
            return SimpleNamespace(output_parsed=parsed)

    class FakeResponses:
        def stream(self, **kwargs):
            assert kwargs["text_format"] is SemanticDossier
            return FakeStream()

    class FakeClient:
        responses = FakeResponses()

        async def close(self):
            return None

    monkeypatch.setattr(model_gateway, "AsyncOpenAI", lambda **kwargs: FakeClient())
    gateway = OpenAIModelGateway(
        Settings(
            model_provider="openai",
            openai_api_key="test-only-key",
        )
    )
    trace = AgentPerformanceTrace()
    token = bind_performance_trace(trace)
    try:
        result = await gateway.understand(
            original_query="什么时候开学？",
            conversation_context=[],
            profile_context={},
            current_time=datetime(2026, 7, 26, tzinfo=UTC),
        )
    finally:
        trace.finish()
        reset_performance_trace(token)
        await gateway.close()

    performance, spans = trace.snapshot()
    assert result is parsed
    assert performance.model_call_count == 1
    assert performance.model_ttft_measurable is True
    assert spans[0]["first_event_ms"] is not None


@pytest.mark.asyncio
async def test_stream_protocol_failure_falls_back_to_non_streaming(monkeypatch) -> None:
    parsed = SemanticDossier(
        goal_hypotheses=[
            GoalHypothesis(
                goal="核验校历",
                confidence=0.9,
                support=["用户原始问题"],
            )
        ]
    )
    parse_calls: list[dict] = []
    stream_calls: list[dict] = []

    class BrokenStream:
        """Mimics the SDK dying on a vendor event before response.created."""

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return None

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise RuntimeError("Unexpected event codex.rate_limits before response.created")

        async def get_final_response(self):  # pragma: no cover - never reached
            raise AssertionError("stream should have failed before completion")

    class FakeResponses:
        def stream(self, **kwargs):
            stream_calls.append(kwargs)
            return BrokenStream()

        async def parse(self, **kwargs):
            parse_calls.append(kwargs)
            return SimpleNamespace(output_parsed=parsed)

    class FakeClient:
        responses = FakeResponses()

        async def close(self):
            return None

    monkeypatch.setattr(model_gateway, "AsyncOpenAI", lambda **kwargs: FakeClient())
    gateway = OpenAIModelGateway(
        Settings(
            model_provider="openai",
            openai_api_key="test-only-key",
        )
    )
    trace = AgentPerformanceTrace()
    token = bind_performance_trace(trace)
    try:
        result = await gateway.understand(
            original_query="什么时候开学？",
            conversation_context=[],
            profile_context={},
            current_time=datetime(2026, 7, 27, tzinfo=UTC),
        )
        second_result = await gateway.understand(
            original_query="校创项目是否中期检查？",
            conversation_context=[],
            profile_context={},
            current_time=datetime(2026, 7, 27, tzinfo=UTC),
        )
    finally:
        trace.finish()
        reset_performance_trace(token)
        await gateway.close()

    performance, _ = trace.snapshot()
    assert result is parsed
    assert second_result is parsed
    assert len(stream_calls) == 1
    assert len(parse_calls) == 2
    assert performance.model_call_count == 2
    # The fallback answer is real, but its first-token latency is unmeasurable.
    assert performance.model_ttft_measurable is False
