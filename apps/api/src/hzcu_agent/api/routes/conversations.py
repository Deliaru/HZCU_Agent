import asyncio
import base64
import json
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from hzcu_agent.api.dependencies import (
    enforce_csrf,
    enforce_required_login,
    request_principal,
    request_session,
)
from hzcu_agent.auth.service import RequestPrincipal
from hzcu_agent.models import (
    AgentTask,
    AnswerClaimRecord,
    AnswerGroundingRecord,
    AnswerRecord,
    ClaimEvidenceRecord,
    CommunityQuestion,
    Conversation,
    EvidenceRecord,
    Message,
    ProfileAttribute,
    TaskPerformanceRecord,
    new_id,
    utc_now,
)
from hzcu_agent.schemas import (
    AcceptedTaskResponse,
    AgentPerformance,
    AnswerClaim,
    AnswerResponse,
    ClaimCitation,
    ConversationDetailResponse,
    ConversationListResponse,
    ConversationMessageResponse,
    ConversationPatchRequest,
    ConversationResponse,
    ConversationSummaryResponse,
    ConversationTaskSummary,
    CreateConversationRequest,
    Evidence,
    GroundingSummary,
    ProfileAttributeResponse,
    QuestionOfferResponse,
    SendMessageRequest,
    TaskResponse,
    VerificationFinding,
)
from hzcu_agent.services.agent_admission import AgentAdmissionError, admission_http_exception
from hzcu_agent.text_safety import clean_product_json, clean_product_text

router = APIRouter(tags=["agent"])
SessionDependency = Annotated[AsyncSession, Depends(request_session)]
PrincipalDependency = Annotated[RequestPrincipal, Depends(request_principal)]
TERMINAL_TASK_STATES = frozenset({"completed", "failed", "canceled"})


@router.post(
    "/conversations",
    response_model=ConversationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_conversation(
    payload: CreateConversationRequest,
    request: Request,
    session: SessionDependency,
    principal: PrincipalDependency,
) -> ConversationResponse:
    _require_agent_access(principal)
    enforce_required_login(request, principal)
    enforce_csrf(request, principal)
    if principal.product_subject_id is None:
        raise HTTPException(status_code=503, detail="Product identity unavailable")
    created_at = utc_now()
    cleaned_title = clean_product_text(payload.title).strip() if payload.title else None
    conversation = Conversation(
        id=new_id("conv"),
        owner_user_id=principal.user_id,
        owner_subject_id=principal.product_subject_id,
        title=cleaned_title or None,
        # Legacy field remains for compatibility; only confirmed profile rows are
        # ever passed to the model in Stage 6.
        profile_context={},
        created_at=created_at,
        updated_at=created_at,
    )
    session.add(conversation)
    await session.commit()
    return ConversationResponse(conversation_id=conversation.id, created_at=created_at)


@router.get("/conversations", response_model=ConversationListResponse)
async def list_conversations(
    session: SessionDependency,
    principal: PrincipalDependency,
    limit: int = Query(default=30, ge=1, le=60),
    cursor: str | None = Query(default=None, max_length=300),
) -> ConversationListResponse:
    _require_agent_access(principal)
    subject_id = _require_subject(principal)
    query = select(Conversation).where(Conversation.owner_subject_id == subject_id)
    if cursor:
        cursor_time, cursor_id = _decode_cursor(cursor)
        query = query.where(
            or_(
                Conversation.updated_at < cursor_time,
                and_(
                    Conversation.updated_at == cursor_time,
                    Conversation.id < cursor_id,
                ),
            )
        )
    conversations = list(
        (
            await session.scalars(
                query.order_by(
                    Conversation.updated_at.desc(),
                    Conversation.id.desc(),
                ).limit(limit + 1)
            )
        ).all()
    )
    has_more = len(conversations) > limit
    page = conversations[:limit]
    items: list[ConversationSummaryResponse] = []
    for conversation in page:
        last_message = await session.scalar(
            select(Message)
            .where(Message.conversation_id == conversation.id)
            .order_by(Message.created_at.desc())
            .limit(1)
        )
        last_task = await session.scalar(
            select(AgentTask)
            .where(AgentTask.conversation_id == conversation.id)
            .order_by(AgentTask.created_at.desc())
            .limit(1)
        )
        items.append(
            ConversationSummaryResponse(
                conversation_id=conversation.id,
                title=conversation.title,
                created_at=_as_utc(conversation.created_at),
                updated_at=_as_utc(conversation.updated_at),
                last_message=(
                    clean_product_text(last_message.content)[:180]
                    if last_message is not None
                    else None
                ),
                last_task_status=last_task.status if last_task is not None else None,
            )
        )
    next_cursor = _encode_cursor(page[-1].updated_at, page[-1].id) if has_more and page else None
    return ConversationListResponse(items=items, next_cursor=next_cursor)


@router.get(
    "/conversations/{conversation_id}",
    response_model=ConversationDetailResponse,
)
async def get_conversation(
    conversation_id: str,
    session: SessionDependency,
    principal: PrincipalDependency,
) -> ConversationDetailResponse:
    _require_agent_access(principal)
    conversation = await session.get(Conversation, conversation_id)
    if conversation is None or not _can_access_conversation(conversation, principal):
        raise HTTPException(status_code=404, detail="Conversation not found")
    messages = list(
        (
            await session.scalars(
                select(Message)
                .where(Message.conversation_id == conversation_id)
                .order_by(Message.created_at.asc(), Message.id.asc())
            )
        ).all()
    )
    tasks = list(
        (
            await session.scalars(
                select(AgentTask)
                .where(AgentTask.conversation_id == conversation_id)
                .order_by(AgentTask.created_at.asc(), AgentTask.id.asc())
            )
        ).all()
    )
    return ConversationDetailResponse(
        conversation_id=conversation.id,
        title=conversation.title,
        created_at=_as_utc(conversation.created_at),
        updated_at=_as_utc(conversation.updated_at),
        messages=[
            ConversationMessageResponse(
                message_id=item.id,
                role=item.role,
                content=clean_product_text(item.content),
                created_at=_as_utc(item.created_at),
                client_message_id=item.client_message_id,
            )
            for item in messages
        ],
        tasks=[
            ConversationTaskSummary(
                task_id=item.id,
                user_message_id=item.user_message_id,
                status=item.status,
                error_code=item.error_code,
                answer_id=item.answer_id,
                request_mode=item.request_mode,
                parent_task_id=item.parent_task_id,
                created_at=_as_utc(item.created_at),
                updated_at=_as_utc(item.updated_at),
            )
            for item in tasks
        ],
    )


@router.patch(
    "/conversations/{conversation_id}",
    response_model=ConversationSummaryResponse,
)
async def patch_conversation(
    conversation_id: str,
    payload: ConversationPatchRequest,
    request: Request,
    session: SessionDependency,
    principal: PrincipalDependency,
) -> ConversationSummaryResponse:
    _require_agent_access(principal)
    enforce_csrf(request, principal)
    conversation = await session.get(Conversation, conversation_id)
    if conversation is None or not _can_access_conversation(conversation, principal):
        raise HTTPException(status_code=404, detail="Conversation not found")
    conversation.title = clean_product_text(payload.title).strip()
    conversation.updated_at = utc_now()
    await session.commit()
    return ConversationSummaryResponse(
        conversation_id=conversation.id,
        title=conversation.title,
        created_at=_as_utc(conversation.created_at),
        updated_at=_as_utc(conversation.updated_at),
    )


@router.delete(
    "/conversations/{conversation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_conversation(
    conversation_id: str,
    request: Request,
    session: SessionDependency,
    principal: PrincipalDependency,
) -> None:
    _require_agent_access(principal)
    enforce_csrf(request, principal)
    conversation = await session.get(Conversation, conversation_id)
    if conversation is None or not _can_access_conversation(conversation, principal):
        raise HTTPException(status_code=404, detail="Conversation not found")
    task_ids = list(
        (
            await session.scalars(
                select(AgentTask.id).where(AgentTask.conversation_id == conversation_id)
            )
        ).all()
    )
    running_tasks: list[asyncio.Task[object]] = []
    for task_id in task_ids:
        running = request.app.state.background_tasks.get(task_id)
        if running is not None and not running.done():
            running.cancel()
            running_tasks.append(running)
    if running_tasks:
        await asyncio.gather(*running_tasks, return_exceptions=True)
    await session.delete(conversation)
    await session.commit()


@router.post(
    "/conversations/{conversation_id}/messages",
    response_model=AcceptedTaskResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def send_message(
    conversation_id: str,
    payload: SendMessageRequest,
    request: Request,
    session: SessionDependency,
    principal: PrincipalDependency,
) -> AcceptedTaskResponse:
    _require_agent_access(principal)
    enforce_required_login(request, principal)
    enforce_csrf(request, principal)
    conversation = await session.get(Conversation, conversation_id)
    if conversation is None or not _can_access_conversation(conversation, principal):
        raise HTTPException(status_code=404, detail="Conversation not found")

    client_message_id = (
        clean_product_text(payload.client_message_id).strip() if payload.client_message_id else None
    )
    if payload.client_message_id and not client_message_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="client_message_id must contain visible text",
        )
    if client_message_id:
        prior_message = await session.scalar(
            select(Message).where(
                Message.conversation_id == conversation_id,
                Message.client_message_id == client_message_id,
            )
        )
        if prior_message is not None:
            prior_task = await session.scalar(
                select(AgentTask).where(AgentTask.user_message_id == prior_message.id)
            )
            if prior_task is not None:
                return await _accepted_task_response(request, session, prior_task)

    message_text = clean_product_text(payload.message).strip()
    if not message_text:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Message must contain visible text",
        )
    max_message_length = request.app.state.policy.snapshot().max_message_length
    if len(message_text) > max_message_length:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "MESSAGE_TOO_LONG",
                "message": f"单次提问最多 {max_message_length} 个字符。",
            },
        )
    # Close the read transaction opened while checking the conversation before
    # waiting on the process-wide admission lease.  This prevents a queued
    # request from holding a SQLite shared lock while another request commits
    # its reservation and task in the same controlled transaction.
    await session.commit()
    now = utc_now()
    task_id = new_id("task")
    admission = None
    try:
        try:
            admission = await request.app.state.admission.admit(
                session,
                request=request,
                principal=principal,
                task_id=task_id,
                request_kind="normal",
                hold_lease=True,
            )
        except AgentAdmissionError as exc:
            raise admission_http_exception(exc) from exc
        message = Message(
            id=new_id("msg"),
            conversation_id=conversation_id,
            role="user",
            content=message_text,
            client_message_id=client_message_id,
            created_at=now,
        )
        task = AgentTask(
            id=task_id,
            conversation_id=conversation_id,
            user_message_id=message.id,
            status="queued",
            access_scopes=sorted(principal.visibility_scopes),
            request_mode="normal",
            response_style=payload.response_style,
            requested_by_subject_id=principal.product_subject_id,
            queue_deadline_at=admission.queue_deadline_at,
            created_at=now,
            updated_at=now,
        )
        if not conversation.title:
            conversation.title = message_text[:60]
        conversation.updated_at = now
        session.add(message)
        try:
            await session.flush()
            session.add(task)
            await session.commit()
        except IntegrityError:
            # The database uniqueness constraint is the final idempotency gate.
            # Two browser retries may pass the optimistic lookup concurrently; the
            # loser resolves the already-created task instead of surfacing a 500.
            await session.rollback()
            if client_message_id:
                prior_message = await session.scalar(
                    select(Message).where(
                        Message.conversation_id == conversation_id,
                        Message.client_message_id == client_message_id,
                    )
                )
                prior_task = (
                    await session.scalar(
                        select(AgentTask).where(AgentTask.user_message_id == prior_message.id)
                    )
                    if prior_message is not None
                    else None
                )
                if prior_task is not None:
                    return await _accepted_task_response(request, session, prior_task)
            raise
    finally:
        if admission is not None:
            await admission.release()
    await _launch_task(request, task)
    return await _accepted_task_response(request, session, task)


@router.get("/tasks/{task_id}", response_model=TaskResponse)
async def get_task(
    task_id: str,
    session: SessionDependency,
    principal: PrincipalDependency,
) -> TaskResponse:
    _require_agent_access(principal)
    task = await session.get(AgentTask, task_id)
    if task is None or not await _can_access_task(session, task, principal):
        raise HTTPException(status_code=404, detail="Task not found")
    return TaskResponse(
        task_id=task.id,
        status=task.status,
        answer_id=task.answer_id,
        error_code=task.error_code,
        queue_position=await _queue_position(session, task),
    )


@router.post("/tasks/{task_id}/cancel", response_model=TaskResponse)
async def cancel_task(
    task_id: str,
    request: Request,
    session: SessionDependency,
    principal: PrincipalDependency,
) -> TaskResponse:
    _require_agent_access(principal)
    enforce_csrf(request, principal)
    task = await session.get(AgentTask, task_id)
    if task is None or not await _can_access_task(session, task, principal):
        raise HTTPException(status_code=404, detail="Task not found")

    canceled_at = utc_now()
    cancellation = await session.execute(
        update(AgentTask)
        .where(
            AgentTask.id == task.id,
            AgentTask.status.not_in(TERMINAL_TASK_STATES),
            AgentTask.answer_id.is_(None),
        )
        .values(
            status="canceled",
            error_code="CANCELED_BY_USER",
            updated_at=canceled_at,
        )
    )
    cancellation_won = cancellation.rowcount == 1
    await session.commit()
    await session.refresh(task)

    if cancellation_won:
        background = request.app.state.background_tasks.get(task.id)
        if background is not None:
            background.cancel()
        await request.app.state.broker.publish(
            task.id,
            "task.failed",
            {
                "task_id": task.id,
                "error_code": "CANCELED_BY_USER",
                "message": "已取消本次调查。",
            },
        )
    elif task.answer_id is not None and task.status != "completed":
        # An answer that crossed the persistence boundary is immutable and can
        # already be restored. Treat a very late cancel click as completion
        # instead of leaving a canceled task that still owns an answer.
        await session.execute(
            update(AgentTask)
            .where(
                AgentTask.id == task.id,
                AgentTask.answer_id.is_not(None),
                AgentTask.status != "completed",
            )
            .values(
                status="completed",
                error_code=None,
                updated_at=utc_now(),
            )
        )
        await session.commit()
        await session.refresh(task)
    return TaskResponse(
        task_id=task.id,
        status=task.status,
        answer_id=task.answer_id,
        error_code=task.error_code,
        queue_position=await _queue_position(session, task),
    )


@router.post(
    "/tasks/{task_id}/retry",
    response_model=AcceptedTaskResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def retry_task(
    task_id: str,
    request: Request,
    session: SessionDependency,
    principal: PrincipalDependency,
) -> AcceptedTaskResponse:
    _require_agent_access(principal)
    enforce_csrf(request, principal)
    parent = await session.get(AgentTask, task_id)
    if parent is None or not await _can_access_task(session, parent, principal):
        raise HTTPException(status_code=404, detail="Task not found")
    if parent.status not in {"failed", "canceled", "completed"}:
        raise HTTPException(status_code=409, detail="Task is still active")
    await session.commit()
    task = _derived_task(parent, principal, mode="retry")
    admission = None
    try:
        try:
            admission = await request.app.state.admission.admit(
                session,
                request=request,
                principal=principal,
                task_id=task.id,
                request_kind="retry",
                hold_lease=True,
            )
        except AgentAdmissionError as exc:
            raise admission_http_exception(exc) from exc
        task.queue_deadline_at = admission.queue_deadline_at
        session.add(task)
        await session.commit()
    finally:
        if admission is not None:
            await admission.release()
    await _launch_task(request, task)
    return await _accepted_task_response(request, session, task)


@router.post(
    "/answers/{answer_id}/reverify",
    response_model=AcceptedTaskResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def reverify_answer(
    answer_id: str,
    request: Request,
    session: SessionDependency,
    principal: PrincipalDependency,
) -> AcceptedTaskResponse:
    _require_agent_access(principal)
    enforce_csrf(request, principal)
    answer = await session.get(AnswerRecord, answer_id)
    parent = await session.get(AgentTask, answer.task_id) if answer is not None else None
    if parent is None or not await _can_access_task(session, parent, principal):
        raise HTTPException(status_code=404, detail="Answer not found")
    await session.commit()
    task = _derived_task(parent, principal, mode="live_reverify")
    admission = None
    try:
        try:
            admission = await request.app.state.admission.admit(
                session,
                request=request,
                principal=principal,
                task_id=task.id,
                request_kind="live_reverify",
                hold_lease=True,
            )
        except AgentAdmissionError as exc:
            raise admission_http_exception(exc) from exc
        task.queue_deadline_at = admission.queue_deadline_at
        session.add(task)
        await session.commit()
    finally:
        if admission is not None:
            await admission.release()
    await _launch_task(request, task)
    return await _accepted_task_response(request, session, task)


@router.get("/tasks/{task_id}/events")
async def stream_task_events(
    task_id: str,
    request: Request,
    session: SessionDependency,
    principal: PrincipalDependency,
    after: int = Query(default=0, ge=0),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> EventSourceResponse:
    _require_agent_access(principal)
    task = await session.get(AgentTask, task_id)
    if task is None or not await _can_access_task(session, task, principal):
        raise HTTPException(status_code=404, detail="Task not found")
    try:
        after_sequence = max(after, int(last_event_id or 0))
    except ValueError:
        after_sequence = after

    async def event_generator():
        async for event in request.app.state.broker.subscribe(task_id, after_sequence):
            if await request.is_disconnected():
                break
            yield {
                "id": str(event.sequence),
                "event": event.event,
                "data": json.dumps(event.data, ensure_ascii=False),
            }

    return EventSourceResponse(
        event_generator(),
        ping=15,
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


@router.get(
    "/answers/{answer_id}/evidence",
    response_model=list[Evidence],
)
async def list_answer_evidence(
    answer_id: str,
    session: SessionDependency,
    principal: PrincipalDependency,
) -> list[Evidence]:
    _require_agent_access(principal)
    answer = await session.get(AnswerRecord, answer_id)
    task = await session.get(AgentTask, answer.task_id) if answer is not None else None
    if task is None or not await _can_access_task(session, task, principal):
        raise HTTPException(status_code=404, detail="Answer not found")
    records = list(
        (
            await session.scalars(
                select(EvidenceRecord)
                .where(EvidenceRecord.answer_id == answer_id)
                .order_by(EvidenceRecord.id.asc())
            )
        ).all()
    )
    return [_evidence_response(item) for item in records]


@router.get("/evidence/{evidence_id}", response_model=Evidence)
async def get_evidence(
    evidence_id: str,
    session: SessionDependency,
    principal: PrincipalDependency,
) -> Evidence:
    _require_agent_access(principal)
    record = await session.get(EvidenceRecord, evidence_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Evidence not found")
    answer = await session.get(AnswerRecord, record.answer_id)
    task = await session.get(AgentTask, answer.task_id) if answer is not None else None
    if task is None or not await _can_access_task(session, task, principal):
        raise HTTPException(status_code=404, detail="Evidence not found")
    return _evidence_response(record)


@router.get("/answers/{answer_id}", response_model=AnswerResponse)
async def get_answer(
    answer_id: str,
    session: SessionDependency,
    principal: PrincipalDependency,
) -> AnswerResponse:
    _require_agent_access(principal)
    answer = await session.get(AnswerRecord, answer_id)
    if answer is None:
        raise HTTPException(status_code=404, detail="Answer not found")
    task = await session.get(AgentTask, answer.task_id)
    if task is None or not await _can_access_task(session, task, principal):
        raise HTTPException(status_code=404, detail="Answer not found")
    evidence_records = list(
        (
            await session.scalars(
                select(EvidenceRecord)
                .where(EvidenceRecord.answer_id == answer_id)
                .order_by(EvidenceRecord.id.asc())
            )
        ).all()
    )
    evidence = [_evidence_response(item) for item in evidence_records]
    claim_records = list(
        (
            await session.scalars(
                select(AnswerClaimRecord)
                .where(AnswerClaimRecord.answer_id == answer_id)
                .order_by(AnswerClaimRecord.ordinal.asc())
            )
        ).all()
    )
    claim_ids = [item.id for item in claim_records]
    citation_records = (
        list(
            (
                await session.scalars(
                    select(ClaimEvidenceRecord).where(ClaimEvidenceRecord.claim_id.in_(claim_ids))
                )
            ).all()
        )
        if claim_ids
        else []
    )
    citations_by_claim: dict[str, list[ClaimCitation]] = {}
    for citation in citation_records:
        citations_by_claim.setdefault(citation.claim_id, []).append(
            ClaimCitation(
                evidence_id=citation.evidence_id,
                relation=citation.relation,
                support_status=citation.support_status,
                rationale=clean_product_text(citation.rationale),
                supporting_excerpt=clean_product_text(citation.supporting_excerpt),
            )
        )
    claims = [
        AnswerClaim(
            claim_id=clean_product_text(item.claim_key),
            text=clean_product_text(item.text),
            statement_type=item.statement_type,
            importance=item.importance,
            scope=clean_product_text(item.scope),
            valid_at=_as_utc(item.valid_at) if item.valid_at else None,
            support_status=item.support_status,
            citations=citations_by_claim.get(item.id, []),
            uncertainty=clean_product_text(item.uncertainty),
        )
        for item in claim_records
    ]
    grounding_record = await session.get(AnswerGroundingRecord, answer_id)
    grounding = (
        GroundingSummary(
            status=clean_product_text(grounding_record.status),
            summary=clean_product_text(grounding_record.summary),
            verifier_verdict=clean_product_text(grounding_record.verifier_verdict),
            verifier_summary=clean_product_text(grounding_record.verifier_summary),
            citation_coverage=grounding_record.citation_coverage,
            fully_supported_rate=grounding_record.fully_supported_rate,
            findings=[
                VerificationFinding.model_validate(item) for item in grounding_record.findings
            ],
        )
        if grounding_record is not None
        else None
    )
    performance_record = await session.get(TaskPerformanceRecord, answer.task_id)
    performance = (
        AgentPerformance(
            scenario=performance_record.scenario,
            total_duration_ms=performance_record.total_duration_ms,
            excluded_model_ttft_ms=performance_record.excluded_model_ttft_ms,
            controllable_duration_ms=performance_record.controllable_duration_ms,
            first_progress_ms=performance_record.first_progress_ms,
            model_call_count=performance_record.model_call_count,
            tool_call_count=performance_record.tool_call_count,
            model_ttft_measurable=performance_record.model_ttft_measurable,
            spans=clean_product_json(performance_record.spans),
        )
        if performance_record is not None
        else None
    )
    suggestions = list(
        (
            await session.scalars(
                select(ProfileAttribute)
                .where(
                    ProfileAttribute.source_answer_id == answer_id,
                    ProfileAttribute.subject_id == principal.product_subject_id,
                )
                .order_by(ProfileAttribute.created_at.asc())
            )
        ).all()
    )
    return AnswerResponse(
        answer_id=answer.id,
        task_id=answer.task_id,
        headline=clean_product_text(answer.headline),
        answer_markdown=clean_product_text(answer.answer_markdown),
        assumptions=[clean_product_text(item) for item in answer.assumptions],
        next_actions=[clean_product_text(item) for item in answer.next_actions],
        confidence=answer.confidence,
        verification_mode=answer.verification_mode,
        response_style=task.response_style
        if task.response_style in {"neutral", "congyu"}
        else "neutral",
        evidence=evidence,
        claims=claims,
        grounding=grounding,
        performance=performance,
        profile_suggestions=[_profile_attribute_response(item) for item in suggestions],
        question_offer=await _question_offer_response(
            session,
            answer,
            user_message_id=task.user_message_id,
            task_status=task.status,
        ),
        created_at=_as_utc(answer.created_at),
    )


async def _launch_task(request: Request, task: AgentTask) -> None:
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is not None:
        await scheduler.enqueue(task.id)
        return
    broker = request.app.state.broker
    await broker.ensure(task.id)
    async with request.app.state.database.session_factory() as session:
        queue_position = await _queue_position(session, task)
    await broker.publish(
        task.id,
        "task.accepted",
        {
            "task_id": task.id,
            "conversation_id": task.conversation_id,
            "queue_position": queue_position,
        },
    )
    background_task = asyncio.create_task(
        request.app.state.coordinator.run(task.id),
        name=f"agent:{task.id}",
    )
    request.app.state.background_tasks[task.id] = background_task
    background_task.add_done_callback(
        lambda _completed, task_id=task.id: request.app.state.background_tasks.pop(
            task_id,
            None,
        )
    )


async def _accepted_task_response(
    request: Request,
    session: AsyncSession,
    task: AgentTask,
) -> AcceptedTaskResponse:
    return AcceptedTaskResponse(
        task_id=task.id,
        stream_url=f"{request.app.state.settings.api_prefix}/tasks/{task.id}/events",
        queue_position=await _queue_position(session, task),
    )


async def _queue_position(session: AsyncSession, task: AgentTask) -> int:
    if task.status != "queued" or not task.requested_by_subject_id:
        return 0
    earlier = await session.scalar(
        select(func.count(AgentTask.id)).where(
            AgentTask.status == "queued",
            or_(
                AgentTask.created_at < task.created_at,
                and_(
                    AgentTask.created_at == task.created_at,
                    AgentTask.id < task.id,
                ),
            ),
        )
    )
    return int(earlier or 0)


def _derived_task(
    parent: AgentTask,
    principal: RequestPrincipal,
    *,
    mode: str,
) -> AgentTask:
    now = utc_now()
    return AgentTask(
        id=new_id("task"),
        conversation_id=parent.conversation_id,
        user_message_id=parent.user_message_id,
        status="queued",
        access_scopes=sorted(principal.visibility_scopes),
        request_mode=mode,
        response_style=parent.response_style
        if parent.response_style in {"neutral", "congyu"}
        else "neutral",
        parent_task_id=parent.id,
        requested_by_subject_id=principal.product_subject_id,
        created_at=now,
        updated_at=now,
    )


def _require_subject(principal: RequestPrincipal) -> str:
    if principal.product_subject_id is None:
        raise HTTPException(status_code=503, detail="Product identity unavailable")
    return principal.product_subject_id


def _require_agent_access(principal: RequestPrincipal) -> None:
    if principal.authenticated and principal.role == "contributor":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "CONTRIBUTOR_AGENT_ACCESS_DENIED",
                "message": "贡献者账号仅可浏览问题广场并提交授权回答。",
            },
        )


def _can_access_conversation(
    conversation: Conversation,
    principal: RequestPrincipal,
) -> bool:
    if principal.product_subject_id is not None:
        return conversation.owner_subject_id == principal.product_subject_id
    return False


async def _can_access_task(
    session: AsyncSession,
    task: AgentTask,
    principal: RequestPrincipal,
) -> bool:
    conversation = await session.get(Conversation, task.conversation_id)
    return conversation is not None and _can_access_conversation(conversation, principal)


def _profile_attribute_response(item: ProfileAttribute) -> ProfileAttributeResponse:
    return ProfileAttributeResponse(
        attribute_id=item.id,
        attribute_key=item.attribute_key,
        attribute_value=clean_product_text(item.attribute_value),
        status=item.status,
        source_kind=item.source_kind,
        supporting_user_text=clean_product_text(item.supporting_user_text),
        source_answer_id=item.source_answer_id,
        created_at=_as_utc(item.created_at),
        updated_at=_as_utc(item.updated_at),
    )


def _evidence_response(item: EvidenceRecord) -> Evidence:
    return Evidence(
        evidence_id=clean_product_text(item.id),
        title=clean_product_text(item.title),
        publisher=clean_product_text(item.publisher),
        canonical_url=clean_product_text(item.canonical_url),
        excerpt=clean_product_text(item.excerpt),
        published_at=_as_utc(item.published_at) if item.published_at else None,
        observed_at=_as_utc(item.observed_at),
        fresh_until=_as_utc(item.fresh_until) if item.fresh_until else None,
        source_id=clean_product_text(item.source_id),
        resource_ref=(clean_product_text(item.resource_ref) if item.resource_ref else None),
        document_version_id=(
            clean_product_text(item.document_version_id) if item.document_version_id else None
        ),
        authority_level=item.authority_level,
        audience_scopes=[clean_product_text(value) for value in (item.audience_scopes or [])],
        effective_from=(_as_utc(item.effective_from) if item.effective_from else None),
        effective_to=_as_utc(item.effective_to) if item.effective_to else None,
        retrieval_mode=item.retrieval_mode,
    )


async def _question_offer_response(
    session: AsyncSession,
    answer: AnswerRecord,
    *,
    user_message_id: str,
    task_status: str,
) -> QuestionOfferResponse | None:
    if task_status != "completed":
        return None
    reason = answer.question_offer_reason
    if not reason:
        return None
    user_message = await session.get(Message, user_message_id)
    original = clean_product_text(user_message.content).strip() if user_message else ""
    question = await session.scalar(
        select(CommunityQuestion).where(CommunityQuestion.answer_id == answer.id)
    )
    details = (
        "这次回答缺少足够的可核验材料。你可以补充想确认的范围，提交后由管理员审核，"
        "再交给其他同学协助核对。"
    )
    if reason == "grounding_conflicting":
        details = "检索到的材料存在冲突，暂时无法安全确认；可以把具体年份、学院或通知范围写进问题。"
    elif reason == "grounding_stale":
        details = "找到的材料可能已经过期，暂时不能把它当作当前安排；可以补充需要核对的年份或事项。"
    elif reason == "verification_degraded":
        details = "最终引用校验没有完成，当前只展示安全边界；可以提交问题，让管理员继续整理。"
    gap = {
        "no_evidence": "没有找到足够的关联校园证据。",
        "low_confidence": "当前回答置信度较低，需要补充范围或权威材料。",
        "grounding_insufficient": "引用材料不足以覆盖回答中的关键断言。",
        "grounding_stale": "现有材料可能已经过期，无法确认当前安排。",
        "grounding_conflicting": "检索到的权威材料存在冲突，需要人工核对。",
        "verification_degraded": "引用核验未完成，暂时无法给出可靠结论。",
    }.get(reason, "当前证据不足，需要进一步核对。")
    return QuestionOfferResponse(
        reason=reason,
        title=question.title
        if question
        else (f"关于{original[:80]}的具体安排" if original else "需要进一步核对的校园问题"),
        details=question.details if question else details,
        evidence_gap=gap,
        existing_question_id=question.id if question else None,
        existing_status=question.status if question else None,
    )


def _encode_cursor(updated_at: datetime, conversation_id: str) -> str:
    value = f"{_as_utc(updated_at).isoformat()}|{conversation_id}".encode()
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _decode_cursor(value: str) -> tuple[datetime, str]:
    try:
        padding = "=" * (-len(value) % 4)
        decoded = base64.urlsafe_b64decode(f"{value}{padding}").decode()
        timestamp, conversation_id = decoded.split("|", maxsplit=1)
        parsed = datetime.fromisoformat(timestamp)
        if not conversation_id.startswith("conv_"):
            raise ValueError
        return _as_utc(parsed), conversation_id
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid cursor") from exc


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
