from datetime import timedelta
from types import SimpleNamespace

from hzcu_agent.models import new_id, utc_now
from hzcu_agent.schemas import (
    AgentAnswer,
    AnswerClaim,
    AnswerRevision,
    ClaimCitation,
    ClaimPatch,
    Evidence,
    GoalHypothesis,
    GroundedAnswerComposition,
    GroundingAssessment,
    InvestigationStep,
    RiskAssessment,
    SemanticDossier,
    VerificationFinding,
)
from hzcu_agent.services.coordinator import AgentCoordinator, _merge_campus_notice_steps
from hzcu_agent.services.evidence_workspace import EvidenceWorkspace
from hzcu_agent.services.grounding import (
    CitationVerifier,
    StructuralGroundingResult,
    apply_answer_revision,
    prune_invalid_citations,
    restore_workspace_citation_urls,
)
from hzcu_agent.services.performance import AgentPerformanceTrace, PerformanceSpan
from hzcu_agent.services.stage5_metrics import build_stage5_metrics, nearest_rank
from hzcu_agent.services.tool_gateway import ToolGateway
from hzcu_agent.tools.campus_notices import _should_read_image


def _evidence(url: str, *, excerpt: str = "学生应在规定时间完成选课。") -> Evidence:
    observed_at = utc_now()
    return Evidence(
        evidence_id=new_id("temporary"),
        title="关于新学期选课工作的通知",
        publisher="浙大城市学院教务处",
        canonical_url=url,
        observed_at=observed_at,
        fresh_until=observed_at + timedelta(hours=6),
        excerpt=excerpt,
        source_id="hzcu-jwc",
        authority_level="official",
        retrieval_mode="live_public",
    )


def test_evidence_workspace_keeps_stable_ids_when_newer_page_replaces_old() -> None:
    workspace = EvidenceWorkspace("task_abcdef1234567890")
    original = _evidence("https://jwc.hzcu.edu.cn/notice/1")
    first = workspace.merge([original])[0]
    newer = original.model_copy(
        update={
            "evidence_id": new_id("temporary"),
            "observed_at": original.observed_at + timedelta(minutes=1),
            "excerpt": "更新后的选课安排。",
        }
    )

    replaced = workspace.merge([newer])[0]

    assert first.evidence_id == "ev001_abcdef123456"
    assert replaced.evidence_id == first.evidence_id
    assert workspace.items[0].excerpt == "更新后的选课安排。"


def test_evidence_workspace_merges_query_passages_from_same_document_version() -> None:
    workspace = EvidenceWorkspace("task_abcdef1234567890")
    original = _evidence(
        "https://gc.hzcu.edu.cn/plan.pdf",
        excerpt="计划学制四年，授予学位工学学士。",
    ).model_copy(update={"document_version_id": "docv_plan"})
    first = workspace.merge([original])[0]
    another_query = original.model_copy(
        update={
            "evidence_id": new_id("temporary"),
            "excerpt": "最低毕业学分165.0，设数字建造和智慧管理与运维方向。",
        }
    )

    merged = workspace.merge([another_query])[0]

    assert merged.evidence_id == first.evidence_id
    assert "计划学制四年" in workspace.items[0].excerpt
    assert "最低毕业学分165.0" in workspace.items[0].excerpt


def test_evidence_workspace_preserves_model_selected_full_document() -> None:
    workspace = EvidenceWorkspace("task_abcdef1234567890")
    generic = _evidence(
        "https://gc.hzcu.edu.cn/plan.pdf",
        excerpt="机械电子工程专业培养目标与毕业要求。",
    ).model_copy(update={"document_version_id": "docv_plan"})
    full_text = "【PDF 第 1 页】\n" + ("完整正文内容。" * 1_000) + "\n【PDF 第 20 页】\n文档末尾"
    full_document = generic.model_copy(
        update={
            "evidence_id": new_id("temporary"),
            "excerpt": full_text,
        }
    )

    workspace.merge([generic])
    workspace.merge([full_document])

    excerpt = workspace.items[0].excerpt
    assert "培养目标与毕业要求" in excerpt
    assert "【PDF 第 1 页】" in excerpt
    assert "【PDF 第 20 页】\n文档末尾" in excerpt
    assert len(excerpt) > 3_600


def test_citation_verifier_checks_claim_ids_support_and_urls_without_semantic_regex() -> None:
    evidence = _evidence("https://jwc.hzcu.edu.cn/notice/1")
    evidence = evidence.model_copy(update={"evidence_id": "ev001_task"})
    answer = AgentAnswer(
        headline="选课准备",
        answer_markdown=(
            "教务处要求学生在规定时间完成选课。[来源：选课通知](https://jwc.hzcu.edu.cn/notice/1)"
        ),
        claims=[
            AnswerClaim(
                claim_id="claim-1",
                text="学生应在规定时间完成选课。",
                statement_type="campus_fact",
                importance="key",
                support_status="full",
                citations=[
                    ClaimCitation(
                        evidence_id=evidence.evidence_id,
                        support_status="full",
                        supporting_excerpt=evidence.excerpt,
                    )
                ],
            )
        ],
    )

    result = CitationVerifier().verify(answer, [evidence])

    assert result.passed is True
    assert result.citation_coverage == 1
    assert result.fully_supported_rate == 1


def test_citation_verifier_escalates_missing_evidence_and_unregistered_url() -> None:
    evidence = _evidence("https://jwc.hzcu.edu.cn/notice/1").model_copy(
        update={"evidence_id": "ev001_task"}
    )
    answer = AgentAnswer(
        headline="未经支持的结论",
        answer_markdown="[来源](https://example.com/not-in-workspace)",
        claims=[
            AnswerClaim(
                claim_id="claim-1",
                text="下周一定开始补选。",
                statement_type="campus_fact",
                support_status="unsupported",
            )
        ],
    )

    result = CitationVerifier().verify(answer, [evidence])

    assert result.passed is False
    assert result.requires_semantic_verifier is True
    assert {item.code for item in result.findings} >= {
        "missing_citation",
        "unsupported",
        "invalid_url",
    }


def test_restores_sanitized_relative_url_from_a_clear_workspace_title_match() -> None:
    evidence = _evidence("/api/v1/sources/verified/resources/growth-handbook/original").model_copy(
        update={
            "evidence_id": "ev001_task",
            "title": ("《我的第二三四课堂》2021级 ZUCCer 成长修炼手册（扫描 OCR，历史口径）"),
        }
    )
    answer = AgentAnswer(
        headline="第三课堂分值",
        answer_markdown=("按手册记 15 分/次。[来源：2021级成长修炼手册](<PRIVATE_URL>)"),
        claims=[_claim("claim-1", "符合完整条件时记 15 分/次。")],
    )

    restored = restore_workspace_citation_urls(answer, [evidence])

    assert (
        "[来源：2021级成长修炼手册]"
        "(</api/v1/sources/verified/resources/growth-handbook/original>)"
        in restored.answer_markdown
    )
    assert "<PRIVATE_URL>" not in restored.answer_markdown
    assert CitationVerifier().verify(restored, [evidence]).passed is True


def test_ambiguous_private_url_is_not_guessed_and_is_pruned_to_plain_text() -> None:
    evidence = _evidence("/api/v1/sources/verified/resources/one/original").model_copy(
        update={"evidence_id": "ev001_task", "title": "第一份同名材料"}
    )
    another = _evidence("/api/v1/sources/verified/resources/two/original").model_copy(
        update={"evidence_id": "ev002_task", "title": "第二份同名材料"}
    )
    answer = AgentAnswer(
        headline="待核对",
        answer_markdown="请查看[来源：同名材料](<PRIVATE_URL>)。",
        claims=[_claim("claim-1", "该信息需要核对。")],
    )

    restored = restore_workspace_citation_urls(answer, [evidence, another])
    structural = CitationVerifier().verify(restored, [evidence, another])
    pruned = prune_invalid_citations(restored, [evidence, another])

    assert restored is answer
    assert structural.passed is False
    assert any(item.code == "invalid_url" for item in structural.findings)
    assert "<PRIVATE_URL>" not in pruned.answer.answer_markdown
    assert "来源：同名材料" in pruned.answer.answer_markdown


def test_performance_trace_excludes_union_of_model_ttft_intervals() -> None:
    trace = AgentPerformanceTrace()
    trace.started_ns = 0
    trace.completed_ns = 10_000_000_000
    trace.first_progress_ns = 200_000_000
    trace.spans = [
        PerformanceSpan(
            kind="model",
            name="prepare",
            started_ns=1_000_000_000,
            first_event_ns=3_000_000_000,
            completed_ns=4_000_000_000,
        ),
        PerformanceSpan(
            kind="model",
            name="compose",
            started_ns=2_000_000_000,
            first_event_ns=5_000_000_000,
            completed_ns=6_000_000_000,
        ),
        PerformanceSpan(
            kind="tool",
            name="search_official_live",
            started_ns=6_000_000_000,
            completed_ns=8_000_000_000,
        ),
    ]
    trace.add_scenario_hint("public_live")

    performance, spans = trace.snapshot()

    assert performance.total_duration_ms == 10_000
    assert performance.excluded_model_ttft_ms == 4_000
    assert performance.controllable_duration_ms == 6_000
    assert performance.first_progress_ms == 200
    assert performance.model_call_count == 2
    assert performance.tool_call_count == 1
    assert performance.scenario == "public_live"
    assert len(spans) == 3


def test_stage5_metrics_require_real_sample_volume_and_labeled_support_accuracy() -> None:
    performance_records = [
        type(
            "PerformanceRecord",
            (),
            {
                "scenario": "no_live_read",
                "model_ttft_measurable": True,
                "controllable_duration_ms": value,
                "model_call_count": 2,
                "first_progress_ms": 100,
            },
        )()
        for value in (1_000, 2_000, 3_000)
    ]
    grounding_records = [
        type(
            "GroundingRecord",
            (),
            {
                "citation_coverage": 1.0,
                "fully_supported_rate": 1.0,
                "findings": [],
            },
        )()
    ]

    report = build_stage5_metrics(
        performance_records,
        grounding_records,
        minimum_samples_per_scenario=3,
    )

    assert nearest_rank((1, 2, 3, 4), 0.95) == 4
    assert report["performance"]["scenarios"]["no_live_read"]["status"] == "passed"
    assert report["performance"]["scenarios"]["public_live"]["status"] == "insufficient_samples"
    assert report["stage5_gate_status"] == "incomplete"


def test_image_reader_skips_decorative_images_when_text_is_already_readable() -> None:
    assert _should_read_image(0, "正文" * 100) is False
    assert _should_read_image(4, "正文" * 100) is True
    assert _should_read_image(0, "校历正文在图片中") is True


def _claim(claim_id: str, text: str) -> AnswerClaim:
    return AnswerClaim(
        claim_id=claim_id,
        text=text,
        statement_type="campus_fact",
        support_status="full",
        citations=[
            ClaimCitation(
                evidence_id="ev001_task",
                support_status="full",
            )
        ],
    )


def _candidate_answer() -> AgentAnswer:
    return AgentAnswer(
        headline="国创与校创中期检查",
        answer_markdown="校创中期检查初定 10 月；国创以当年通知为准。",
        claims=[
            _claim("claim-1", "校创中期检查初定于 10 月。"),
            _claim("claim-2", "国创中期检查时间以当年通知为准。"),
        ],
    )


def test_apply_answer_revision_patches_only_the_changed_claims() -> None:
    answer = _candidate_answer()
    replacement = _claim("claim-1", "2026 年校创中期检查初定于 10 月。")

    applied = apply_answer_revision(
        answer,
        AnswerRevision(
            answer_markdown="2026 年校创中期检查初定 10 月；国创以当年通知为准。",
            claim_patches=[
                ClaimPatch(action="replace", claim_id="claim-1", claim=replacement),
                ClaimPatch(
                    action="add",
                    claim_id="claim-3",
                    claim=_claim("claim-3", "历年国创检查集中在 10—11 月。"),
                ),
            ],
        ),
    )

    assert applied.protocol_violation is False
    assert [claim.claim_id for claim in applied.answer.claims] == [
        "claim-1",
        "claim-2",
        "claim-3",
    ]
    assert applied.answer.claims[0].text.startswith("2026 年")
    # Untouched claims keep their original object content.
    assert applied.answer.claims[1] == answer.claims[1]
    assert applied.answer.answer_markdown.startswith("2026 年")
    assert applied.answer.headline == answer.headline


def test_apply_answer_revision_removes_claims_and_ignores_unknown_ids() -> None:
    answer = _candidate_answer()

    applied = apply_answer_revision(
        answer,
        AnswerRevision(
            answer_markdown="国创以当年通知为准。",
            claim_patches=[
                ClaimPatch(action="remove", claim_id="claim-1"),
                ClaimPatch(action="remove", claim_id="claim-missing"),
            ],
        ),
    )

    assert applied.protocol_violation is False
    assert [claim.claim_id for claim in applied.answer.claims] == ["claim-2"]
    assert any(item.message.startswith("修订补丁要删除") for item in applied.findings)


def test_apply_answer_revision_flags_claim_patches_without_markdown() -> None:
    answer = _candidate_answer()

    applied = apply_answer_revision(
        answer,
        AnswerRevision(
            claim_patches=[ClaimPatch(action="remove", claim_id="claim-1")],
        ),
    )

    assert applied.protocol_violation is True
    assert any(item.severity == "error" for item in applied.findings)


def test_apply_answer_revision_without_patches_edits_prose_only() -> None:
    answer = _candidate_answer()

    applied = apply_answer_revision(
        answer,
        AnswerRevision(confidence="low"),
    )

    assert applied.protocol_violation is False
    assert applied.answer.confidence == "low"
    assert applied.answer.claims == answer.claims
    assert applied.answer.answer_markdown == answer.answer_markdown


def _notice_step(step_id: str, query: str, *, limit: int = 5) -> InvestigationStep:
    return InvestigationStep(
        id=step_id,
        purpose=f"查证 {query}",
        tool="search_campus_notices_live",
        arguments={"query": query, "limit": limit},
        can_run_in_parallel=True,
        success_condition="取得当前官方页面",
    )


def test_parallel_campus_notice_steps_merge_into_one_lease_batch() -> None:
    memory_step = InvestigationStep(
        id="memory",
        purpose="记忆召回",
        tool="search_campus_memory",
        arguments={"query": "国创", "top_k": 6},
        can_run_in_parallel=True,
        success_condition="召回相关材料",
    )
    batch = [
        _notice_step("live-1", "国创 中期检查", limit=4),
        memory_step,
        _notice_step("live-2", "校创 中期检查", limit=6),
        _notice_step("live-3", "国创 中期检查"),
    ]

    dispatch, merged_origins = _merge_campus_notice_steps(batch)

    merged = next(step for step in dispatch if step.tool == "search_campus_notices_live")
    assert len(dispatch) == 2
    assert merged.arguments.query is None
    assert merged.arguments.queries == ["国创 中期检查", "校创 中期检查"]
    assert merged.arguments.limit == 6
    assert merged_origins[merged.id] == ("live-1", "live-2", "live-3")
    assert memory_step in dispatch


def test_single_campus_notice_step_stays_unmerged() -> None:
    batch = [_notice_step("live-1", "国创 中期检查")]

    dispatch, merged_origins = _merge_campus_notice_steps(batch)

    assert dispatch == batch
    assert merged_origins == {}


def test_prune_keeps_claims_that_still_have_workspace_support() -> None:
    evidence = _evidence("https://jwc.hzcu.edu.cn/notice/1").model_copy(
        update={"evidence_id": "ev001_task"}
    )
    answer = AgentAnswer(
        headline="校创中期检查",
        answer_markdown=(
            "校创中期检查初定 10 月。[来源：官方通知](https://jwc.hzcu.edu.cn/notice/1)"
        ),
        claims=[
            AnswerClaim(
                claim_id="claim-1",
                text="校创中期检查初定于 10 月。",
                statement_type="campus_fact",
                support_status="full",
                citations=[
                    ClaimCitation(evidence_id="ev001_task", support_status="full"),
                    # A verifier patch mis-bound this citation to a stale id.
                    ClaimCitation(evidence_id="ev_gone", support_status="full"),
                ],
            )
        ],
    )

    pruned = prune_invalid_citations(answer, [evidence])

    assert pruned.changed is True
    assert [c.evidence_id for c in pruned.answer.claims[0].citations] == ["ev001_task"]
    assert CitationVerifier().verify(pruned.answer, [evidence]).passed is True


def test_prune_strips_links_outside_the_workspace_but_keeps_prose() -> None:
    evidence = _evidence("https://jwc.hzcu.edu.cn/notice/1").model_copy(
        update={"evidence_id": "ev001_task"}
    )
    answer = AgentAnswer(
        headline="校创中期检查",
        answer_markdown=(
            "校创中期检查初定 10 月。[来源：错误链接](https://example.com/wrong) "
            "详见 https://example.com/bare 页面。"
        ),
        claims=[_claim("claim-1", "校创中期检查初定于 10 月。")],
    )

    pruned = prune_invalid_citations(answer, [evidence])

    assert pruned.changed is True
    assert "example.com" not in pruned.answer.answer_markdown
    assert "来源：错误链接" in pruned.answer.answer_markdown
    assert "校创中期检查初定 10 月。" in pruned.answer.answer_markdown


def test_prune_reports_no_change_for_a_fully_valid_answer() -> None:
    evidence = _evidence("https://jwc.hzcu.edu.cn/notice/1").model_copy(
        update={"evidence_id": "ev001_task"}
    )
    answer = AgentAnswer(
        headline="校创中期检查",
        answer_markdown=(
            "校创中期检查初定 10 月。[来源：官方通知](https://jwc.hzcu.edu.cn/notice/1)"
        ),
        claims=[_claim("claim-1", "校创中期检查初定于 10 月。")],
    )

    pruned = prune_invalid_citations(answer, [evidence])

    assert pruned.changed is False
    assert pruned.answer is answer
    assert pruned.findings == []


def _dossier(risk_level: str = "normal") -> SemanticDossier:
    return SemanticDossier(
        goal_hypotheses=[GoalHypothesis(goal="查询校历", confidence=0.8)],
        risk=RiskAssessment(level=risk_level),
    )


def _composition(
    *,
    status: str = "sufficient",
    requires_verification: bool = False,
) -> GroundedAnswerComposition:
    return GroundedAnswerComposition(
        assessment=GroundingAssessment(status=status, summary="ok"),
        requires_independent_verification=requires_verification,
    )


def _structural(findings: list[VerificationFinding]) -> StructuralGroundingResult:
    return StructuralGroundingResult(
        findings=findings,
        citation_coverage=1.0,
        fully_supported_rate=0.5,
    )


def test_structural_warnings_alone_no_longer_force_the_semantic_verifier() -> None:
    warning = VerificationFinding(
        claim_id="claim-1",
        severity="warning",
        code="partial_support",
        message="部分支持",
    )

    assert (
        AgentCoordinator._requires_independent_verification(
            composition=_composition(),
            dossier=_dossier(),
            structural=_structural([warning]),
        )
        is False
    )


def test_real_risk_signals_still_force_the_semantic_verifier() -> None:
    error = VerificationFinding(
        severity="error",
        code="missing_citation",
        message="缺少引用",
    )

    assert (
        AgentCoordinator._requires_independent_verification(
            composition=_composition(),
            dossier=_dossier(),
            structural=_structural([error]),
        )
        is True
    )
    assert (
        AgentCoordinator._requires_independent_verification(
            composition=_composition(),
            dossier=_dossier("academic_high"),
            structural=_structural([]),
        )
        is True
    )
    assert (
        AgentCoordinator._requires_independent_verification(
            composition=_composition(status="conflicting"),
            dossier=_dossier(),
            structural=_structural([]),
        )
        is True
    )
    assert (
        AgentCoordinator._requires_independent_verification(
            composition=_composition(requires_verification=True),
            dossier=_dossier(),
            structural=_structural([]),
        )
        is False
    )


def _memory_only_plan() -> list[InvestigationStep]:
    return [
        InvestigationStep(
            id="memory",
            purpose="镜像召回",
            tool="search_campus_memory",
            arguments={"query": "校历", "top_k": 6},
            success_condition="召回相关材料",
        )
    ]


def test_initial_memory_plan_is_bounded_and_parallel() -> None:
    steps = [
        InvestigationStep(
            id=f"memory-{index}",
            purpose=f"检索信息点 {index}",
            tool="search_campus_memory",
            arguments={
                "query": f"正式查询名称{index}",
                "source_ids": ["model-hint-must-be-removed"],
                "filters": {"college": "工程学院"},
                "top_k": 8,
            },
            success_condition="取得官方材料",
        )
        for index in range(4)
    ]

    normalized = AgentCoordinator._normalize_initial_steps(steps)

    assert [step.id for step in normalized] == ["memory-0", "memory-1", "memory-2"]
    assert all(step.can_run_in_parallel for step in normalized)
    assert all(set(step.arguments.tool_payload()) == {"query", "top_k"} for step in normalized)


def test_memory_query_preserves_the_model_selected_expression() -> None:
    step = InvestigationStep(
        id="memory-budget",
        purpose="检索课程容量政策",
        tool="search_campus_memory",
        arguments={
            "query": "课程容量已满 加课 补选 增容 选课 教务处",
            "top_k": 8,
        },
        success_condition="取得官方材料",
    )

    initial = AgentCoordinator._normalize_initial_steps([step])
    follow_up = AgentCoordinator._normalize_follow_up_steps(
        [step],
        freshness="current",
    )

    expected = "课程容量已满 加课 补选 增容 选课 教务处"
    assert initial[0].arguments.query == expected
    assert follow_up[0].arguments.query == expected


def test_memory_plans_drop_calls_without_a_query() -> None:
    missing_query = InvestigationStep(
        id="missing-query",
        purpose="无效检索",
        tool="search_campus_memory",
        arguments={"top_k": 8},
        success_condition="不应执行",
    )

    assert AgentCoordinator._normalize_initial_steps([missing_query]) == []
    assert (
        AgentCoordinator._normalize_follow_up_steps(
            [missing_query],
            freshness="current",
        )
        == []
    )


def test_document_exploration_drops_calls_without_required_locator() -> None:
    missing_document = InvestigationStep(
        id="find-without-document",
        purpose="无效文内查找",
        tool="find_in_campus_document",
        arguments={"query": "指导性课程计划"},
        success_condition="不应执行",
    )
    missing_locator = InvestigationStep(
        id="read-without-locator",
        purpose="无效定位单元读取",
        tool="read_campus_document_locator",
        arguments={"document_version_id": "docv_example"},
        success_condition="不应执行",
    )

    assert AgentCoordinator._normalize_initial_steps([missing_document]) == []
    assert (
        AgentCoordinator._normalize_follow_up_steps(
            [missing_document],
            freshness="current",
        )
        == []
    )
    assert (
        AgentCoordinator._normalize_follow_up_steps(
            [missing_locator],
            freshness="current",
        )
        == []
    )


def test_follow_up_keeps_the_model_selected_read_only_tools() -> None:
    local = InvestigationStep(
        id="retry-memory",
        purpose="缩短表述重试本地检索",
        tool="search_campus_memory",
        arguments={"query": "校创 中期检查", "filters": {"college": "工程学院"}},
        success_condition="取得校创中期检查材料",
    )
    live = InvestigationStep(
        id="public-live",
        purpose="补查官网",
        tool="search_official_live",
        arguments={"query": "校创中期检查", "limit": 5},
        success_condition="取得实时材料",
    )

    normal = AgentCoordinator._normalize_follow_up_steps(
        [live, local],
        freshness="current",
    )
    live_required = AgentCoordinator._normalize_follow_up_steps(
        [live, local],
        freshness="live_required",
    )

    assert [step.id for step in normal] == ["public-live", "retry-memory"]
    assert set(normal[1].arguments.tool_payload()) == {"query"}
    assert [step.id for step in live_required] == ["public-live", "retry-memory"]


def test_disabled_campus_route_is_not_advertised_to_the_model() -> None:
    document_tools = SimpleNamespace(
        inspect_name="inspect_campus_document",
        find_name="find_in_campus_document",
        read_locator_name="read_campus_document_locator",
        read_name="read_campus_document_segment",
    )
    gateway = ToolGateway(
        official_search=SimpleNamespace(),
        campus_memory=SimpleNamespace(),
        campus_notices=SimpleNamespace(enabled=False),
        ingestion=SimpleNamespace(),
        campus_documents=document_tools,
    )

    catalog = gateway.catalog(frozenset({"public", "campus"}))
    names = {item["name"] for item in catalog}
    memory_schema = next(
        item["input_schema"] for item in catalog if item["name"] == "search_campus_memory"
    )
    schemas = {item["name"]: item["input_schema"] for item in catalog}

    assert "search_campus_notices_live" not in names
    assert {
        "inspect_campus_document",
        "find_in_campus_document",
        "read_campus_document_locator",
        "read_campus_document_segment",
    }.issubset(names)
    assert set(memory_schema["properties"]) == {"query", "top_k"}
    assert set(schemas["inspect_campus_document"]["properties"]) == {
        "document_version_id",
    }
    assert set(schemas["find_in_campus_document"]["properties"]) == {
        "document_version_id",
        "query",
        "top_k",
        "context_chars",
    }
    assert set(schemas["read_campus_document_locator"]["properties"]) == {
        "document_version_id",
        "locator",
    }
    assert set(schemas["read_campus_document_segment"]["properties"]) == {
        "document_version_id",
        "offset",
        "max_chars",
    }


def test_pilot_mirror_scope_does_not_unlock_live_campus_tool() -> None:
    gateway = ToolGateway(
        official_search=SimpleNamespace(),
        campus_memory=SimpleNamespace(),
        campus_notices=SimpleNamespace(
            enabled=True,
            name="search_campus_notices_live",
        ),
        ingestion=SimpleNamespace(),
        campus_documents=SimpleNamespace(
            inspect_name="inspect_campus_document",
            find_name="find_in_campus_document",
            read_locator_name="read_campus_document_locator",
            read_name="read_campus_document_segment",
        ),
    )

    catalog = gateway.catalog(
        frozenset({"public"}),
        memory_visibilities=frozenset({"public", "campus"}),
    )
    memory = next(item for item in catalog if item["name"] == "search_campus_memory")
    live = next(item for item in catalog if item["name"] == "search_campus_notices_live")

    assert "Campus 本地镜像" in memory["description"]
    assert live["available_now"] is False


def test_local_evidence_cannot_be_reported_as_live_verified() -> None:
    answer = AgentAnswer(
        headline="校历已找到",
        answer_markdown="校历显示开课日期。",
        confidence="high",
        verification_mode="live_verified",
    )
    evidence = Evidence(
        evidence_id="ev-calendar",
        title="2026-2027学年校历",
        publisher="教务处",
        canonical_url="https://example.test/calendar.png",
        excerpt="9月14日全校开始上课。",
        observed_at=utc_now(),
        source_id="calendar",
        resource_ref="memory:calendar",
        retrieval_mode="memory",
    )

    calibrated = AgentCoordinator._calibrate_answer(answer, [evidence])

    assert calibrated.verification_mode == "cache"
    assert calibrated.confidence == "high"


_LIVE_CATALOG = [
    {"name": "search_campus_memory", "available_now": True},
    {"name": "search_official_live", "available_now": True},
    {"name": "search_campus_notices_live", "available_now": True},
]


def test_current_freshness_answers_from_the_local_mirror_without_forced_live() -> None:
    planned = AgentCoordinator._ensure_live_verification(
        _memory_only_plan(),
        original_query="下学期什么时候开学",
        freshness="current",
        tool_catalog=_LIVE_CATALOG,
    )

    assert [step.tool for step in planned] == ["search_campus_memory"]


def test_live_required_freshness_still_injects_a_live_step() -> None:
    planned = AgentCoordinator._ensure_live_verification(
        _memory_only_plan(),
        original_query="今天还能不能提交国创申报",
        freshness="live_required",
        tool_catalog=_LIVE_CATALOG,
    )

    assert planned[-1].id == "runtime-live-verification"
    assert planned[-1].tool == "search_campus_notices_live"
