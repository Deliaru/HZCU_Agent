import sqlite3
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from hzcu_agent.auth.service import AuthService
from hzcu_agent.config import Settings
from hzcu_agent.main import create_app
from hzcu_agent.services import model_gateway
from hzcu_agent.services.model_gateway import AnthropicModelGateway
from hzcu_agent.services.model_runtime import ModelEndpointConfig


def _settings(tmp_path) -> Settings:
    return Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'admin-model.db'}",
        model_provider="demo",
        auth_mode="optional_cas",
        auth_session_secret="test-only-session-secret-with-at-least-32-characters",
        admin_cas_subjects="admin-account",
        cas_service_registered=True,
        cas_browser_base_url="https://ca.hzcu.edu.cn/cas",
        cas_server_base_url="https://ca.hzcu.edu.cn/cas",
        public_api_base_url="http://testserver",
        web_app_url="http://web.test",
    )


def _login_admin(client: TestClient) -> str:
    started = client.get(
        "/api/v1/auth/login",
        params={"return_to": "http://web.test/admin"},
        follow_redirects=False,
    )
    service_url = parse_qs(urlsplit(started.headers["location"]).query)["service"][0]
    service = urlsplit(service_url)
    callback = client.get(
        f"{service.path}?{service.query}&ticket=ST-admin-test",
        follow_redirects=False,
    )
    assert callback.status_code == 303
    identity = client.get("/api/v1/auth/me").json()
    assert identity["role"] == "admin"
    return client.cookies.get("hzcu_csrf")


def test_admin_can_persist_and_switch_server_model_endpoint(tmp_path, monkeypatch) -> None:
    async def fake_validate_ticket(self, *, ticket: str, service_url: str) -> str:
        del self, ticket, service_url
        return "admin-account"

    monkeypatch.setattr(AuthService, "_validate_ticket", fake_validate_ticket)
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        csrf = _login_admin(client)
        initial = client.get("/api/v1/admin/model-config")
        assert initial.status_code == 200
        assert initial.json()["protocol"] == "demo"
        assert initial.json()["api_key_configured"] is False

        saved = client.put(
            "/api/v1/admin/model-config",
            headers={"X-CSRF-Token": csrf},
            json={
                "protocol": "openai_responses",
                "base_url": "https://relay.example/v1/responses",
                "api_key": "public-server-secret",
                "agent_model": "agent-model",
                "utility_model": "utility-model",
                "reasoning_effort": "high",
                "utility_reasoning_effort": "low",
                "timeout_seconds": 180,
            },
        )
        assert saved.status_code == 200
        body = saved.json()
        assert body["protocol"] == "openai_responses"
        assert body["base_url"] == "https://relay.example/v1"
        assert body["api_key_hint"] == "••••cret"
        assert "public-server-secret" not in saved.text
        assert client.get("/api/v1/health").json()["model_provider"] == "openai"

        switched = client.put(
            "/api/v1/admin/model-config",
            headers={"X-CSRF-Token": csrf},
            json={
                "protocol": "anthropic_messages",
                "base_url": "https://claude-relay.example/v1/messages",
                "agent_model": "claude-agent",
                "utility_model": "claude-utility",
                "reasoning_effort": "high",
                "utility_reasoning_effort": "low",
                "timeout_seconds": 120,
            },
        )
        assert switched.status_code == 200
        assert switched.json()["base_url"] == "https://claude-relay.example"
        assert switched.json()["api_key_hint"] == "••••cret"
        assert client.get("/api/v1/health").json()["model_provider"] == "anthropic"

    database_path = tmp_path / "admin-model.db"
    with sqlite3.connect(database_path) as connection:
        encrypted = connection.execute(
            "SELECT encrypted_api_key FROM runtime_model_configurations WHERE id = 'primary'"
        ).fetchone()[0]
    assert encrypted != "public-server-secret"
    assert "public-server-secret" not in encrypted

    with TestClient(create_app(settings)) as restarted:
        health = restarted.get("/api/v1/health").json()
        assert health["model_provider"] == "anthropic"
        assert health["model_configured"] is True


class _StructuredResult(BaseModel):
    value: str


class _UnsupportedOutputError(RuntimeError):
    status_code = 400


@pytest.mark.asyncio
async def test_anthropic_gateway_uses_parse_then_standard_tool_fallback(monkeypatch) -> None:
    calls: list[tuple[str, dict]] = []

    class FakeMessages:
        async def parse(self, **kwargs):
            calls.append(("parse", kwargs))
            raise _UnsupportedOutputError("unknown field output_format")

        async def create(self, **kwargs):
            calls.append(("create", kwargs))
            return SimpleNamespace(
                content=[
                    SimpleNamespace(
                        type="tool_use",
                        name="emit_structured_result",
                        input={"value": "ok"},
                    )
                ]
            )

    class FakeClient:
        def __init__(self) -> None:
            self.messages = FakeMessages()

        async def close(self) -> None:
            return None

    monkeypatch.setattr(model_gateway, "AsyncAnthropic", lambda **kwargs: FakeClient())
    gateway = AnthropicModelGateway(
        ModelEndpointConfig(
            protocol="anthropic_messages",
            api_key="test-key",
            base_url="https://relay.example",
            agent_model="claude-agent",
            utility_model="claude-utility",
            reasoning_effort="medium",
            utility_reasoning_effort="low",
            timeout_seconds=30,
        )
    )

    result = await gateway._parse(
        role="test",
        model="claude-agent",
        instructions="Return a result.",
        payload={"question": "hello"},
        schema=_StructuredResult,
    )

    assert result == _StructuredResult(value="ok")
    assert [name for name, _ in calls] == ["parse", "create"]
    assert calls[1][1]["tool_choice"] == {
        "type": "tool",
        "name": "emit_structured_result",
    }
    assert calls[1][1]["tools"][0]["input_schema"]["properties"]["value"] == {
        "title": "Value",
        "type": "string",
    }
    await gateway.close()
