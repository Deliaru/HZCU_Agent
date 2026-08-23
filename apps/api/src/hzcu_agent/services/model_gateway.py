import asyncio
import json
import logging
import re
from datetime import datetime
from typing import Any, Protocol

from anthropic import AsyncAnthropic
from openai import AsyncOpenAI
from pydantic import ValidationError

from hzcu_agent.config import Settings
from hzcu_agent.prompts import (
    ANSWER_COMPOSER_PROMPT,
    ANSWER_VERIFIER_PROMPT,
    CITATION_REPAIR_PROMPT,
    INVESTIGATION_REVIEW_PROMPT,
    PLANNER_PROMPT,
    PREPARED_INVESTIGATION_PROMPT,
    SEMANTIC_PERCEPTION_PROMPT,
)
from hzcu_agent.schemas import (
    AgentAnswer,
    AnswerClaim,
    AnswerRevision,
    AnswerVerification,
    ClaimCitation,
    EmotionalContext,
    Evidence,
    GoalHypothesis,
    GroundedAnswerComposition,
    GroundingAssessment,
    InvestigationPlan,
    InvestigationReview,
    InvestigationStep,
    PreparedInvestigation,
    RiskAssessment,
    SemanticDossier,
    SemanticSignals,
    ToolError,
    VerificationFinding,
)
from hzcu_agent.services.model_runtime import (
    ModelEndpointConfig,
    model_config_from_settings,
)
from hzcu_agent.services.performance import current_performance_trace

logger = logging.getLogger(__name__)
_MODEL_RETRY_DELAYS = (1.0, 2.0, 4.0)


class ModelConfigurationError(RuntimeError):
    pass


class _StructuredOutputUnavailableError(RuntimeError):
    """The endpoint answered successfully without a schema-conformant result."""


class ModelGateway(Protocol):
    provider: str
    agent_model: str

    async def prepare(
        self,
        *,
        original_query: str,
        conversation_context: list[dict[str, str]],
        profile_context: dict[str, Any],
        tool_catalog: list[dict[str, Any]],
        current_time: datetime,
    ) -> PreparedInvestigation: ...

    async def understand(
        self,
        *,
        original_query: str,
        conversation_context: list[dict[str, str]],
        profile_context: dict[str, Any],
        current_time: datetime,
    ) -> SemanticDossier: ...

    async def plan(
        self,
        *,
        original_query: str,
        dossier: SemanticDossier,
        conversation_context: list[dict[str, str]],
        tool_catalog: list[dict[str, Any]],
        current_time: datetime,
    ) -> InvestigationPlan: ...

    async def review(
        self,
        *,
        original_query: str,
        dossier: SemanticDossier,
        conversation_context: list[dict[str, str]],
        plan: InvestigationPlan,
        attempted_steps: list[InvestigationStep],
        evidence: list[Evidence],
        tool_errors: list[ToolError],
        tool_observations: list[dict[str, Any]],
        tool_catalog: list[dict[str, Any]],
        current_time: datetime,
    ) -> InvestigationReview: ...

    async def compose(
        self,
        *,
        original_query: str,
        dossier: SemanticDossier,
        conversation_context: list[dict[str, str]],
        profile_context: dict[str, Any],
        evidence: list[Evidence],
        tool_errors: list[ToolError],
        tool_observations: list[dict[str, Any]],
        tool_catalog: list[dict[str, Any]],
        can_research_more: bool,
        current_time: datetime,
    ) -> GroundedAnswerComposition: ...

    async def verify(
        self,
        *,
        original_query: str,
        dossier: SemanticDossier,
        conversation_context: list[dict[str, str]],
        evidence: list[Evidence],
        composition: GroundedAnswerComposition,
        current_time: datetime,
    ) -> AnswerVerification: ...

    async def repair_citations(
        self,
        *,
        original_query: str,
        answer: AgentAnswer,
        evidence: list[Evidence],
        findings: list[VerificationFinding],
        current_time: datetime,
    ) -> AnswerRevision: ...

    async def close(self) -> None: ...


class DemoModelGateway:
    """A transparent no-key adapter for exercising the full product pipeline."""

    provider = "demo"
    agent_model = "deterministic-demo"

    async def prepare(
        self,
        *,
        original_query: str,
        conversation_context: list[dict[str, str]],
        profile_context: dict[str, Any],
        tool_catalog: list[dict[str, Any]],
        current_time: datetime,
    ) -> PreparedInvestigation:
        _record_demo_model_call("prepare")
        dossier = await self.understand(
            original_query=original_query,
            conversation_context=conversation_context,
            profile_context=profile_context,
            current_time=current_time,
        )
        plan = await self.plan(
            original_query=original_query,
            dossier=dossier,
            conversation_context=conversation_context,
            tool_catalog=tool_catalog,
            current_time=current_time,
        )
        return PreparedInvestigation(dossier=dossier, plan=plan)

    async def understand(
        self,
        *,
        original_query: str,
        conversation_context: list[dict[str, str]],
        profile_context: dict[str, Any],
        current_time: datetime,
    ) -> SemanticDossier:
        _record_demo_model_call("understand", nested=True)
        del conversation_context, profile_context, current_time
        return SemanticDossier(
            goal_hypotheses=[
                GoalHypothesis(
                    goal=f"查清并解释：{original_query}",
                    confidence=0.78,
                    support=["用户本轮原始表达"],
                    required_evidence=["浙大城市学院官方公开信息"],
                )
            ],
            signals=SemanticSignals(
                domains=["校园综合"],
                intents=["信息查询"],
                freshness="current",
                task_shape="simple",
            ),
            emotional_context=EmotionalContext(),
            risk=RiskAssessment(level="normal", reason="当前未识别到高风险操作"),
            candidate_evidence_types=["校园官网实时信息"],
        )

    async def plan(
        self,
        *,
        original_query: str,
        dossier: SemanticDossier,
        conversation_context: list[dict[str, str]],
        tool_catalog: list[dict[str, Any]],
        current_time: datetime,
    ) -> InvestigationPlan:
        _record_demo_model_call("plan", nested=True)
        del dossier, conversation_context, tool_catalog, current_time
        return InvestigationPlan(
            objective=f"从校园时间版本记忆调查“{original_query}”",
            hypotheses_to_test=[original_query],
            steps=[
                InvestigationStep(
                    id="campus-memory",
                    purpose="从已采集的多来源校园材料中召回相关当前版本",
                    tool="search_campus_memory",
                    arguments={"query": original_query, "top_k": 8},
                    success_condition="召回至少一条相关的校园官方材料",
                )
            ],
            stop_conditions=["已有材料足以覆盖问题"],
            fallbacks=["提示演示模式限制，并建议缩短或替换检索关键词"],
        )

    async def review(
        self,
        *,
        original_query: str,
        dossier: SemanticDossier,
        conversation_context: list[dict[str, str]],
        plan: InvestigationPlan,
        attempted_steps: list[InvestigationStep],
        evidence: list[Evidence],
        tool_errors: list[ToolError],
        tool_observations: list[dict[str, Any]],
        tool_catalog: list[dict[str, Any]],
        current_time: datetime,
    ) -> InvestigationReview:
        _record_demo_model_call("review")
        del (
            original_query,
            dossier,
            conversation_context,
            plan,
            attempted_steps,
            tool_errors,
            tool_observations,
            tool_catalog,
            current_time,
        )
        if evidence:
            return InvestigationReview(
                status="sufficient",
                can_answer=True,
                summary="演示调查已取得可引用的校园材料。",
            )
        return InvestigationReview(
            status="insufficient",
            can_answer=True,
            summary="演示调查未取得材料，将透明降级回答。",
            missing_evidence=["相关校园官方材料"],
        )

    async def compose(
        self,
        *,
        original_query: str,
        dossier: SemanticDossier,
        conversation_context: list[dict[str, str]],
        profile_context: dict[str, Any],
        evidence: list[Evidence],
        tool_errors: list[ToolError],
        tool_observations: list[dict[str, Any]],
        tool_catalog: list[dict[str, Any]],
        can_research_more: bool,
        current_time: datetime,
    ) -> GroundedAnswerComposition:
        _record_demo_model_call("compose")
        del (
            dossier,
            conversation_context,
            profile_context,
            tool_observations,
            tool_catalog,
            can_research_more,
            current_time,
        )
        if evidence:
            result_lines = []
            claims = []
            for index, item in enumerate(evidence, start=1):
                excerpt = item.excerpt.replace("\n", " ").strip()
                result_lines.append(f"{index}. **{item.title}**：{excerpt[:180]}… [来源{index}]")
                claims.append(
                    AnswerClaim(
                        claim_id=f"claim-{index}",
                        text=f"{item.title}包含与问题相关的校园材料。",
                        statement_type="campus_fact",
                        importance="supporting",
                        support_status="full",
                        citations=[
                            ClaimCitation(
                                evidence_id=item.evidence_id,
                                support_status="full",
                                rationale="演示模式仅陈述该页面包含当前展示的原文摘录。",
                                supporting_excerpt=excerpt[:200],
                            )
                        ],
                    )
                )
            return GroundedAnswerComposition(
                answer=AgentAnswer(
                    headline="已完成校园镜像检索",
                    answer_markdown=(
                        f"我围绕“{original_query}”找到了以下官方页面：\n\n"
                        + "\n\n".join(result_lines)
                        + "\n\n当前运行的是**无模型密钥演示模式**：实时检索、证据链、"
                        "任务流和界面均真实运行，但尚未调用大模型完成语义归纳与个性化建议。"
                    ),
                    next_actions=["配置模型密钥后，让 Agent 对证据进行综合判断与个性化回答"],
                    confidence="medium",
                    verification_mode="cache",
                    claims=claims,
                ),
                assessment=GroundingAssessment(
                    status="sufficient",
                    summary="演示模式已把展示事实映射到取得的证据。",
                ),
            )

        error_note = "；".join(error.message for error in tool_errors)
        return GroundedAnswerComposition(
            answer=AgentAnswer(
                headline="这次没有取得可核验的官网材料",
                answer_markdown=(
                    f"我尝试围绕“{original_query}”检索校园官网，但没有取得可用证据。"
                    + (f"\n\n工具反馈：{error_note}" if error_note else "")
                    + "\n\n当前是无模型密钥演示模式，我不会在缺少证据时补写校园事实。"
                ),
                next_actions=["换用更短的关键词重试", "配置真实模型适配器以启用语义改写检索"],
                confidence="low",
                verification_mode="degraded",
            ),
            assessment=GroundingAssessment(
                status="insufficient",
                summary="没有取得可核验的校园材料。",
            ),
        )

    async def verify(
        self,
        *,
        original_query: str,
        dossier: SemanticDossier,
        conversation_context: list[dict[str, str]],
        evidence: list[Evidence],
        composition: GroundedAnswerComposition,
        current_time: datetime,
    ) -> AnswerVerification:
        _record_demo_model_call("verify")
        del (
            original_query,
            dossier,
            conversation_context,
            evidence,
            composition,
            current_time,
        )
        return AnswerVerification(
            verdict="passed",
            summary="演示回答通过结构化引用检查。",
        )

    async def repair_citations(
        self,
        *,
        original_query: str,
        answer: AgentAnswer,
        evidence: list[Evidence],
        findings: list[VerificationFinding],
        current_time: datetime,
    ) -> AnswerRevision:
        _record_demo_model_call("repair_citations")
        del original_query, answer, evidence, findings, current_time
        # Demo mode cannot judge semantic support, so it never rebinds citations;
        # the coordinator falls through to the transparent degraded answer.
        return AnswerRevision()

    async def close(self) -> None:
        return None


class OpenAIModelGateway:
    provider = "openai"

    def __init__(self, settings: Settings | ModelEndpointConfig) -> None:
        config = (
            model_config_from_settings(settings) if isinstance(settings, Settings) else settings
        )
        if config.protocol != "openai_responses" or not config.api_key:
            raise ModelConfigurationError(
                "HZCU_MODEL_PROVIDER=openai 时必须配置 HZCU_OPENAI_API_KEY"
            )
        self.agent_model = config.agent_model
        self._utility_model = config.utility_model
        self._reasoning_effort = config.reasoning_effort
        self._utility_reasoning_effort = config.utility_reasoning_effort
        client_options: dict[str, Any] = {
            "api_key": config.api_key,
            "timeout": config.timeout_seconds,
        }
        if config.base_url:
            client_options["base_url"] = config.base_url
        self._client = AsyncOpenAI(**client_options)
        self._streaming_available: bool | None = None

    async def prepare(
        self,
        *,
        original_query: str,
        conversation_context: list[dict[str, str]],
        profile_context: dict[str, Any],
        tool_catalog: list[dict[str, Any]],
        current_time: datetime,
    ) -> PreparedInvestigation:
        payload = {
            "current_time": current_time.isoformat(),
            "original_query": original_query,
            "conversation_context": conversation_context,
            "confirmed_profile_context": profile_context,
            "tool_catalog": tool_catalog,
        }
        return await self._parse(
            role="prepare",
            model=self._utility_model,
            instructions=PREPARED_INVESTIGATION_PROMPT,
            payload=payload,
            schema=PreparedInvestigation,
        )

    async def understand(
        self,
        *,
        original_query: str,
        conversation_context: list[dict[str, str]],
        profile_context: dict[str, Any],
        current_time: datetime,
    ) -> SemanticDossier:
        payload = {
            "current_time": current_time.isoformat(),
            "original_query": original_query,
            "conversation_context": conversation_context,
            "confirmed_profile_context": profile_context,
        }
        return await self._parse(
            role="understand",
            model=self._utility_model,
            instructions=SEMANTIC_PERCEPTION_PROMPT,
            payload=payload,
            schema=SemanticDossier,
        )

    async def plan(
        self,
        *,
        original_query: str,
        dossier: SemanticDossier,
        conversation_context: list[dict[str, str]],
        tool_catalog: list[dict[str, Any]],
        current_time: datetime,
    ) -> InvestigationPlan:
        payload = {
            "current_time": current_time.isoformat(),
            "original_query": original_query,
            "conversation_context": conversation_context,
            "semantic_dossier": dossier.model_dump(mode="json"),
            "tool_catalog": tool_catalog,
        }
        return await self._parse(
            role="plan",
            model=self._utility_model,
            instructions=PLANNER_PROMPT,
            payload=payload,
            schema=InvestigationPlan,
        )

    async def review(
        self,
        *,
        original_query: str,
        dossier: SemanticDossier,
        conversation_context: list[dict[str, str]],
        plan: InvestigationPlan,
        attempted_steps: list[InvestigationStep],
        evidence: list[Evidence],
        tool_errors: list[ToolError],
        tool_observations: list[dict[str, Any]],
        tool_catalog: list[dict[str, Any]],
        current_time: datetime,
    ) -> InvestigationReview:
        payload = {
            "current_time": current_time.isoformat(),
            "original_query": original_query,
            "conversation_context": conversation_context,
            "semantic_dossier": dossier.model_dump(mode="json"),
            "original_plan": plan.model_dump(mode="json"),
            "attempted_steps": [step.model_dump(mode="json") for step in attempted_steps],
            "evidence": [
                {"source_number": index, **item.model_dump(mode="json")}
                for index, item in enumerate(evidence, start=1)
            ],
            "tool_errors": [error.model_dump(mode="json") for error in tool_errors],
            "tool_observations": tool_observations,
            "tool_catalog": tool_catalog,
        }
        return await self._parse(
            role="review",
            model=self._utility_model,
            instructions=INVESTIGATION_REVIEW_PROMPT,
            payload=payload,
            schema=InvestigationReview,
        )

    async def compose(
        self,
        *,
        original_query: str,
        dossier: SemanticDossier,
        conversation_context: list[dict[str, str]],
        profile_context: dict[str, Any],
        evidence: list[Evidence],
        tool_errors: list[ToolError],
        tool_observations: list[dict[str, Any]],
        tool_catalog: list[dict[str, Any]],
        can_research_more: bool,
        current_time: datetime,
    ) -> GroundedAnswerComposition:
        payload = {
            "current_time": current_time.isoformat(),
            "original_query": original_query,
            "conversation_context": conversation_context,
            "confirmed_profile_context": profile_context,
            "semantic_dossier": dossier.model_dump(mode="json"),
            "evidence": [
                {"source_number": index, **item.model_dump(mode="json")}
                for index, item in enumerate(evidence, start=1)
            ],
            "tool_errors": [error.model_dump(mode="json") for error in tool_errors],
            "tool_observations": tool_observations,
            "tool_catalog": tool_catalog,
            "can_research_more": can_research_more,
        }
        return await self._parse(
            role="compose",
            model=self.agent_model,
            instructions=ANSWER_COMPOSER_PROMPT,
            payload=payload,
            schema=GroundedAnswerComposition,
        )

    async def verify(
        self,
        *,
        original_query: str,
        dossier: SemanticDossier,
        conversation_context: list[dict[str, str]],
        evidence: list[Evidence],
        composition: GroundedAnswerComposition,
        current_time: datetime,
    ) -> AnswerVerification:
        payload = {
            "current_time": current_time.isoformat(),
            "original_query": original_query,
            "conversation_context": conversation_context,
            "semantic_dossier": dossier.model_dump(mode="json"),
            "evidence": [item.model_dump(mode="json") for item in evidence],
            "candidate_composition": composition.model_dump(mode="json"),
        }
        return await self._parse(
            role="verify",
            model=self._utility_model,
            instructions=ANSWER_VERIFIER_PROMPT,
            payload=payload,
            schema=AnswerVerification,
        )

    async def repair_citations(
        self,
        *,
        original_query: str,
        answer: AgentAnswer,
        evidence: list[Evidence],
        findings: list[VerificationFinding],
        current_time: datetime,
    ) -> AnswerRevision:
        payload = {
            "current_time": current_time.isoformat(),
            "original_query": original_query,
            "failed_findings": [item.model_dump(mode="json") for item in findings],
            "candidate_answer": answer.model_dump(mode="json"),
            # Keep enough of any model-selected full document for citation
            # repair without applying topic-specific excerpt rules.
            "evidence_workspace": [
                {
                    "evidence_id": item.evidence_id,
                    "title": item.title,
                    "publisher": item.publisher,
                    "canonical_url": item.canonical_url,
                    "excerpt": item.excerpt[:200_000],
                }
                for item in evidence
            ],
        }
        return await self._parse(
            role="repair_citations",
            model=self._utility_model,
            instructions=CITATION_REPAIR_PROMPT,
            payload=payload,
            schema=AnswerRevision,
        )

    async def _parse(
        self,
        *,
        role: str,
        model: str,
        instructions: str,
        payload: dict,
        schema,
    ):
        reasoning_effort = (
            self._utility_reasoning_effort
            if role in {"prepare", "understand", "plan", "review", "repair_citations"}
            else self._reasoning_effort
        )
        request = {
            "model": model,
            "instructions": instructions,
            "input": json.dumps(payload, ensure_ascii=False),
            "text_format": schema,
            "reasoning": {"effort": reasoning_effort},
            "store": False,
        }
        for attempt in range(len(_MODEL_RETRY_DELAYS) + 1):
            try:
                response = await self._send_parse_request(role=role, request=request)
                return self._parse_openai_response(
                    response=response,
                    model=model,
                    schema=schema,
                )
            except Exception as exc:
                if not _is_transient_model_error(exc) or attempt >= len(_MODEL_RETRY_DELAYS):
                    if _should_fallback_openai_structured_output(exc):
                        recovered = _parse_openai_validation_input(schema, exc)
                        if recovered is not None:
                            logger.info(
                                "Recovered JSON from a non-structured model response",
                                extra={
                                    "event": "model.structured_output.recovered",
                                    "role": role,
                                },
                            )
                            return recovered
                        logger.warning(
                            "Structured model output failed schema parsing; "
                            "retrying with plain JSON",
                            extra={
                                "event": "model.structured_output.retry",
                                "role": role,
                            },
                        )
                        return await self._send_structured_repair_request(
                            role=role,
                            request=request,
                            schema=schema,
                        )
                    raise
                delay = _MODEL_RETRY_DELAYS[attempt]
                logger.warning(
                    "Transient model API failure; retrying",
                    extra={
                        "event": "model.request.retry",
                        "role": role,
                        "attempt": attempt + 1,
                        "delay_seconds": delay,
                    },
                )
                await asyncio.sleep(delay)
        raise AssertionError("model retry loop exited unexpectedly")

    async def _send_parse_request(self, *, role: str, request: dict[str, Any]):
        trace = current_performance_trace()
        if trace is None:
            return await self._client.responses.parse(**request)
        span = trace.start_span("model", role)
        try:
            if self._streaming_available is False:
                trace.mark_unmeasurable(span)
                return await self._client.responses.parse(**request)
            try:
                async with self._client.responses.stream(**request) as stream:
                    async for event in stream:
                        event_type = getattr(event, "type", "")
                        if event_type in {
                            "response.output_text.delta",
                            "response.refusal.delta",
                        }:
                            trace.mark_first_event(span)
                    response = await stream.get_final_response()
                self._streaming_available = True
                if span.first_event_ns is None:
                    trace.mark_unmeasurable(span)
                return response
            except Exception:
                # Some relays emit vendor metadata events (e.g. a
                # codex.rate_limits frame) before response.created,
                # which the SDK stream state machine rejects outright.
                # Remember that incompatibility for this process so
                # later calls go straight to the non-streaming API.
                if span.first_event_ns is not None:
                    raise
                self._streaming_available = False
                logger.warning(
                    "Model stream failed before any output; disabling streaming",
                    extra={"event": "model.stream.fallback", "role": role},
                    exc_info=True,
                )
                trace.mark_unmeasurable(span)
                return await self._client.responses.parse(**request)
        except Exception:
            trace.mark_unmeasurable(span)
            raise
        finally:
            trace.finish_span(span)

    @staticmethod
    def _parse_openai_response(*, response: Any, model: str, schema):
        parsed = getattr(response, "output_parsed", None)
        if parsed is not None:
            return parsed

        # A compatible relay can return text while leaving output_parsed empty.
        # Accept a schema-valid JSON object here before making a second request.
        text_result = getattr(response, "output_text", None)
        if isinstance(text_result, str) and text_result.strip():
            try:
                return _parse_schema_text(schema, text_result)
            except (ValidationError, ValueError):
                pass

        raise _StructuredOutputUnavailableError(
            f"Model {model} did not return a structured response"
        )

    async def _send_structured_repair_request(
        self,
        *,
        role: str,
        request: dict[str, Any],
        schema,
    ):
        schema_json = json.dumps(schema.model_json_schema(), ensure_ascii=False)
        repair_request = {key: value for key, value in request.items() if key != "text_format"}
        repair_request["instructions"] = (
            f"{request['instructions']}\n\n"
            "The previous response was not directly parseable as the required schema. "
            "Return exactly one JSON object with no Markdown fences, no commentary, "
            "and no surrounding text. It must validate against this JSON Schema:\n"
            f"{schema_json}"
        )

        trace = current_performance_trace()
        if trace is None:
            response = await self._client.responses.create(**repair_request)
        else:
            span = trace.start_span("model", f"{role}.structured_retry")
            trace.mark_unmeasurable(span)
            try:
                response = await self._client.responses.create(**repair_request)
            finally:
                trace.finish_span(span)

        text_result = getattr(response, "output_text", None)
        if not isinstance(text_result, str) or not text_result.strip():
            raise _StructuredOutputUnavailableError(
                "Structured JSON repair response did not contain text"
            )
        return _parse_schema_text(schema, text_result)

    async def close(self) -> None:
        await self._client.close()


class AnthropicModelGateway(OpenAIModelGateway):
    """Run the same model-native workflow through an Anthropic Messages endpoint."""

    provider = "anthropic"

    def __init__(self, config: ModelEndpointConfig) -> None:
        if config.protocol != "anthropic_messages" or not config.api_key:
            raise ModelConfigurationError("Anthropic Messages 端点必须配置 API 密钥")
        self.agent_model = config.agent_model
        self._utility_model = config.utility_model
        self._reasoning_effort = config.reasoning_effort
        self._utility_reasoning_effort = config.utility_reasoning_effort
        client_options: dict[str, Any] = {
            "api_key": config.api_key,
            "timeout": config.timeout_seconds,
        }
        if config.base_url:
            client_options["base_url"] = config.base_url
        self._client = AsyncAnthropic(**client_options)
        self._structured_output_available: bool | None = None

    async def _parse(
        self,
        *,
        role: str,
        model: str,
        instructions: str,
        payload: dict,
        schema,
    ):
        for attempt in range(len(_MODEL_RETRY_DELAYS) + 1):
            try:
                if self._structured_output_available is False:
                    return await self._send_tool_result_request(
                        role=role,
                        model=model,
                        instructions=instructions,
                        payload=payload,
                        schema=schema,
                    )
                try:
                    response = await self._send_anthropic_request(
                        role=role,
                        model=model,
                        instructions=instructions,
                        payload=payload,
                        schema=schema,
                    )
                    parsed = getattr(response, "parsed_output", None)
                    if parsed is None:
                        raise _StructuredOutputUnavailableError(
                            f"Model {model} did not return a structured response"
                        )
                    self._structured_output_available = True
                    return parsed
                except Exception as exc:
                    if not _should_fallback_anthropic_structured_output(exc):
                        raise
                    self._structured_output_available = False
                    logger.info(
                        "Anthropic endpoint lacks structured outputs; using a forced tool result",
                        extra={
                            "event": "model.anthropic.structured_output_fallback",
                            "role": role,
                        },
                    )
                    return await self._send_tool_result_request(
                        role=role,
                        model=model,
                        instructions=instructions,
                        payload=payload,
                        schema=schema,
                    )
            except Exception as exc:
                if not _is_transient_model_error(exc) or attempt >= len(_MODEL_RETRY_DELAYS):
                    raise
                await asyncio.sleep(_MODEL_RETRY_DELAYS[attempt])
        raise AssertionError("model retry loop exited unexpectedly")

    async def _send_anthropic_request(
        self,
        *,
        role: str,
        model: str,
        instructions: str,
        payload: dict,
        schema,
    ):
        request = {
            "model": model,
            "max_tokens": 16_384,
            "system": instructions,
            "messages": [
                {
                    "role": "user",
                    "content": json.dumps(payload, ensure_ascii=False),
                }
            ],
            "output_format": schema,
        }
        return await self._timed_anthropic_call(
            role,
            lambda: self._client.messages.parse(**request),
        )

    async def _send_tool_result_request(
        self,
        *,
        role: str,
        model: str,
        instructions: str,
        payload: dict,
        schema,
    ):
        tool_name = "emit_structured_result"
        request = {
            "model": model,
            "max_tokens": 16_384,
            "system": (
                f"{instructions}\n\n"
                "Return the complete result by calling emit_structured_result exactly once."
            ),
            "messages": [
                {
                    "role": "user",
                    "content": json.dumps(payload, ensure_ascii=False),
                }
            ],
            "tools": [
                {
                    "name": tool_name,
                    "description": "Return the structured result for this model turn.",
                    "input_schema": schema.model_json_schema(),
                }
            ],
            "tool_choice": {"type": "tool", "name": tool_name},
        }
        response = await self._timed_anthropic_call(
            role,
            lambda: self._client.messages.create(**request),
        )
        invalid_result: str | None = None
        text_parts: list[str] = []
        for block in response.content:
            if (
                getattr(block, "type", None) == "tool_use"
                and getattr(block, "name", None) == tool_name
            ):
                try:
                    return schema.model_validate(block.input)
                except ValidationError:
                    invalid_result = json.dumps(block.input, ensure_ascii=False)
            elif getattr(block, "type", None) == "text":
                text_parts.append(getattr(block, "text", ""))

        text_result = "\n".join(part for part in text_parts if part).strip()
        if text_result:
            try:
                return _parse_schema_text(schema, text_result)
            except (ValidationError, ValueError):
                invalid_result = text_result

        return await self._send_prompted_json_request(
            role=role,
            model=model,
            instructions=instructions,
            payload=payload,
            schema=schema,
            previous_result=invalid_result,
        )

    async def _send_prompted_json_request(
        self,
        *,
        role: str,
        model: str,
        instructions: str,
        payload: dict,
        schema,
        previous_result: str | None,
    ):
        schema_json = json.dumps(schema.model_json_schema(), ensure_ascii=False)
        messages: list[dict[str, str]] = [
            {
                "role": "user",
                "content": json.dumps(payload, ensure_ascii=False),
            }
        ]
        if previous_result:
            messages.extend(
                [
                    {"role": "assistant", "content": previous_result},
                    {
                        "role": "user",
                        "content": (
                            "The previous result did not match the required JSON Schema. "
                            "Correct it and return only the complete JSON object."
                        ),
                    },
                ]
            )
        request = {
            "model": model,
            "max_tokens": 16_384,
            "system": (
                f"{instructions}\n\n"
                "Return exactly one JSON object with no Markdown or commentary. "
                f"It must validate against this JSON Schema: {schema_json}"
            ),
            "messages": messages,
        }
        response = await self._timed_anthropic_call(
            role,
            lambda: self._client.messages.create(**request),
        )
        text_result = "\n".join(
            getattr(block, "text", "")
            for block in response.content
            if getattr(block, "type", None) == "text"
        ).strip()
        if not text_result:
            raise RuntimeError(f"Model {model} did not return a JSON result")
        return _parse_schema_text(schema, text_result)

    async def _timed_anthropic_call(self, role: str, request):
        trace = current_performance_trace()
        if trace is None:
            return await request()
        span = trace.start_span("model", role)
        trace.mark_unmeasurable(span)
        try:
            return await request()
        finally:
            trace.finish_span(span)

    async def close(self) -> None:
        await self._client.close()


class ManagedModelGateway:
    """Atomically swaps the server gateway while letting in-flight calls finish."""

    def __init__(self, config: ModelEndpointConfig) -> None:
        self._config = config
        self._gateway = build_model_gateway_from_config(config)
        self._retired: list[ModelGateway] = []
        self._replace_lock = asyncio.Lock()

    @property
    def provider(self) -> str:
        return self._gateway.provider

    @property
    def agent_model(self) -> str:
        return self._gateway.agent_model

    @property
    def config(self) -> ModelEndpointConfig:
        return self._config

    async def replace(self, config: ModelEndpointConfig) -> None:
        replacement = build_model_gateway_from_config(config)
        async with self._replace_lock:
            self._retired.append(self._gateway)
            self._gateway = replacement
            self._config = config

    async def prepare(self, **kwargs):
        return await self._gateway.prepare(**kwargs)

    async def understand(self, **kwargs):
        return await self._gateway.understand(**kwargs)

    async def plan(self, **kwargs):
        return await self._gateway.plan(**kwargs)

    async def review(self, **kwargs):
        return await self._gateway.review(**kwargs)

    async def compose(self, **kwargs):
        return await self._gateway.compose(**kwargs)

    async def verify(self, **kwargs):
        return await self._gateway.verify(**kwargs)

    async def repair_citations(self, **kwargs):
        return await self._gateway.repair_citations(**kwargs)

    async def close(self) -> None:
        gateways = [self._gateway, *self._retired]
        self._retired = []
        await asyncio.gather(*(gateway.close() for gateway in gateways))


def build_model_gateway(settings: Settings) -> ModelGateway:
    return build_model_gateway_from_config(model_config_from_settings(settings))


def build_model_gateway_from_config(config: ModelEndpointConfig) -> ModelGateway:
    if config.protocol == "openai_responses":
        return OpenAIModelGateway(config)
    if config.protocol == "anthropic_messages":
        return AnthropicModelGateway(config)
    return DemoModelGateway()


def _is_transient_model_error(exc: Exception) -> bool:
    status_code = getattr(exc, "status_code", None)
    if status_code in {408, 409, 425, 429} or (isinstance(status_code, int) and status_code >= 500):
        return True
    message = str(exc).lower()
    return any(
        marker in message
        for marker in (
            "overloaded",
            "upstream_error",
            "temporarily unavailable",
            "try again later",
            "connection error",
            "connection timeout",
        )
    )


def _should_fallback_openai_structured_output(exc: Exception) -> bool:
    """Detect OpenAI-compatible endpoints that return text instead of JSON."""

    if isinstance(exc, (ValidationError, _StructuredOutputUnavailableError)):
        return True
    status_code = getattr(exc, "status_code", None)
    if status_code not in {400, 404, 422}:
        return False
    message = str(exc).lower()
    return any(
        marker in message
        for marker in (
            "text.format",
            "json_schema",
            "structured output",
            "response_format",
            "unsupported",
            "unknown field",
        )
    )


def _is_unsupported_anthropic_structured_output(exc: Exception) -> bool:
    status_code = getattr(exc, "status_code", None)
    if status_code not in {400, 404, 422}:
        return False
    message = str(exc).lower()
    return any(
        marker in message
        for marker in (
            "output_config",
            "output_format",
            "structured output",
            "unknown field",
            "extra inputs are not permitted",
        )
    )


def _should_fallback_anthropic_structured_output(exc: Exception) -> bool:
    """Detect both rejected and silently ignored Anthropic output schemas.

    Some Anthropic-compatible relays accept ``output_format`` but return free-form
    text or JSON that does not conform to the requested schema. The SDK exposes
    those successful-but-invalid responses as a Pydantic validation error rather
    than an HTTP capability error. In either case, retry through the same generic
    schema as a forced tool call.
    """

    return isinstance(exc, (ValidationError, _StructuredOutputUnavailableError)) or (
        _is_unsupported_anthropic_structured_output(exc)
    )


def _parse_openai_validation_input(schema, exc: Exception):
    """Recover a valid fenced JSON result preserved in a Pydantic parse error."""

    if not isinstance(exc, ValidationError):
        return None
    for error in exc.errors():
        candidate = error.get("input")
        if not isinstance(candidate, str):
            continue
        try:
            return _parse_schema_text(schema, candidate)
        except (ValidationError, ValueError):
            continue
    return None


def _parse_schema_text(schema, text: str):
    """Extract generic JSON candidates and accept only a schema-valid object."""

    candidates = [text.strip()]
    candidates.extend(
        match.group(1).strip()
        for match in re.finditer(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
    )
    decoder = json.JSONDecoder()
    candidates.extend(text[index:] for index, character in enumerate(text) if character == "{")
    last_error: Exception | None = None
    for candidate in candidates:
        if not candidate:
            continue
        try:
            value, _ = decoder.raw_decode(candidate)
            return schema.model_validate(value)
        except (json.JSONDecodeError, ValidationError) as exc:
            last_error = exc
    raise ValueError("Model text did not contain a schema-valid JSON object") from last_error


def _record_demo_model_call(name: str, *, nested: bool = False) -> None:
    if nested:
        return
    trace = current_performance_trace()
    if trace is None:
        return
    span = trace.start_span("model", name)
    trace.mark_first_event(span)
    trace.finish_span(span)
