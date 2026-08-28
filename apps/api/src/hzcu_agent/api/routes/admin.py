from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import urlsplit, urlunsplit

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from hzcu_agent.api.dependencies import enforce_csrf, request_principal, request_session
from hzcu_agent.auth.service import RequestPrincipal
from hzcu_agent.models import AgentTask, SecurityAuditEvent, new_id, utc_now
from hzcu_agent.observability import request_id_context
from hzcu_agent.schemas import (
    AdminAgentPolicyResponse,
    AdminAgentPolicyUpdate,
    AdminModelConfigurationResponse,
    AdminModelConfigurationUpdate,
)
from hzcu_agent.services.agent_policy import AgentPolicyConfigError
from hzcu_agent.services.model_runtime import (
    ModelEndpointConfig,
    StoredModelConfigurationError,
)

router = APIRouter(prefix="/admin", tags=["admin"])
SessionDependency = Annotated[AsyncSession, Depends(request_session)]
PrincipalDependency = Annotated[RequestPrincipal, Depends(request_principal)]


@router.get("/agent-policy", response_model=AdminAgentPolicyResponse)
async def get_agent_policy(
    request: Request,
    session: SessionDependency,
    principal: PrincipalDependency,
) -> AdminAgentPolicyResponse:
    _require_admin(principal)
    await _record_event(session, principal, "admin.agent_policy.read", {"read_only": True})
    return await _agent_policy_response(request)


@router.put("/agent-policy", response_model=AdminAgentPolicyResponse)
async def update_agent_policy(
    payload: AdminAgentPolicyUpdate,
    request: Request,
    session: SessionDependency,
    principal: PrincipalDependency,
) -> AdminAgentPolicyResponse:
    _require_admin(principal)
    enforce_csrf(request, principal)
    if principal.user_id is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not found")
    # Keep an explicit null sitekey so an administrator can clear stale
    # Turnstile metadata; the secret itself is handled separately and is
    # never returned by the API.
    values = payload.model_dump(mode="python", exclude_unset=True)
    secret = values.pop("turnstile_secret", None)
    if secret is not None:
        values["turnstile_secret"] = secret.get_secret_value()
    try:
        updated = await request.app.state.policy.update(values, actor_user_id=principal.user_id)
    except AgentPolicyConfigError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "AGENT_POLICY_INVALID", "message": str(exc)},
        ) from exc
    session.add(
        SecurityAuditEvent(
            id=new_id("audit"),
            actor_user_id=principal.user_id,
            event_type="admin.agent_policy.update",
            outcome="succeeded",
            request_id=request_id_context.get(),
            event_metadata={
                "mode": updated.mode,
                "agent_concurrency": updated.agent_concurrency,
                "model_concurrency": updated.model_concurrency,
                "turnstile_enabled": updated.turnstile_enabled,
                "turnstile_secret_changed": secret is not None,
            },
            occurred_at=utc_now(),
        )
    )
    await session.commit()
    return await _agent_policy_response(request)


@router.get("/model-config", response_model=AdminModelConfigurationResponse)
async def get_model_configuration(
    request: Request,
    session: SessionDependency,
    principal: PrincipalDependency,
) -> AdminModelConfigurationResponse:
    _require_admin(principal)
    await _record_event(session, principal, "admin.model_config.read", {"read_only": True})
    return _response(request.app.state.models.config)


@router.put("/model-config", response_model=AdminModelConfigurationResponse)
async def update_model_configuration(
    payload: AdminModelConfigurationUpdate,
    request: Request,
    session: SessionDependency,
    principal: PrincipalDependency,
) -> AdminModelConfigurationResponse:
    _require_admin(principal)
    enforce_csrf(request, principal)
    if principal.user_id is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not found")

    current = request.app.state.models.config
    submitted_key = payload.api_key.get_secret_value().strip() if payload.api_key else None
    api_key = submitted_key or current.api_key
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "MODEL_API_KEY_REQUIRED",
                "message": "首次保存公用模型端点时必须填写 API 密钥。",
            },
        )
    try:
        base_url = _normalize_endpoint_url(payload.protocol, payload.base_url)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "MODEL_ENDPOINT_INVALID", "message": str(exc)},
        ) from exc

    config = ModelEndpointConfig(
        protocol=payload.protocol,
        api_key=api_key,
        base_url=base_url,
        agent_model=payload.agent_model,
        utility_model=payload.utility_model,
        reasoning_effort=payload.reasoning_effort,
        utility_reasoning_effort=payload.utility_reasoning_effort,
        timeout_seconds=payload.timeout_seconds,
    )
    try:
        persisted = await request.app.state.model_configuration_store.save(
            session,
            config,
            actor_user_id=principal.user_id,
        )
    except StoredModelConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "MODEL_CONFIG_UNAVAILABLE", "message": str(exc)},
        ) from exc

    session.add(
        SecurityAuditEvent(
            id=new_id("audit"),
            actor_user_id=principal.user_id,
            event_type="admin.model_config.update",
            outcome="succeeded",
            request_id=request_id_context.get(),
            event_metadata={
                "protocol": persisted.protocol,
                "base_url_configured": persisted.base_url is not None,
                "agent_model": persisted.agent_model,
                "utility_model": persisted.utility_model,
            },
            occurred_at=utc_now(),
        )
    )
    await session.commit()
    await request.app.state.models.replace(persisted)
    return _response(persisted)


def _response(config: ModelEndpointConfig) -> AdminModelConfigurationResponse:
    return AdminModelConfigurationResponse(
        protocol=config.protocol,
        base_url=config.base_url,
        agent_model=config.agent_model,
        utility_model=config.utility_model,
        reasoning_effort=config.reasoning_effort,
        utility_reasoning_effort=config.utility_reasoning_effort,
        timeout_seconds=config.timeout_seconds,
        api_key_configured=bool(config.api_key),
        api_key_hint=config.api_key_hint,
        source=config.source,
        updated_at=config.updated_at,
    )


def _normalize_endpoint_url(protocol: str, value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().rstrip("/")
    if not normalized:
        return None
    parsed = urlsplit(normalized)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("端点必须是没有账号、查询参数或片段的完整 HTTP(S) 地址。")
    path = parsed.path.rstrip("/")
    if protocol == "openai_responses" and path.endswith("/responses"):
        path = path[: -len("/responses")]
    elif protocol == "anthropic_messages" and path.endswith("/v1/messages"):
        path = path[: -len("/v1/messages")]
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", "")).rstrip("/")


def _require_admin(principal: RequestPrincipal) -> None:
    if not principal.authenticated or principal.role != "admin":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


async def _record_event(
    session: AsyncSession,
    principal: RequestPrincipal,
    event_type: str,
    metadata: dict,
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
            event_metadata=metadata,
            occurred_at=utc_now(),
        )
    )
    await session.commit()


async def _agent_policy_response(request: Request) -> AdminAgentPolicyResponse:
    policy = request.app.state.policy
    data = policy.public_dict()
    usage = await policy.usage()
    rejection_counts = await policy.rejection_counts()
    async with request.app.state.database.session_factory() as session:
        running = int(
            await session.scalar(
                select(func.count(AgentTask.id)).where(AgentTask.status == "running")
            )
            or 0
        )
        queued = int(
            await session.scalar(
                select(func.count(AgentTask.id)).where(AgentTask.status == "queued")
            )
            or 0
        )
        oldest_queued_at = await session.scalar(
            select(func.min(AgentTask.created_at)).where(AgentTask.status == "queued")
        )
    oldest_queue_wait_seconds = (
        max(0, int((utc_now() - _as_utc(oldest_queued_at)).total_seconds()))
        if oldest_queued_at is not None
        else 0
    )
    return AdminAgentPolicyResponse(
        **data,
        today_task_count=usage["global_tasks"],
        today_model_call_count=usage["global_model_calls"],
        today_rejection_counts=rejection_counts,
        running_count=running,
        queued_count=queued,
        oldest_queue_wait_seconds=oldest_queue_wait_seconds,
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
