"""Run the two Stage 5 regressions with a Campus-scoped local app session."""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from accept_stage4 import _load_api_config
from fastapi.testclient import TestClient

from hzcu_agent.auth.service import AuthService
from hzcu_agent.config import Settings
from hzcu_agent.db import Database
from hzcu_agent.main import create_app

QUESTIONS = (
    "这个学年暑假后什么时候开学",
    "国创大概什么时候会中期检查。校创需不需要中期检查。",
)
ROOT = Path(__file__).resolve().parents[3]


async def _create_campus_session(settings: Settings) -> tuple[str, str]:
    database = Database(settings)
    await database.initialize()
    auth = AuthService(settings=settings, database=database)
    try:
        established = await auth.establish_verified_subject(
            subject="stage5-local-mirror-acceptance",
            channel="acceptance_fixture",
        )
        return established.session_token, established.csrf_token
    finally:
        await auth.close()
        await database.close()


def _wait(
    client: TestClient,
    conversation_id: str,
    question: str,
    csrf_token: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    accepted = client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={"message": question},
        headers={"x-csrf-token": csrf_token},
    )
    accepted.raise_for_status()
    task_id = accepted.json()["task_id"]
    deadline = time.monotonic() + 420
    while time.monotonic() < deadline:
        task = client.get(f"/api/v1/tasks/{task_id}")
        task.raise_for_status()
        payload = task.json()
        if payload["status"] == "failed":
            raise RuntimeError(f"Agent task failed: {payload.get('error_code')}")
        if payload["status"] == "completed":
            answer = client.get(f"/api/v1/answers/{payload['answer_id']}")
            answer.raise_for_status()
            history = client.app.state.broker._channels[task_id].history
            return answer.json(), [
                {"event": item.event, "data": item.data} for item in history
            ]
        time.sleep(1)
    raise TimeoutError(f"Agent task {task_id} timed out")


def _report(
    question: str,
    answer: dict[str, Any],
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    tool_calls = [
        {
            "tool": event["data"]["tool"],
            "arguments": event["data"].get("arguments", {}),
        }
        for event in events
        if event["event"] == "tool.started"
    ]
    return {
        "question": question,
        "headline": answer["headline"],
        "answer_markdown": answer["answer_markdown"],
        "verification_mode": answer["verification_mode"],
        "evidence": [
            {
                "title": item["title"],
                "canonical_url": item["canonical_url"],
            }
            for item in answer["evidence"]
        ],
        "performance": answer["performance"],
        "tool_calls": tool_calls,
        "verifier_started": any(
            event["event"] == "answer.verification.started" for event in events
        ),
    }


def main() -> int:
    base_url, api_key = _load_api_config()
    source_database = ROOT / "data" / "hzcu_agent.db"
    questions = tuple(sys.argv[1:]) or QUESTIONS
    with tempfile.TemporaryDirectory(
        prefix="stage5-fts-agent-",
        dir=ROOT / "tmp",
    ) as temp_dir:
        database_path = Path(temp_dir) / "agent.db"
        shutil.copy2(source_database, database_path)
        settings = Settings(
            environment="test",
            database_url=f"sqlite+aiosqlite:///{database_path.as_posix()}",
            snapshot_directory=str(Path(temp_dir) / "snapshots"),
            model_provider="openai",
            openai_api_key=api_key,
            openai_base_url=base_url,
            agent_model="gpt-5.6-luna",
            utility_model="gpt-5.6-terra",
            reasoning_effort="medium",
            utility_reasoning_effort="low",
            model_timeout_seconds=180,
            max_tool_rounds=2,
            max_tool_calls=8,
            auth_mode="optional_cas",
            auth_session_secret="stage5-local-mirror-acceptance-secret",
            campus_query_route="disabled",
            log_level="WARNING",
        )
        session_token, csrf_token = asyncio.run(_create_campus_session(settings))
        with TestClient(create_app(settings)) as client:
            client.cookies.set(settings.auth_cookie_name, session_token)
            client.cookies.set(settings.auth_csrf_cookie_name, csrf_token)
            reports = []
            for question in questions:
                conversation = client.post(
                    "/api/v1/conversations",
                    json={"title": "Stage 5 FTS acceptance"},
                    headers={"x-csrf-token": csrf_token},
                )
                conversation.raise_for_status()
                answer, events = _wait(
                    client,
                    conversation.json()["conversation_id"],
                    question,
                    csrf_token,
                )
                reports.append(_report(question, answer, events))

    checks = {
        "all_have_evidence": all(item["evidence"] for item in reports),
        "local_tools_only": all(
            all(call["tool"] == "search_campus_memory" for call in item["tool_calls"])
            for item in reports
        ),
        "no_verifier": all(not item["verifier_started"] for item in reports),
        "model_calls_at_most_two": all(
            item["performance"]["model_call_count"] <= 2 for item in reports
        ),
        "tool_calls_at_most_three": all(
            item["performance"]["tool_call_count"] <= 3 for item in reports
        ),
    }
    if questions == QUESTIONS:
        calendar_answer = (
            reports[0]["answer_markdown"].replace(" ", "").replace("*", "")
        )
        innovation_answer = (
            reports[1]["answer_markdown"].replace(" ", "").replace("*", "")
        )
        calendar_urls = {
            evidence["canonical_url"] for evidence in reports[0]["evidence"]
        }
        innovation_urls = {
            evidence["canonical_url"] for evidence in reports[1]["evidence"]
        }
        checks.update(
            {
                "calendar_has_start_dates": all(
                    value in calendar_answer
                    for value in ("8月28日", "9月11日", "9月14日")
                ),
                "calendar_uses_expected_evidence": any(
                    "fdd5d1ce5c7f472a96d091803889b1af" in url
                    or "e67ec572d1914fa594868f9d1881e502" in url
                    for url in calendar_urls
                ),
                "innovation_has_local_midterm": (
                    "校创" in innovation_answer
                    and any(
                        statement in innovation_answer
                        for statement in (
                            "需要中期检查",
                            "须参加中期检查",
                            "有中期检查安排",
                        )
                    )
                ),
                "innovation_has_expected_evidence": (
                    any(
                        "NewsNo=100C" in url or "6917" in url
                        for url in innovation_urls
                    )
                    and any(
                        "245687" in url or "6926" in url
                        for url in innovation_urls
                    )
                ),
            }
        )
    print(
        json.dumps(
            {
                "passed": all(checks.values()),
                "checks": checks,
                "reports": reports,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
