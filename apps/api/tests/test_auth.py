from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient

from hzcu_agent.auth.campus_access import CampusAccessBroker, PreparedCampusAccess
from hzcu_agent.auth.service import AuthService, _parse_cas_subject
from hzcu_agent.config import Settings
from hzcu_agent.main import create_app
from hzcu_agent.models import utc_now


def _auth_settings(tmp_path, **overrides) -> Settings:
    return Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'auth.db'}",
        model_provider="demo",
        auth_mode="optional_cas",
        auth_session_secret="test-only-session-secret-with-at-least-32-characters",
        cas_service_registered=True,
        cas_browser_base_url="https://ca.hzcu.edu.cn/cas",
        cas_server_base_url="https://ca.hzcu.edu.cn/cas",
        public_api_base_url="http://testserver",
        web_app_url="http://web.test",
        **overrides,
    )


def test_optional_cas_login_issues_scoped_session_and_protects_owned_conversation(
    tmp_path,
    monkeypatch,
) -> None:
    validated: dict[str, str] = {}

    async def fake_validate_ticket(self, *, ticket: str, service_url: str) -> str:
        del self
        validated["ticket"] = ticket
        validated["service_url"] = service_url
        return "student-account-00001234"

    monkeypatch.setattr(AuthService, "_validate_ticket", fake_validate_ticket)
    app = create_app(_auth_settings(tmp_path))
    with TestClient(app) as client:
        anonymous = client.get("/api/v1/auth/me")
        assert anonymous.status_code == 200
        assert anonymous.json()["authenticated"] is False
        assert anonymous.json()["visibility_scopes"] == ["public"]

        anonymous_sources = client.get("/api/v1/sources")
        assert anonymous_sources.status_code == 200
        assert all(item["visibility"] == "public" for item in anonymous_sources.json())

        started = client.get(
            "/api/v1/auth/login",
            params={"return_to": "http://web.test/sources"},
            follow_redirects=False,
        )
        assert started.status_code == 302
        cas_location = started.headers["location"]
        service_url = parse_qs(urlsplit(cas_location).query)["service"][0]
        service = urlsplit(service_url)
        callback_url = f"{service.path}?{service.query}&ticket=ST-unit-test-ticket"

        callback = client.get(callback_url, follow_redirects=False)
        assert callback.status_code == 303
        assert callback.headers["location"] == "http://web.test/sources?auth=success"
        assert callback.headers["cache-control"] == "no-store"
        assert callback.headers["referrer-policy"] == "no-referrer"
        assert validated == {
            "ticket": "ST-unit-test-ticket",
            "service_url": service_url,
        }

        authenticated = client.get("/api/v1/auth/me")
        assert authenticated.status_code == 200
        assert authenticated.json()["authenticated"] is True
        assert authenticated.json()["subject_hint"].endswith("1234")
        assert authenticated.json()["visibility_scopes"] == ["campus", "public"]

        campus_sources = client.get("/api/v1/sources")
        assert campus_sources.status_code == 200
        assert len(campus_sources.json()) > len(anonymous_sources.json())
        assert {item["visibility"] for item in campus_sources.json()} == {
            "public",
            "campus",
        }

        without_csrf = client.post("/api/v1/conversations", json={})
        assert without_csrf.status_code == 403

        csrf_token = client.cookies.get("hzcu_csrf")
        created = client.post(
            "/api/v1/conversations",
            json={},
            headers={"X-CSRF-Token": csrf_token},
        )
        assert created.status_code == 201
        conversation_id = created.json()["conversation_id"]

        client.cookies.clear()
        hidden = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            json={"message": "教务处最近有什么选课通知？"},
        )
        assert hidden.status_code == 404


def test_pilot_anonymous_reads_campus_mirror_without_gaining_campus_identity(
    tmp_path,
) -> None:
    settings = _auth_settings(
        tmp_path,
        pilot_anonymous_campus_mirror=True,
    )
    with TestClient(create_app(settings)) as client:
        identity = client.get("/api/v1/auth/me")
        assert identity.status_code == 200
        assert identity.json()["authenticated"] is False
        assert identity.json()["visibility_scopes"] == ["public"]
        assert identity.json()["mirror_visibility_scopes"] == ["campus", "public"]

        sources = client.get("/api/v1/sources")
        assert sources.status_code == 200
        assert {item["visibility"] for item in sources.json()} == {
            "public",
            "campus",
        }


def test_cas_callback_rejects_state_mismatch_without_validating_ticket(
    tmp_path,
    monkeypatch,
) -> None:
    called = False

    async def should_not_validate(self, *, ticket: str, service_url: str) -> str:
        del self, ticket, service_url
        nonlocal called
        called = True
        return "unexpected"

    monkeypatch.setattr(AuthService, "_validate_ticket", should_not_validate)
    with TestClient(create_app(_auth_settings(tmp_path))) as client:
        response = client.get(
            "/api/v1/auth/callback",
            params={
                "state": "different-state-value",
                "return_to": "http://web.test/",
                "ticket": "ST-unit-test-ticket",
            },
            follow_redirects=False,
        )
    assert response.status_code == 303
    assert "auth_error=CAS_STATE_INVALID" in response.headers["location"]
    assert response.headers["referrer-policy"] == "no-referrer"
    assert called is False


def test_cas_subject_parser_supports_xml_and_cas_1_plain_response() -> None:
    xml = b"""<?xml version="1.0"?>
    <cas:serviceResponse xmlns:cas="http://www.yale.edu/tp/cas">
      <cas:authenticationSuccess><cas:user>student-1</cas:user></cas:authenticationSuccess>
    </cas:serviceResponse>"""
    assert _parse_cas_subject(xml) == "student-1"
    assert _parse_cas_subject(b"yes\nstudent-2\n") == "student-2"
    assert _parse_cas_subject(b"no\nINVALID_TICKET\n") is None
    assert _parse_cas_subject(b"<!DOCTYPE foo><foo />") is None


def test_vpn_credential_handoff_uses_challenge_and_issues_notice_only_session(
    tmp_path,
    monkeypatch,
) -> None:
    settings = Settings.model_validate(
        {
            **_auth_settings(tmp_path).model_dump(),
            "credential_vpn_enabled": True,
            "campus_query_route": "vpn_sidecar",
            "vpn_sidecar_base_url": "https://sidecar.test",
            "vpn_sidecar_api_token": ("test-sidecar-token-with-at-least-32-characters"),
        }
    )
    captured: dict[str, str] = {}

    async def fake_prepare(self, *, username: str, password: str):
        del self
        captured["username"] = username
        captured["password_seen"] = "yes" if password else "no"
        return PreparedCampusAccess(
            subject="student-account-00005678",
            session_handle="opaque-sidecar-session-handle-1234567890",
            expires_at=utc_now() + timedelta(minutes=15),
        )

    async def fake_delete(self, handle: str):
        del self, handle

    monkeypatch.setattr(CampusAccessBroker, "prepare_vpn_access", fake_prepare)
    monkeypatch.setattr(CampusAccessBroker, "_delete_sidecar_session", fake_delete)

    with TestClient(create_app(settings)) as client:
        challenge_response = client.get("/api/v1/auth/credential-challenge")
        assert challenge_response.status_code == 200
        challenge = challenge_response.json()["challenge"]

        denied = client.post(
            "/api/v1/auth/credential-login",
            json={
                "username": "student-account-00005678",
                "password": "dummy-test-password",
                "challenge": challenge,
            },
            headers={"Origin": "https://attacker.test"},
        )
        assert denied.status_code == 403

        logged_in = client.post(
            "/api/v1/auth/credential-login",
            json={
                "username": "student-account-00005678",
                "password": "dummy-test-password",
                "challenge": challenge,
            },
            headers={"Origin": "http://web.test"},
        )
        assert logged_in.status_code == 200
        body = logged_in.json()
        assert body["authenticated"] is True
        assert body["query_access"] == "vpn"
        assert body["read_only_capability"] == "campus_notice.read"
        assert "dummy-test-password" not in logged_in.text
        assert captured == {
            "username": "student-account-00005678",
            "password_seen": "yes",
        }
