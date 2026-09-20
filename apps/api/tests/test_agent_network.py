from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request
from test_agent_admission import _csrf, _policy_payload, _settings, _setup_admin, _wait_for_task

from hzcu_agent.main import create_app
from hzcu_agent.services.agent_policy import AgentPolicySnapshot
from hzcu_agent.services.client_network import client_ip, network_access

ALLOWED = ["202.107.195.201/32", "202.107.195.203/32", "183.26.172.31/32"]


def request_for(peer, headers=(), trusted=""):
    app = SimpleNamespace(
        state=SimpleNamespace(settings=SimpleNamespace(network_trusted_proxy_cidrs=trusted))
    )
    return Request({"type": "http", "client": (peer, 123), "headers": headers, "app": app})


def test_proxy_assertion_is_only_trusted_from_configured_peer():
    forged = [(b"x-hzcu-client-ip", b"202.107.195.201"), (b"x-forwarded-for", b"202.107.195.201")]
    assert client_ip(request_for("198.51.100.1", forged, "172.18.0.0/16")) == "198.51.100.1"
    assert client_ip(request_for("172.18.0.5", forged, "172.18.0.0/16")) == "202.107.195.201"
    for headers in [
        [],
        forged + forged,
        [(b"x-hzcu-client-ip", b"bad")],
        [(b"x-hzcu-client-ip", b"1.2.3.4, 202.107.195.201")],
    ]:
        assert client_ip(request_for("172.18.0.5", headers, "172.18.0.0/16")) is None


@pytest.mark.parametrize(
    "ip,allowed",
    [
        ("202.107.195.201", True),
        ("202.107.195.203", True),
        ("183.26.172.31", True),
        ("202.107.195.202", False),
        ("124.90.245.132", False),
        ("2001:db8::1", False),
    ],
)
def test_exact_allowlist_and_authenticated_role_bypasses(ip, allowed):
    snapshot = AgentPolicySnapshot(
        network_restriction_enabled=True, network_allowed_cidrs=tuple(ALLOWED)
    )
    visitor = SimpleNamespace(authenticated=False, role="visitor")
    assert network_access(request_for(ip), visitor, snapshot)["allowed"] is allowed
    for role in ["admin", "contributor"]:
        principal = SimpleNamespace(authenticated=True, role=role)
        assert network_access(request_for(ip), principal, snapshot)["allowed"]
        principal.authenticated = False
        assert network_access(request_for(ip), principal, snapshot)["allowed"] is allowed
        principal.authenticated = True
        restricted = replace(snapshot, **{f"network_{role}_bypass": False})
        assert network_access(request_for(ip), principal, restricted)["allowed"] is allowed


def test_policy_persists_and_all_task_entrypoints_reject_without_spending(tmp_path):
    settings = _settings(tmp_path)
    settings.network_trusted_proxy_cidrs = "127.0.0.1/32"
    with TestClient(create_app(settings), client=("127.0.0.1", 123)) as client:
        _setup_admin(client)
        payload = {
            **_policy_payload(),
            "mode": "observe",
            "network_restriction_enabled": True,
            "network_allowed_cidrs": ALLOWED,
            "network_admin_bypass": True,
            "network_contributor_bypass": True,
            "network_denied_message": "网络不允许",
        }
        saved = client.put("/api/v1/admin/agent-policy", json=payload, headers=_csrf(client))
        assert saved.status_code == 200, saved.text
        assert saved.json()["current_network_reason"] == "admin_bypass"
        before = saved.json()["today_task_count"]
        invalid = client.put(
            "/api/v1/admin/agent-policy",
            json={**payload, "network_allowed_cidrs": ["202.107.195.201/24"]},
            headers=_csrf(client),
        )
        assert invalid.status_code == 422
        assert client.app.state.policy.snapshot().network_allowed_cidrs == tuple(ALLOWED)
        # Admin actually creates a task from an unknown source.
        conv = client.post("/api/v1/conversations", json={}, headers=_csrf(client)).json()[
            "conversation_id"
        ]
        task = client.post(
            f"/api/v1/conversations/{conv}/messages",
            json={"message": "你好"},
            headers=_csrf(client),
        )
        assert task.status_code == 202, task.text
        finished = _wait_for_task(client, task.json()["task_id"])
        # Turning off exemption immediately applies to the same authenticated admin.
        blocked = client.put(
            "/api/v1/admin/agent-policy",
            json={**payload, "network_admin_bypass": False},
            headers=_csrf(client),
        )
        assert blocked.status_code == 200
        count = blocked.json()["today_task_count"]
        assert count == before + 1
        for path, body in [
            (f"conversations/{conv}/messages", {"message": "再次提问"}),
            (f"tasks/{finished['task_id']}/retry", {}),
            (f"answers/{finished['answer_id']}/reverify", {}),
        ]:
            result = client.post(
                "/api/v1/" + path,
                json=body,
                headers={**_csrf(client), "X-Forwarded-For": "202.107.195.201"},
            )
            assert result.status_code == 403, result.text
            assert result.json()["detail"]["code"] == "AGENT_NETWORK_DENIED"
        assert client.get("/api/v1/admin/agent-policy").json()["today_task_count"] == count
        assert client.get("/api/v1/agent/access").json()["network_allowed"] is False
        assert client.get(f"/api/v1/tasks/{finished['task_id']}").status_code == 200
        assert client.get("/api/v1/conversations").status_code == 200
        # Trusted edge header permits anonymous tasks; arbitrary XFF alone does not.
        client.post("/api/v1/auth/logout", headers=_csrf(client))
        client.get("/api/v1/auth/me")
        client.headers["X-HZCU-Client-IP"] = "202.107.195.203"
        assert client.get("/api/v1/agent/access").json()["network_allowed"] is True
        conv2 = client.post("/api/v1/conversations", json={}, headers=_csrf(client)).json()[
            "conversation_id"
        ]
        accepted = client.post(
            f"/api/v1/conversations/{conv2}/messages",
            json={"message": "你好"},
            headers=_csrf(client),
        )
        assert accepted.status_code == 202, accepted.text
        _wait_for_task(client, accepted.json()["task_id"])
    with TestClient(create_app(settings), client=("127.0.0.1", 123)) as restarted:
        assert restarted.app.state.policy.snapshot().network_allowed_cidrs == tuple(ALLOWED)
        assert restarted.get("/api/v1/agent/access").json()["network_allowed"] is False


def test_contributor_can_launch_from_outside_network_but_not_after_exemption_removed(tmp_path):
    settings = _settings(tmp_path)
    app = create_app(settings)
    with TestClient(app) as client:
        _setup_admin(client)
        payload = {
            **_policy_payload(),
            "network_restriction_enabled": True,
            "network_allowed_cidrs": ALLOWED,
        }
        assert (
            client.put(
                "/api/v1/admin/agent-policy", json=payload, headers=_csrf(client)
            ).status_code
            == 200
        )
        response = client.post(
            "/api/v1/admin/contributors",
            json={
                "username": "helper",
                "password": "helper-password",
                "public_name": "助教",
                "unit": "工程学院",
            },
            headers=_csrf(client),
        )
        assert response.status_code == 201, response.text
        client.post("/api/v1/auth/logout", headers=_csrf(client))
        challenge = client.get("/api/v1/auth/contributor/challenge").json()["challenge"]
        logged_in = client.post(
            "/api/v1/auth/contributor/login",
            json={"username": "helper", "password": "helper-password", "challenge": challenge},
            headers={"Origin": "http://web.test"},
        )
        assert logged_in.status_code == 200, logged_in.text
        assert client.get("/api/v1/agent/access").json()["network_allowed"] is True
        conv = client.post("/api/v1/conversations", json={}, headers=_csrf(client)).json()[
            "conversation_id"
        ]
        result = client.post(
            f"/api/v1/conversations/{conv}/messages",
            json={"message": "你好"},
            headers=_csrf(client),
        )
        assert result.status_code == 202, result.text
        _wait_for_task(client, result.json()["task_id"])
        assert client.get("/api/v1/admin/agent-policy").status_code == 404
        # Exercise the same saved setting the administrator changes through the API.
        client.app.state.policy._snapshot = replace(
            client.app.state.policy.snapshot(), network_contributor_bypass=False
        )
        denied = client.post(
            f"/api/v1/conversations/{conv}/messages",
            json={"message": "再次提问"},
            headers=_csrf(client),
        )
        assert denied.status_code == 403
        assert denied.json()["detail"]["code"] == "AGENT_NETWORK_DENIED"
