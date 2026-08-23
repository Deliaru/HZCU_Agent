from collections.abc import AsyncIterator

from fastapi import HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from hzcu_agent.auth.service import CasAuthenticationError, RequestPrincipal


async def request_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.database.session_factory() as session:
        yield session


async def request_principal(request: Request, response: Response) -> RequestPrincipal:
    settings = request.app.state.settings
    token = request.cookies.get(settings.auth_cookie_name)
    auth_principal = await request.app.state.auth.resolve_principal(token)
    identity = await request.app.state.product_identity.resolve(
        auth_principal,
        request.cookies.get(settings.visitor_cookie_name),
        request.cookies.get(settings.auth_csrf_cookie_name),
    )
    if identity.visitor_token and identity.visitor_expires_at:
        response.set_cookie(
            key=settings.visitor_cookie_name,
            value=identity.visitor_token,
            max_age=settings.visitor_session_days * 24 * 60 * 60,
            expires=identity.visitor_expires_at,
            httponly=True,
            secure=settings.resolved_auth_cookie_secure,
            samesite="lax",
            path="/",
        )
    if identity.csrf_token and not auth_principal.authenticated:
        response.set_cookie(
            key=settings.auth_csrf_cookie_name,
            value=identity.csrf_token,
            max_age=settings.visitor_session_days * 24 * 60 * 60,
            expires=identity.visitor_expires_at,
            httponly=False,
            secure=settings.resolved_auth_cookie_secure,
            samesite="lax",
            path="/",
        )
    return identity.principal


def enforce_csrf(request: Request, principal: RequestPrincipal) -> None:
    settings = request.app.state.settings
    try:
        request.app.state.auth.require_csrf(
            principal,
            csrf_header=request.headers.get("x-csrf-token"),
            csrf_cookie=request.cookies.get(settings.auth_csrf_cookie_name),
        )
    except CasAuthenticationError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc


def enforce_required_login(request: Request, principal: RequestPrincipal) -> None:
    if request.app.state.settings.auth_mode == "required_cas" and not principal.authenticated:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "CAMPUS_LOGIN_REQUIRED",
                "message": "该部署要求先通过校园统一身份认证。",
            },
        )
