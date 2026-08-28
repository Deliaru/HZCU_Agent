import sqlite3
import time

from fastapi.testclient import TestClient

from hzcu_agent.config import Settings
from hzcu_agent.main import create_app
from hzcu_agent.schemas import GoalHypothesis, SemanticDossier, SemanticSignals
from hzcu_agent.services.coordinator import AgentCoordinator


def _settings(tmp_path) -> Settings:
    return Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'admission.db'}",
        model_provider="demo",
        auth_mode="anonymous",
        local_admin_enabled=True,
        auth_session_secret="test-only-session-secret-with-at-least-32-characters",
        admin_cas_subjects="pilot-admin",
        public_api_base_url="http://testserver",
        web_app_url="http://web.test",
    )


def _csrf(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies.get("hzcu_csrf")}


def _policy_payload() -> dict[str, object]:
    return {
        "mode": "enforce",
        "subject_window_limit": 5,
        "subject_window_seconds": 1800,
        "subject_daily_limit": 15,
        "max_running_per_subject": 1,
        "max_queued_per_subject": 1,
        "global_queue_limit": 30,
        "queue_timeout_seconds": 300,
        "agent_concurrency": 4,
        "model_concurrency": 4,
        "global_daily_task_limit": 300,
        "global_daily_model_call_limit": 1500,
        "per_task_model_call_limit": 8,
        "max_message_length": 1500,
        "scope_policy": "balanced",
        "timezone": "Asia/Shanghai",
        "turnstile_enabled": False,
        "turnstile_site_key": None,
        "verification_lease_hours": 24,
        "ip_new_subjects_per_hour": 60,
    }


def _setup_admin(client: TestClient) -> None:
    challenge = client.get("/api/v1/auth/local-admin/challenge").json()["challenge"]
    response = client.post(
        "/api/v1/auth/local-admin/setup",
        json={
            "username": "pilot-admin",
            "password": "pilot-admin-password",
            "challenge": challenge,
        },
        headers={"Origin": "http://web.test"},
    )
    assert response.status_code == 200


def _wait_for_task(client: TestClient, task_id: str) -> dict:
    for _ in range(100):
        task = client.get(f"/api/v1/tasks/{task_id}").json()
        if task["status"] in {"completed", "failed", "canceled"}:
            return task
        time.sleep(0.02)
    raise AssertionError(f"task {task_id} did not finish")


def test_anonymous_admission_limits_and_data_deletion_keep_security_counters(tmp_path) -> None:
    database_path = tmp_path / "admission.db"
    with TestClient(create_app(_settings(tmp_path))) as client:
        _setup_admin(client)
        saved = client.put(
            "/api/v1/admin/agent-policy",
            json=_policy_payload(),
            headers=_csrf(client),
        )
        assert saved.status_code == 200

        logged_out = client.post("/api/v1/auth/logout", headers=_csrf(client))
        assert logged_out.status_code == 204
        client.get("/api/v1/auth/me")

        access = client.get("/api/v1/agent/access")
        assert access.status_code == 200
        assert access.json()["window_remaining"] == 5
        assert access.json()["window_reset_at"] is None

        conversation = client.post("/api/v1/conversations", json={}, headers=_csrf(client))
        assert conversation.status_code == 201
        conversation_id = conversation.json()["conversation_id"]
        for index in range(5):
            accepted = client.post(
                f"/api/v1/conversations/{conversation_id}/messages",
                json={
                    "message": f"第 {index + 1} 次查询学校选课通知。",
                    "client_message_id": f"admission-{index}",
                },
                headers=_csrf(client),
            )
            assert accepted.status_code == 202
            assert _wait_for_task(client, accepted.json()["task_id"])["status"] == "completed"

        assert client.get("/api/v1/agent/access").json()["window_remaining"] == 0
        rejected = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            json={"message": "第 6 次查询学校选课通知。", "client_message_id": "admission-6"},
            headers=_csrf(client),
        )
        assert rejected.status_code == 429
        assert rejected.json()["detail"]["code"] == "SUBJECT_RATE_LIMITED"

        deleted = client.delete("/api/v1/profile", headers=_csrf(client))
        assert deleted.status_code == 204
        assert client.get("/api/v1/agent/access").json()["window_remaining"] == 0

    with sqlite3.connect(database_path) as database:
        assert database.execute("SELECT count(*) FROM agent_admission_events").fetchone() == (5,)
        assert database.execute(
            "SELECT sum(task_count) FROM agent_usage_counters WHERE scope_key LIKE 'subject:%'"
        ).fetchone() == (5,)
        assert database.execute(
            "SELECT task_count FROM agent_usage_counters "
            "WHERE scope_key = 'rejection:SUBJECT_RATE_LIMITED'"
        ).fetchone() == (1,)


def test_scope_backstop_rejects_wrapped_generic_work_without_blocking_campus_questions() -> None:
    dossier = SemanticDossier(
        goal_hypotheses=[GoalHypothesis(goal="原始问题", confidence=1.0)],
        signals=SemanticSignals(domain_scope="ambiguous"),
    )

    assert (
        AgentCoordinator._with_domain_scope(dossier, "帮我写一份学校课程介绍")
        .signals.domain_scope
        == "out_of_scope"
    )
    assert (
        AgentCoordinator._with_domain_scope(dossier, "工程学院有几个专业")
        .signals.domain_scope
        == "in_scope"
    )
    assert (
        AgentCoordinator._with_domain_scope(dossier, "这个学期什么时候开学")
        .signals.domain_scope
        == "in_scope"
    )
    # Topic words such as “编程专业” or “翻译专业” may be legitimate
    # campus entities; only an actual request to produce generic code/text is
    # rejected by the safety backstop.
    assert (
        AgentCoordinator._with_domain_scope(dossier, "编程专业有什么课程")
        .signals.domain_scope
        == "in_scope"
    )
    assert (
        AgentCoordinator._with_domain_scope(dossier, "翻译专业在哪个学院")
        .signals.domain_scope
        == "in_scope"
    )
    assert (
        AgentCoordinator._with_domain_scope(dossier, "请写出工程学院有哪些专业")
        .signals.domain_scope
        == "in_scope"
    )
    assert (
        AgentCoordinator._with_domain_scope(dossier, "帮我写一份工程学院专业名单")
        .signals.domain_scope
        == "in_scope"
    )
    assert (
        AgentCoordinator._with_domain_scope(dossier, "帮我写一段 Python 代码")
        .signals.domain_scope
        == "out_of_scope"
    )
