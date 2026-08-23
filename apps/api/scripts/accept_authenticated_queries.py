"""Run selected questions through the complete authenticated Agent."""

from __future__ import annotations

import getpass
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from hzcu_agent.config import Settings
from hzcu_agent.main import create_app

MODEL = "gpt-5.6-luna"
UTILITY_MODEL = "gpt-5.6-terra"
QUESTIONS = (
    "暑假后什么时候开学。",
    "国创大概什么时候会中期检查。校创需不需要中期检查。",
)
ROOT = Path(__file__).resolve().parents[3]
WEB_ORIGIN = "http://testserver"


def _load_api_config() -> tuple[str | None, str]:
    config_path = next(
        (path for path in (ROOT / "APT.txt", ROOT / "API.txt") if path.is_file()),
        None,
    )
    if config_path is None:
        raise RuntimeError("未找到工作区根目录 APT.txt 或 API.txt")

    lines = [
        line.strip()
        for line in config_path.read_text(encoding="utf-8-sig").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    values: dict[str, str] = {}
    positional: list[str] = []
    for line in lines:
        if "=" in line and not line.lower().startswith(("http://", "https://")):
            key, value = line.split("=", 1)
            values[key.strip().lower()] = value.strip().strip("\"'")
        else:
            positional.append(line.strip().strip("\"'"))

    base_url = (
        values.get("openai_base_url")
        or values.get("base_url")
        or next(
            (item for item in positional if item.lower().startswith(("http://", "https://"))),
            None,
        )
    )
    api_key = values.get("openai_api_key") or values.get("api_key")
    if api_key is None:
        api_key = next(
            (item for item in positional if not item.lower().startswith(("http://", "https://"))),
            None,
        )
    if not api_key:
        raise RuntimeError(f"{config_path.name} 中未找到 API Key")
    return base_url, api_key


def _wait_for_answer(
    client: TestClient,
    conversation_id: str,
    question: str,
    csrf_token: str,
    *,
    timeout_seconds: float = 600,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    accepted = client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={"message": question},
        headers={"x-csrf-token": csrf_token},
    )
    accepted.raise_for_status()
    task_id = accepted.json()["task_id"]
    deadline = time.monotonic() + timeout_seconds
    last_status = ""
    while time.monotonic() < deadline:
        task = client.get(f"/api/v1/tasks/{task_id}")
        task.raise_for_status()
        payload = task.json()
        if payload["status"] != last_status:
            print(
                json.dumps(
                    {"task_id": task_id, "status": payload["status"]},
                    ensure_ascii=False,
                ),
                file=sys.stderr,
                flush=True,
            )
            last_status = payload["status"]
        if payload["status"] == "failed":
            raise RuntimeError(
                f"Agent 任务失败：{payload.get('error_code') or 'unknown'}"
            )
        if payload["status"] == "completed":
            answer = client.get(f"/api/v1/answers/{payload['answer_id']}")
            answer.raise_for_status()
            channel = client.app.state.broker._channels[task_id]
            events = [
                {"sequence": event.sequence, "event": event.event, "data": event.data}
                for event in channel.history
            ]
            return answer.json(), events
        time.sleep(1)
    raise TimeoutError(f"Agent 在 {timeout_seconds:.0f} 秒内未完成")


def _report(
    question: str,
    answer: dict[str, Any],
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "question": question,
        "headline": answer["headline"],
        "answer_markdown": answer["answer_markdown"],
        "confidence": answer["confidence"],
        "verification_mode": answer["verification_mode"],
        "claims": answer.get("claims", []),
        "grounding": answer.get("grounding"),
        "performance": answer.get("performance"),
        "evidence": [
            {
                "title": item["title"],
                "publisher": item["publisher"],
                "canonical_url": item["canonical_url"],
                "published_at": item["published_at"],
            }
            for item in answer["evidence"]
        ],
        "agent_trace": {
            "plan": [
                step
                for event in events
                if event["event"] == "plan.created"
                for step in event["data"].get("steps", [])
            ],
            "tool_results": [
                {
                    "tool": event["data"].get("tool"),
                    "status": event["data"].get("status"),
                    "evidence_count": event["data"].get("evidence_count"),
                    "data": event["data"].get("data", {}),
                    "warnings": event["data"].get("warnings", []),
                }
                for event in events
                if event["event"] == "tool.completed"
            ],
            "reviews": [
                event["data"]
                for event in events
                if event["event"] == "evidence.assessed"
            ],
        },
    }


def main() -> int:
    questions = tuple(value.strip() for value in sys.argv[1:] if value.strip()) or QUESTIONS
    sidecar_url = os.environ.get(
        "HZCU_ACCEPT_SIDECAR_URL",
        "http://127.0.0.1:8765",
    ).strip()
    sidecar_token = os.environ.get("HZCU_ACCEPT_SIDECAR_TOKEN", "").strip()
    if len(sidecar_token) < 32:
        raise RuntimeError("请通过 HZCU_ACCEPT_SIDECAR_TOKEN 提供本地侧车令牌")

    username = getpass.getpass("统一身份认证账号：")
    password = getpass.getpass("统一身份认证密码：")
    base_url, api_key = _load_api_config()
    source_database = ROOT / "data" / "hzcu_agent.db"
    if not source_database.is_file():
        raise RuntimeError(f"未找到校园记忆数据库：{source_database}")

    with tempfile.TemporaryDirectory(prefix="hzcu-auth-agent-") as temp_dir:
        temp_root = Path(temp_dir)
        acceptance_database = temp_root / "agent.db"
        shutil.copy2(source_database, acceptance_database)
        settings = Settings(
            environment="test",
            database_url=f"sqlite+aiosqlite:///{acceptance_database.as_posix()}",
            snapshot_directory=str(temp_root / "snapshots"),
            model_provider="openai",
            openai_api_key=api_key,
            openai_base_url=base_url,
            agent_model=MODEL,
            utility_model=UTILITY_MODEL,
            reasoning_effort="medium",
            model_timeout_seconds=180,
            max_tool_rounds=2,
            max_tool_calls=8,
            auth_mode="optional_cas",
            auth_session_secret="local-authenticated-agent-acceptance-secret",
            public_api_base_url=WEB_ORIGIN,
            web_app_url=WEB_ORIGIN,
            credential_vpn_enabled=True,
            campus_query_route="vpn_sidecar",
            vpn_sidecar_base_url=sidecar_url,
            vpn_sidecar_api_token=sidecar_token,
            vpn_sidecar_timeout_seconds=300,
            log_level="WARNING",
        )
        try:
            with TestClient(create_app(settings), base_url=WEB_ORIGIN) as client:
                challenge = client.get("/api/v1/auth/credential-challenge")
                challenge.raise_for_status()
                login = client.post(
                    "/api/v1/auth/credential-login",
                    headers={"origin": WEB_ORIGIN},
                    json={
                        "username": username,
                        "password": password,
                        "challenge": challenge.json()["challenge"],
                    },
                )
                if not login.is_success:
                    raise RuntimeError(
                        "校园查询会话建立失败："
                        f"HTTP {login.status_code} "
                        f"{json.dumps(login.json(), ensure_ascii=False)}"
                    )
                auth = login.json()
                if not auth["authenticated"] or auth["query_access"] != "vpn":
                    raise RuntimeError(f"校园查询会话未建立：{auth}")
                csrf_token = client.cookies.get(settings.auth_csrf_cookie_name)
                if not csrf_token:
                    raise RuntimeError("登录后未获得 CSRF 会话令牌")

                reports = []
                for index, question in enumerate(questions, start=1):
                    conversation = client.post(
                        "/api/v1/conversations",
                        json={"title": f"登录态真实 Agent 验收 {index}"},
                        headers={"x-csrf-token": csrf_token},
                    )
                    conversation.raise_for_status()
                    answer, events = _wait_for_answer(
                        client,
                        conversation.json()["conversation_id"],
                        question,
                        csrf_token,
                    )
                    reports.append(_report(question, answer, events))
        finally:
            password = ""
            username = ""

    print(
        json.dumps(
            {
                "model": MODEL,
                "utility_model": UTILITY_MODEL,
                "authenticated_query_access": "vpn",
                "questions": reports,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
