from typing import Annotated
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import SecretStr

from hzcu_agent.api.dependencies import request_principal
from hzcu_agent.auth.campus_access import (
    CampusAccessError,
    new_login_challenge,
    same_web_origin,
    valid_login_challenge,
)
from hzcu_agent.auth.contributor import ContributorAuthenticationError
from hzcu_agent.auth.local_admin import LocalAdminAuthenticationError
from hzcu_agent.auth.service import CasAuthenticationError, RequestPrincipal
from hzcu_agent.schemas import (
    AuthSessionResponse,
    ContributorLoginChallengeResponse,
    CredentialLoginChallengeResponse,
    CredentialLoginRequest,
    LocalAdminChallengeResponse,
    LocalAdminCredentialRequest,
)

router = APIRouter(prefix="/auth", tags=["identity"])
PrincipalDependency = Annotated[RequestPrincipal, Depends(request_principal)]


@router.get("/me", response_model=AuthSessionResponse)
async def get_auth_session(
    request: Request,
    principal: PrincipalDependency,
) -> AuthSessionResponse:
    return await _auth_session_response(request, principal)


@router.get("/login")
async def begin_cas_login(
    request: Request,
    return_to: str | None = Query(default=None, max_length=2048),
) -> RedirectResponse:
    try:
        login = request.app.state.auth.start_login(return_to)
    except CasAuthenticationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    settings = request.app.state.settings
    response = RedirectResponse(login.redirect_url, status_code=status.HTTP_302_FOUND)
    response.set_cookie(
        key=settings.auth_state_cookie_name,
        value=login.state,
        max_age=settings.auth_state_minutes * 60,
        httponly=True,
        secure=settings.resolved_auth_cookie_secure,
        samesite="lax",
        path=f"{settings.api_prefix}/auth",
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/callback")
async def finish_cas_login(
    request: Request,
    state: str = Query(min_length=16, max_length=256),
    return_to: str = Query(max_length=2048),
    ticket: str = Query(),
) -> RedirectResponse:
    settings = request.app.state.settings
    try:
        established = await request.app.state.auth.finish_login(
            state=state,
            state_cookie=request.cookies.get(settings.auth_state_cookie_name),
            ticket=ticket,
            return_to=return_to,
        )
    except CasAuthenticationError as exc:
        try:
            safe_return = request.app.state.auth.normalize_return_to(return_to)
        except CasAuthenticationError:
            safe_return = settings.web_app_url
        response = RedirectResponse(
            _with_query(safe_return, "auth_error", exc.code),
            status_code=status.HTTP_303_SEE_OTHER,
        )
        response.delete_cookie(
            settings.auth_state_cookie_name,
            path=f"{settings.api_prefix}/auth",
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    max_age = settings.auth_session_hours * 60 * 60
    response = RedirectResponse(
        _with_query(established.return_to, "auth", "success"),
        status_code=status.HTTP_303_SEE_OTHER,
    )
    response.set_cookie(
        key=settings.auth_cookie_name,
        value=established.session_token,
        max_age=max_age,
        expires=established.expires_at,
        httponly=True,
        secure=settings.resolved_auth_cookie_secure,
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        key=settings.auth_csrf_cookie_name,
        value=established.csrf_token,
        max_age=max_age,
        expires=established.expires_at,
        httponly=False,
        secure=settings.resolved_auth_cookie_secure,
        samesite="lax",
        path="/",
    )
    response.delete_cookie(
        settings.auth_state_cookie_name,
        path=f"{settings.api_prefix}/auth",
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@router.get(
    "/local-admin/challenge",
    response_model=LocalAdminChallengeResponse,
)
async def local_admin_challenge(request: Request) -> JSONResponse:
    settings = request.app.state.settings
    if not settings.local_admin_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    challenge = new_login_challenge()
    response = JSONResponse(
        LocalAdminChallengeResponse(
            challenge=challenge,
            expires_in_seconds=300,
        ).model_dump(mode="json")
    )
    response.set_cookie(
        key=settings.auth_login_csrf_cookie_name,
        value=challenge,
        max_age=300,
        httponly=True,
        secure=settings.resolved_auth_cookie_secure,
        samesite="strict",
        path=f"{settings.api_prefix}/auth",
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/local-admin/setup", response_model=AuthSessionResponse)
async def setup_local_admin(
    payload: LocalAdminCredentialRequest,
    request: Request,
) -> JSONResponse:
    _validate_local_admin_request(payload, request)
    settings = request.app.state.settings
    password = payload.password.get_secret_value()
    try:
        subject = await request.app.state.local_admin.setup(
            username=payload.username,
            password=password,
        )
        established = await request.app.state.auth.establish_local_admin_subject(
            subject=subject,
            return_to=settings.web_app_url,
        )
    except LocalAdminAuthenticationError as exc:
        raise _local_admin_http_error(exc) from exc
    except CasAuthenticationError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    finally:
        password = ""
        payload.password = SecretStr("")
    return await _local_admin_login_response(request, established)


@router.post("/local-admin/login", response_model=AuthSessionResponse)
async def login_local_admin(
    payload: LocalAdminCredentialRequest,
    request: Request,
) -> JSONResponse:
    _validate_local_admin_request(payload, request)
    settings = request.app.state.settings
    password = payload.password.get_secret_value()
    client_key = request.client.host if request.client else "unknown"
    try:
        subject = await request.app.state.local_admin.authenticate(
            username=payload.username,
            password=password,
            client_key=client_key,
        )
        established = await request.app.state.auth.establish_local_admin_subject(
            subject=subject,
            return_to=settings.web_app_url,
        )
    except LocalAdminAuthenticationError as exc:
        raise _local_admin_http_error(exc) from exc
    except CasAuthenticationError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    finally:
        password = ""
        payload.password = SecretStr("")
    return await _local_admin_login_response(request, established)


@router.get(
    "/contributor/challenge",
    response_model=ContributorLoginChallengeResponse,
)
async def contributor_login_challenge(request: Request) -> JSONResponse:
    challenge = new_login_challenge()
    response = JSONResponse(
        ContributorLoginChallengeResponse(
            challenge=challenge,
            expires_in_seconds=300,
        ).model_dump(mode="json")
    )
    response.set_cookie(
        key=request.app.state.settings.auth_login_csrf_cookie_name,
        value=challenge,
        max_age=300,
        httponly=True,
        secure=request.app.state.settings.resolved_auth_cookie_secure,
        samesite="strict",
        path=f"{request.app.state.settings.api_prefix}/auth",
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/contributor/login", response_model=AuthSessionResponse)
async def contributor_login(
    payload: CredentialLoginRequest,
    request: Request,
) -> JSONResponse:
    settings = request.app.state.settings
    if not same_web_origin(request.headers.get("origin"), settings.web_app_url):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "LOGIN_ORIGIN_DENIED", "message": "登录请求来源不受信任。"},
        )
    if not valid_login_challenge(
        submitted=payload.challenge,
        cookie=request.cookies.get(settings.auth_login_csrf_cookie_name),
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "LOGIN_CHALLENGE_INVALID",
                "message": "登录安全校验已失效，请重新打开登录窗口。",
            },
        )
    client_key = request.client.host if request.client else "unknown"
    password = payload.password.get_secret_value()
    try:
        username = await request.app.state.contributors.authenticate(
            username=payload.username,
            password=password,
            client_key=client_key,
        )
        established = await request.app.state.auth.establish_local_contributor_subject(
            subject=username,
            return_to=settings.web_app_url,
        )
    except ContributorAuthenticationError as exc:
        code_status = (
            status.HTTP_429_TOO_MANY_REQUESTS
            if exc.code == "CONTRIBUTOR_RATE_LIMITED"
            else status.HTTP_401_UNAUTHORIZED
        )
        raise HTTPException(
            status_code=code_status, detail={"code": exc.code, "message": str(exc)}
        ) from exc
    except CasAuthenticationError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail={"code": exc.code, "message": str(exc)}
        ) from exc
    finally:
        password = ""
        payload.password = SecretStr("")
    payload_response = await _auth_session_response(request, established.principal)
    response = JSONResponse(payload_response.model_dump(mode="json"))
    _set_application_session_cookies(response, request, established)
    response.delete_cookie(
        settings.auth_login_csrf_cookie_name,
        path=f"{settings.api_prefix}/auth",
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get(
    "/credential-challenge",
    response_model=CredentialLoginChallengeResponse,
)
async def credential_login_challenge(request: Request) -> JSONResponse:
    settings = request.app.state.settings
    if not settings.credential_vpn_enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "VPN_CREDENTIAL_HANDOFF_DISABLED",
                "message": "当前部署没有启用校外 VPN 凭据通道。",
            },
        )
    challenge = new_login_challenge()
    response = JSONResponse(
        CredentialLoginChallengeResponse(
            challenge=challenge,
            expires_in_seconds=300,
        ).model_dump(mode="json")
    )
    response.set_cookie(
        key=settings.auth_login_csrf_cookie_name,
        value=challenge,
        max_age=300,
        httponly=True,
        secure=settings.resolved_auth_cookie_secure,
        samesite="strict",
        path=f"{settings.api_prefix}/auth",
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/credential-login", response_model=AuthSessionResponse)
async def credential_login(
    payload: CredentialLoginRequest,
    request: Request,
) -> JSONResponse:
    settings = request.app.state.settings
    if not settings.credential_vpn_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    if not same_web_origin(request.headers.get("origin"), settings.web_app_url):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "LOGIN_ORIGIN_DENIED",
                "message": "登录请求来源不受信任。",
            },
        )
    if not valid_login_challenge(
        submitted=payload.challenge,
        cookie=request.cookies.get(settings.auth_login_csrf_cookie_name),
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "LOGIN_CHALLENGE_INVALID",
                "message": "登录安全校验已失效，请重新打开登录窗口。",
            },
        )

    client_key = request.client.host if request.client else "unknown"
    password = payload.password.get_secret_value()
    prepared = None
    try:
        await request.app.state.campus_access.check_credential_attempt(
            payload.username,
            client_key,
        )
        prepared = await request.app.state.campus_access.prepare_vpn_access(
            username=payload.username,
            password=password,
        )
        established = await request.app.state.auth.establish_verified_subject(
            subject=prepared.subject,
            return_to=settings.web_app_url,
            channel="vpn_credential_handoff",
        )
        if established.principal.user_id is None:
            raise CasAuthenticationError(
                "CAS_SUBJECT_INVALID",
                "学校认证通道没有返回可用校园身份。",
            )
        await request.app.state.campus_access.bind(
            established.principal.user_id,
            prepared,
        )
        await request.app.state.campus_access.clear_credential_attempts(
            payload.username,
            client_key,
        )
    except CampusAccessError as exc:
        raise HTTPException(
            status_code=(
                status.HTTP_429_TOO_MANY_REQUESTS
                if exc.code == "CREDENTIAL_RATE_LIMITED"
                else (
                    status.HTTP_401_UNAUTHORIZED
                    if exc.code == "CAMPUS_CREDENTIALS_REJECTED"
                    else status.HTTP_503_SERVICE_UNAVAILABLE
                )
            ),
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    except CasAuthenticationError as exc:
        if prepared is not None:
            await request.app.state.campus_access.abort(prepared)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    finally:
        password = ""
        payload.password = SecretStr("")

    payload = await _auth_session_response(
        request,
        established.principal,
        visitor_data_available=(request.cookies.get(settings.visitor_cookie_name) is not None),
    )
    response = JSONResponse(payload.model_dump(mode="json"))
    _set_application_session_cookies(response, request, established)
    response.delete_cookie(
        settings.auth_login_csrf_cookie_name,
        path=f"{settings.api_prefix}/auth",
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    principal: PrincipalDependency,
) -> Response:
    settings = request.app.state.settings
    try:
        await request.app.state.auth.logout(
            principal=principal,
            csrf_header=request.headers.get("x-csrf-token"),
            csrf_cookie=request.cookies.get(settings.auth_csrf_cookie_name),
        )
    except CasAuthenticationError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    await request.app.state.campus_access.release(principal.user_id)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(settings.auth_cookie_name, path="/")
    response.delete_cookie(settings.auth_csrf_cookie_name, path="/")
    response.headers["Cache-Control"] = "no-store"
    return response


def _set_application_session_cookies(
    response: Response,
    request: Request,
    established,
) -> None:
    settings = request.app.state.settings
    max_age = settings.auth_session_hours * 60 * 60
    response.set_cookie(
        key=settings.auth_cookie_name,
        value=established.session_token,
        max_age=max_age,
        expires=established.expires_at,
        httponly=True,
        secure=settings.resolved_auth_cookie_secure,
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        key=settings.auth_csrf_cookie_name,
        value=established.csrf_token,
        max_age=max_age,
        expires=established.expires_at,
        httponly=False,
        secure=settings.resolved_auth_cookie_secure,
        samesite="lax",
        path="/",
    )


async def _auth_session_response(
    request: Request,
    principal: RequestPrincipal,
    *,
    visitor_data_available: bool | None = None,
) -> AuthSessionResponse:
    settings = request.app.state.settings
    access = await request.app.state.campus_access.status(principal.user_id)
    local_admin = await request.app.state.local_admin.status()
    login_url = None
    if settings.cas_login_ready and not principal.authenticated:
        login_url = f"{settings.public_api_base_url}{settings.api_prefix}/auth/login?" + urlencode(
            {"return_to": settings.web_app_url}
        )
    subject_kind = "visitor"
    if principal.authenticated:
        if principal.identity_provider == "local_admin":
            subject_kind = "local_admin"
        elif principal.identity_provider == "local_contributor":
            subject_kind = "contributor"
        else:
            subject_kind = "campus"
    return AuthSessionResponse(
        authenticated=principal.authenticated,
        auth_mode=settings.auth_mode,
        cas_enabled=settings.cas_is_enabled,
        subject_hint=principal.subject_hint,
        visibility_scopes=sorted(principal.visibility_scopes),
        mirror_visibility_scopes=sorted(
            settings.local_mirror_visibility_scopes(
                principal.visibility_scopes,
                authenticated=principal.authenticated,
            )
        ),
        login_url=login_url,
        service_registration_required=(
            settings.cas_is_enabled and not settings.cas_service_registered
        ),
        query_access=access.mode,
        query_access_expires_at=access.expires_at,
        credential_handoff_available=access.credential_handoff_available,
        read_only_capability=(
            "community.answer" if principal.role == "contributor" else "campus_notice.read"
        ),
        subject_kind=subject_kind,
        role=principal.role,
        visitor_data_available=(
            principal.visitor_data_available
            if visitor_data_available is None
            else visitor_data_available
        ),
        local_admin_enabled=local_admin.enabled,
        local_admin_configured=local_admin.configured,
        local_admin_setup_available=local_admin.setup_available,
    )


async def _local_admin_login_response(request: Request, established) -> JSONResponse:
    settings = request.app.state.settings
    payload = await _auth_session_response(
        request,
        established.principal,
        visitor_data_available=(request.cookies.get(settings.visitor_cookie_name) is not None),
    )
    response = JSONResponse(payload.model_dump(mode="json"))
    _set_application_session_cookies(response, request, established)
    response.delete_cookie(
        settings.auth_login_csrf_cookie_name,
        path=f"{settings.api_prefix}/auth",
    )
    response.headers["Cache-Control"] = "no-store"
    return response


def _validate_local_admin_request(
    payload: LocalAdminCredentialRequest,
    request: Request,
) -> None:
    settings = request.app.state.settings
    if not settings.local_admin_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    if not same_web_origin(request.headers.get("origin"), settings.web_app_url):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "LOGIN_ORIGIN_DENIED", "message": "登录请求来源不受信任。"},
        )
    if not valid_login_challenge(
        submitted=payload.challenge,
        cookie=request.cookies.get(settings.auth_login_csrf_cookie_name),
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "LOGIN_CHALLENGE_INVALID",
                "message": "登录安全校验已失效，请重新提交。",
            },
        )


def _local_admin_http_error(exc: LocalAdminAuthenticationError) -> HTTPException:
    status_code = status.HTTP_400_BAD_REQUEST
    if exc.code == "LOCAL_ADMIN_CREDENTIALS_INVALID":
        status_code = status.HTTP_401_UNAUTHORIZED
    elif exc.code == "LOCAL_ADMIN_RATE_LIMITED":
        status_code = status.HTTP_429_TOO_MANY_REQUESTS
    elif exc.code == "LOCAL_ADMIN_ALREADY_CONFIGURED":
        status_code = status.HTTP_409_CONFLICT
    elif exc.code in {"LOCAL_ADMIN_DISABLED", "LOCAL_ADMIN_SETUP_DISABLED"}:
        status_code = status.HTTP_403_FORBIDDEN
    return HTTPException(
        status_code=status_code,
        detail={"code": exc.code, "message": str(exc)},
    )


def _with_query(url: str, key: str, value: str) -> str:
    parsed = urlsplit(url)
    query = [(item_key, item_value) for item_key, item_value in parse_qsl(parsed.query)]
    query.append((key, value))
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urlencode(query),
            parsed.fragment,
        )
    )
