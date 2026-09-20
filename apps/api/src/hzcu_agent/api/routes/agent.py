from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from hzcu_agent.api.dependencies import enforce_csrf, request_principal, request_session
from hzcu_agent.auth.service import RequestPrincipal
from hzcu_agent.schemas import (
    AgentAccessResponse,
    AgentVerificationRequest,
    AgentVerificationResponse,
)
from hzcu_agent.services.agent_admission import AgentAdmissionError, admission_http_exception
from hzcu_agent.services.client_network import network_access

router = APIRouter(tags=["agent-access"])
SessionDependency = Annotated[AsyncSession, Depends(request_session)]
PrincipalDependency = Annotated[RequestPrincipal, Depends(request_principal)]


@router.get("/agent/access", response_model=AgentAccessResponse)
async def get_agent_access(
    request: Request,
    principal: PrincipalDependency,
) -> AgentAccessResponse:
    network = network_access(request, principal, request.app.state.policy.snapshot())
    return AgentAccessResponse(
        **await _admission(request).access(principal=principal),
        network_allowed=network["allowed"],
        network_denied_message=None
        if network["allowed"]
        else request.app.state.policy.snapshot().network_denied_message,
    )


@router.post("/agent/verification", response_model=AgentVerificationResponse)
async def verify_agent_access(
    payload: AgentVerificationRequest,
    request: Request,
    principal: PrincipalDependency,
) -> AgentVerificationResponse:
    enforce_csrf(request, principal)
    try:
        verified_until = await _admission(request).verify_turnstile(
            request=request,
            principal=principal,
            token=payload.token,
        )
    except AgentAdmissionError as exc:
        raise admission_http_exception(exc) from exc
    return AgentVerificationResponse(verified_until=verified_until)


def _admission(request: Request | None = None):
    if request is None:
        raise RuntimeError("request context unavailable")
    return request.app.state.admission
