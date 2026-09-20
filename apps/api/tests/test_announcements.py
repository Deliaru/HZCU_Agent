import sqlite3
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient

from hzcu_agent.auth.service import AuthService
from hzcu_agent.config import Settings
from hzcu_agent.main import create_app


def csrf(client):
    return {"X-CSRF-Token": client.cookies.get("hzcu_csrf")}


def test_announcement_publish_read_isolation_withdraw_and_audit(tmp_path, monkeypatch):
    async def validate(self, *, ticket, service_url):
        return "announcement-admin"

    monkeypatch.setattr(AuthService, "_validate_ticket", validate)
    path = tmp_path / "announcements.db"
    settings = Settings(
        environment="test",
        model_provider="demo",
        database_url=f"sqlite+aiosqlite:///{path}",
        auth_mode="optional_cas",
        auth_session_secret="announcement-test-secret-at-least-32-characters",
        cas_service_registered=True,
        cas_browser_base_url="https://ca.hzcu.edu.cn/cas",
        cas_server_base_url="https://ca.hzcu.edu.cn/cas",
        public_api_base_url="http://testserver",
        web_app_url="http://web.test",
        admin_cas_subjects="announcement-admin",
    )
    app = create_app(settings)
    with TestClient(app) as admin:
        visitor = TestClient(app)
        other = TestClient(app)
        endpoint = "/api/v1/admin/announcements"
        assert visitor.get("/api/v1/announcements/unread").json() == []
        assert visitor.get(endpoint).status_code == 404
        assert (
            visitor.post(
                endpoint, json={"title": "x", "content": "x"}, headers=csrf(visitor)
            ).status_code
            == 404
        )
        started = admin.get(
            "/api/v1/auth/login", params={"return_to": "http://web.test/"}, follow_redirects=False
        )
        service = urlsplit(parse_qs(urlsplit(started.headers["location"]).query)["service"][0])
        assert (
            admin.get(
                f"{service.path}?{service.query}&ticket=ST-test", follow_redirects=False
            ).status_code
            == 303
        )
        body = {"title": "测试公告", "content": "第一行\n<script>alert(1)</script>"}
        assert admin.post(endpoint, json=body).status_code == 403
        assert (
            admin.post(
                endpoint, json={"title": " ", "content": "x"}, headers=csrf(admin)
            ).status_code
            == 422
        )
        assert (
            admin.post(
                endpoint, json={"title": "x", "content": "x" * 20001}, headers=csrf(admin)
            ).status_code
            == 422
        )
        published = admin.post(endpoint, json=body, headers=csrf(admin))
        assert published.status_code == 201, published.text
        first = published.json()["id"]
        second = admin.post(
            endpoint, json={"title": "第二条", "content": "内容"}, headers=csrf(admin)
        ).json()["id"]
        unread = visitor.get("/api/v1/announcements/unread")
        assert unread.headers["cache-control"] == "no-store"
        assert [a["id"] for a in unread.json()] == [first, second]
        assert unread.json()[0]["content"] == body["content"]
        assert "read_count" not in unread.json()[0]
        assert {a["id"]: a["read_count"] for a in admin.get(endpoint).json()} == {
            first: 0,
            second: 0,
        }
        ack = f"/api/v1/announcements/{first}/read"
        assert visitor.post(ack).status_code == 403
        assert visitor.post(ack, headers=csrf(visitor)).status_code == 204
        assert visitor.post(ack, headers=csrf(visitor)).status_code == 204
        assert [a["id"] for a in visitor.get("/api/v1/announcements/unread").json()] == [second]
        assert len(other.get("/api/v1/announcements/unread").json()) == 2
        assert {a["id"]: a["read_count"] for a in admin.get(endpoint).json()} == {
            first: 1,
            second: 0,
        }
        assert other.post(ack, headers=csrf(other)).status_code == 204
        assert {a["id"]: a["read_count"] for a in admin.get(endpoint).json()} == {
            first: 2,
            second: 0,
        }
        assert (
            visitor.post(f"{endpoint}/{second}/withdraw", headers=csrf(visitor)).status_code == 404
        )
        assert admin.post(f"{endpoint}/{second}/withdraw", headers=csrf(admin)).status_code == 204
        assert visitor.get("/api/v1/announcements/unread").json() == []
        assert len(other.get("/api/v1/announcements/unread").json()) == 0
        assert admin.get(endpoint).json()[0]["active"] is False
        assert (
            visitor.post("/api/v1/announcements/missing/read", headers=csrf(visitor)).status_code
            == 404
        )
        assert admin.post(f"{endpoint}/{first}/withdraw", headers=csrf(admin)).status_code == 204
        assert {a["id"]: a["read_count"] for a in admin.get(endpoint).json()} == {
            first: 2,
            second: 0,
        }
        with sqlite3.connect(path) as db:
            assert db.execute("select count(*) from announcement_reads").fetchone()[0] == 2
            assert (
                db.execute(
                    "select count(*) from security_audit_events "
                    "where event_type like 'admin.announcement.%'"
                ).fetchone()[0]
                == 4
            )
        visitor.close()
        other.close()
