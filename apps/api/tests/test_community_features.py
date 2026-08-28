import time

from fastapi.testclient import TestClient

from hzcu_agent.config import Settings
from hzcu_agent.main import create_app
from hzcu_agent.schemas import GoalHypothesis, SemanticDossier, SemanticSignals
from hzcu_agent.services.model_gateway import DemoModelGateway


def _settings(tmp_path) -> Settings:
    return Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'community.db'}",
        model_provider="demo",
        auth_mode="anonymous",
        local_admin_enabled=True,
        auth_session_secret="community-test-session-secret-with-32-chars",
        public_api_base_url="http://testserver",
        web_app_url="http://testserver",
    )


def _csrf(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies.get("hzcu_csrf") or ""}


def _wait_for_task(client: TestClient, task_id: str) -> dict[str, object]:
    result: dict[str, object] = {}
    for _ in range(200):
        result = client.get(f"/api/v1/tasks/{task_id}").json()
        if result.get("status") in {"completed", "failed", "canceled"}:
            break
        time.sleep(0.05)
    assert result.get("status") == "completed", result
    return result


def _admin_setup(client: TestClient) -> None:
    client.get("/api/v1/auth/me")
    challenge = client.get("/api/v1/auth/local-admin/challenge")
    assert challenge.status_code == 200
    response = client.post(
        "/api/v1/auth/local-admin/setup",
        json={
            "username": "community-admin",
            "password": "community-admin-password",
            "challenge": challenge.json()["challenge"],
        },
        headers={"Origin": "http://testserver"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["role"] == "admin"


def test_low_evidence_question_offer_review_contributor_and_knowledge_lifecycle(
    tmp_path,
    monkeypatch,
) -> None:
    async def in_scope_understand(
        self: DemoModelGateway,
        *,
        original_query: str,
        conversation_context,
        profile_context,
        current_time,
    ) -> SemanticDossier:
        del self, conversation_context, profile_context, current_time
        scope = "out_of_scope" if "写诗" in original_query else "in_scope"
        return SemanticDossier(
            goal_hypotheses=[
                GoalHypothesis(
                    goal=original_query,
                    confidence=0.9,
                    support=["用户原始问题"],
                    required_evidence=["校园官方材料"],
                )
            ],
            signals=SemanticSignals(
                domains=["校园综合"],
                intents=["信息查询"],
                domain_scope=scope,
                scope_reason="集成测试范围信号",
            ),
        )

    monkeypatch.setattr(DemoModelGateway, "understand", in_scope_understand)
    settings = _settings(tmp_path)

    with TestClient(create_app(settings)) as visitor:
        assert visitor.get("/api/v1/auth/me").status_code == 200
        conversation = visitor.post(
            "/api/v1/conversations",
            json={},
            headers=_csrf(visitor),
        )
        assert conversation.status_code == 201
        conversation_id = conversation.json()["conversation_id"]
        accepted = visitor.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            json={"message": "工程学院机械系第二党支部安排是什么？"},
            headers=_csrf(visitor),
        )
        assert accepted.status_code == 202
        task = _wait_for_task(visitor, accepted.json()["task_id"])
        answer_id = str(task["answer_id"])
        answer = visitor.get(f"/api/v1/answers/{answer_id}")
        assert answer.status_code == 200
        offer = answer.json()["question_offer"]
        assert offer["reason"] == "no_evidence"
        assert answer.json()["response_style"] == "neutral"

        question_response = visitor.put(
            f"/api/v1/answers/{answer_id}/question",
            json={"title": "机械系党支部安排", "details": "请补充本年度具体安排。"},
            headers=_csrf(visitor),
        )
        assert question_response.status_code == 200
        question_id = question_response.json()["question_id"]
        assert question_response.json()["status"] == "pending_review"
        assert visitor.get("/api/v1/questions").json() == []

        out_of_scope_conversation = visitor.post(
            "/api/v1/conversations",
            json={},
            headers=_csrf(visitor),
        )
        out_of_scope = visitor.post(
            f"/api/v1/conversations/{out_of_scope_conversation.json()['conversation_id']}/messages",
            json={"message": "请写诗"},
            headers=_csrf(visitor),
        )
        assert out_of_scope.status_code == 202
        out_of_scope_task = _wait_for_task(visitor, out_of_scope.json()["task_id"])
        out_of_scope_answer = visitor.get(f"/api/v1/answers/{out_of_scope_task['answer_id']}")
        assert out_of_scope_answer.status_code == 200
        assert out_of_scope_answer.json()["question_offer"] is None

    with TestClient(create_app(settings)) as admin:
        _admin_setup(admin)
        reviewed = admin.put(
            f"/api/v1/admin/questions/{question_id}",
            json={"status": "open"},
            headers=_csrf(admin),
        )
        assert reviewed.status_code == 200
        contributor = admin.post(
            "/api/v1/admin/contributors",
            json={
                "username": "mechanics-helper",
                "password": "helper-password",
                "public_name": "机械系助教",
                "unit": "工程学院",
            },
            headers=_csrf(admin),
        )
        assert contributor.status_code == 201, contributor.text
        contributor_id = contributor.json()["contributor_id"]

        detail = admin.get(f"/api/v1/questions/{question_id}")
        assert detail.status_code == 200
        assert detail.json()["answers"] == []

        challenge = admin.get("/api/v1/auth/contributor/challenge")
        assert challenge.status_code == 200

    with TestClient(create_app(settings)) as answerer:
        challenge = answerer.get("/api/v1/auth/contributor/challenge")
        login = answerer.post(
            "/api/v1/auth/contributor/login",
            json={
                "username": "mechanics-helper",
                "password": "helper-password",
                "challenge": challenge.json()["challenge"],
            },
            headers={"Origin": "http://testserver"},
        )
        assert login.status_code == 200, login.text
        assert login.json()["role"] == "contributor"
        assert login.json()["read_only_capability"] == "community.answer"
        assert (
            answerer.post(
                "/api/v1/conversations",
                json={},
                headers=_csrf(answerer),
            ).status_code
            == 403
        )
        assert answerer.get("/api/v1/agent/access").status_code == 403
        assert answerer.get("/api/v1/profile").status_code == 403
        posted = answerer.post(
            f"/api/v1/questions/{question_id}/answers",
            json={"answer_markdown": "这是一份授权贡献者提供的待核验说明。"},
            headers=_csrf(answerer),
        )
        assert posted.status_code == 201, posted.text
        community_answer_id = posted.json()["answer_id"]

        duplicate = answerer.post(
            f"/api/v1/questions/{question_id}/answers",
            json={"answer_markdown": "重复回答不应创建第二条。"},
            headers=_csrf(answerer),
        )
        assert duplicate.status_code == 409

    with TestClient(create_app(settings)) as admin:
        challenge = admin.get("/api/v1/auth/local-admin/challenge").json()["challenge"]
        login = admin.post(
            "/api/v1/auth/local-admin/login",
            json={
                "username": "community-admin",
                "password": "community-admin-password",
                "challenge": challenge,
            },
            headers={"Origin": "http://testserver"},
        )
        assert login.status_code == 200
        public_questions = admin.get("/api/v1/questions")
        assert public_questions.status_code == 200
        assert public_questions.json()[0]["status"] == "answered"
        assert public_questions.json()[0]["answer_count"] == 1
        assert public_questions.json()[0].get("question_id") == question_id

        entry_payload = {
            "question_id": question_id,
            "title": "机械系第二党支部安排",
            "canonical_question": "工程学院机械系第二党支部本年度安排是什么？",
            "answer_markdown": "安排以学院当年发布的正式通知为准。",
            "category": "组织安排",
            "alternative_phrasings": ["机械系党支部安排"],
            "applicable_scope": "工程学院机械系",
            "maintainer_unit": "工程学院",
            "basis_note": "授权贡献者回答，管理员人工核验。",
            "validity": "stable",
            "visibility": "public",
            "origin_answer_ids": [community_answer_id],
        }
        created = admin.post(
            "/api/v1/admin/knowledge",
            json=entry_payload,
            headers=_csrf(admin),
        )
        assert created.status_code == 201, created.text
        entry_id = created.json()["entry_id"]
        published = admin.post(
            f"/api/v1/admin/knowledge/{entry_id}/publish",
            json={},
            headers=_csrf(admin),
        )
        assert published.status_code == 200, published.text
        first_version = published.json()["published_version_id"]
        assert published.json()["status"] == "published"
        republished_same = admin.post(
            f"/api/v1/admin/knowledge/{entry_id}/publish",
            json={},
            headers=_csrf(admin),
        )
        assert republished_same.status_code == 200, republished_same.text
        assert republished_same.json()["published_version_id"] != first_version

        entry_payload["title"] = "机械系第二党支部年度安排"
        updated = admin.put(
            f"/api/v1/admin/knowledge/{entry_id}",
            json=entry_payload,
            headers=_csrf(admin),
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["status"] == "draft"
        republished = admin.post(
            f"/api/v1/admin/knowledge/{entry_id}/publish",
            json={},
            headers=_csrf(admin),
        )
        assert republished.status_code == 200
        assert (
            republished.json()["published_version_id"]
            != republished_same.json()["published_version_id"]
        )

        public_entry = admin.get(f"/api/v1/knowledge/{entry_id}")
        assert public_entry.status_code == 200
        public_detail = admin.get(f"/api/v1/questions/{question_id}")
        assert public_detail.json()["answers"][0]["knowledge_review_state"] == "published"
        retired = admin.post(
            f"/api/v1/admin/knowledge/{entry_id}/retire",
            json={},
            headers=_csrf(admin),
        )
        assert retired.status_code == 200
        assert admin.get(f"/api/v1/knowledge/{entry_id}").status_code == 404

        disabled = admin.put(
            f"/api/v1/admin/contributors/{contributor_id}",
            json={"status": "disabled"},
            headers=_csrf(admin),
        )
        assert disabled.status_code == 200
