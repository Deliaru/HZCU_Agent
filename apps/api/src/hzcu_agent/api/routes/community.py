from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from hzcu_agent.api.dependencies import enforce_csrf, request_principal, request_session
from hzcu_agent.auth.service import RequestPrincipal
from hzcu_agent.ingestion.indexing import DocumentIndexer
from hzcu_agent.ingestion.parsers import normalize_text
from hzcu_agent.ingestion.search_index import refresh_source_search_profile
from hzcu_agent.models import (
    AgentTask,
    AnswerRecord,
    CampusUser,
    CommunityAnswer,
    CommunityQuestion,
    Conversation,
    DocumentVersion,
    KnowledgeEntry,
    KnowledgeEntryOrigin,
    LocalContributorCredential,
    SecurityAuditEvent,
    SourceDefinitionRecord,
    SourceResource,
    new_id,
    utc_now,
)
from hzcu_agent.observability import request_id_context
from hzcu_agent.schemas import (
    CommunityAnswerCreateRequest,
    CommunityAnswerModerationRequest,
    CommunityAnswerResponse,
    CommunityAnswerUpdateRequest,
    ContributorCreateRequest,
    ContributorResponse,
    ContributorUpdateRequest,
    KnowledgeEntryRequest,
    KnowledgeEntryResponse,
    KnowledgeOptimizationResponse,
    QuestionCreateRequest,
    QuestionDetailResponse,
    QuestionReviewRequest,
    QuestionSummaryResponse,
)
from hzcu_agent.services.agent_policy import AgentModelBudgetExceeded
from hzcu_agent.services.model_gateway import (
    ModelConfigurationError,
    StructuredModelOutputError,
)
from hzcu_agent.text_safety import clean_product_text

logger = logging.getLogger(__name__)
router = APIRouter(tags=["questions"])
admin_router = APIRouter(prefix="/admin", tags=["knowledge-governance"])
SessionDependency = Annotated[AsyncSession, Depends(request_session)]
PrincipalDependency = Annotated[RequestPrincipal, Depends(request_principal)]


@router.put(
    "/answers/{answer_id}/question",
    response_model=QuestionDetailResponse,
)
async def create_question_from_answer(
    answer_id: str,
    payload: QuestionCreateRequest,
    request: Request,
    session: SessionDependency,
    principal: PrincipalDependency,
) -> QuestionDetailResponse:
    enforce_csrf(request, principal)
    if principal.authenticated and principal.role == "contributor":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "CONTRIBUTOR_QUESTION_CREATE_DENIED",
                "message": "贡献者账号不能创建悬赏问题。",
            },
        )
    subject_id = _require_subject(principal)
    answer = await session.get(AnswerRecord, answer_id)
    if answer is None:
        raise HTTPException(status_code=404, detail="Answer not found")
    task = await session.get(AgentTask, answer.task_id)
    conversation = await session.get(Conversation, task.conversation_id) if task else None
    if task is None or conversation is None or conversation.owner_subject_id != subject_id:
        raise HTTPException(status_code=404, detail="Answer not found")
    if task.status != "completed" or not answer.question_offer_reason:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "QUESTION_OFFER_NOT_AVAILABLE",
                "message": "该回答当前没有可提交的悬赏提问资格。",
            },
        )

    title, details = _clean_question_payload(payload)
    existing = await session.scalar(
        select(CommunityQuestion).where(CommunityQuestion.answer_id == answer_id)
    )
    now = utc_now()
    if existing is not None:
        if existing.status in {"open", "answered", "hidden"}:
            return await _question_detail(session, existing, public=False, principal=principal)
        existing.title = title
        existing.details = details
        existing.evidence_gap = _question_gap(answer.question_offer_reason)
        existing.status = "pending_review"
        existing.review_note = None
        existing.reviewed_by_user_id = None
        existing.reviewed_at = None
        existing.updated_at = now
        question = existing
    else:
        question = CommunityQuestion(
            id=new_id("question"),
            answer_id=answer_id,
            owner_subject_id=subject_id,
            title=title,
            details=details,
            evidence_gap=_question_gap(answer.question_offer_reason),
            status="pending_review",
            created_at=now,
            updated_at=now,
        )
        session.add(question)
    await _audit(
        session,
        principal,
        "community.question.submit",
        {
            "question_id": question.id,
            "status": question.status,
            "content_hash": _hash_text(details),
        },
    )
    try:
        await session.commit()
    except IntegrityError as exc:
        # Two tabs can submit the same eligible answer at once.  The unique
        # answer_id constraint is the final idempotency gate; resolve the
        # winner instead of exposing a database 500 to the user.
        await session.rollback()
        winner = await session.scalar(
            select(CommunityQuestion).where(CommunityQuestion.answer_id == answer_id)
        )
        if winner is None:
            raise HTTPException(status_code=409, detail="该回答已经提交过悬赏问题。") from exc
        return await _question_detail(session, winner, public=False, principal=principal)
    return await _question_detail(session, question, public=False, principal=principal)


@router.get("/questions", response_model=list[QuestionSummaryResponse])
async def list_public_questions(
    session: SessionDependency,
    _principal: PrincipalDependency,
) -> list[QuestionSummaryResponse]:
    questions = list(
        (
            await session.scalars(
                select(CommunityQuestion)
                .where(CommunityQuestion.status.in_(("open", "answered")))
                .order_by(
                    (CommunityQuestion.status == "open").desc(),
                    CommunityQuestion.created_at.asc(),
                    CommunityQuestion.id.asc(),
                )
            )
        ).all()
    )
    return [await _question_summary(session, item) for item in questions]


@router.get("/questions/{question_id}", response_model=QuestionDetailResponse)
async def get_public_question(
    question_id: str,
    session: SessionDependency,
    _principal: PrincipalDependency,
) -> QuestionDetailResponse:
    question = await session.get(CommunityQuestion, question_id)
    if question is None or question.status not in {"open", "answered"}:
        raise HTTPException(status_code=404, detail="Question not found")
    return await _question_detail(session, question, public=True, principal=_principal)


@router.post(
    "/questions/{question_id}/answers",
    response_model=CommunityAnswerResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_community_answer(
    question_id: str,
    payload: CommunityAnswerCreateRequest,
    request: Request,
    session: SessionDependency,
    principal: PrincipalDependency,
) -> CommunityAnswerResponse:
    enforce_csrf(request, principal)
    _require_answerer(principal)
    question = await session.get(CommunityQuestion, question_id)
    if question is None or question.status not in {"open", "answered"}:
        raise HTTPException(status_code=404, detail="Question not found")
    user_id = _require_user(principal)
    user = await session.get(CampusUser, user_id)
    if user is None or user.status != "active" or user.role not in {"contributor", "admin"}:
        raise HTTPException(status_code=403, detail="Contributor account is not active")
    body = _clean_answer(payload.answer_markdown)
    if not body:
        raise HTTPException(status_code=422, detail="Answer must contain visible text")
    existing = await session.scalar(
        select(CommunityAnswer).where(
            CommunityAnswer.question_id == question_id,
            CommunityAnswer.contributor_user_id == user_id,
        )
    )
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "COMMUNITY_ANSWER_EXISTS", "message": "你已经回答过这个问题。"},
        )
    now = utc_now()
    answer = CommunityAnswer(
        id=new_id("community_answer"),
        question_id=question_id,
        contributor_user_id=user_id,
        answer_markdown=body,
        status="visible",
        knowledge_review_state="not_reviewed",
        created_at=now,
        updated_at=now,
    )
    session.add(answer)
    question.status = "answered"
    question.updated_at = now
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "COMMUNITY_ANSWER_EXISTS", "message": "你已经回答过这个问题。"},
        ) from exc
    await _audit(
        session,
        principal,
        "community.answer.create",
        {"answer_id": answer.id, "question_id": question_id, "status": answer.status},
    )
    await session.commit()
    return await _community_answer_response(session, answer)


@router.put(
    "/questions/{question_id}/answers/{answer_id}",
    response_model=CommunityAnswerResponse,
)
async def update_community_answer(
    question_id: str,
    answer_id: str,
    payload: CommunityAnswerUpdateRequest,
    request: Request,
    session: SessionDependency,
    principal: PrincipalDependency,
) -> CommunityAnswerResponse:
    enforce_csrf(request, principal)
    _require_answerer(principal)
    answer = await session.get(CommunityAnswer, answer_id)
    if answer is None or answer.question_id != question_id:
        raise HTTPException(status_code=404, detail="Community answer not found")
    question = await session.get(CommunityQuestion, question_id)
    if (
        principal.role != "admin"
        and (question is None or question.status not in {"open", "answered"})
    ):
        raise HTTPException(status_code=404, detail="Question not found")
    user_id = _require_user(principal)
    if principal.role != "admin" and answer.contributor_user_id != user_id:
        raise HTTPException(status_code=404, detail="Community answer not found")
    body = _clean_answer(payload.answer_markdown)
    if not body:
        raise HTTPException(status_code=422, detail="Answer must contain visible text")
    answer.answer_markdown = body
    answer.updated_at = utc_now()
    if answer.knowledge_review_state in {"published", "source_changed"}:
        answer.knowledge_review_state = "source_changed"
    await _audit(
        session,
        principal,
        "community.answer.update",
        {"answer_id": answer.id, "question_id": question_id, "status": answer.status},
    )
    await session.commit()
    return await _community_answer_response(session, answer)


@router.get("/knowledge/{entry_id}", response_model=KnowledgeEntryResponse)
async def get_public_knowledge(
    entry_id: str,
    session: SessionDependency,
    principal: PrincipalDependency,
) -> KnowledgeEntryResponse:
    entry = await session.get(KnowledgeEntry, entry_id)
    if entry is None or entry.status != "published":
        raise HTTPException(status_code=404, detail="Knowledge entry not found")
    if entry.visibility not in principal.visibility_scopes:
        raise HTTPException(status_code=404, detail="Knowledge entry not found")
    return _knowledge_response(entry, await _origin_ids(session, entry.id))


@admin_router.get("/questions", response_model=list[QuestionDetailResponse])
async def admin_list_questions(
    session: SessionDependency,
    principal: PrincipalDependency,
    status_filter: str | None = Query(default=None, alias="status"),
) -> list[QuestionDetailResponse]:
    _require_admin(principal)
    query = select(CommunityQuestion).order_by(
        CommunityQuestion.status.asc(),
        CommunityQuestion.created_at.asc(),
    )
    if status_filter:
        if status_filter not in {"pending_review", "open", "answered", "rejected", "hidden"}:
            raise HTTPException(status_code=422, detail="Invalid question status")
        query = query.where(CommunityQuestion.status == status_filter)
    questions = list((await session.scalars(query)).all())
    return [
        await _question_detail(session, item, public=False, principal=principal)
        for item in questions
    ]


@admin_router.put("/questions/{question_id}", response_model=QuestionDetailResponse)
async def admin_review_question(
    question_id: str,
    payload: QuestionReviewRequest,
    request: Request,
    session: SessionDependency,
    principal: PrincipalDependency,
) -> QuestionDetailResponse:
    _require_admin(principal)
    enforce_csrf(request, principal)
    question = await session.get(CommunityQuestion, question_id)
    if question is None:
        raise HTTPException(status_code=404, detail="Question not found")
    now = utc_now()
    question.status = payload.status
    question.review_note = clean_product_text(payload.review_note or "").strip() or None
    question.reviewed_by_user_id = principal.user_id
    question.reviewed_at = now
    question.updated_at = now
    if payload.status == "open" and question.published_at is None:
        question.published_at = now
    await _audit(
        session,
        principal,
        "admin.question.review",
        {"question_id": question.id, "status": question.status},
    )
    await session.commit()
    return await _question_detail(session, question, public=False, principal=principal)


@admin_router.put(
    "/questions/{question_id}/answers/{answer_id}",
    response_model=CommunityAnswerResponse,
)
async def admin_moderate_answer(
    question_id: str,
    answer_id: str,
    payload: CommunityAnswerModerationRequest,
    request: Request,
    session: SessionDependency,
    principal: PrincipalDependency,
) -> CommunityAnswerResponse:
    _require_admin(principal)
    enforce_csrf(request, principal)
    answer = await session.get(CommunityAnswer, answer_id)
    if answer is None or answer.question_id != question_id:
        raise HTTPException(status_code=404, detail="Community answer not found")
    answer.status = payload.status
    answer.updated_at = utc_now()
    question = await session.get(CommunityQuestion, question_id)
    if question is not None and question.status in {"open", "answered"}:
        visible_answers = await session.scalar(
            select(func.count(CommunityAnswer.id)).where(
                CommunityAnswer.question_id == question_id,
                CommunityAnswer.status == "visible",
            )
        )
        # A question with no visible answers should return to the unanswered
        # lane; restoring any answer makes it answered again.  Hidden/rejected
        # questions keep their review state unchanged.
        question.status = "answered" if int(visible_answers or 0) > 0 else "open"
        question.updated_at = answer.updated_at
    await _audit(
        session,
        principal,
        "admin.community_answer.moderate",
        {"answer_id": answer.id, "question_id": question_id, "status": answer.status},
    )
    await session.commit()
    return await _community_answer_response(session, answer)


@admin_router.get("/contributors", response_model=list[ContributorResponse])
async def admin_list_contributors(
    session: SessionDependency,
    principal: PrincipalDependency,
) -> list[ContributorResponse]:
    _require_admin(principal)
    credentials = list(
        (
            await session.scalars(
                select(LocalContributorCredential).order_by(
                    LocalContributorCredential.created_at.desc()
                )
            )
        ).all()
    )
    return [_contributor_response(item) for item in credentials]


@admin_router.post("/contributors", response_model=ContributorResponse, status_code=201)
async def admin_create_contributor(
    payload: ContributorCreateRequest,
    request: Request,
    session: SessionDependency,
    principal: PrincipalDependency,
) -> ContributorResponse:
    _require_admin(principal)
    enforce_csrf(request, principal)
    if principal.user_id is None:
        raise HTTPException(status_code=403, detail="Not found")
    password = payload.password.get_secret_value()
    try:
        subject_hash = request.app.state.auth.subject_hash_for(
            payload.username,
            identity_provider="local_contributor",
        )
        credential = await request.app.state.contributors.create(
            username=payload.username,
            password=password,
            public_name=payload.public_name,
            unit=payload.unit,
            subject_hash=subject_hash,
        )
    except Exception as exc:
        code = getattr(exc, "code", None)
        if code and str(code).startswith("CONTRIBUTOR_"):
            raise HTTPException(
                status_code=409 if code == "CONTRIBUTOR_USERNAME_TAKEN" else 422,
                detail={"code": code, "message": str(exc)},
            ) from exc
        raise
    finally:
        payload.password = type(payload.password)("")
        password = ""
    await _audit(
        session,
        principal,
        "admin.contributor.create",
        {"contributor_id": credential.id, "status": credential.status},
    )
    await session.commit()
    return _contributor_response(credential)


@admin_router.put("/contributors/{contributor_id}", response_model=ContributorResponse)
async def admin_update_contributor(
    contributor_id: str,
    payload: ContributorUpdateRequest,
    request: Request,
    session: SessionDependency,
    principal: PrincipalDependency,
) -> ContributorResponse:
    _require_admin(principal)
    enforce_csrf(request, principal)
    password = payload.password.get_secret_value() if payload.password else None
    try:
        credential = await request.app.state.contributors.update(
            contributor_id,
            public_name=payload.public_name,
            unit=payload.unit,
            status=payload.status,
            password=password,
        )
    except Exception as exc:
        code = getattr(exc, "code", None)
        if code and str(code).startswith("CONTRIBUTOR_"):
            raise HTTPException(
                status_code=404 if code == "CONTRIBUTOR_NOT_FOUND" else 422,
                detail={"code": code, "message": str(exc)},
            ) from exc
        raise
    finally:
        if payload.password is not None:
            payload.password = type(payload.password)("")
        password = ""
    await _audit(
        session,
        principal,
        "admin.contributor.update",
        {"contributor_id": credential.id, "status": credential.status},
    )
    await session.commit()
    return _contributor_response(credential)


@admin_router.get("/knowledge", response_model=list[KnowledgeEntryResponse])
async def admin_list_knowledge(
    session: SessionDependency,
    principal: PrincipalDependency,
) -> list[KnowledgeEntryResponse]:
    _require_admin(principal)
    entries = list(
        (
            await session.scalars(select(KnowledgeEntry).order_by(KnowledgeEntry.updated_at.desc()))
        ).all()
    )
    return [_knowledge_response(item, await _origin_ids(session, item.id)) for item in entries]


@admin_router.get("/knowledge/{entry_id}", response_model=KnowledgeEntryResponse)
async def admin_get_knowledge(
    entry_id: str,
    session: SessionDependency,
    principal: PrincipalDependency,
) -> KnowledgeEntryResponse:
    _require_admin(principal)
    entry = await session.get(KnowledgeEntry, entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Knowledge entry not found")
    return _knowledge_response(entry, await _origin_ids(session, entry.id))


@admin_router.post("/knowledge", response_model=KnowledgeEntryResponse, status_code=201)
async def admin_create_knowledge(
    payload: KnowledgeEntryRequest,
    request: Request,
    session: SessionDependency,
    principal: PrincipalDependency,
) -> KnowledgeEntryResponse:
    _require_admin(principal)
    enforce_csrf(request, principal)
    entry = await _create_knowledge_entry(session, payload, principal)
    await _audit(
        session,
        principal,
        "admin.knowledge.create",
        {"entry_id": entry.id, "status": entry.status, "content_hash": _knowledge_hash(payload)},
    )
    await session.commit()
    return _knowledge_response(entry, await _origin_ids(session, entry.id))


@admin_router.put("/knowledge/{entry_id}", response_model=KnowledgeEntryResponse)
async def admin_update_knowledge(
    entry_id: str,
    payload: KnowledgeEntryRequest,
    request: Request,
    session: SessionDependency,
    principal: PrincipalDependency,
) -> KnowledgeEntryResponse:
    _require_admin(principal)
    enforce_csrf(request, principal)
    entry = await session.get(KnowledgeEntry, entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Knowledge entry not found")
    if payload.question_id and payload.question_id != entry.question_id:
        conflict = await session.scalar(
            select(KnowledgeEntry).where(
                KnowledgeEntry.question_id == payload.question_id,
                KnowledgeEntry.id != entry.id,
            )
        )
        if conflict is not None:
            raise HTTPException(status_code=409, detail="该问题已经存在人工知识条目。")
    await _apply_knowledge_payload(session, entry, payload)
    await _replace_origins(session, entry, payload.origin_answer_ids, payload.question_id)
    if entry.status in {"published", "retired"}:
        entry.status = "draft"
    entry.updated_at = utc_now()
    await _audit(
        session,
        principal,
        "admin.knowledge.update",
        {"entry_id": entry.id, "status": entry.status, "content_hash": _knowledge_hash(payload)},
    )
    await session.commit()
    return _knowledge_response(entry, await _origin_ids(session, entry.id))


@admin_router.post(
    "/knowledge/{entry_id}/optimize",
    response_model=KnowledgeOptimizationResponse,
)
async def admin_optimize_knowledge(
    entry_id: str,
    request: Request,
    session: SessionDependency,
    principal: PrincipalDependency,
) -> KnowledgeOptimizationResponse:
    _require_admin(principal)
    enforce_csrf(request, principal)
    entry = await session.get(KnowledgeEntry, entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Knowledge entry not found")
    fallback = _fallback_knowledge_optimization(entry)
    result = fallback
    try:
        result = await request.app.state.models.optimize_knowledge(
            entry_id=entry.id,
            title=entry.title,
            canonical_question=entry.canonical_question,
            answer_markdown=entry.answer_markdown,
            category=entry.category,
            alternative_phrasings=list(entry.alternative_phrasings or []),
            applicable_scope=entry.applicable_scope,
            maintainer_unit=entry.maintainer_unit,
            basis_note=entry.basis_note,
            validity=entry.validity,
            effective_from=entry.effective_from,
            effective_to=entry.effective_to,
            visibility=entry.visibility,
            current_time=utc_now(),
        )
    except (AgentModelBudgetExceeded, StructuredModelOutputError, ModelConfigurationError) as exc:
        # Optimization is an optional administrator aid.  A depleted daily
        # budget, unavailable endpoint, or malformed structured response must
        # never block hand-authored knowledge from being saved or published.
        logger.info(
            "knowledge optimization fell back to deterministic suggestions",
            extra={
                "event": "admin.knowledge.optimize_fallback",
                "entry_id": entry.id,
                "reason": type(exc).__name__,
            },
        )
    except Exception as exc:
        # Do not make an external model outage a governance outage.  The
        # exception itself is intentionally not persisted to avoid leaking
        # endpoint details into audit data.
        logger.warning(
            "knowledge optimization failed; using deterministic suggestions",
            extra={
                "event": "admin.knowledge.optimize_fallback",
                "entry_id": entry.id,
                "reason": type(exc).__name__,
            },
        )
    result = _sanitize_knowledge_optimization(result, fallback)
    await _audit(
        session,
        principal,
        "admin.knowledge.optimize",
        {
            "entry_id": entry.id,
            "content_hash": _hash_text(
                json.dumps(result.model_dump(), ensure_ascii=False, sort_keys=True)
            ),
        },
    )
    await session.commit()
    return result


@admin_router.post("/knowledge/{entry_id}/publish", response_model=KnowledgeEntryResponse)
async def admin_publish_knowledge(
    entry_id: str,
    request: Request,
    session: SessionDependency,
    principal: PrincipalDependency,
) -> KnowledgeEntryResponse:
    _require_admin(principal)
    enforce_csrf(request, principal)
    entry = await session.get(KnowledgeEntry, entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Knowledge entry not found")
    if entry.validity == "time_bounded" and (
        entry.effective_from is None or entry.effective_to is None
    ):
        raise HTTPException(status_code=422, detail="time_bounded 条目必须填写生效和失效日期")
    source_id = (
        "hzcu-curated-campus-knowledge"
        if entry.visibility == "campus"
        else "hzcu-curated-public-knowledge"
    )
    await _validate_origins_for_publish(session, entry)
    source = await session.get(SourceDefinitionRecord, source_id)
    if source is None or not source.enabled:
        raise HTTPException(status_code=503, detail="Curated knowledge source is unavailable")
    old_resource_id = entry.published_resource_id
    old_source_id = entry.published_source_id
    if old_resource_id and old_source_id != source_id:
        old_resource = await session.get(SourceResource, old_resource_id)
        if old_resource is not None:
            old_resource.current_version_id = None

    factual = _knowledge_factual_payload(entry)
    factual_digest = _hash_text(
        json.dumps(factual, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )
    canonical_uri = f"{request.app.state.settings.web_app_url}/knowledge/{entry.id}"
    now = utc_now()
    # DocumentVersion is globally content-deduplicated for ordinary ingestion.
    # A governance publish is a deliberate immutable release, however, so a
    # re-publish of unchanged facts receives a fresh hash/version instead of
    # silently reusing an earlier release.  Changed facts retain the canonical
    # factual digest; reverting to an older release is still recorded as a new
    # publication because that digest already exists in the ledger.
    resource = await session.scalar(
        select(SourceResource).where(
            SourceResource.source_id == source_id,
            SourceResource.canonical_uri == canonical_uri,
        )
    )
    if resource is None:
        resource = SourceResource(
            id=new_id("res"),
            source_id=source_id,
            canonical_uri=canonical_uri,
            external_id=entry.id,
            resource_type="curated_markdown",
            first_seen_at=now,
            last_seen_at=now,
        )
        session.add(resource)
        await session.flush()
    else:
        resource.last_seen_at = now
    existing_factual_version = await session.scalar(
        select(DocumentVersion).where(
            DocumentVersion.resource_id == resource.id,
            DocumentVersion.content_hash == factual_digest,
        )
    )
    digest = factual_digest
    if existing_factual_version is not None:
        digest = _hash_text(
            json.dumps(
                {
                    "content_hash": factual_digest,
                    "publication_id": new_id("knowledge_publish"),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    version = await session.scalar(
        select(DocumentVersion).where(
            DocumentVersion.resource_id == resource.id,
            DocumentVersion.content_hash == digest,
        )
    )
    if version is None:
        version = DocumentVersion(
            id=new_id("docv"),
            resource_id=resource.id,
            content_hash=digest,
            raw_snapshot_uri=None,
            media_type="text/markdown",
            normalized_text=_knowledge_document_text(entry),
            title=entry.title,
            publisher="HZCU Agent 知识治理（人工核验资料）",
            published_at=now,
            effective_from=entry.effective_from,
            effective_to=entry.effective_to,
            observed_at=now,
            parser_version="curated-markdown-v1",
            quality_status="accepted",
            document_metadata={
                "knowledge_entry_id": entry.id,
                "authority_level": "curated",
                "visibility": entry.visibility,
            },
        )
        session.add(version)
        await session.flush()
    else:
        version.quality_status = "accepted"
        version.published_at = now
    await DocumentIndexer().ensure_version_index(session, version, force=True)
    resource.current_version_id = version.id
    entry.published_source_id = source_id
    entry.published_resource_id = resource.id
    entry.published_version_id = version.id
    entry.content_hash = digest
    entry.status = "published"
    entry.published_at = now
    entry.updated_at = now
    await _mark_origins_published(session, entry.id)
    await _audit(
        session,
        principal,
        "admin.knowledge.publish",
        {
            "entry_id": entry.id,
            "version_id": version.id,
            "source_id": source_id,
            "content_hash": digest,
        },
    )
    await session.commit()
    await _refresh_curated_source_profiles(
        request.app.state.database,
        source_id,
        old_source_id=old_source_id if old_resource_id else None,
    )
    return _knowledge_response(entry, await _origin_ids(session, entry.id))


@admin_router.post("/knowledge/{entry_id}/retire", response_model=KnowledgeEntryResponse)
async def admin_retire_knowledge(
    entry_id: str,
    request: Request,
    session: SessionDependency,
    principal: PrincipalDependency,
) -> KnowledgeEntryResponse:
    _require_admin(principal)
    enforce_csrf(request, principal)
    entry = await session.get(KnowledgeEntry, entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Knowledge entry not found")
    now = utc_now()
    source_id = entry.published_source_id
    if entry.published_resource_id:
        resource = await session.get(SourceResource, entry.published_resource_id)
        if resource is not None:
            # Keep an immutable tombstone in the document ledger and point the
            # resource at it. Retrieval explicitly excludes ``retracted`` so
            # source routing and document exploration cannot surface retired
            # facts, while administrators retain a complete audit trail.
            tombstone_text = normalize_text(
                f"人工知识条目已退休，不应作为当前资料引用。\n原条目：{entry.title}"
            )
            tombstone_hash = _hash_text(
                json.dumps(
                    {
                        "entry_id": entry.id,
                        "previous_version_id": entry.published_version_id,
                        "content_hash": entry.content_hash,
                        "retired_at": now.isoformat(),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            tombstone = DocumentVersion(
                id=new_id("docv"),
                resource_id=resource.id,
                content_hash=tombstone_hash,
                raw_snapshot_uri=None,
                media_type="text/markdown",
                normalized_text=tombstone_text,
                title=f"已退休：{entry.title}"[:500],
                publisher="HZCU Agent 知识治理（撤回记录）",
                published_at=now,
                effective_from=None,
                effective_to=None,
                observed_at=now,
                parser_version="curated-retraction-v1",
                quality_status="retracted",
                document_metadata={
                    "knowledge_entry_id": entry.id,
                    "authority_level": "curated",
                    "visibility": entry.visibility,
                    "retraction": True,
                },
            )
            session.add(tombstone)
            await session.flush()
            await DocumentIndexer().ensure_version_index(session, tombstone, force=True)
            resource.current_version_id = tombstone.id
            entry.published_version_id = tombstone.id
            entry.content_hash = tombstone_hash
    entry.status = "retired"
    entry.updated_at = now
    await _audit(
        session,
        principal,
        "admin.knowledge.retire",
        {"entry_id": entry.id, "status": entry.status},
    )
    await session.commit()
    if source_id:
        await _refresh_curated_source_profiles(
            request.app.state.database,
            source_id,
        )
    return _knowledge_response(entry, await _origin_ids(session, entry.id))


async def _refresh_curated_source_profiles(
    database,
    source_id: str,
    *,
    old_source_id: str | None = None,
) -> None:
    source_ids = [source_id]
    if old_source_id and old_source_id != source_id:
        source_ids.append(old_source_id)
    for item in source_ids:
        try:
            await refresh_source_search_profile(database, item)
        except SQLAlchemyError:
            # Source routing is an optimization. Global retrieval remains the
            # safe fallback when the disposable FTS profile is unavailable.
            logger.warning(
                "curated source routing profile refresh failed",
                extra={"event": "knowledge.source_profile_refresh_failed", "source_id": item},
            )


def _require_subject(principal: RequestPrincipal) -> str:
    if principal.product_subject_id is None:
        raise HTTPException(status_code=503, detail="Product identity unavailable")
    return principal.product_subject_id


def _require_user(principal: RequestPrincipal) -> str:
    if principal.user_id is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return principal.user_id


def _require_answerer(principal: RequestPrincipal) -> None:
    if not principal.authenticated or principal.role not in {"contributor", "admin"}:
        raise HTTPException(status_code=403, detail="Contributor access required")


def _require_admin(principal: RequestPrincipal | None) -> None:
    if principal is None or not principal.authenticated or principal.role != "admin":
        raise HTTPException(status_code=404, detail="Not found")


def _clean_question_payload(payload: QuestionCreateRequest) -> tuple[str, str]:
    title = clean_product_text(payload.title).strip()[:240]
    details = clean_product_text(payload.details).strip()[:6000]
    if len(title) < 2 or len(details) < 2:
        raise HTTPException(status_code=422, detail="Question must contain visible text")
    return title, details


def _clean_answer(value: str) -> str:
    return clean_product_text(value).strip()[:12000]


def _question_gap(reason: str | None) -> str:
    return {
        "no_evidence": "没有找到足够的关联校园证据。",
        "low_confidence": "当前回答置信度较低，需要补充范围或权威材料。",
        "grounding_insufficient": "引用材料不足以覆盖回答中的关键断言。",
        "grounding_stale": "现有材料可能已经过期，无法确认当前安排。",
        "grounding_conflicting": "检索到的权威材料存在冲突，需要人工核对。",
        "verification_degraded": "引用核验未完成，暂时无法给出可靠结论。",
    }.get(reason or "", "当前证据不足，需要进一步核对。")


async def _question_summary(
    session: AsyncSession, question: CommunityQuestion
) -> QuestionSummaryResponse:
    count = int(
        await session.scalar(
            select(func.count(CommunityAnswer.id)).where(
                CommunityAnswer.question_id == question.id,
                CommunityAnswer.status == "visible",
            )
        )
        or 0
    )
    created = _as_utc(question.created_at)
    return QuestionSummaryResponse(
        question_id=question.id,
        title=clean_product_text(question.title),
        details=clean_product_text(question.details),
        status=question.status,
        answer_count=count,
        waiting_seconds=max(0, int((utc_now() - created).total_seconds())),
        created_at=created,
        updated_at=_as_utc(question.updated_at),
    )


async def _question_detail(
    session: AsyncSession,
    question: CommunityQuestion,
    *,
    public: bool,
    principal: RequestPrincipal | None = None,
) -> QuestionDetailResponse:
    summary = await _question_summary(session, question)
    answer_query = (
        select(CommunityAnswer, CampusUser, LocalContributorCredential)
        .join(CampusUser, CommunityAnswer.contributor_user_id == CampusUser.id)
        .outerjoin(
            LocalContributorCredential,
            LocalContributorCredential.user_id == CampusUser.id,
        )
        .where(CommunityAnswer.question_id == question.id)
    )
    if public:
        answer_query = answer_query.where(CommunityAnswer.status == "visible")
    answers = list(
        (await session.execute(answer_query.order_by(CommunityAnswer.created_at.asc()))).all()
    )
    return QuestionDetailResponse(
        **summary.model_dump(),
        evidence_gap=clean_product_text(question.evidence_gap),
        answers=[
            _community_answer_response_value(
                answer,
                user,
                credential,
                can_edit=(
                    principal is not None
                    and principal.authenticated
                    and (
                        principal.role == "admin" or answer.contributor_user_id == principal.user_id
                    )
                ),
            )
            for answer, user, credential in answers
        ],
    )


async def _community_answer_response(
    session: AsyncSession,
    answer: CommunityAnswer,
) -> CommunityAnswerResponse:
    row = (
        await session.execute(
            select(CampusUser, LocalContributorCredential)
            .outerjoin(
                LocalContributorCredential,
                LocalContributorCredential.user_id == CampusUser.id,
            )
            .where(CampusUser.id == answer.contributor_user_id)
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Contributor not found")
    user, credential = row
    return _community_answer_response_value(answer, user, credential)


def _community_answer_response_value(
    answer: CommunityAnswer,
    user: CampusUser,
    credential: LocalContributorCredential | None = None,
    *,
    can_edit: bool = False,
) -> CommunityAnswerResponse:
    # Login names are deliberately never exposed on the public board.
    public_name = (
        clean_product_text(credential.public_name)
        if credential is not None
        else ("管理员" if user.role == "admin" else "授权贡献者")
    )
    unit = clean_product_text(credential.unit) if credential and credential.unit else None
    return CommunityAnswerResponse(
        answer_id=answer.id,
        question_id=answer.question_id,
        answer_markdown=clean_product_text(answer.answer_markdown),
        display_name=public_name,
        unit=unit,
        status=answer.status,
        knowledge_review_state=answer.knowledge_review_state,
        can_edit=can_edit,
        created_at=_as_utc(answer.created_at),
        updated_at=_as_utc(answer.updated_at),
    )


def _contributor_response(item: LocalContributorCredential) -> ContributorResponse:
    return ContributorResponse(
        contributor_id=item.id,
        username=item.username,
        public_name=clean_product_text(item.public_name),
        unit=clean_product_text(item.unit) if item.unit else None,
        status=item.status,
        created_at=_as_utc(item.created_at),
        updated_at=_as_utc(item.updated_at),
        last_login_at=_as_utc(item.last_login_at) if item.last_login_at else None,
    )


async def _create_knowledge_entry(
    session: AsyncSession,
    payload: KnowledgeEntryRequest,
    principal: RequestPrincipal,
) -> KnowledgeEntry:
    if payload.question_id:
        existing = await session.scalar(
            select(KnowledgeEntry).where(KnowledgeEntry.question_id == payload.question_id)
        )
        if existing is not None:
            raise HTTPException(status_code=409, detail="该问题已经存在人工知识条目。")
        question = await session.get(CommunityQuestion, payload.question_id)
        if question is None:
            raise HTTPException(status_code=404, detail="Question not found")
    now = utc_now()
    entry = KnowledgeEntry(
        id=new_id("knowledge"),
        question_id=payload.question_id,
        title=clean_product_text(payload.title).strip(),
        canonical_question=clean_product_text(payload.canonical_question).strip(),
        answer_markdown=clean_product_text(payload.answer_markdown).strip(),
        category=clean_product_text(payload.category).strip() or "校园综合",
        alternative_phrasings=[
            clean_product_text(item).strip() for item in payload.alternative_phrasings
        ],
        applicable_scope=clean_product_text(payload.applicable_scope).strip(),
        maintainer_unit=clean_product_text(payload.maintainer_unit).strip(),
        basis_note=clean_product_text(payload.basis_note).strip(),
        validity=payload.validity,
        effective_from=payload.effective_from,
        effective_to=payload.effective_to,
        visibility=payload.visibility,
        status="draft",
        created_by_user_id=principal.user_id,
        created_at=now,
        updated_at=now,
    )
    session.add(entry)
    await session.flush()
    await _replace_origins(session, entry, payload.origin_answer_ids, payload.question_id)
    return entry


async def _apply_knowledge_payload(
    session: AsyncSession,
    entry: KnowledgeEntry,
    payload: KnowledgeEntryRequest,
) -> None:
    entry.question_id = payload.question_id
    entry.title = clean_product_text(payload.title).strip()
    entry.canonical_question = clean_product_text(payload.canonical_question).strip()
    entry.answer_markdown = clean_product_text(payload.answer_markdown).strip()
    entry.category = clean_product_text(payload.category).strip() or "校园综合"
    entry.alternative_phrasings = [
        clean_product_text(item).strip() for item in payload.alternative_phrasings
    ]
    entry.applicable_scope = clean_product_text(payload.applicable_scope).strip()
    entry.maintainer_unit = clean_product_text(payload.maintainer_unit).strip()
    entry.basis_note = clean_product_text(payload.basis_note).strip()
    entry.validity = payload.validity
    entry.effective_from = payload.effective_from
    entry.effective_to = payload.effective_to
    entry.visibility = payload.visibility


async def _replace_origins(
    session: AsyncSession,
    entry: KnowledgeEntry,
    answer_ids: list[str],
    question_id: str | None,
) -> None:
    previous_origins = list(
        (
            await session.scalars(
                select(KnowledgeEntryOrigin).where(
                    KnowledgeEntryOrigin.knowledge_entry_id == entry.id,
                    KnowledgeEntryOrigin.community_answer_id.is_not(None),
                )
            )
        ).all()
    )
    for origin in previous_origins:
        if origin.community_answer_id is None:
            continue
        previous_answer = await session.get(CommunityAnswer, origin.community_answer_id)
        if previous_answer is not None and previous_answer.knowledge_review_state == "published":
            previous_answer.knowledge_review_state = "source_changed"
    await session.execute(
        delete(KnowledgeEntryOrigin).where(KnowledgeEntryOrigin.knowledge_entry_id == entry.id)
    )
    if not answer_ids:
        session.add(
            KnowledgeEntryOrigin(
                id=new_id("knowledge_origin"),
                knowledge_entry_id=entry.id,
                community_answer_id=None,
                origin_kind="manual",
                content_hash=_hash_text(entry.answer_markdown),
                created_at=utc_now(),
            )
        )
        return
    answers = list(
        (
            await session.scalars(select(CommunityAnswer).where(CommunityAnswer.id.in_(answer_ids)))
        ).all()
    )
    found = {answer.id: answer for answer in answers}
    if len(found) != len(set(answer_ids)):
        raise HTTPException(status_code=404, detail="Origin community answer not found")
    for answer_id in dict.fromkeys(answer_ids):
        answer = found[answer_id]
        answer_question = await session.get(CommunityQuestion, answer.question_id)
        if (
            answer.status != "visible"
            or answer_question is None
            or answer_question.status not in {"open", "answered"}
            or (question_id and answer.question_id != question_id)
        ):
            raise HTTPException(status_code=422, detail="Origin answer is not eligible")
        session.add(
            KnowledgeEntryOrigin(
                id=new_id("knowledge_origin"),
                knowledge_entry_id=entry.id,
                community_answer_id=answer.id,
                origin_kind="community_answer",
                content_hash=_hash_text(answer.answer_markdown),
                created_at=utc_now(),
            )
        )


async def _validate_origins_for_publish(
    session: AsyncSession,
    entry: KnowledgeEntry,
) -> None:
    """Keep hidden or unpublished community answers out of the Agent corpus."""

    origins = list(
        (
            await session.scalars(
                select(KnowledgeEntryOrigin).where(
                    KnowledgeEntryOrigin.knowledge_entry_id == entry.id,
                    KnowledgeEntryOrigin.community_answer_id.is_not(None),
                )
            )
        ).all()
    )
    for origin in origins:
        if origin.community_answer_id is None:
            continue
        answer = await session.get(CommunityAnswer, origin.community_answer_id)
        question = await session.get(CommunityQuestion, answer.question_id) if answer else None
        if (
            answer is None
            or answer.status != "visible"
            or question is None
            or question.status not in {"open", "answered"}
        ):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Origin answer must remain visible on an open question before publication",
            )


async def _mark_origins_published(session: AsyncSession, entry_id: str) -> None:
    origins = list(
        (
            await session.scalars(
                select(KnowledgeEntryOrigin).where(
                    KnowledgeEntryOrigin.knowledge_entry_id == entry_id
                )
            )
        ).all()
    )
    for origin in origins:
        origin.origin_kind = "published"
        if origin.community_answer_id:
            answer = await session.get(CommunityAnswer, origin.community_answer_id)
            if answer is not None:
                answer.knowledge_review_state = "published"


async def _origin_ids(session: AsyncSession, entry_id: str) -> list[str]:
    return [
        item
        for item in (
            await session.scalars(
                select(KnowledgeEntryOrigin.community_answer_id).where(
                    KnowledgeEntryOrigin.knowledge_entry_id == entry_id,
                    KnowledgeEntryOrigin.community_answer_id.is_not(None),
                )
            )
        ).all()
        if item is not None
    ]


def _knowledge_response(entry: KnowledgeEntry, origin_ids: list[str]) -> KnowledgeEntryResponse:
    return KnowledgeEntryResponse(
        question_id=entry.question_id,
        title=clean_product_text(entry.title),
        canonical_question=clean_product_text(entry.canonical_question),
        answer_markdown=clean_product_text(entry.answer_markdown),
        category=clean_product_text(entry.category),
        alternative_phrasings=[
            clean_product_text(item) for item in (entry.alternative_phrasings or [])
        ],
        applicable_scope=clean_product_text(entry.applicable_scope),
        maintainer_unit=clean_product_text(entry.maintainer_unit),
        basis_note=clean_product_text(entry.basis_note),
        validity=entry.validity,
        effective_from=_as_utc(entry.effective_from) if entry.effective_from else None,
        effective_to=_as_utc(entry.effective_to) if entry.effective_to else None,
        visibility=entry.visibility,
        origin_answer_ids=origin_ids,
        entry_id=entry.id,
        status=entry.status,
        published_source_id=entry.published_source_id,
        published_resource_id=entry.published_resource_id,
        published_version_id=entry.published_version_id,
        content_hash=entry.content_hash,
        created_by_user_id=entry.created_by_user_id,
        created_at=_as_utc(entry.created_at),
        updated_at=_as_utc(entry.updated_at),
        published_at=_as_utc(entry.published_at) if entry.published_at else None,
    )


def _fallback_knowledge_optimization(entry: KnowledgeEntry) -> KnowledgeOptimizationResponse:
    title = normalize_text(entry.title).strip() or entry.canonical_question[:120]
    category = normalize_text(entry.category).strip() or "校园综合"
    phrases = list(dict.fromkeys([entry.canonical_question, *(entry.alternative_phrasings or [])]))[
        :12
    ]
    if entry.validity == "time_bounded":
        risk = "时间限定条目：回答时必须同时核对生效、失效日期，不能推广到其他年份。"
    elif entry.visibility == "campus":
        risk = "校园可见条目：只对具备 campus 可见范围的主体召回。"
    else:
        risk = "未发现额外范围风险。"
    return KnowledgeOptimizationResponse(
        entry_id=entry.id,
        suggested_title=title,
        suggested_category=category,
        suggested_phrasings=phrases,
        scope_risk=risk,
    )


def _sanitize_knowledge_optimization(
    result: KnowledgeOptimizationResponse,
    fallback: KnowledgeOptimizationResponse,
) -> KnowledgeOptimizationResponse:
    title = clean_product_text(result.suggested_title).strip()[:240] or fallback.suggested_title
    category = (
        clean_product_text(result.suggested_category).strip()[:120] or fallback.suggested_category
    )
    phrases = []
    for value in result.suggested_phrasings:
        phrase = clean_product_text(value).strip()[:240]
        if phrase and phrase not in phrases:
            phrases.append(phrase)
        if len(phrases) >= 12:
            break
    if not phrases:
        phrases = fallback.suggested_phrasings
    risk = clean_product_text(result.scope_risk).strip()[:1000] or fallback.scope_risk
    return KnowledgeOptimizationResponse(
        entry_id=fallback.entry_id,
        suggested_title=title,
        suggested_category=category,
        suggested_phrasings=phrases,
        scope_risk=risk,
    )


def _knowledge_factual_payload(entry: KnowledgeEntry) -> dict[str, object]:
    return {
        "title": entry.title,
        "canonical_question": entry.canonical_question,
        "answer_markdown": entry.answer_markdown,
        "category": entry.category,
        "alternative_phrasings": entry.alternative_phrasings or [],
        "applicable_scope": entry.applicable_scope,
        "maintainer_unit": entry.maintainer_unit,
        "basis_note": entry.basis_note,
        "validity": entry.validity,
        "effective_from": entry.effective_from.isoformat() if entry.effective_from else None,
        "effective_to": entry.effective_to.isoformat() if entry.effective_to else None,
        "visibility": entry.visibility,
    }


def _knowledge_hash(payload: KnowledgeEntryRequest) -> str:
    return _hash_text(
        json.dumps(
            {
                "title": payload.title,
                "canonical_question": payload.canonical_question,
                "answer_markdown": payload.answer_markdown,
                "category": payload.category,
                "alternative_phrasings": payload.alternative_phrasings,
                "applicable_scope": payload.applicable_scope,
                "maintainer_unit": payload.maintainer_unit,
                "basis_note": payload.basis_note,
                "validity": payload.validity,
                "effective_from": payload.effective_from.isoformat()
                if payload.effective_from
                else None,
                "effective_to": payload.effective_to.isoformat() if payload.effective_to else None,
                "visibility": payload.visibility,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


def _knowledge_document_text(entry: KnowledgeEntry) -> str:
    phrases = "、".join(entry.alternative_phrasings or [])
    window = ""
    if entry.effective_from or entry.effective_to:
        window = f"\n生效范围：{entry.effective_from or ''} 至 {entry.effective_to or ''}"
    return normalize_text(
        "\n".join(
            item
            for item in (
                f"规范标题：{entry.title}",
                f"典型问题：{entry.canonical_question}",
                f"回答：{entry.answer_markdown}",
                f"分类：{entry.category}",
                f"替代表达：{phrases}" if phrases else "",
                f"适用范围：{entry.applicable_scope}" if entry.applicable_scope else "",
                f"维护单位：{entry.maintainer_unit}" if entry.maintainer_unit else "",
                f"依据说明：{entry.basis_note}" if entry.basis_note else "",
                f"资料属性：人工核验资料；可见范围 {entry.visibility}{window}",
            )
            if item
        )
    )


async def _audit(
    session: AsyncSession,
    principal: RequestPrincipal,
    event_type: str,
    metadata: dict[str, object],
) -> None:
    session.add(
        SecurityAuditEvent(
            id=new_id("audit"),
            actor_user_id=principal.user_id,
            event_type=event_type,
            outcome="succeeded",
            request_id=request_id_context.get(),
            event_metadata=metadata,
            occurred_at=utc_now(),
        )
    )


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
