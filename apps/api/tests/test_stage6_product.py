import json
import sqlite3
import time
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient

from hzcu_agent.auth.service import AuthService
from hzcu_agent.cli import _pilot_backup, _pilot_restore
from hzcu_agent.config import Settings
from hzcu_agent.main import create_app


def _settings(tmp_path, **overrides) -> Settings:
    return Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'stage6.db'}",
        model_provider="demo",
        **overrides,
    )


def _csrf(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies.get("hzcu_csrf")}


def _cas_login(client: TestClient) -> None:
    started = client.get(
        "/api/v1/auth/login",
        params={"return_to": "http://web.test/"},
        follow_redirects=False,
    )
    service_url = parse_qs(urlsplit(started.headers["location"]).query)["service"][0]
    service = urlsplit(service_url)
    callback = client.get(
        f"{service.path}?{service.query}&ticket=ST-stage6-test",
        follow_redirects=False,
    )
    assert callback.status_code == 303


def _complete_answer(client: TestClient, prompt: str) -> tuple[str, str, str]:
    conversation = client.post(
        "/api/v1/conversations",
        json={"title": "删除回归会话"},
        headers=_csrf(client),
    )
    assert conversation.status_code == 201
    conversation_id = conversation.json()["conversation_id"]
    accepted = client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={
            "message": prompt,
            "client_message_id": f"delete-regression-{conversation_id}",
        },
        headers=_csrf(client),
    )
    assert accepted.status_code == 202
    task_id = accepted.json()["task_id"]
    task = None
    for _ in range(100):
        task = client.get(f"/api/v1/tasks/{task_id}").json()
        if task["status"] in {"completed", "failed"}:
            break
        time.sleep(0.02)
    assert task is not None
    assert task["status"] == "completed"
    assert task["answer_id"]
    return conversation_id, task_id, task["answer_id"]


def test_anonymous_devices_are_isolated_and_support_profile_todos(tmp_path) -> None:
    app = create_app(_settings(tmp_path))
    with TestClient(app) as first:
        assert first.get("/api/v1/auth/me").status_code == 200
        conversation = first.post(
            "/api/v1/conversations",
            json={},
            headers=_csrf(first),
        )
        conversation_id = conversation.json()["conversation_id"]
        profile = first.patch(
            "/api/v1/profile",
            json={
                "onboarding_completed": True,
                "attributes": [
                    {
                        "attribute_key": "major",
                        "attribute_value": "电子信息工程",
                    }
                ],
            },
            headers=_csrf(first),
        )
        assert profile.status_code == 200
        assert profile.json()["confirmed"][0]["attribute_value"] == "电子信息工程"
        todo = first.post(
            "/api/v1/todos",
            json={"title": "查看选课通知"},
            headers=_csrf(first),
        )
        assert todo.status_code == 201
        assert first.get("/api/v1/todos").json()[0]["title"] == "查看选课通知"

    with TestClient(app) as second:
        second.get("/api/v1/auth/me")
        hidden = second.get(f"/api/v1/conversations/{conversation_id}")
        assert hidden.status_code == 404
        assert second.get("/api/v1/conversations").json()["items"] == []
        assert second.get("/api/v1/todos").json() == []


def test_profile_field_can_be_confirmed_rejected_and_deleted_individually(
    tmp_path,
) -> None:
    with TestClient(create_app(_settings(tmp_path))) as client:
        identity = client.get("/api/v1/auth/me")
        assert identity.json()["visitor_data_available"] is False
        csrf = _csrf(client)
        saved = client.patch(
            "/api/v1/profile",
            json={
                "onboarding_completed": True,
                "attributes": [
                    {"attribute_key": "major", "attribute_value": "电子信息工程"},
                ],
            },
            headers=csrf,
        )
        assert saved.status_code == 200
        assert client.get("/api/v1/auth/me").json()["visitor_data_available"] is True

        deleted = client.delete("/api/v1/profile/attributes/major", headers=csrf)
        assert deleted.status_code == 204
        assert client.get("/api/v1/profile").json()["confirmed"] == []

        recreated = client.patch(
            "/api/v1/profile",
            json={
                "attributes": [
                    {"attribute_key": "major", "attribute_value": "软件工程"},
                ]
            },
            headers=csrf,
        )
        assert recreated.status_code == 200
        rejected = client.post(
            "/api/v1/profile/attributes/major/reject",
            headers=csrf,
        )
        assert rejected.status_code == 200
        assert rejected.json()["status"] == "rejected"

        missing = client.post(
            "/api/v1/profile/attributes/does-not-exist/confirm",
            headers=csrf,
        )
        assert missing.status_code == 422


def test_message_client_id_is_idempotent_and_conversation_can_be_restored(
    tmp_path,
) -> None:
    with TestClient(create_app(_settings(tmp_path))) as client:
        client.get("/api/v1/auth/me")
        conversation = client.post(
            "/api/v1/conversations",
            json={},
            headers=_csrf(client),
        )
        conversation_id = conversation.json()["conversation_id"]
        payload = {
            "message": "这学期什么时候开始选课？",
            "client_message_id": "browser-message-001",
        }
        first = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            json=payload,
            headers=_csrf(client),
        )
        second = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            json=payload,
            headers=_csrf(client),
        )
        assert first.status_code == second.status_code == 202
        assert first.json()["task_id"] == second.json()["task_id"]
        detail = client.get(f"/api/v1/conversations/{conversation_id}").json()
        assert len([item for item in detail["messages"] if item["role"] == "user"]) == 1


def test_personal_data_deletion_cascades_completed_answers(tmp_path) -> None:
    database_path = tmp_path / "stage6.db"
    with TestClient(create_app(_settings(tmp_path))) as client:
        client.get("/api/v1/auth/me")
        conversation_id, task_id, answer_id = _complete_answer(
            client,
            "删除个人数据前先生成一条完整回答。",
        )
        assert (
            client.patch(
                "/api/v1/profile",
                json={
                    "onboarding_completed": True,
                    "attributes": [
                        {"attribute_key": "major", "attribute_value": "软件工程"},
                    ],
                },
                headers=_csrf(client),
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/api/v1/todos",
                json={"title": "删除回归待办", "source_answer_id": answer_id},
                headers=_csrf(client),
            ).status_code
            == 201
        )
        assert (
            client.put(
                f"/api/v1/answers/{answer_id}/feedback",
                json={"rating": "helpful"},
                headers=_csrf(client),
            ).status_code
            == 200
        )

        deleted = client.delete("/api/v1/profile", headers=_csrf(client))

        assert deleted.status_code == 204
        assert client.get("/api/v1/conversations").json()["items"] == []
        assert client.get("/api/v1/todos").json() == []
        reset_profile = client.get("/api/v1/profile").json()
        assert reset_profile["confirmed"] == []
        assert reset_profile["suggestions"] == []
        assert reset_profile["onboarding_completed"] is False
        assert client.get(f"/api/v1/conversations/{conversation_id}").status_code == 404
        assert client.get(f"/api/v1/answers/{answer_id}").status_code == 404

    with sqlite3.connect(database_path) as database:
        assert database.execute(
            "SELECT count(*) FROM conversations WHERE id = ?", (conversation_id,)
        ).fetchone() == (0,)
        assert database.execute(
            "SELECT count(*) FROM agent_tasks WHERE id = ?", (task_id,)
        ).fetchone() == (0,)
        assert database.execute(
            "SELECT count(*) FROM answers WHERE id = ?", (answer_id,)
        ).fetchone() == (0,)
        assert database.execute("SELECT count(*) FROM answer_feedback").fetchone() == (0,)


def test_single_conversation_deletion_cascades_completed_answer(tmp_path) -> None:
    database_path = tmp_path / "stage6.db"
    with TestClient(create_app(_settings(tmp_path))) as client:
        client.get("/api/v1/auth/me")
        conversation_id, task_id, answer_id = _complete_answer(
            client,
            "删除单个会话前先生成一条完整回答。",
        )

        deleted = client.delete(
            f"/api/v1/conversations/{conversation_id}",
            headers=_csrf(client),
        )

        assert deleted.status_code == 204
        assert client.get("/api/v1/conversations").json()["items"] == []

    with sqlite3.connect(database_path) as database:
        assert database.execute(
            "SELECT count(*) FROM agent_tasks WHERE id = ?", (task_id,)
        ).fetchone() == (0,)
        assert database.execute(
            "SELECT count(*) FROM answers WHERE id = ?", (answer_id,)
        ).fetchone() == (0,)


def test_visitor_data_deletion_cascades_completed_answer(tmp_path) -> None:
    database_path = tmp_path / "stage6.db"
    with TestClient(create_app(_settings(tmp_path))) as client:
        client.get("/api/v1/auth/me")
        conversation_id, task_id, answer_id = _complete_answer(
            client,
            "删除匿名访客数据前先生成一条完整回答。",
        )

        deleted = client.delete(
            "/api/v1/identity/visitor-data",
            headers=_csrf(client),
        )

        assert deleted.status_code == 204
        assert client.get("/api/v1/auth/me").status_code == 200
        assert client.get("/api/v1/conversations").json()["items"] == []

    with sqlite3.connect(database_path) as database:
        assert database.execute(
            "SELECT count(*) FROM conversations WHERE id = ?", (conversation_id,)
        ).fetchone() == (0,)
        assert database.execute(
            "SELECT count(*) FROM agent_tasks WHERE id = ?", (task_id,)
        ).fetchone() == (0,)
        assert database.execute(
            "SELECT count(*) FROM answers WHERE id = ?", (answer_id,)
        ).fetchone() == (0,)


def test_pilot_backup_and_restore_round_trip_preserves_sqlite_state(tmp_path) -> None:
    database_path = tmp_path / "pilot.db"
    backup_path = tmp_path / "backups" / "pilot-copy.db"
    database_url = f"sqlite+aiosqlite:///{database_path}"

    with sqlite3.connect(database_path) as database:
        database.execute("PRAGMA journal_mode=WAL")
        database.execute("CREATE TABLE pilot_probe (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        database.execute(
            "INSERT INTO pilot_probe (id, value) VALUES (?, ?)",
            (1, "before-backup"),
        )
        database.commit()

    assert _pilot_backup(database_url, str(backup_path)) == 0
    assert backup_path.is_file()
    with sqlite3.connect(backup_path) as backup:
        assert backup.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert backup.execute("SELECT value FROM pilot_probe WHERE id = 1").fetchone() == (
            "before-backup",
        )

    with sqlite3.connect(database_path) as database:
        database.execute("UPDATE pilot_probe SET value = ? WHERE id = 1", ("mutated",))
        database.commit()

    assert _pilot_restore(database_url, str(backup_path)) == 0
    with sqlite3.connect(database_path) as restored:
        assert restored.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert restored.execute("SELECT value FROM pilot_probe WHERE id = 1").fetchone() == (
            "before-backup",
        )


def test_ca_login_explicitly_merges_visitor_data_and_enables_admin_console(
    tmp_path,
    monkeypatch,
) -> None:
    async def fake_validate_ticket(self, *, ticket: str, service_url: str) -> str:
        del self, ticket, service_url
        return "pilot-owner-0001"

    monkeypatch.setattr(AuthService, "_validate_ticket", fake_validate_ticket)
    settings = _settings(
        tmp_path,
        auth_mode="optional_cas",
        auth_session_secret="stage6-test-secret-with-at-least-32-characters",
        cas_service_registered=True,
        cas_browser_base_url="https://ca.hzcu.edu.cn/cas",
        cas_server_base_url="https://ca.hzcu.edu.cn/cas",
        public_api_base_url="http://testserver",
        web_app_url="http://web.test",
        admin_cas_subjects="pilot-owner-0001",
    )
    with TestClient(create_app(settings)) as client:
        client.get("/api/v1/auth/me")
        visitor_conversation = client.post(
            "/api/v1/conversations",
            json={"title": "登录前的问题"},
            headers=_csrf(client),
        ).json()["conversation_id"]
        accepted = client.post(
            f"/api/v1/conversations/{visitor_conversation}/messages",
            json={
                "message": "这条公开试用会话需要能够按 ID 追溯。",
                "client_message_id": "trace-demo-message",
            },
            headers=_csrf(client),
        )
        assert accepted.status_code == 202
        task_id = accepted.json()["task_id"]
        task = None
        for _ in range(100):
            task = client.get(f"/api/v1/tasks/{task_id}").json()
            if task["status"] in {"completed", "failed"}:
                break
            time.sleep(0.02)
        assert task is not None
        assert task["status"] == "completed"
        answer_id = task["answer_id"]
        detail_before_login = client.get(f"/api/v1/conversations/{visitor_conversation}").json()
        message_id = detail_before_login["messages"][0]["message_id"]
        assert (
            client.get(f"/api/v1/admin/conversation-trace/{visitor_conversation}").status_code
            == 404
        )
        client.post(
            "/api/v1/todos",
            json={"title": "登录前待办"},
            headers=_csrf(client),
        )

        _cas_login(client)
        identity = client.get("/api/v1/auth/me").json()
        assert identity["role"] == "admin"
        assert identity["visitor_data_available"] is True
        merged = client.post(
            "/api/v1/identity/merge-visitor",
            headers=_csrf(client),
        )
        assert merged.status_code == 200
        assert merged.json()["conversations_moved"] == 1
        assert merged.json()["todos_moved"] == 1
        assert client.get(f"/api/v1/conversations/{visitor_conversation}").status_code == 200
        assert client.get("/api/v1/admin/overview").status_code == 200
        assert client.get("/api/v1/admin/feedback").status_code == 200
        assert client.get("/api/v1/admin/task-health").status_code == 200
        for trace_id in (visitor_conversation, message_id, task_id, answer_id):
            trace = client.get(f"/api/v1/admin/conversation-trace/{trace_id}")
            assert trace.status_code == 200
            assert trace.json()["conversation_id"] == visitor_conversation
            assert trace.json()["matched_trace_id"] == trace_id
            assert trace.json()["messages"][0]["client_message_id"] == "trace-demo-message"
            assert trace.json()["tasks"][0]["task_id"] == task_id
        assert client.get("/api/v1/admin/conversation-trace/ans_missing").status_code == 404

        with sqlite3.connect(tmp_path / "stage6.db") as database:
            audit_rows = database.execute(
                """
                SELECT event_type, outcome, request_id, metadata
                FROM security_audit_events
                WHERE event_type LIKE 'admin.%'
                ORDER BY occurred_at, id
                """
            ).fetchall()
        assert [row[0] for row in audit_rows] == [
            "admin.overview",
            "admin.feedback",
            "admin.task_health",
            "admin.conversation_trace",
            "admin.conversation_trace",
            "admin.conversation_trace",
            "admin.conversation_trace",
        ]
        assert all(row[1] == "succeeded" for row in audit_rows)
        assert all(row[2].startswith("req_") for row in audit_rows)
        assert len({row[2] for row in audit_rows}) == len(audit_rows)
        assert all(json.loads(row[3]) == {"read_only": True} for row in audit_rows)
