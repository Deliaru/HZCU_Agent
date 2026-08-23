import asyncio
import threading
import time
from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import event

from hzcu_agent.config import Settings
from hzcu_agent.main import create_app
from hzcu_agent.models import new_id, utc_now
from hzcu_agent.schemas import Evidence, ToolResult
from hzcu_agent.services.coordinator import AgentCoordinator
from hzcu_agent.tools.campus_memory import CampusMemorySearchTool


async def _single_evidence_memory_run(
    self,
    arguments,
    trace_id,
    *,
    allowed_visibilities=None,
):
    del self, arguments, allowed_visibilities
    observed_at = utc_now()
    return ToolResult(
        tool="search_campus_memory",
        status="ok",
        data={"query": "选课", "result_count": 1},
        evidence=[
            Evidence(
                evidence_id=new_id("ev"),
                title="关于新学期选课工作的通知",
                publisher="浙大城市学院教务处",
                canonical_url="https://www.hzcu.edu.cn/info/1001/12345.htm",
                published_at=observed_at - timedelta(days=1),
                observed_at=observed_at,
                fresh_until=None,
                excerpt="学生应在规定时间内完成预选、正选和补退选。",
                source_id="hzcu-jwc",
                authority_level="official",
                audience_scopes=["public"],
                effective_from=observed_at - timedelta(days=1),
                retrieval_mode="memory",
            )
        ],
        trace_id=trace_id,
    )


def test_demo_mode_completes_full_agent_task(
    tmp_path,
    monkeypatch,
) -> None:
    observed_memory_scopes: list[frozenset[str] | None] = []

    async def pilot_memory_run(
        self,
        arguments,
        trace_id,
        *,
        allowed_visibilities=None,
    ):
        observed_memory_scopes.append(allowed_visibilities)
        return await _single_evidence_memory_run(
            self,
            arguments,
            trace_id,
            allowed_visibilities=allowed_visibilities,
        )

    monkeypatch.setattr(CampusMemorySearchTool, "run", pilot_memory_run)
    settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'agent.db'}",
        model_provider="demo",
        pilot_anonymous_campus_mirror=True,
    )

    with TestClient(create_app(settings)) as client:
        health = client.get("/api/v1/health")
        assert health.status_code == 200
        assert health.json()["model_provider"] == "demo"
        assert health.headers["x-request-id"].startswith("req_")

        conversation = client.post(
            "/api/v1/conversations",
            json={"profile_context": {"student_type": "undergraduate"}},
        )
        assert conversation.status_code == 201
        conversation_id = conversation.json()["conversation_id"]

        accepted = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            json={"message": "选课快开始了吗？我应该先准备什么？"},
            headers={"X-CSRF-Token": client.cookies.get("hzcu_csrf")},
        )
        assert accepted.status_code == 202
        task_id = accepted.json()["task_id"]

        task_data = None
        for _ in range(100):
            response = client.get(f"/api/v1/tasks/{task_id}")
            task_data = response.json()
            if task_data["status"] in {"completed", "failed"}:
                break
            time.sleep(0.02)

        assert task_data is not None
        assert task_data["status"] == "completed"
        assert observed_memory_scopes
        assert set(observed_memory_scopes) == {
            frozenset({"public", "campus"}),
        }
        answer = client.get(f"/api/v1/answers/{task_data['answer_id']}")
        assert answer.status_code == 200
        answer_data = answer.json()
        assert answer_data["verification_mode"] == "cache"
        assert len(answer_data["evidence"]) == 1
        assert len(answer_data["claims"]) == 1
        assert answer_data["claims"][0]["statement_type"] == "campus_fact"
        assert (
            answer_data["claims"][0]["citations"][0]["evidence_id"]
            == (answer_data["evidence"][0]["evidence_id"])
        )
        assert answer_data["grounding"]["citation_coverage"] == 1
        assert answer_data["grounding"]["fully_supported_rate"] == 1
        assert answer_data["performance"]["model_call_count"] == 2
        assert len(answer_data["performance"]["spans"]) >= 2
        assert answer_data["performance"]["scenario"] == "no_live_read"
        assert answer_data["performance"]["model_ttft_measurable"] is True
        assert answer_data["evidence"][0]["document_version_id"] is None
        assert answer_data["evidence"][0]["canonical_url"].startswith("https://")
        assert answer_data["evidence"][0]["authority_level"] == "official"
        assert answer_data["evidence"][0]["audience_scopes"] == ["public"]
        evidence_id = answer_data["evidence"][0]["evidence_id"]
        listed_evidence = client.get(f"/api/v1/answers/{answer_data['answer_id']}/evidence")
        assert listed_evidence.status_code == 200
        assert listed_evidence.json()[0]["evidence_id"] == evidence_id
        single_evidence = client.get(f"/api/v1/evidence/{evidence_id}")
        assert single_evidence.status_code == 200
        feedback = client.post(
            "/api/v1/feedback",
            json={"answer_id": answer_data["answer_id"], "rating": "helpful"},
            headers={"X-CSRF-Token": client.cookies.get("hzcu_csrf")},
        )
        assert feedback.status_code == 200
        assert feedback.json()["rating"] == "helpful"
        assert "[来源1]" in answer_data["answer_markdown"]

        sources = client.get("/api/v1/sources")
        assert sources.status_code == 200
        source_rows = sources.json()
        # Count tracks the built-in sources.yaml allowlist (grows as campus sources are added).
        assert len(source_rows) >= 8
        assert all("health_state" in source for source in source_rows)
        assert all("chunk_count" in source for source in source_rows)
        assert sum(item["resource_count"] for item in sources.json()) == 0

        alerts = client.get("/api/v1/sources/alerts")
        assert alerts.status_code == 200


def test_source_observatory_uses_bounded_aggregate_queries(tmp_path) -> None:
    settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'source-observatory.db'}",
        model_provider="demo",
    )

    with TestClient(create_app(settings)) as client:
        client.get("/api/v1/auth/me")
        statements: list[str] = []

        def capture_statement(
            _connection,
            _cursor,
            statement,
            _parameters,
            _context,
            _executemany,
        ) -> None:
            statements.append(statement)

        engine = client.app.state.database.engine.sync_engine
        event.listen(engine, "before_cursor_execute", capture_statement)
        try:
            response = client.get("/api/v1/sources")
        finally:
            event.remove(engine, "before_cursor_execute", capture_statement)

    assert response.status_code == 200
    assert len(response.json()) >= 8
    select_count = sum(statement.lstrip().upper().startswith("SELECT") for statement in statements)
    assert select_count <= 12


def test_cancel_at_answer_persistence_boundary_keeps_restorable_answer(
    tmp_path,
    monkeypatch,
) -> None:
    persistence_started = threading.Event()
    release_persistence = threading.Event()
    original_persist_performance = AgentCoordinator._persist_performance

    async def held_persist_performance(
        self,
        task_id,
        performance,
        spans,
    ):
        persistence_started.set()
        await asyncio.to_thread(release_persistence.wait)
        return await original_persist_performance(
            self,
            task_id,
            performance,
            spans,
        )

    monkeypatch.setattr(CampusMemorySearchTool, "run", _single_evidence_memory_run)
    monkeypatch.setattr(
        AgentCoordinator,
        "_persist_performance",
        held_persist_performance,
    )
    settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'cancel-race.db'}",
        model_provider="demo",
    )

    with TestClient(create_app(settings)) as client:
        client.get("/api/v1/auth/me")
        csrf = {"X-CSRF-Token": client.cookies.get("hzcu_csrf")}
        conversation_id = client.post(
            "/api/v1/conversations",
            json={},
            headers=csrf,
        ).json()["conversation_id"]
        accepted = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            json={"message": "选课开始前应该准备什么？"},
            headers=csrf,
        )
        task_id = accepted.json()["task_id"]

        try:
            assert persistence_started.wait(timeout=10)
            canceled = client.post(
                f"/api/v1/tasks/{task_id}/cancel",
                headers=csrf,
            )
            assert canceled.status_code == 200
            task = canceled.json()
            assert task["status"] == "completed"
            assert task["error_code"] is None
            assert task["answer_id"] is not None
            assert client.get(f"/api/v1/answers/{task['answer_id']}").status_code == 200
        finally:
            release_persistence.set()

        for _ in range(100):
            task = client.get(f"/api/v1/tasks/{task_id}").json()
            if task["status"] == "completed":
                break
            time.sleep(0.02)
        assert task["status"] == "completed"
        assert task["answer_id"] is not None
