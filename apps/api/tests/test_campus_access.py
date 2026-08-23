import json
from datetime import UTC, datetime, timedelta

import httpx

from hzcu_agent.auth.campus_access import CampusAccessBroker, PreparedCampusAccess
from hzcu_agent.config import Settings
from hzcu_agent.db import Database
from hzcu_agent.ingestion.catalog import SourceRegistry


async def test_vpn_query_accepts_registered_public_notice_route(tmp_path) -> None:
    now = datetime.now(UTC)
    sidecar_requests: list[httpx.Request] = []

    async def sidecar(request: httpx.Request) -> httpx.Response:
        sidecar_requests.append(request)
        if request.url.path == "/v1/notices/search":
            return httpx.Response(
                200,
                json={
                    "capability": "campus_notice.read",
                    "evidence": [
                        {
                            "title": "伪造业务页面",
                            "publisher": "不可信发布者",
                            "canonical_url": "http://tw.hzcu.edu.cn/apply/submit",
                            "excerpt": "即使主机匹配，非通知详情路径也必须被中心 API 丢弃。",
                            "source_id": "hzcu-tw-notices",
                            "published_at": now.isoformat(),
                            "observed_at": now.isoformat(),
                        },
                        {
                            "title": "社团通知",
                            "publisher": "不可信发布者",
                            "canonical_url": (
                                "http://tw.hzcu.edu.cn/redir.php?catalog_id=161&object_id=123"
                            ),
                            "excerpt": "这是一条只读通知内容，用于验证校外代理信源。",
                            "source_id": "hzcu-tw-notices",
                            "published_at": now.isoformat(),
                            "observed_at": now.isoformat(),
                        },
                    ],
                },
            )
        return httpx.Response(204)

    settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'access.db').as_posix()}",
        auth_mode="optional_cas",
        auth_session_secret="test-auth-secret-at-least-32-characters",
        campus_query_route="vpn_sidecar",
        credential_vpn_enabled=True,
        vpn_sidecar_base_url="http://sidecar.test",
        vpn_sidecar_api_token="test-sidecar-token-at-least-32-characters",
    )
    database = Database(settings)
    registry = SourceRegistry(database, settings.resolved_source_registry_path)
    client = httpx.AsyncClient(transport=httpx.MockTransport(sidecar))
    broker = CampusAccessBroker(settings=settings, registry=registry, client=client)
    await broker.bind(
        "usr_test",
        PreparedCampusAccess(
            subject="student-test",
            session_handle="opaque-handle-that-is-at-least-32-characters",
            expires_at=now + timedelta(minutes=10),
        ),
    )
    try:
        evidence = await broker.query_vpn(
            user_id="usr_test",
            queries=["最近有什么社团通知"],
            limit=3,
        )
        assert [item.source_id for item in evidence] == ["hzcu-tw-notices"]
        assert evidence[0].publisher == "校团委通知公告"
    finally:
        await broker.close()
        await client.aclose()
        await database.close()
    revoke = next(request for request in sidecar_requests if request.method == "DELETE")
    assert revoke.url.path == "/v1/sessions"
    assert "opaque-session-handle" not in str(revoke.url)


async def test_vpn_batch_query_sends_both_shapes_and_reports_per_query_counts(
    tmp_path,
) -> None:
    now = datetime.now(UTC)
    search_bodies: list[dict] = []

    async def sidecar(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/notices/search":
            search_bodies.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "capability": "campus_notice.read",
                    "evidence": [
                        {
                            "title": "社团通知",
                            "publisher": "不可信发布者",
                            "canonical_url": (
                                "http://tw.hzcu.edu.cn/redir.php?catalog_id=161&object_id=123"
                            ),
                            "excerpt": "这是一条只读通知内容，用于验证批查询透传。",
                            "source_id": "hzcu-tw-notices",
                            "published_at": now.isoformat(),
                            "observed_at": now.isoformat(),
                        }
                    ],
                    "search_trace": {
                        "attempted_source_ids": ["hzcu-tw-notices"],
                        "waves": 1,
                        "exhausted": False,
                        "candidate_count": 4,
                        "hydrated_candidate_count": 2,
                        "per_query_result_counts": {"社团通知": 1},
                    },
                },
            )
        return httpx.Response(204)

    settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'batch.db').as_posix()}",
        auth_mode="optional_cas",
        auth_session_secret="test-auth-secret-at-least-32-characters",
        campus_query_route="vpn_sidecar",
        credential_vpn_enabled=True,
        vpn_sidecar_base_url="http://sidecar.test",
        vpn_sidecar_api_token="test-sidecar-token-at-least-32-characters",
    )
    database = Database(settings)
    registry = SourceRegistry(database, settings.resolved_source_registry_path)
    client = httpx.AsyncClient(transport=httpx.MockTransport(sidecar))
    broker = CampusAccessBroker(settings=settings, registry=registry, client=client)
    await broker.bind(
        "usr_test",
        PreparedCampusAccess(
            subject="student-test",
            session_handle="opaque-handle-that-is-at-least-32-characters",
            expires_at=now + timedelta(minutes=10),
        ),
    )
    try:
        outcome = await broker.query_vpn(
            user_id="usr_test",
            queries=["社团通知", "社团通知", "校历"],
            limit=3,
        )
    finally:
        await broker.close()
        await client.aclose()
        await database.close()

    body = search_bodies[0]
    # A sidecar that predates batching ignores "queries" and still serves "query".
    assert body["query"] == "社团通知"
    assert body["queries"] == ["社团通知", "校历"]
    # A zero here means this query matched nothing in the pages read so far,
    # never that the school has no such information.
    assert outcome.per_query_result_counts == {"社团通知": 1, "校历": 0}
