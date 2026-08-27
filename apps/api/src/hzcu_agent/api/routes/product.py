import asyncio
from datetime import UTC, datetime
from statistics import median
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from hzcu_agent.api.dependencies import enforce_csrf, request_principal, request_session
from hzcu_agent.auth.service import RequestPrincipal
from hzcu_agent.models import (
    AgentTask,
    AnswerFeedback,
    AnswerRecord,
    Conversation,
    Message,
    ProductSubject,
    ProfileAttribute,
    SecurityAuditEvent,
    StudentProfile,
    SyncRun,
    TaskPerformanceRecord,
    UserTodo,
    VisitorSession,
    new_id,
    utc_now,
)
from hzcu_agent.observability import request_id_context
from hzcu_agent.schemas import (
    AdminConversationTraceResponse,
    AdminOverviewResponse,
    AdminTaskHealthItem,
    AdminTaskHealthResponse,
    ConversationMessageResponse,
    ConversationTaskSummary,
    FeedbackCreateRequest,
    FeedbackRequest,
    FeedbackResponse,
    IdentityMergeResponse,
    ProfileAttributeKey,
    ProfileAttributeResponse,
    ProfilePatchRequest,
    ProfileResponse,
    TodoCreateRequest,
    TodoPatchRequest,
    TodoResponse,
)
from hzcu_agent.text_safety import clean_product_text

router = APIRouter(tags=["product"])
SessionDependency = Annotated[AsyncSession, Depends(request_session)]
PrincipalDependency = Annotated[RequestPrincipal, Depends(request_principal)]


@router.get("/profile", response_model=ProfileResponse)
async def get_profile(
    session: SessionDependency,
    principal: PrincipalDependency,
) -> ProfileResponse:
    subject_id = _require_subject(principal)
    profile = await _ensure_profile(session, subject_id)
    await session.commit()
    return await _profile_response(session, profile)


@router.patch("/profile", response_model=ProfileResponse)
async def patch_profile(
    payload: ProfilePatchRequest,
    request: Request,
    session: SessionDependency,
    principal: PrincipalDependency,
) -> ProfileResponse:
    enforce_csrf(request, principal)
    subject_id = _require_subject(principal)
    profile = await _ensure_profile(session, subject_id)
    if payload.personalization_enabled is not None:
        profile.personalization_enabled = payload.personalization_enabled
    if payload.onboarding_completed is not None:
        profile.onboarding_completed = payload.onboarding_completed
    now = utc_now()
    for incoming in payload.attributes:
        confirmed = list(
            (
                await session.scalars(
                    select(ProfileAttribute).where(
                        ProfileAttribute.subject_id == subject_id,
                        ProfileAttribute.attribute_key == incoming.attribute_key,
                        ProfileAttribute.status == "confirmed",
                    )
                )
            ).all()
        )
        value = clean_product_text(incoming.attribute_value).strip()
        if not value:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Profile attribute must contain visible text",
            )
        if confirmed:
            confirmed[0].attribute_value = value
            confirmed[0].source_kind = "user_manual"
            confirmed[0].supporting_user_text = value
            confirmed[0].updated_at = now
            for duplicate in confirmed[1:]:
                duplicate.status = "rejected"
                duplicate.updated_at = now
        else:
            session.add(
                ProfileAttribute(
                    id=new_id("pattr"),
                    subject_id=subject_id,
                    attribute_key=incoming.attribute_key,
                    attribute_value=value,
                    status="confirmed",
                    source_kind="user_manual",
                    supporting_user_text=value,
                    created_at=now,
                    updated_at=now,
                )
            )
    profile.updated_at = now
    await session.commit()
    return await _profile_response(session, profile)


@router.delete("/profile", status_code=status.HTTP_204_NO_CONTENT)
async def delete_personal_data(
    request: Request,
    session: SessionDependency,
    principal: PrincipalDependency,
) -> None:
    """Delete all Stage 6 personal product data while keeping a CA login usable."""

    enforce_csrf(request, principal)
    subject_id = _require_subject(principal)
    task_ids = list(
        (
            await session.scalars(
                select(AgentTask.id)
                .join(Conversation, AgentTask.conversation_id == Conversation.id)
                .where(Conversation.owner_subject_id == subject_id)
            )
        ).all()
    )
    running_tasks: list[asyncio.Task[object]] = []
    for task_id in task_ids:
        task = request.app.state.background_tasks.get(task_id)
        if task is not None and not task.done():
            task.cancel()
            running_tasks.append(task)
    if running_tasks:
        await asyncio.gather(*running_tasks, return_exceptions=True)
    await session.execute(delete(Conversation).where(Conversation.owner_subject_id == subject_id))
    await session.execute(delete(UserTodo).where(UserTodo.subject_id == subject_id))
    await session.execute(delete(AnswerFeedback).where(AnswerFeedback.subject_id == subject_id))
    await session.execute(delete(ProfileAttribute).where(ProfileAttribute.subject_id == subject_id))
    profile = await session.get(StudentProfile, subject_id)
    if profile is None:
        session.add(
            StudentProfile(
                subject_id=subject_id,
                personalization_enabled=True,
                onboarding_completed=False,
                created_at=utc_now(),
                updated_at=utc_now(),
            )
        )
    else:
        profile.personalization_enabled = True
        profile.onboarding_completed = False
        profile.updated_at = utc_now()
    await session.commit()


@router.post(
    "/profile/suggestions/{attribute_id}/confirm",
    response_model=ProfileAttributeResponse,
)
async def confirm_profile_suggestion(
    attribute_id: str,
    request: Request,
    session: SessionDependency,
    principal: PrincipalDependency,
) -> ProfileAttributeResponse:
    enforce_csrf(request, principal)
    subject_id = _require_subject(principal)
    attribute = await session.get(ProfileAttribute, attribute_id)
    if attribute is None or attribute.subject_id != subject_id:
        raise HTTPException(status_code=404, detail="Profile suggestion not found")
    now = utc_now()
    prior = list(
        (
            await session.scalars(
                select(ProfileAttribute).where(
                    ProfileAttribute.subject_id == subject_id,
                    ProfileAttribute.attribute_key == attribute.attribute_key,
                    ProfileAttribute.status == "confirmed",
                    ProfileAttribute.id != attribute.id,
                )
            )
        ).all()
    )
    for item in prior:
        item.status = "rejected"
        item.updated_at = now
    attribute.status = "confirmed"
    attribute.updated_at = now
    await session.commit()
    return _attribute_response(attribute)


@router.post(
    "/profile/suggestions/{attribute_id}/reject",
    response_model=ProfileAttributeResponse,
)
async def reject_profile_suggestion(
    attribute_id: str,
    request: Request,
    session: SessionDependency,
    principal: PrincipalDependency,
) -> ProfileAttributeResponse:
    enforce_csrf(request, principal)
    subject_id = _require_subject(principal)
    attribute = await session.get(ProfileAttribute, attribute_id)
    if attribute is None or attribute.subject_id != subject_id:
        raise HTTPException(status_code=404, detail="Profile suggestion not found")
    attribute.status = "rejected"
    attribute.updated_at = utc_now()
    await session.commit()
    return _attribute_response(attribute)


@router.post(
    "/profile/attributes/{attribute_name}/confirm",
    response_model=ProfileAttributeResponse,
)
async def confirm_profile_attribute(
    attribute_name: ProfileAttributeKey,
    request: Request,
    session: SessionDependency,
    principal: PrincipalDependency,
) -> ProfileAttributeResponse:
    """Compatibility endpoint for clients that address a field by its name."""

    enforce_csrf(request, principal)
    subject_id = _require_subject(principal)
    attribute = await _latest_attribute_by_name(
        session,
        subject_id,
        attribute_name,
        statuses=("suggested", "confirmed"),
    )
    if attribute is None:
        raise HTTPException(status_code=404, detail="Profile attribute not found")
    now = utc_now()
    prior = list(
        (
            await session.scalars(
                select(ProfileAttribute).where(
                    ProfileAttribute.subject_id == subject_id,
                    ProfileAttribute.attribute_key == attribute_name,
                    ProfileAttribute.status == "confirmed",
                    ProfileAttribute.id != attribute.id,
                )
            )
        ).all()
    )
    for item in prior:
        item.status = "rejected"
        item.updated_at = now
    attribute.status = "confirmed"
    attribute.updated_at = now
    await session.commit()
    return _attribute_response(attribute)


@router.post(
    "/profile/attributes/{attribute_name}/reject",
    response_model=ProfileAttributeResponse,
)
async def reject_profile_attribute(
    attribute_name: ProfileAttributeKey,
    request: Request,
    session: SessionDependency,
    principal: PrincipalDependency,
) -> ProfileAttributeResponse:
    """Compatibility endpoint for clients that address a field by its name."""

    enforce_csrf(request, principal)
    subject_id = _require_subject(principal)
    attribute = await _latest_attribute_by_name(
        session,
        subject_id,
        attribute_name,
        statuses=("suggested", "confirmed"),
    )
    if attribute is None:
        raise HTTPException(status_code=404, detail="Profile attribute not found")
    attribute.status = "rejected"
    attribute.updated_at = utc_now()
    await session.commit()
    return _attribute_response(attribute)


@router.delete(
    "/profile/attributes/{attribute_name}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_profile_attribute(
    attribute_name: ProfileAttributeKey,
    request: Request,
    session: SessionDependency,
    principal: PrincipalDependency,
) -> None:
    """Delete one allow-listed profile field without deleting the whole profile."""

    enforce_csrf(request, principal)
    subject_id = _require_subject(principal)
    await session.execute(
        delete(ProfileAttribute).where(
            ProfileAttribute.subject_id == subject_id,
            ProfileAttribute.attribute_key == attribute_name,
        )
    )
    profile = await _ensure_profile(session, subject_id)
    profile.updated_at = utc_now()
    await session.commit()


@router.get("/todos", response_model=list[TodoResponse])
async def list_todos(
    session: SessionDependency,
    principal: PrincipalDependency,
) -> list[TodoResponse]:
    subject_id = _require_subject(principal)
    todos = list(
        (
            await session.scalars(
                select(UserTodo)
                .where(UserTodo.subject_id == subject_id)
                .order_by(UserTodo.status.asc(), UserTodo.created_at.desc())
            )
        ).all()
    )
    return [_todo_response(item) for item in todos]


@router.post("/todos", response_model=TodoResponse, status_code=status.HTTP_201_CREATED)
async def create_todo(
    payload: TodoCreateRequest,
    request: Request,
    session: SessionDependency,
    principal: PrincipalDependency,
) -> TodoResponse:
    enforce_csrf(request, principal)
    subject_id = _require_subject(principal)
    if payload.source_answer_id and not await _answer_owned(
        session,
        payload.source_answer_id,
        subject_id,
    ):
        raise HTTPException(status_code=404, detail="Source answer not found")
    now = utc_now()
    todo = UserTodo(
        id=new_id("todo"),
        subject_id=subject_id,
        title=clean_product_text(payload.title).strip(),
        notes=clean_product_text(payload.notes).strip(),
        due_at=payload.due_at,
        status="open",
        source_answer_id=payload.source_answer_id,
        source_action_index=payload.source_action_index,
        created_at=now,
        updated_at=now,
    )
    if not todo.title:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Todo title must contain visible text",
        )
    session.add(todo)
    await session.commit()
    return _todo_response(todo)


@router.patch("/todos/{todo_id}", response_model=TodoResponse)
async def patch_todo(
    todo_id: str,
    payload: TodoPatchRequest,
    request: Request,
    session: SessionDependency,
    principal: PrincipalDependency,
) -> TodoResponse:
    enforce_csrf(request, principal)
    subject_id = _require_subject(principal)
    todo = await session.get(UserTodo, todo_id)
    if todo is None or todo.subject_id != subject_id:
        raise HTTPException(status_code=404, detail="Todo not found")
    changed = payload.model_fields_set
    if "title" in changed and payload.title is not None:
        title = clean_product_text(payload.title).strip()
        if not title:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Todo title must contain visible text",
            )
        todo.title = title
    if "notes" in changed and payload.notes is not None:
        todo.notes = clean_product_text(payload.notes).strip()
    if "due_at" in changed:
        todo.due_at = payload.due_at
    if "status" in changed and payload.status is not None:
        todo.status = payload.status
    todo.updated_at = utc_now()
    await session.commit()
    return _todo_response(todo)


@router.delete("/todos/{todo_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_todo(
    todo_id: str,
    request: Request,
    session: SessionDependency,
    principal: PrincipalDependency,
) -> None:
    enforce_csrf(request, principal)
    subject_id = _require_subject(principal)
    todo = await session.get(UserTodo, todo_id)
    if todo is None or todo.subject_id != subject_id:
        raise HTTPException(status_code=404, detail="Todo not found")
    await session.delete(todo)
    await session.commit()


@router.put("/answers/{answer_id}/feedback", response_model=FeedbackResponse)
async def put_feedback(
    answer_id: str,
    payload: FeedbackRequest,
    request: Request,
    session: SessionDependency,
    principal: PrincipalDependency,
) -> FeedbackResponse:
    enforce_csrf(request, principal)
    subject_id = _require_subject(principal)
    return await _upsert_feedback(
        session,
        subject_id=subject_id,
        answer_id=answer_id,
        payload=payload,
    )


@router.post("/feedback", response_model=FeedbackResponse)
async def create_feedback(
    payload: FeedbackCreateRequest,
    request: Request,
    session: SessionDependency,
    principal: PrincipalDependency,
) -> FeedbackResponse:
    """Compatibility endpoint matching the public Web API specification."""

    enforce_csrf(request, principal)
    subject_id = _require_subject(principal)
    return await _upsert_feedback(
        session,
        subject_id=subject_id,
        answer_id=payload.answer_id,
        payload=payload,
    )


@router.post("/identity/merge-visitor", response_model=IdentityMergeResponse)
async def merge_visitor_identity(
    request: Request,
    session: SessionDependency,
    principal: PrincipalDependency,
) -> IdentityMergeResponse:
    enforce_csrf(request, principal)
    if not principal.authenticated or not principal.visitor_subject_id:
        raise HTTPException(status_code=409, detail="No visitor data is available to merge")
    campus_id = _require_subject(principal)
    visitor_id = principal.visitor_subject_id
    if campus_id == visitor_id:
        raise HTTPException(status_code=409, detail="Identity is already merged")

    conversations = list(
        (
            await session.scalars(
                select(Conversation).where(Conversation.owner_subject_id == visitor_id)
            )
        ).all()
    )
    for item in conversations:
        item.owner_subject_id = campus_id
        item.owner_user_id = principal.user_id

    todos = list(
        (await session.scalars(select(UserTodo).where(UserTodo.subject_id == visitor_id))).all()
    )
    for item in todos:
        item.subject_id = campus_id

    moved_feedback = 0
    visitor_feedback = list(
        (
            await session.scalars(
                select(AnswerFeedback).where(AnswerFeedback.subject_id == visitor_id)
            )
        ).all()
    )
    for item in visitor_feedback:
        existing = await session.scalar(
            select(AnswerFeedback).where(
                AnswerFeedback.subject_id == campus_id,
                AnswerFeedback.answer_id == item.answer_id,
            )
        )
        if existing is None:
            item.subject_id = campus_id
            moved_feedback += 1
        else:
            await session.delete(item)

    campus_confirmed = {
        item.attribute_key
        for item in (
            await session.scalars(
                select(ProfileAttribute).where(
                    ProfileAttribute.subject_id == campus_id,
                    ProfileAttribute.status == "confirmed",
                )
            )
        ).all()
    }
    suggestions_created = 0
    visitor_attributes = list(
        (
            await session.scalars(
                select(ProfileAttribute).where(ProfileAttribute.subject_id == visitor_id)
            )
        ).all()
    )
    for item in visitor_attributes:
        item.subject_id = campus_id
        if item.status == "confirmed" and item.attribute_key in campus_confirmed:
            item.status = "suggested"
            suggestions_created += 1
        elif item.status == "confirmed":
            campus_confirmed.add(item.attribute_key)
        elif item.status == "suggested":
            suggestions_created += 1

    visitor = await session.get(ProductSubject, visitor_id)
    if visitor is not None:
        visitor.status = "merged"
        visitor.merged_into_subject_id = campus_id
        visitor.invalidated_at = utc_now()
    visitor_sessions = list(
        (
            await session.scalars(
                select(VisitorSession).where(VisitorSession.subject_id == visitor_id)
            )
        ).all()
    )
    for item in visitor_sessions:
        item.revoked_at = utc_now()
    visitor_profile = await session.get(StudentProfile, visitor_id)
    if visitor_profile is not None:
        await session.delete(visitor_profile)
    await session.commit()
    return IdentityMergeResponse(
        merged=True,
        conversations_moved=len(conversations),
        todos_moved=len(todos),
        feedback_moved=moved_feedback,
        suggestions_created=suggestions_created,
    )


@router.delete("/identity/visitor-data", status_code=status.HTTP_204_NO_CONTENT)
async def delete_visitor_data(
    request: Request,
    response: Response,
    session: SessionDependency,
    principal: PrincipalDependency,
) -> None:
    enforce_csrf(request, principal)
    if principal.authenticated:
        visitor_id = principal.visitor_subject_id
    else:
        visitor_id = principal.product_subject_id
    if visitor_id is None:
        return
    visitor = await session.get(ProductSubject, visitor_id)
    if visitor is not None and visitor.subject_kind == "visitor":
        task_ids = list(
            (
                await session.scalars(
                    select(AgentTask.id)
                    .join(Conversation, AgentTask.conversation_id == Conversation.id)
                    .where(Conversation.owner_subject_id == visitor_id)
                )
            ).all()
        )
        running_tasks: list[asyncio.Task[object]] = []
        for task_id in task_ids:
            task = request.app.state.background_tasks.get(task_id)
            if task is not None and not task.done():
                task.cancel()
                running_tasks.append(task)
        if running_tasks:
            await asyncio.gather(*running_tasks, return_exceptions=True)
        await session.delete(visitor)
        await session.commit()
    settings = request.app.state.settings
    response.delete_cookie(settings.visitor_cookie_name, path="/")
    if not principal.authenticated:
        response.delete_cookie(settings.auth_csrf_cookie_name, path="/")


@router.get("/admin/overview", response_model=AdminOverviewResponse)
async def admin_overview(
    session: SessionDependency,
    principal: PrincipalDependency,
) -> AdminOverviewResponse:
    _require_admin(principal)
    await _record_admin_read(session, principal, "admin.overview")
    statuses = list((await session.scalars(select(AgentTask.status))).all())
    completed = sum(item == "completed" for item in statuses)
    failed = sum(item == "failed" for item in statuses)
    durations = sorted(
        float(item)
        for item in (await session.scalars(select(TaskPerformanceRecord.total_duration_ms))).all()
    )
    p95 = durations[min(len(durations) - 1, int(len(durations) * 0.95))] if durations else None
    feedback_count = int(await session.scalar(select(func.count(AnswerFeedback.id))) or 0)
    source_alert_count = int(
        await session.scalar(
            select(func.count(SyncRun.id)).where(SyncRun.status.in_(("failed", "partial")))
        )
        or 0
    )
    return AdminOverviewResponse(
        task_count=len(statuses),
        completed_count=completed,
        failed_count=failed,
        success_rate=(completed / len(statuses) if statuses else 1.0),
        median_duration_ms=median(durations) if durations else None,
        p95_duration_ms=p95,
        feedback_count=feedback_count,
        source_alert_count=source_alert_count,
    )


@router.get("/admin/feedback", response_model=list[FeedbackResponse])
async def admin_feedback(
    session: SessionDependency,
    principal: PrincipalDependency,
) -> list[FeedbackResponse]:
    _require_admin(principal)
    await _record_admin_read(session, principal, "admin.feedback")
    rows = list(
        (
            await session.scalars(
                select(AnswerFeedback).order_by(AnswerFeedback.updated_at.desc()).limit(200)
            )
        ).all()
    )
    return [_feedback_response(item) for item in rows]


@router.get("/admin/task-health", response_model=AdminTaskHealthResponse)
async def admin_task_health(
    session: SessionDependency,
    principal: PrincipalDependency,
) -> AdminTaskHealthResponse:
    _require_admin(principal)
    await _record_admin_read(session, principal, "admin.task_health")
    rows = (
        await session.execute(
            select(AgentTask, TaskPerformanceRecord)
            .outerjoin(
                TaskPerformanceRecord,
                TaskPerformanceRecord.task_id == AgentTask.id,
            )
            .order_by(AgentTask.created_at.desc())
            .limit(200)
        )
    ).all()
    return AdminTaskHealthResponse(
        items=[
            AdminTaskHealthItem(
                task_id=task.id,
                conversation_id=task.conversation_id,
                answer_id=task.answer_id,
                status=task.status,
                error_code=task.error_code,
                request_mode=task.request_mode,
                model_call_count=performance.model_call_count if performance else None,
                tool_call_count=performance.tool_call_count if performance else None,
                total_duration_ms=performance.total_duration_ms if performance else None,
                created_at=_as_utc(task.created_at),
            )
            for task, performance in rows
        ]
    )


@router.get(
    "/admin/conversation-trace/{trace_id}",
    response_model=AdminConversationTraceResponse,
)
async def admin_conversation_trace(
    trace_id: str,
    session: SessionDependency,
    principal: PrincipalDependency,
) -> AdminConversationTraceResponse:
    """Resolve an exact conversation, message, task, or answer ID to its conversation."""

    _require_admin(principal)
    conversation_id: str | None = None
    if trace_id.startswith("conv_"):
        conversation_id = trace_id
    elif trace_id.startswith("msg_"):
        message = await session.get(Message, trace_id)
        conversation_id = message.conversation_id if message is not None else None
    elif trace_id.startswith("task_"):
        task = await session.get(AgentTask, trace_id)
        conversation_id = task.conversation_id if task is not None else None
    elif trace_id.startswith("ans_"):
        answer = await session.get(AnswerRecord, trace_id)
        task = await session.get(AgentTask, answer.task_id) if answer is not None else None
        conversation_id = task.conversation_id if task is not None else None

    conversation = (
        await session.get(Conversation, conversation_id) if conversation_id is not None else None
    )
    if conversation is None:
        raise HTTPException(status_code=404, detail="Trace not found")
    await _record_admin_read(session, principal, "admin.conversation_trace")

    messages = list(
        (
            await session.scalars(
                select(Message)
                .where(Message.conversation_id == conversation.id)
                .order_by(Message.created_at.asc(), Message.id.asc())
            )
        ).all()
    )
    tasks = list(
        (
            await session.scalars(
                select(AgentTask)
                .where(AgentTask.conversation_id == conversation.id)
                .order_by(AgentTask.created_at.asc(), AgentTask.id.asc())
            )
        ).all()
    )
    subject_kind = await session.scalar(
        select(ProductSubject.subject_kind).where(
            ProductSubject.id == conversation.owner_subject_id
        )
    )
    return AdminConversationTraceResponse(
        matched_trace_id=trace_id,
        conversation_id=conversation.id,
        title=clean_product_text(conversation.title) if conversation.title else None,
        subject_kind=subject_kind,
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


async def _ensure_profile(
    session: AsyncSession,
    subject_id: str,
) -> StudentProfile:
    profile = await session.get(StudentProfile, subject_id)
    if profile is None:
        now = utc_now()
        profile = StudentProfile(
            subject_id=subject_id,
            personalization_enabled=True,
            onboarding_completed=False,
            created_at=now,
            updated_at=now,
        )
        session.add(profile)
        await session.flush()
    return profile


async def _profile_response(
    session: AsyncSession,
    profile: StudentProfile,
) -> ProfileResponse:
    attributes = list(
        (
            await session.scalars(
                select(ProfileAttribute)
                .where(ProfileAttribute.subject_id == profile.subject_id)
                .order_by(ProfileAttribute.updated_at.desc())
            )
        ).all()
    )
    return ProfileResponse(
        personalization_enabled=profile.personalization_enabled,
        onboarding_completed=profile.onboarding_completed,
        confirmed=[_attribute_response(item) for item in attributes if item.status == "confirmed"],
        suggestions=[
            _attribute_response(item) for item in attributes if item.status == "suggested"
        ],
    )


async def _latest_attribute_by_name(
    session: AsyncSession,
    subject_id: str,
    attribute_name: ProfileAttributeKey,
    *,
    statuses: tuple[str, ...],
) -> ProfileAttribute | None:
    return await session.scalar(
        select(ProfileAttribute)
        .where(
            ProfileAttribute.subject_id == subject_id,
            ProfileAttribute.attribute_key == attribute_name,
            ProfileAttribute.status.in_(statuses),
        )
        .order_by(ProfileAttribute.updated_at.desc(), ProfileAttribute.id.desc())
        .limit(1)
    )


async def _answer_owned(
    session: AsyncSession,
    answer_id: str,
    subject_id: str,
) -> bool:
    owned = await session.scalar(
        select(AnswerRecord.id)
        .join(AgentTask, AnswerRecord.task_id == AgentTask.id)
        .join(Conversation, AgentTask.conversation_id == Conversation.id)
        .where(
            AnswerRecord.id == answer_id,
            Conversation.owner_subject_id == subject_id,
        )
    )
    return owned is not None


def _require_subject(principal: RequestPrincipal) -> str:
    if principal.product_subject_id is None:
        raise HTTPException(status_code=503, detail="Product identity unavailable")
    return principal.product_subject_id


def _require_admin(principal: RequestPrincipal) -> None:
    if not principal.authenticated or principal.role != "admin":
        raise HTTPException(status_code=404, detail="Not found")


async def _record_admin_read(
    session: AsyncSession,
    principal: RequestPrincipal,
    event_type: str,
) -> None:
    if principal.user_id is None:
        return
    session.add(
        SecurityAuditEvent(
            id=new_id("audit"),
            actor_user_id=principal.user_id,
            event_type=event_type,
            outcome="succeeded",
            request_id=request_id_context.get(),
            event_metadata={"read_only": True},
            occurred_at=utc_now(),
        )
    )
    await session.commit()


def _attribute_response(item: ProfileAttribute) -> ProfileAttributeResponse:
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


def _todo_response(item: UserTodo) -> TodoResponse:
    return TodoResponse(
        todo_id=item.id,
        title=clean_product_text(item.title),
        notes=clean_product_text(item.notes),
        due_at=_as_utc(item.due_at) if item.due_at else None,
        status=item.status,
        source_answer_id=item.source_answer_id,
        source_action_index=item.source_action_index,
        created_at=_as_utc(item.created_at),
        updated_at=_as_utc(item.updated_at),
    )


async def _upsert_feedback(
    session: AsyncSession,
    *,
    subject_id: str,
    answer_id: str,
    payload: FeedbackRequest,
) -> FeedbackResponse:
    if not await _answer_owned(session, answer_id, subject_id):
        raise HTTPException(status_code=404, detail="Answer not found")
    feedback = await session.scalar(
        select(AnswerFeedback).where(
            AnswerFeedback.subject_id == subject_id,
            AnswerFeedback.answer_id == answer_id,
        )
    )
    now = utc_now()
    if feedback is None:
        feedback = AnswerFeedback(
            id=new_id("feedback"),
            subject_id=subject_id,
            answer_id=answer_id,
            rating=payload.rating,
            categories=_clean_categories(payload.categories),
            comment=clean_product_text(payload.comment).strip(),
            created_at=now,
            updated_at=now,
        )
        session.add(feedback)
    else:
        feedback.rating = payload.rating
        feedback.categories = _clean_categories(payload.categories)
        feedback.comment = clean_product_text(payload.comment).strip()
        feedback.updated_at = now
    await session.commit()
    return _feedback_response(feedback)


def _feedback_response(item: AnswerFeedback) -> FeedbackResponse:
    return FeedbackResponse(
        feedback_id=item.id,
        answer_id=item.answer_id,
        rating=item.rating,
        categories=item.categories,
        comment=clean_product_text(item.comment),
        created_at=_as_utc(item.created_at),
        updated_at=_as_utc(item.updated_at),
    )


def _clean_categories(values: list[str]) -> list[str]:
    return list(
        dict.fromkeys(
            clean_product_text(item).strip()[:80]
            for item in values
            if clean_product_text(item).strip()
        )
    )[:8]


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
