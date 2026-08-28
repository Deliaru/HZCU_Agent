import asyncio
import json
import logging
import re
from typing import Any

from sqlalchemy import select, update

from hzcu_agent.config import Settings
from hzcu_agent.db import Database
from hzcu_agent.models import (
    AgentTask,
    AnswerClaimRecord,
    AnswerGroundingRecord,
    AnswerRecord,
    ClaimEvidenceRecord,
    Conversation,
    EvidenceRecord,
    Message,
    ProfileAttribute,
    StudentProfile,
    TaskPerformanceRecord,
    new_id,
    utc_now,
)
from hzcu_agent.runtime import TaskEventBroker
from hzcu_agent.schemas import (
    AgentAnswer,
    AgentPerformance,
    AnswerVerification,
    Evidence,
    GoalHypothesis,
    GroundedAnswerComposition,
    GroundingAssessment,
    GroundingSummary,
    InvestigationPlan,
    InvestigationStep,
    PreparedInvestigation,
    SemanticDossier,
    SemanticSignals,
    ToolError,
    ToolResult,
    VerificationFinding,
)
from hzcu_agent.services.agent_policy import (
    AgentModelBudgetExceeded,
    AgentPolicyService,
    reset_current_agent_task,
    set_current_agent_task,
)
from hzcu_agent.services.evidence_workspace import EvidenceWorkspace
from hzcu_agent.services.grounding import (
    CitationVerifier,
    StructuralGroundingResult,
    apply_answer_revision,
    prune_invalid_citations,
    restore_workspace_citation_urls,
)
from hzcu_agent.services.model_gateway import ModelGateway, StructuredModelOutputError
from hzcu_agent.services.performance import (
    AgentPerformanceTrace,
    bind_performance_trace,
    current_performance_trace,
    reset_performance_trace,
)
from hzcu_agent.services.tool_gateway import ToolGateway
from hzcu_agent.text_safety import clean_product_json, clean_product_text

logger = logging.getLogger(__name__)

_CAMPUS_NOTICE_TOOL = "search_campus_notices_live"
_CAMPUS_NOTICE_BATCH_SIZE = 4
_LOCAL_MIRROR_TOOLS = {
    "search_campus_memory",
    "inspect_campus_document",
    "find_in_campus_document",
    "read_campus_document_locator",
    "read_campus_document_segment",
}
_DOCUMENT_EXPLORATION_TOOLS = {
    "inspect_campus_document",
    "find_in_campus_document",
    "read_campus_document_locator",
    "read_campus_document_segment",
}


def _merge_campus_notice_steps(
    batch: list[InvestigationStep],
) -> tuple[list[InvestigationStep], dict[str, tuple[str, ...]]]:
    """Fold same-batch campus notice steps into one lease-aware batch query.

    The sidecar serializes queries behind one browser lease, so dispatching
    them separately repeats login, source discovery and page parsing. Planning
    still keeps one step per evidence goal; only execution is batched.

    Returns the steps to dispatch plus, for each merged step, the original step
    ids it stands for.
    """

    notice_steps = [step for step in batch if step.tool == _CAMPUS_NOTICE_TOOL]
    if len(notice_steps) < 2:
        return list(batch), {}

    others = [step for step in batch if step.tool != _CAMPUS_NOTICE_TOOL]
    dispatch: list[InvestigationStep] = list(others)
    merged_origins: dict[str, tuple[str, ...]] = {}
    for offset in range(0, len(notice_steps), _CAMPUS_NOTICE_BATCH_SIZE):
        group = notice_steps[offset : offset + _CAMPUS_NOTICE_BATCH_SIZE]
        if len(group) == 1:
            dispatch.append(group[0])
            continue
        queries: list[str] = []
        for step in group:
            for value in (step.arguments.query, *step.arguments.queries):
                if value and value not in queries:
                    queries.append(value)
        merged = group[0].model_copy(
            update={
                "id": "+".join(step.id for step in group),
                "purpose": "；".join(dict.fromkeys(step.purpose for step in group)),
                "arguments": group[0].arguments.model_copy(
                    update={
                        "query": None,
                        "queries": queries[:_CAMPUS_NOTICE_BATCH_SIZE],
                        "limit": max((step.arguments.limit or 6) for step in group),
                    }
                ),
                "depends_on": list(
                    dict.fromkeys(dependency for step in group for dependency in step.depends_on)
                ),
                "success_condition": "；".join(
                    dict.fromkeys(step.success_condition for step in group)
                ),
            }
        )
        dispatch.append(merged)
        merged_origins[merged.id] = tuple(step.id for step in group)
    return dispatch, merged_origins


class AgentCoordinator:
    def __init__(
        self,
        *,
        settings: Settings,
        database: Database,
        broker: TaskEventBroker,
        models: ModelGateway,
        tools: ToolGateway,
        policy: AgentPolicyService | None = None,
    ) -> None:
        self._settings = settings
        self._database = database
        self._broker = broker
        self._models = models
        self._tools = tools
        self._policy = policy
        self._citation_verifier = CitationVerifier()
        self._task_slots = asyncio.Semaphore(settings.max_concurrent_agent_tasks)
        self._subject_locks: dict[str, asyncio.Lock] = {}

    async def run(self, task_id: str) -> None:
        task_token = set_current_agent_task(task_id)
        try:
            subject_id = await self._task_subject_id(task_id)
            if self._policy is None:
                subject_lock = self._subject_locks.setdefault(
                    subject_id or task_id,
                    asyncio.Lock(),
                )
                async with self._task_slots, subject_lock:
                    await self._run_in_slot(task_id)
            else:
                # The database-backed scheduler already enforces the dynamic
                # ``max_running_per_subject`` policy.  Keeping the legacy
                # mutex here would silently cap every subject at one running
                # task even after an administrator raised that setting.  The
                # no-policy path retains the mutex for older direct callers
                # that do not have a scheduler coordinating fairness.
                async with self._policy.task_slot():
                    await self._run_in_slot(task_id)
        finally:
            reset_current_agent_task(task_token)

    async def _run_in_slot(self, task_id: str) -> None:
        performance_trace = AgentPerformanceTrace()
        performance_token = bind_performance_trace(performance_trace)
        try:
            task_context = await self._load_task_context(task_id)
            if task_context is None:
                await self._broker.publish(
                    task_id,
                    "task.failed",
                    {"task_id": task_id, "error_code": "TASK_NOT_FOUND"},
                )
                return

            if not await self._set_task_status(task_id, "running"):
                return
            logger.info(
                "Agent task started",
                extra={"event": "agent.task.started", "task_id": task_id},
            )
            performance_trace.mark_progress()
            await self._broker.publish(
                task_id,
                "thinking.started",
                {"task_id": task_id, "label": "正在理解问题"},
            )

            current_time = utc_now()
            tool_catalog = self._tools.catalog(
                task_context["access_scopes"],
                memory_visibilities=task_context["mirror_access_scopes"],
            )
            try:
                prepared = await self._models.prepare(
                    original_query=task_context["original_query"],
                    conversation_context=task_context["conversation_context"],
                    profile_context=task_context["profile_context"],
                    tool_catalog=tool_catalog,
                    current_time=current_time,
                )
            except AgentModelBudgetExceeded:
                await self._set_task_failed(task_id, "AGENT_MODEL_BUDGET_EXHAUSTED")
                await self._broker.publish(
                    task_id,
                    "task.failed",
                    {
                        "task_id": task_id,
                        "error_code": "AGENT_MODEL_BUDGET_EXHAUSTED",
                        "message": "今日 Agent 模型调用额度已用完，请明天再试。",
                    },
                )
                return
            except StructuredModelOutputError as exc:
                logger.warning(
                    "Prepare output exhausted structured recovery; using minimal plan",
                    extra={
                        "event": "agent.prepare.structured_fallback",
                        "task_id": task_id,
                        **exc.details,
                    },
                )
                prepared = self._fallback_prepared_investigation(
                    task_context["original_query"]
                )
            dossier = self._with_answer_shape(
                prepared.dossier,
                task_context["original_query"],
            )
            dossier = self._with_domain_scope(dossier, task_context["original_query"])
            plan = prepared.plan
            await self._broker.publish(
                task_id,
                "perception.completed",
                {
                    "goals": [
                        {"goal": item.goal, "confidence": item.confidence}
                        for item in dossier.goal_hypotheses
                    ],
                    "signals": dossier.signals.model_dump(mode="json"),
                    "uncertainties": dossier.uncertainties,
                    "risk_level": dossier.risk.level,
                },
            )

            initial_steps = (
                []
                if dossier.signals.domain_scope == "out_of_scope"
                else self._normalize_initial_steps(
                    plan.steps,
                    original_query=task_context["original_query"],
                )
            )
            investigation_freshness = (
                "live_required"
                if task_context["request_mode"] == "live_reverify"
                else dossier.signals.freshness
            )
            planned_steps = self._ensure_live_verification(
                initial_steps,
                original_query=task_context["original_query"],
                freshness=investigation_freshness,
                tool_catalog=tool_catalog,
            )
            if dossier.signals.domain_scope == "ambiguous":
                # Ambiguous wording is allowed to discover a campus answer,
                # but it must stay inside the local, registered read-only
                # corpus.  Do not let a vague request turn into a general web
                # search or an authenticated notice query.
                planned_steps = self._restrict_ambiguous_steps(
                    planned_steps,
                    original_query=task_context["original_query"],
                )
            executable_steps = planned_steps[: self._settings.max_tool_calls]
            await self._broker.publish(
                task_id,
                "plan.created",
                {
                    "objective": plan.objective,
                    "steps": [
                        {
                            "id": step.id,
                            "purpose": step.purpose,
                            "tool": step.tool,
                            "arguments": step.arguments.model_dump(
                                mode="json",
                                exclude_none=True,
                                exclude_defaults=True,
                            ),
                            "depends_on": step.depends_on,
                            "can_run_in_parallel": step.can_run_in_parallel,
                        }
                        for step in executable_steps
                    ],
                },
            )

            workspace = EvidenceWorkspace(task_id)
            tool_errors: list[ToolError] = []
            tool_observations: list[dict[str, Any]] = []
            attempted_steps: list[InvestigationStep] = []
            attempted_signatures: set[str] = set()
            completed_step_ids: set[str] = set()
            pending_steps = executable_steps
            tool_call_count = 0
            composition: GroundedAnswerComposition | None = None

            for round_number in range(1, self._settings.max_tool_rounds + 1):
                if dossier.signals.domain_scope == "out_of_scope":
                    break
                remaining_calls = self._settings.max_tool_calls - tool_call_count
                round_steps = self._unique_steps(
                    pending_steps,
                    attempted_signatures,
                    remaining_calls,
                )
                if round_steps:
                    await self._broker.publish(
                        task_id,
                        "investigation.round.started",
                        {
                            "round": round_number,
                            "step_count": len(round_steps),
                            "remaining_tool_calls": remaining_calls,
                        },
                    )
                    round_results = await self._execute_round(
                        task_id=task_id,
                        steps=round_steps,
                        completed_step_ids=completed_step_ids,
                        access_scopes=task_context["access_scopes"],
                        mirror_access_scopes=task_context["mirror_access_scopes"],
                        actor_user_id=task_context["actor_user_id"],
                    )
                    tool_call_count += len(round_results)
                    attempted_steps.extend(step for step, _ in round_results)

                    for step, result in round_results:
                        completed_step_ids.add(step.id)
                        if result.error:
                            tool_errors.append(result.error)
                        tool_observations.append(
                            {
                                "tool": result.tool,
                                "status": result.status,
                                "data": result.data,
                                "warnings": result.warnings,
                                "error_code": (result.error.code if result.error else None),
                            }
                        )
                        new_evidence = workspace.merge(
                            result.evidence,
                            retrieval_scores=self._retrieval_scores(result),
                        )
                        logger.info(
                            "Agent tool call completed",
                            extra={
                                "event": "agent.tool.completed",
                                "task_id": task_id,
                                "tool": result.tool,
                                "trace_id": result.trace_id,
                                "evidence_count": len(result.evidence),
                                "error_code": (result.error.code if result.error else None),
                                **self._retrieval_diagnostics(result),
                            },
                        )
                        await self._publish_tool_completed(
                            task_id=task_id,
                            step=step,
                            result=result,
                            new_evidence=new_evidence,
                        )

                    await self._broker.publish(
                        task_id,
                        "investigation.round.completed",
                        {
                            "round": round_number,
                            "tool_calls": len(round_results),
                            "evidence_count": len(workspace.items),
                        },
                    )

                answer_evidence = workspace.ranked(limit=24)
                await self._broker.publish(
                    task_id,
                    "answer.composing",
                    {
                        "task_id": task_id,
                        "round": round_number,
                        "evidence_count": len(answer_evidence),
                    },
                )
                try:
                    composition = await self._models.compose(
                        original_query=task_context["original_query"],
                        dossier=dossier,
                        conversation_context=task_context["conversation_context"],
                        profile_context=task_context["profile_context"],
                        evidence=answer_evidence,
                        tool_errors=tool_errors,
                        tool_observations=tool_observations,
                        tool_catalog=tool_catalog,
                        can_research_more=(
                            round_number < self._settings.max_tool_rounds
                            and tool_call_count < self._settings.max_tool_calls
                        ),
                        current_time=utc_now(),
                    )
                except AgentModelBudgetExceeded:
                    composition = GroundedAnswerComposition(
                        answer=self._safe_grounding_fallback(
                            original_query=task_context["original_query"],
                            evidence=answer_evidence,
                        ),
                        assessment=GroundingAssessment(
                            status="conditional",
                            summary="模型调用额度已用完，已使用当前证据安全收束。",
                            missing_evidence=["未完成的模型核验"],
                        ),
                    )
                except StructuredModelOutputError as exc:
                    logger.warning(
                        "Compose output exhausted structured recovery; "
                        "returning safe evidence desk",
                        extra={
                            "event": "agent.compose.structured_fallback",
                            "task_id": task_id,
                            **exc.details,
                        },
                    )
                    composition = GroundedAnswerComposition(
                        answer=self._safe_grounding_fallback(
                            original_query=task_context["original_query"],
                            evidence=answer_evidence,
                        ),
                        assessment=GroundingAssessment(
                            status="conditional",
                            summary="回答模型未能生成可验证结构，已安全降级为证据入口。",
                            missing_evidence=["可验证的结构化回答"],
                        ),
                    )
                await self._broker.publish(
                    task_id,
                    "evidence.assessed",
                    {
                        "round": round_number,
                        "status": composition.assessment.status,
                        "can_answer": not composition.assessment.needs_more_research,
                        "summary": composition.assessment.summary,
                        "missing_evidence": composition.assessment.missing_evidence,
                        "conflicts": composition.assessment.conflicts,
                    },
                )
                can_continue = (
                    composition.assessment.needs_more_research
                    and round_number < self._settings.max_tool_rounds
                    and tool_call_count < self._settings.max_tool_calls
                )
                if not can_continue:
                    break
                pending_steps = self._normalize_follow_up_steps(
                    composition.assessment.follow_up_steps,
                    freshness=investigation_freshness,
                )
                if dossier.signals.domain_scope == "ambiguous":
                    pending_steps = self._restrict_ambiguous_steps(
                        pending_steps,
                        original_query=task_context["original_query"],
                    )
                if not self._unique_steps(
                    pending_steps,
                    set(attempted_signatures),
                    self._settings.max_tool_calls - tool_call_count,
                ):
                    break

            if dossier.signals.domain_scope == "out_of_scope":
                composition = GroundedAnswerComposition(
                    answer=self._scope_refusal_answer(),
                    assessment=GroundingAssessment(
                        status="unauthorized",
                        summary="本服务仅处理浙大城市学院相关的官方信息查询。",
                    ),
                )
            elif composition is None:
                raise RuntimeError("Answer composition did not run")

            evidence = workspace.ranked(limit=24)
            composed_answer = composition.answer or self._safe_grounding_fallback(
                original_query=task_context["original_query"],
                evidence=evidence,
            )
            if dossier.signals.domain_scope == "ambiguous" and (
                not evidence
                or not self._campus_evidence_is_relevant(
                    task_context["original_query"],
                    evidence,
                )
            ):
                answer = self._scope_refusal_answer()
            else:
                answer = self._calibrate_answer(composed_answer, evidence)
            retrieval_coverage_risk = self._retrieval_coverage_risk(tool_observations)
            if retrieval_coverage_risk and answer.confidence == "high":
                answer = answer.model_copy(update={"confidence": "medium"})
            composition = composition.model_copy(update={"answer": answer})
            structural = self._citation_verifier.verify(answer, evidence)
            requires_independent = self._requires_independent_verification(
                composition=composition,
                dossier=dossier,
                structural=structural,
                retrieval_coverage_risk=retrieval_coverage_risk,
            )
            verification = AnswerVerification(
                verdict="passed",
                summary="普通路径已完成结构化引用检查，未触发同步独立语义复核。",
            )
            revision_findings: list[VerificationFinding] = []
            if requires_independent:
                await self._broker.publish(
                    task_id,
                    "answer.verification.started",
                    {
                        "reason": (
                            composition.verification_reason or "证据状态或结构化引用需要独立复核"
                        )
                    },
                )
                try:
                    verification = await self._models.verify(
                        original_query=task_context["original_query"],
                        dossier=dossier,
                        conversation_context=task_context["conversation_context"],
                        evidence=evidence,
                        composition=composition,
                        current_time=utc_now(),
                    )
                except AgentModelBudgetExceeded:
                    verification = AnswerVerification(
                        verdict="research_required",
                        summary="模型调用额度已用完，候选结论未被视为已核验。",
                    )
                except StructuredModelOutputError as exc:
                    logger.warning(
                        "Verifier output exhausted structured recovery; failing closed",
                        extra={
                            "event": "agent.verify.structured_fallback",
                            "task_id": task_id,
                            **exc.details,
                        },
                    )
                    verification = AnswerVerification(
                        verdict="research_required",
                        summary="独立核验未能返回可验证结构，候选结论未被视为已核验。",
                    )
                await self._broker.publish(
                    task_id,
                    "answer.verification.completed",
                    {
                        "verdict": verification.verdict,
                        "summary": verification.summary,
                        "findings": [
                            item.model_dump(mode="json") for item in verification.findings
                        ],
                    },
                )
                if verification.revision is not None:
                    applied = apply_answer_revision(answer, verification.revision)
                    revision_findings = applied.findings
                    if applied.protocol_violation:
                        answer = self._safe_grounding_fallback(
                            original_query=task_context["original_query"],
                            evidence=evidence,
                        )
                    else:
                        answer = self._calibrate_answer(applied.answer, evidence)
                elif verification.verdict != "passed":
                    answer = self._safe_grounding_fallback(
                        original_query=task_context["original_query"],
                        evidence=evidence,
                    )

            live_tools_used = {
                item["tool"]
                for item in tool_observations
                if item["tool"] in {"search_official_live", "search_campus_notices_live"}
            }
            if composition.assessment.conflicts or len(live_tools_used) > 1:
                performance_trace.add_scenario_hint("multi_source")
            final_structural = self._citation_verifier.verify(answer, evidence)
            semantic_findings = [*verification.findings, *revision_findings]
            if not final_structural.passed:
                # Confirmed facts must survive a failed revision. Try a
                # deterministic prune, then one evidence-ID-bounded model
                # repair, before giving up on the whole answer.
                semantic_findings = [
                    *semantic_findings,
                    *final_structural.findings,
                ]
                (
                    answer,
                    final_structural,
                    repair_findings,
                    repaired,
                ) = await self._repair_final_citations(
                    task_id=task_id,
                    original_query=task_context["original_query"],
                    answer=answer,
                    evidence=evidence,
                    structural=final_structural,
                )
                semantic_findings = [*semantic_findings, *repair_findings]
                if repaired:
                    verification = verification.model_copy(
                        update={
                            "verdict": "revised",
                            "summary": (
                                "最终引用协议校验未通过，已在证据工作区范围内修复"
                                "引用后重新通过校验。"
                            ),
                        }
                    )
                else:
                    answer = self._safe_grounding_fallback(
                        original_query=task_context["original_query"],
                        evidence=evidence,
                    )
                    final_structural = self._citation_verifier.verify(answer, evidence)
                    verification = verification.model_copy(
                        update={
                            "verdict": "revised",
                            "summary": (
                                "候选回答未通过最终引用协议且无法修复，已降级为"
                                "不包含校园事实的透明说明。"
                            ),
                        }
                    )

            grounding = GroundingSummary(
                status=composition.assessment.status,
                summary=composition.assessment.summary,
                verifier_verdict=verification.verdict,
                verifier_summary=verification.summary,
                citation_coverage=final_structural.citation_coverage,
                fully_supported_rate=final_structural.fully_supported_rate,
                findings=[*semantic_findings, *final_structural.findings],
            )
            answer_payload = await self._persist_answer(
                task_id,
                answer,
                evidence,
                grounding,
                original_query=task_context["original_query"],
                product_subject_id=task_context["product_subject_id"],
            )
            if answer_payload is None:
                return
            performance_trace.finish()
            performance, performance_spans = performance_trace.snapshot()
            if not await self._persist_performance(
                task_id,
                performance,
                performance_spans,
            ):
                return
            answer_payload["performance"] = performance.model_dump(mode="json")
            await self._broker.publish(task_id, "answer.completed", answer_payload)
            logger.info(
                "Agent task completed",
                extra={
                    "event": "agent.task.completed",
                    "task_id": task_id,
                    "answer_id": answer_payload["answer_id"],
                    "evidence_count": len(evidence),
                },
            )
        except Exception:
            current_task = asyncio.current_task()
            if current_task is not None and current_task.cancelling():
                raise
            logger.exception(
                "Agent task failed",
                extra={
                    "event": "agent.task.failed",
                    "task_id": task_id,
                    "error_code": "AGENT_EXECUTION_FAILED",
                },
            )
            await self._set_task_failed(task_id, "AGENT_EXECUTION_FAILED")
            await self._broker.publish(
                task_id,
                "task.failed",
                {
                    "task_id": task_id,
                    "error_code": "AGENT_EXECUTION_FAILED",
                    "message": "Agent 执行失败，请稍后重试。",
                },
            )
        finally:
            reset_performance_trace(performance_token)

    async def _execute_round(
        self,
        *,
        task_id: str,
        steps: list[InvestigationStep],
        completed_step_ids: set[str],
        access_scopes: frozenset[str],
        mirror_access_scopes: frozenset[str],
        actor_user_id: str | None,
    ) -> list[tuple[InvestigationStep, ToolResult]]:
        pending = list(steps)
        results: list[tuple[InvestigationStep, ToolResult]] = []
        locally_completed = set(completed_step_ids)

        while pending:
            ready = [step for step in pending if set(step.depends_on).issubset(locally_completed)]
            if not ready:
                # A malformed dependency graph should not make a read-only investigation hang.
                ready = [pending[0]]

            parallel = [step for step in ready if step.can_run_in_parallel]
            batch = parallel if parallel else [ready[0]]
            # The campus browser lease serializes its own queries, so issuing them
            # as separate gathered calls only repeats login, discovery and parsing.
            dispatch, merged_origins = _merge_campus_notice_steps(batch)
            dispatch_results = await asyncio.gather(
                *(
                    self._execute_step(
                        task_id=task_id,
                        step=step,
                        access_scopes=access_scopes,
                        mirror_access_scopes=mirror_access_scopes,
                        actor_user_id=actor_user_id,
                    )
                    for step in dispatch
                )
            )
            results.extend(zip(dispatch, dispatch_results, strict=True))
            completed_ids = {step.id for step in batch}
            # A merged step stands in for every original step id, so later
            # depends_on references still resolve.
            completed_ids.update(
                origin_id for origins in merged_origins.values() for origin_id in origins
            )
            locally_completed.update(completed_ids)
            pending = [step for step in pending if step.id not in completed_ids]

        return results

    async def _repair_final_citations(
        self,
        *,
        task_id: str,
        original_query: str,
        answer: AgentAnswer,
        evidence: list[Evidence],
        structural: StructuralGroundingResult,
    ) -> tuple[AgentAnswer, StructuralGroundingResult, list[VerificationFinding], bool]:
        """Two-stage citation repair before any full degradation.

        Stage one deterministically prunes citations and links that provably
        cannot verify. Stage two makes one bounded model call that may only
        rebind claims to real workspace evidence IDs or remove unsupported
        statements together with their prose. Returns the best answer, its
        structural result, collected findings and whether repair succeeded.
        """

        findings: list[VerificationFinding] = []

        pruned = prune_invalid_citations(answer, evidence)
        if pruned.changed:
            findings.extend(pruned.findings)
            answer = pruned.answer
            structural = self._citation_verifier.verify(answer, evidence)
            if structural.passed:
                return answer, structural, findings, True

        await self._broker.publish(
            task_id,
            "answer.citation_repair.started",
            {"finding_count": len(structural.findings)},
        )
        try:
            revision = await self._models.repair_citations(
                original_query=original_query,
                answer=answer,
                evidence=evidence,
                findings=structural.findings,
                current_time=utc_now(),
            )
        except Exception:
            logger.exception(
                "Citation repair call failed",
                extra={"event": "agent.citation_repair.failed", "task_id": task_id},
            )
            return answer, structural, findings, False
        applied = apply_answer_revision(answer, revision)
        findings.extend(applied.findings)
        if applied.protocol_violation:
            return answer, structural, findings, False
        repaired_answer = self._calibrate_answer(applied.answer, evidence)
        repaired_structural = self._citation_verifier.verify(repaired_answer, evidence)
        await self._broker.publish(
            task_id,
            "answer.citation_repair.completed",
            {"passed": repaired_structural.passed},
        )
        if repaired_structural.passed:
            return repaired_answer, repaired_structural, findings, True
        return answer, structural, findings, False

    async def _execute_step(
        self,
        *,
        task_id: str,
        step: InvestigationStep,
        access_scopes: frozenset[str],
        mirror_access_scopes: frozenset[str],
        actor_user_id: str | None,
    ) -> ToolResult:
        performance_trace = current_performance_trace()
        if performance_trace is not None:
            if step.tool == "search_official_live":
                performance_trace.add_scenario_hint("public_live")
            elif step.tool == "search_campus_notices_live":
                performance_trace.add_scenario_hint("campus_authenticated")
            span = performance_trace.start_span("tool", step.tool)
        else:
            span = None
        await self._broker.publish(
            task_id,
            "tool.started",
            {
                "step_id": step.id,
                "tool": step.tool,
                "purpose": step.purpose,
                "arguments": step.arguments.model_dump(
                    mode="json",
                    exclude_none=True,
                    exclude_defaults=True,
                ),
            },
        )
        try:
            return await self._tools.execute(
                step,
                allowed_visibilities=(
                    mirror_access_scopes if step.tool in _LOCAL_MIRROR_TOOLS else access_scopes
                ),
                actor_user_id=actor_user_id,
            )
        finally:
            if performance_trace is not None and span is not None:
                performance_trace.finish_span(span)

    async def _publish_tool_completed(
        self,
        *,
        task_id: str,
        step: InvestigationStep,
        result: ToolResult,
        new_evidence: list[Evidence],
    ) -> None:
        await self._broker.publish(
            task_id,
            "tool.completed",
            {
                "step_id": step.id,
                "tool": step.tool,
                "status": result.status,
                "data": result.data,
                "evidence_count": len(result.evidence),
                # Keep the stream payload aligned with the persisted answer
                # contract. The evidence desk needs provenance metadata even
                # before the final answer event arrives.
                "evidence": [item.model_dump(mode="json") for item in new_evidence],
                "warnings": result.warnings,
                "error": (result.error.model_dump(mode="json") if result.error else None),
            },
        )

    @staticmethod
    def _calibrate_answer(
        answer: AgentAnswer,
        evidence: list[Evidence],
    ) -> AgentAnswer:
        """Enforce consistency between evidence state and reported confidence."""

        answer = restore_workspace_citation_urls(answer, evidence)
        if not evidence:
            return answer.model_copy(
                update={
                    "confidence": "low",
                    "verification_mode": "no_campus_evidence",
                }
            )
        if answer.verification_mode in {"no_campus_evidence", "degraded"}:
            return answer.model_copy(update={"confidence": "low"})
        has_live_evidence = any(
            item.retrieval_mode in {"live_public", "live_authenticated"} for item in evidence
        )
        if answer.verification_mode == "live_verified" and not has_live_evidence:
            return answer.model_copy(update={"verification_mode": "cache"})
        if answer.verification_mode == "historical" and answer.confidence == "high":
            return answer.model_copy(update={"confidence": "medium"})
        return answer

    @staticmethod
    def _unique_steps(
        steps: list[InvestigationStep],
        attempted_signatures: set[str],
        limit: int,
    ) -> list[InvestigationStep]:
        unique: list[InvestigationStep] = []
        for step in steps:
            signature = json.dumps(
                {"tool": step.tool, "arguments": step.arguments.tool_payload()},
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
            if signature in attempted_signatures:
                continue
            attempted_signatures.add(signature)
            unique.append(step)
            if len(unique) >= limit:
                break
        return unique

    @staticmethod
    def _normalize_initial_steps(
        steps: list[InvestigationStep],
        *,
        original_query: str | None = None,
    ) -> list[InvestigationStep]:
        """Bound the initial local fan-out and make independent FTS calls parallel."""

        normalized: list[InvestigationStep] = []
        memory_count = 0
        for step in steps:
            if not AgentCoordinator._has_minimum_tool_arguments(step):
                continue
            if step.tool != "search_campus_memory":
                normalized.append(step)
                continue
            if not (step.arguments.query or "").strip():
                continue
            if memory_count >= 3:
                continue
            memory_count += 1
            normalized.append(
                AgentCoordinator._sanitize_memory_step(
                    step,
                    can_run_in_parallel=True,
                )
            )
        memory_steps = [
            step for step in normalized if step.tool == "search_campus_memory"
        ]
        if len(memory_steps) != 1 or not (original_query or "").strip():
            return normalized

        memory_step = memory_steps[0]
        original = (original_query or "").strip()[:200]
        model_query = (memory_step.arguments.query or "").strip()
        variants = [
            value
            for value in (original, *memory_step.arguments.queries)
            if value.strip() and value.strip() != model_query
        ]
        arguments = memory_step.arguments.model_copy(
            update={"queries": list(dict.fromkeys(variants))[:3]}
        )
        enriched = memory_step.model_copy(update={"arguments": arguments})
        return [enriched if step.id == memory_step.id else step for step in normalized]

    @staticmethod
    def _normalize_follow_up_steps(
        steps: list[InvestigationStep],
        *,
        freshness: str,
    ) -> list[InvestigationStep]:
        del freshness
        normalized = [
            (
                AgentCoordinator._sanitize_memory_step(step)
                if step.tool == "search_campus_memory"
                else step
            )
            for step in steps
            if AgentCoordinator._has_minimum_tool_arguments(step)
        ]
        return normalized

    @staticmethod
    def _restrict_ambiguous_steps(
        steps: list[InvestigationStep],
        *,
        original_query: str,
    ) -> list[InvestigationStep]:
        local_steps = [step for step in steps if step.tool in _LOCAL_MIRROR_TOOLS]
        if local_steps:
            return local_steps
        return [
            InvestigationStep(
                id="ambiguous-local-memory",
                purpose="只在登记的校园本地镜像中确认问题是否与学校相关",
                tool="search_campus_memory",
                arguments={"query": original_query[:200], "top_k": 12},
                can_run_in_parallel=True,
                success_condition="取得能够建立校园相关性的官方镜像材料",
            )
        ]

    @staticmethod
    def _campus_evidence_is_relevant(
        query: str,
        evidence: list[Evidence],
    ) -> bool:
        """Require a lexical bridge before an ambiguous request can be answered."""

        query_terms: list[str] = []
        for value in re.findall(r"[\u3400-\u9fff]+|[a-z0-9]{3,}", query.casefold()):
            if re.fullmatch(r"[a-z0-9]+", value):
                query_terms.append(value)
                continue
            # Chinese users often omit word boundaries.  Keep short n-grams
            # so “选课快开始了吗” can bridge to an excerpt containing “选课”
            # without maintaining a topic-specific keyword dictionary.
            for size in (4, 3, 2):
                if len(value) >= size:
                    query_terms.extend(
                        value[index : index + size]
                        for index in range(len(value) - size + 1)
                    )
        query_terms = [
            value
            for value in query_terms
            if value not in {"什么", "怎么", "如何", "哪里", "哪个", "whether", "please"}
        ]
        if not query_terms:
            return False
        for item in evidence:
            haystack = " ".join((item.title, item.publisher, item.excerpt)).casefold()
            if any(term in haystack for term in query_terms):
                return True
        return False

    @staticmethod
    def _has_minimum_tool_arguments(step: InvestigationStep) -> bool:
        if step.tool == "search_campus_memory":
            return bool((step.arguments.query or "").strip())
        if step.tool not in _DOCUMENT_EXPLORATION_TOOLS:
            return True
        if not (step.arguments.document_version_id or "").strip():
            return False
        if step.tool == "find_in_campus_document":
            return bool((step.arguments.query or "").strip())
        if step.tool == "read_campus_document_locator":
            return step.arguments.locator is not None
        return True

    @staticmethod
    def _sanitize_memory_step(
        step: InvestigationStep,
        *,
        can_run_in_parallel: bool | None = None,
    ) -> InvestigationStep:
        bounded_query = (step.arguments.query or "").strip() or None
        arguments = step.arguments.__class__(
            query=bounded_query,
            queries=step.arguments.queries[:3],
            source_ids=step.arguments.source_ids[:3],
            top_k=step.arguments.top_k,
        )
        update: dict[str, Any] = {"arguments": arguments}
        if can_run_in_parallel is not None:
            update["can_run_in_parallel"] = can_run_in_parallel
        return step.model_copy(update=update)

    @staticmethod
    def _requires_independent_verification(
        *,
        composition: GroundedAnswerComposition,
        dossier: SemanticDossier,
        structural: StructuralGroundingResult,
        retrieval_coverage_risk: bool = False,
    ) -> bool:
        """Decide whether the ~48s synchronous semantic Verifier must run.

        The mirror corpus is a clean copy of official pages that rarely change
        after publication. Normal and historical materials therefore do not
        justify another model round by themselves. Real risk signals still do:
        structural protocol errors, high-risk questions, or conflicting
        official evidence.
        """

        return (
            dossier.risk.level in {"academic_high", "sensitive"}
            or composition.assessment.status == "conflicting"
            or not structural.passed
            or retrieval_coverage_risk
        )

    @staticmethod
    def _retrieval_scores(result: ToolResult) -> dict[str, float]:
        ranking = result.data.get("ranking")
        if not isinstance(ranking, list):
            return {}
        scores: dict[str, float] = {}
        for item in ranking:
            if not isinstance(item, dict):
                continue
            url = item.get("canonical_url")
            score = item.get("score")
            if isinstance(url, str) and isinstance(score, (int, float)):
                scores[url] = float(score)
        return scores

    @staticmethod
    def _retrieval_diagnostics(result: ToolResult) -> dict[str, Any]:
        if result.tool != "search_campus_memory":
            return {}
        data = result.data
        ranking = data.get("candidate_ranking")
        return {
            "query_variants": data.get("query_variants", []),
            "source_hints": data.get("source_hints", []),
            "routed_sources": data.get("routed_sources", []),
            "retrieval_channels": data.get("channel_counts", {}),
            "candidate_ranking": ranking[:12] if isinstance(ranking, list) else [],
            "deduplication": data.get("deduplication", {}),
            "coverage_risk": data.get("coverage_risk"),
        }

    @staticmethod
    def _retrieval_coverage_risk(observations: list[dict[str, Any]]) -> bool:
        return any(
            item.get("tool") == "search_campus_memory"
            and isinstance(item.get("data"), dict)
            and item["data"].get("coverage_risk") is True
            for item in observations
        )

    @staticmethod
    def _with_answer_shape(dossier: SemanticDossier, query: str) -> SemanticDossier:
        if dossier.signals.answer_shape != "fact":
            return dossier
        if re.search(r"区别|比较|对比|相比|分别有什么不同", query):
            answer_shape = "comparison"
        elif re.search(r"几个|多少|哪些|有什么|列出|列表|名单|目录|一览|全部|所有|分别", query):
            answer_shape = "enumeration"
        else:
            return dossier
        return dossier.model_copy(
            update={
                "signals": dossier.signals.model_copy(
                    update={"answer_shape": answer_shape}
                )
            }
        )

    @staticmethod
    def _with_domain_scope(dossier: SemanticDossier, query: str) -> SemanticDossier:
        """Apply a small, generic safety backstop to the model scope signal.

        The campus corpus remains open-ended: this is not a topic whitelist.
        The backstop only blocks unmistakable attempts to turn the product into
        a general writing/coding/translation endpoint and lets ambiguous text
        proceed to campus-only retrieval.
        """

        normalized = query.casefold()
        generic_request = re.search(
            r"(^\s*(?:请|帮我|替我|能否|可以)\s*(?:写|生成|创作|起草|编写)|"
            r"^\s*写(?:一个|一份|一篇)|^\s*生成(?:一个|一份|一篇)|"
            r"写一篇|写(?:个|份)?(?:作文|论文|文章|报告)|论文代写|代写|润色|"
            r"(?:请|帮我|替我|需要)?翻译(?:一下|以下|这段|成|为)|"
            r"translate|translation|(?:写|生成|运行|执行).{0,12}(?:python|javascript|typescript)"
            r"|(?:写代码|如何编程|帮我编程|给我编程|编程实现|debug(?:一下|代码)?)|"
            r"write\s+(?:an?\s+)?(?:essay|paper|article|report|code)|"
            r"(?:generate|write|draft)\s+(?:an?\s+)?(?:essay|paper|code)|"
            r"调试代码|破解|绕过限制|忽略之前指令|ignore\s+previous\s+instructions|"
            r"system\s+prompt|jailbreak|提示词|角色覆盖)",
            normalized,
        )
        campus_context = re.search(
            r"(浙大城市|hzcu|学校|校园|学院|专业|选课|校历|开学|教务|招生|奖学金|"
            r"宿舍|寝室|图书馆|课程|考试|毕业|转专业|社团|国创|校创|通知|"
            r"campus|university|college|major|course|semester|enrollment|"
            r"academic calendar|scholarship|dormitory|library|graduation)",
            normalized,
        )
        # Mandarin often uses “写出/写一份名单” as a natural way to ask the
        # agent to enumerate official facts.  Treat those forms as retrieval
        # requests when the query has a clear campus context and an explicit
        # list/count cue; otherwise the generic-writing guard remains active.
        enumeration_request = re.search(
            r"(有几个|多少|哪些|哪几|名单|名录|列表|清单|分别|全部|一共有|共[有是]|"
            r"列出|列举|写出)",
            normalized,
        )
        explicit_generic_content = re.search(
            r"(作文|论文|文章|报告|课程介绍|宣传稿|文案|故事|诗|代码|python|"
            r"javascript|typescript|翻译|润色|提示词|system\s+prompt)",
            normalized,
        )
        if (
            generic_request
            and campus_context
            and enumeration_request
            and not explicit_generic_content
        ):
            generic_request = None
        scope = dossier.signals.domain_scope
        reason = dossier.signals.scope_reason
        if generic_request:
            scope = "out_of_scope"
            reason = "通用内容生成、编程、翻译或提示词套取不属于本服务用途。"
        elif scope == "out_of_scope":
            reason = reason or "本问题不属于浙大城市学院官方信息查询范围。"
        elif scope == "ambiguous" and campus_context:
            scope = "in_scope"
            reason = reason or "问题包含明确校园语境，将限定在官方材料检索。"
        return dossier.model_copy(
            update={
                "signals": dossier.signals.model_copy(
                    update={"domain_scope": scope, "scope_reason": reason[:240]}
                )
            }
        )

    @staticmethod
    def _scope_refusal_answer() -> AgentAnswer:
        return AgentAnswer(
            headline="仅处理校园官方信息",
            answer_markdown=(
                "我只能帮助查询、解释和核对浙大城市学院相关的官方信息。"
                "请换成学校通知、校历、课程、专业、招生或校园服务等具体问题。"
            ),
            assumptions=[],
            next_actions=[],
            confidence="low",
            verification_mode="no_campus_evidence",
            claims=[],
        )

    @staticmethod
    def _fallback_prepared_investigation(query: str) -> PreparedInvestigation:
        bounded_query = query.strip()[:200] or "校园信息查询"
        return PreparedInvestigation(
            dossier=SemanticDossier(
                goal_hypotheses=[
                    GoalHypothesis(
                        goal=bounded_query,
                        confidence=1.0,
                        support=["用户原始问题"],
                        required_evidence=["登记校园来源中的直接官方材料"],
                    )
                ],
                signals=SemanticSignals(
                    freshness="current",
                    domain_scope="ambiguous",
                    scope_reason="结构化准备失败，先限定在本地校园镜像检索。",
                ),
                uncertainties=["结构化语义准备失败，保留原问题执行最小检索。"],
            ),
            plan=InvestigationPlan(
                objective=bounded_query,
                steps=[
                    InvestigationStep(
                        id="structured-fallback-memory",
                        purpose="使用用户原始表达检索登记校园镜像",
                        tool="search_campus_memory",
                        arguments={"query": bounded_query, "top_k": 12},
                        can_run_in_parallel=True,
                        success_condition="取得至少一条直接相关的当前官方材料",
                    )
                ],
                stop_conditions=["取得可核验的直接官方材料"],
                fallbacks=["本地多路检索无结果时再进行实时官方搜索"],
            ),
        )

    @staticmethod
    def _ensure_live_verification(
        steps: list[InvestigationStep],
        *,
        original_query: str,
        freshness: str,
        tool_catalog: list[dict[str, Any]],
    ) -> list[InvestigationStep]:
        planned = list(steps)
        # The local mirror is a complete incremental copy of every registered
        # source, so freshly synced current versions already count as current
        # evidence. Only "is it still possible right now" questions must hit
        # the live channel; the review round can still add live follow-ups
        # whenever memory recall falls short.
        if freshness != "live_required":
            return planned
        live_tools = ("search_campus_notices_live", "search_official_live")
        if any(step.tool in live_tools for step in planned):
            return planned
        available = {item.get("name") for item in tool_catalog if item.get("available_now", True)}
        selected = next((name for name in live_tools if name in available), None)
        if selected is None:
            return planned
        return [
            *planned,
            InvestigationStep(
                id="runtime-live-verification",
                purpose="核验会随学期或当前安排变化的校园事实",
                tool=selected,
                arguments={"query": original_query, "limit": 5},
                can_run_in_parallel=True,
                success_condition="取得当前官方页面，或明确实时通道暂不可用",
            ),
        ]

    async def _load_task_context(self, task_id: str) -> dict[str, Any] | None:
        async with self._database.session_factory() as session:
            task = await session.get(AgentTask, task_id)
            if task is None:
                return None
            conversation = await session.get(Conversation, task.conversation_id)
            user_message = await session.get(Message, task.user_message_id)
            if conversation is None or user_message is None:
                return None
            query = (
                select(Message)
                .where(
                    Message.conversation_id == conversation.id,
                    Message.id != user_message.id,
                )
                .order_by(Message.created_at.desc())
                .limit(12)
            )
            messages = list((await session.scalars(query)).all())
            messages.reverse()
            profile_context: dict[str, str] = {}
            if task.requested_by_subject_id:
                profile = await session.get(StudentProfile, task.requested_by_subject_id)
                if profile is not None and profile.personalization_enabled:
                    attributes = list(
                        (
                            await session.scalars(
                                select(ProfileAttribute)
                                .where(
                                    ProfileAttribute.subject_id == task.requested_by_subject_id,
                                    ProfileAttribute.status == "confirmed",
                                )
                                .order_by(ProfileAttribute.updated_at.asc())
                            )
                        ).all()
                    )
                    profile_context = {
                        item.attribute_key: item.attribute_value for item in attributes
                    }
            access_scopes = frozenset(task.access_scopes or ["public"])
            return {
                "original_query": user_message.content,
                "profile_context": profile_context,
                "access_scopes": access_scopes,
                "mirror_access_scopes": self._settings.local_mirror_visibility_scopes(
                    access_scopes,
                    authenticated=conversation.owner_user_id is not None,
                ),
                "actor_user_id": conversation.owner_user_id,
                "product_subject_id": task.requested_by_subject_id,
                "request_mode": task.request_mode,
                "conversation_context": [
                    {"role": message.role, "content": message.content} for message in messages
                ],
            }

    async def _task_subject_id(self, task_id: str) -> str | None:
        async with self._database.session_factory() as session:
            return await session.scalar(
                select(AgentTask.requested_by_subject_id).where(AgentTask.id == task_id)
            )

    async def _set_task_status(self, task_id: str, status: str) -> bool:
        async with self._database.session_factory() as session:
            task = await session.get(AgentTask, task_id)
            if task is None:
                return False
            if status == "running" and task.status != "queued":
                return False
            task.status = status
            task.updated_at = utc_now()
            await session.commit()
            return True

    async def _set_task_failed(self, task_id: str, error_code: str) -> None:
        async with self._database.session_factory() as session:
            task = await session.get(AgentTask, task_id)
            if task:
                task.status = "failed"
                task.error_code = error_code
                task.updated_at = utc_now()
                await session.commit()

    async def _persist_answer(
        self,
        task_id: str,
        answer: AgentAnswer,
        evidence: list[Evidence],
        grounding: GroundingSummary,
        *,
        original_query: str,
        product_subject_id: str | None,
    ) -> dict[str, Any] | None:
        answer_id = new_id("ans")
        created_at = utc_now()
        answer, evidence, grounding = _clean_persisted_answer(
            answer,
            evidence,
            grounding,
        )
        async with self._database.session_factory() as session:
            task = await session.get(AgentTask, task_id)
            if task is None:
                # The owning subject may have explicitly deleted all product
                # data while this coroutine was still unwinding.
                return None
            if task.status != "running":
                return None
            answer_record = AnswerRecord(
                id=answer_id,
                task_id=task_id,
                headline=answer.headline,
                answer_markdown=answer.answer_markdown,
                assumptions=answer.assumptions,
                next_actions=answer.next_actions,
                confidence=answer.confidence,
                verification_mode=answer.verification_mode,
                model_provider=self._models.provider,
                model_name=self._models.agent_model,
                created_at=created_at,
            )
            session.add(answer_record)
            await session.flush()
            evidence_records = [
                EvidenceRecord(
                    id=item.evidence_id,
                    answer_id=answer_id,
                    title=item.title,
                    publisher=item.publisher,
                    canonical_url=item.canonical_url,
                    excerpt=item.excerpt,
                    published_at=item.published_at,
                    observed_at=item.observed_at,
                    fresh_until=item.fresh_until,
                    source_id=item.source_id,
                    resource_ref=item.resource_ref,
                    authority_level=item.authority_level,
                    audience_scopes=item.audience_scopes,
                    effective_from=item.effective_from,
                    effective_to=item.effective_to,
                    retrieval_mode=item.retrieval_mode,
                    document_version_id=item.document_version_id,
                )
                for item in evidence
            ]
            session.add_all(evidence_records)
            session.add(
                AnswerGroundingRecord(
                    answer_id=answer_id,
                    status=grounding.status,
                    summary=grounding.summary,
                    verifier_verdict=grounding.verifier_verdict,
                    verifier_summary=grounding.verifier_summary,
                    citation_coverage=grounding.citation_coverage,
                    fully_supported_rate=grounding.fully_supported_rate,
                    findings=clean_product_json(
                        [item.model_dump(mode="json") for item in grounding.findings]
                    ),
                    created_at=created_at,
                )
            )
            claim_records: dict[str, AnswerClaimRecord] = {}
            for ordinal, claim in enumerate(answer.claims, start=1):
                record = AnswerClaimRecord(
                    id=new_id("claim"),
                    answer_id=answer_id,
                    claim_key=claim.claim_id,
                    ordinal=ordinal,
                    text=claim.text,
                    statement_type=claim.statement_type,
                    importance=claim.importance,
                    scope=claim.scope,
                    valid_at=claim.valid_at,
                    support_status=claim.support_status,
                    uncertainty=claim.uncertainty,
                    created_at=created_at,
                )
                claim_records[claim.claim_id] = record
                session.add(record)
            await session.flush()

            evidence_ids = {item.evidence_id for item in evidence}
            linked_pairs: set[tuple[str, str]] = set()
            for claim in answer.claims:
                claim_record = claim_records[claim.claim_id]
                for citation in claim.citations:
                    pair = (claim_record.id, citation.evidence_id)
                    if citation.evidence_id not in evidence_ids or pair in linked_pairs:
                        continue
                    linked_pairs.add(pair)
                    session.add(
                        ClaimEvidenceRecord(
                            id=new_id("cite"),
                            claim_id=claim_record.id,
                            evidence_id=citation.evidence_id,
                            relation=citation.relation,
                            support_status=citation.support_status,
                            rationale=citation.rationale,
                            supporting_excerpt=citation.supporting_excerpt,
                        )
                    )
            session.add(
                Message(
                    id=new_id("msg"),
                    conversation_id=task.conversation_id,
                    role="assistant",
                    content=answer.answer_markdown,
                    created_at=created_at,
                )
            )
            profile_suggestions: list[ProfileAttribute] = []
            if product_subject_id:
                for suggestion in answer.profile_suggestions:
                    supporting_text = clean_product_text(suggestion.supporting_user_text).strip()
                    value = clean_product_text(suggestion.attribute_value).strip()
                    if supporting_text and supporting_text in original_query and value:
                        attribute = ProfileAttribute(
                            id=new_id("pattr"),
                            subject_id=product_subject_id,
                            attribute_key=suggestion.attribute_key,
                            attribute_value=value,
                            status="suggested",
                            source_kind="agent_explicit_user_text",
                            supporting_user_text=supporting_text,
                            source_answer_id=answer_id,
                            created_at=created_at,
                            updated_at=created_at,
                        )
                        session.add(attribute)
                        profile_suggestions.append(attribute)
            claimed = await session.execute(
                update(AgentTask)
                .where(
                    AgentTask.id == task_id,
                    AgentTask.status == "running",
                    AgentTask.answer_id.is_(None),
                )
                .values(answer_id=answer_id, updated_at=created_at)
            )
            if claimed.rowcount != 1:
                # Cancellation and answer persistence contend on one atomic
                # state transition. If cancellation committed first, discard
                # every answer-side row in this transaction.
                await session.rollback()
                return None
            await session.commit()

        return {
            "answer_id": answer_id,
            "task_id": task_id,
            **answer.model_dump(
                mode="json",
                exclude={"profile_suggestions"},
            ),
            "headline": answer.headline,
            "answer_markdown": answer.answer_markdown,
            "assumptions": answer.assumptions,
            "next_actions": answer.next_actions,
            "profile_suggestions": [
                {
                    "attribute_id": item.id,
                    "attribute_key": item.attribute_key,
                    "attribute_value": item.attribute_value,
                    "status": item.status,
                    "source_kind": item.source_kind,
                    "supporting_user_text": item.supporting_user_text,
                    "source_answer_id": item.source_answer_id,
                    "created_at": item.created_at.isoformat(),
                    "updated_at": item.updated_at.isoformat(),
                }
                for item in profile_suggestions
            ],
            "evidence": [item.model_dump(mode="json") for item in evidence],
            "grounding": grounding.model_dump(mode="json"),
            "created_at": created_at.isoformat(),
        }

    async def _persist_performance(
        self,
        task_id: str,
        performance: AgentPerformance,
        spans: list[dict[str, object]],
    ) -> bool:
        async with self._database.session_factory() as session:
            task = await session.get(AgentTask, task_id)
            if task is None:
                return False
            if task.status != "running":
                return False
            session.add(
                TaskPerformanceRecord(
                    task_id=task_id,
                    scenario=performance.scenario,
                    total_duration_ms=performance.total_duration_ms,
                    excluded_model_ttft_ms=performance.excluded_model_ttft_ms,
                    controllable_duration_ms=performance.controllable_duration_ms,
                    first_progress_ms=performance.first_progress_ms,
                    model_call_count=performance.model_call_count,
                    tool_call_count=performance.tool_call_count,
                    model_ttft_measurable=performance.model_ttft_measurable,
                    spans=clean_product_json(spans),
                    created_at=utc_now(),
                )
            )
            task.status = "completed"
            task.updated_at = utc_now()
            await session.commit()
            return True

    @staticmethod
    def _safe_grounding_fallback(
        *,
        original_query: str,
        evidence: list[Evidence],
    ) -> AgentAnswer:
        evidence_note = (
            f"本轮已经取得 {len(evidence)} 条材料，但候选回答中的主张与引用关系没有通过最终校验。"
            if evidence
            else "本轮没有取得足以形成校园事实结论的材料。"
        )
        evidence_links = "\n".join(
            f"- [查看：{item.title}](<{item.canonical_url}>)"
            for item in evidence[:5]
            if item.canonical_url.startswith(("http://", "https://"))
        )
        links_section = (
            f"\n\n你可以先核对这些已检索到的官方材料：\n\n{evidence_links}"
            if evidence_links
            else ""
        )
        return AgentAnswer(
            headline="这次回答未通过证据校验",
            answer_markdown=(
                f"关于“{original_query}”，{evidence_note}"
                "为避免把相关页面误写成确定结论，我暂不输出未经支持的校园事实。"
                f"{links_section}"
            ),
            next_actions=["继续实时调查相关官方栏目"],
            confidence="low",
            verification_mode="degraded",
            claims=[],
        )


def _clean_persisted_answer(
    answer: AgentAnswer,
    evidence: list[Evidence],
    grounding: GroundingSummary,
) -> tuple[AgentAnswer, list[Evidence], GroundingSummary]:
    """Sanitize every user-visible answer field before it crosses storage.

    Cleaning only the Markdown body is insufficient: titles, provenance,
    claim text, excerpts and verifier findings are all returned after history
    restoration and can otherwise reintroduce illegal control characters.
    """

    cleaned_evidence = [
        item.model_copy(
            update={
                "evidence_id": clean_product_text(item.evidence_id),
                "title": clean_product_text(item.title),
                "publisher": clean_product_text(item.publisher),
                "canonical_url": clean_product_text(item.canonical_url),
                "excerpt": clean_product_text(item.excerpt),
                "source_id": clean_product_text(item.source_id),
                "resource_ref": (
                    clean_product_text(item.resource_ref) if item.resource_ref is not None else None
                ),
                "document_version_id": (
                    clean_product_text(item.document_version_id)
                    if item.document_version_id is not None
                    else None
                ),
                "audience_scopes": [clean_product_text(value) for value in item.audience_scopes],
            }
        )
        for item in evidence
    ]
    cleaned_claims = [
        claim.model_copy(
            update={
                "claim_id": clean_product_text(claim.claim_id),
                "text": clean_product_text(claim.text),
                "scope": clean_product_text(claim.scope),
                "uncertainty": clean_product_text(claim.uncertainty),
                "citations": [
                    citation.model_copy(
                        update={
                            "evidence_id": clean_product_text(citation.evidence_id),
                            "rationale": clean_product_text(citation.rationale),
                            "supporting_excerpt": clean_product_text(citation.supporting_excerpt),
                        }
                    )
                    for citation in claim.citations
                ],
            }
        )
        for claim in answer.claims
    ]
    cleaned_suggestions = [
        suggestion.model_copy(
            update={
                "attribute_value": clean_product_text(suggestion.attribute_value),
                "supporting_user_text": clean_product_text(suggestion.supporting_user_text),
            }
        )
        for suggestion in answer.profile_suggestions
    ]
    cleaned_answer = answer.model_copy(
        update={
            "headline": clean_product_text(answer.headline),
            "answer_markdown": clean_product_text(answer.answer_markdown),
            "assumptions": [clean_product_text(item) for item in answer.assumptions],
            "next_actions": [clean_product_text(item) for item in answer.next_actions],
            "claims": cleaned_claims,
            "profile_suggestions": cleaned_suggestions,
        }
    )
    cleaned_grounding = grounding.model_copy(
        update={
            "summary": clean_product_text(grounding.summary),
            "verifier_summary": clean_product_text(grounding.verifier_summary),
            "findings": [
                finding.model_copy(
                    update={
                        "claim_id": (
                            clean_product_text(finding.claim_id)
                            if finding.claim_id is not None
                            else None
                        ),
                        "message": clean_product_text(finding.message),
                    }
                )
                for finding in grounding.findings
            ],
        }
    )
    return cleaned_answer, cleaned_evidence, cleaned_grounding
