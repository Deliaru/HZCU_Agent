import sqlite3

from fastapi.testclient import TestClient

from hzcu_agent.config import Settings, ensure_local_auth_session_secret
from hzcu_agent.main import create_app


def _settings(tmp_path) -> Settings:
    return Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'local-admin.db'}",
        model_provider="demo",
        auth_mode="anonymous",
        local_admin_enabled=True,
        auth_session_secret="test-only-session-secret-with-at-least-32-characters",
        admin_cas_subjects="initial-admin-account",
        public_api_base_url="http://testserver",
        web_app_url="http://web.test",
    )


def _challenge(client: TestClient) -> str:
    response = client.get("/api/v1/auth/local-admin/challenge")
    assert response.status_code == 200
    return response.json()["challenge"]


def _credentials(challenge: str, *, password: str = "same-campus-password") -> dict[str, str]:
    return {
        "username": "initial-admin-account",
        "password": password,
        "challenge": challenge,
    }


def test_local_admin_first_setup_login_logout_and_restart(tmp_path) -> None:
    settings = _settings(tmp_path)
    database_path = tmp_path / "local-admin.db"

    with TestClient(create_app(settings)) as client:
        status = client.get("/api/v1/auth/me")
        assert status.status_code == 200
        assert status.json()["cas_enabled"] is False
        assert status.json()["local_admin_enabled"] is True
        assert status.json()["local_admin_configured"] is False
        assert status.json()["local_admin_setup_available"] is True

        challenge = _challenge(client)
        rejected_origin = client.post(
            "/api/v1/auth/local-admin/setup",
            json=_credentials(challenge),
            headers={"Origin": "https://attacker.test"},
        )
        assert rejected_origin.status_code == 403

        configured = client.post(
            "/api/v1/auth/local-admin/setup",
            json=_credentials(challenge),
            headers={"Origin": "http://web.test"},
        )
        assert configured.status_code == 200
        assert configured.json()["authenticated"] is True
        assert configured.json()["role"] == "admin"
        assert configured.json()["subject_kind"] == "local_admin"
        assert configured.json()["local_admin_configured"] is True
        assert "same-campus-password" not in configured.text

        admin_console = client.get("/api/v1/admin/model-config")
        assert admin_console.status_code == 200

        csrf = client.cookies.get("hzcu_csrf")
        logged_out = client.post(
            "/api/v1/auth/logout",
            headers={"X-CSRF-Token": csrf},
        )
        assert logged_out.status_code == 204

        challenge = _challenge(client)
        wrong = client.post(
            "/api/v1/auth/local-admin/login",
            json=_credentials(challenge, password="wrong-password"),
            headers={"Origin": "http://web.test"},
        )
        assert wrong.status_code == 401
        assert wrong.json()["detail"]["code"] == "LOCAL_ADMIN_CREDENTIALS_INVALID"
        assert "wrong-password" not in wrong.text

        challenge = _challenge(client)
        logged_in = client.post(
            "/api/v1/auth/local-admin/login",
            json=_credentials(challenge),
            headers={"Origin": "http://web.test"},
        )
        assert logged_in.status_code == 200
        assert logged_in.json()["role"] == "admin"

    with sqlite3.connect(database_path) as database:
        username, password_hash = database.execute(
            "SELECT username, password_hash FROM local_admin_credentials WHERE id = 'primary'"
        ).fetchone()
    assert username == "initial-admin-account"
    assert password_hash.startswith("scrypt$")
    assert "same-campus-password" not in password_hash

    with TestClient(create_app(settings)) as restarted:
        status = restarted.get("/api/v1/auth/me")
        assert status.json()["local_admin_configured"] is True
        assert status.json()["local_admin_setup_available"] is False
        challenge = _challenge(restarted)
        logged_in = restarted.post(
            "/api/v1/auth/local-admin/login",
            json=_credentials(challenge),
            headers={"Origin": "http://web.test"},
        )
        assert logged_in.status_code == 200
        assert logged_in.json()["subject_kind"] == "local_admin"


def test_local_admin_setup_requires_the_configured_initial_subject(tmp_path) -> None:
    with TestClient(create_app(_settings(tmp_path))) as client:
        challenge = _challenge(client)
        denied = client.post(
            "/api/v1/auth/local-admin/setup",
            json={
                "username": "different-account",
                "password": "same-campus-password",
                "challenge": challenge,
            },
            headers={"Origin": "http://web.test"},
        )
        assert denied.status_code == 400
        assert denied.json()["detail"]["code"] == "LOCAL_ADMIN_SUBJECT_NOT_ALLOWED"
        assert client.get("/api/v1/auth/me").json()["local_admin_configured"] is False


def test_local_admin_can_be_first_initialized_without_a_cas_admin_list(tmp_path) -> None:
    settings = _settings(tmp_path).model_copy(update={"admin_cas_subjects": None})
    with TestClient(create_app(settings)) as client:
        status = client.get("/api/v1/auth/me").json()
        assert status["local_admin_setup_available"] is True
        challenge = _challenge(client)
        configured = client.post(
            "/api/v1/auth/local-admin/setup",
            json={
                "username": "initial-admin-account",
                "password": "same-campus-password",
                "challenge": challenge,
            },
            headers={"Origin": "http://web.test"},
        )
        assert configured.status_code == 200
        assert configured.json()["role"] == "admin"


def test_development_local_admin_secret_is_created_once_and_reused(tmp_path) -> None:
    secret_path = tmp_path / "local_auth.secret"
    settings = _settings(tmp_path).model_copy(
        update={
            "auth_session_secret": None,
            "local_auth_secret_path": str(secret_path),
        }
    )
    first = ensure_local_auth_session_secret(settings)
    second = ensure_local_auth_session_secret(settings)
    assert first.auth_session_secret is not None
    assert second.auth_session_secret is not None
    assert (
        first.auth_session_secret.get_secret_value()
        == second.auth_session_secret.get_secret_value()
    )
    assert len(secret_path.read_text(encoding="utf-8").strip()) >= 32
