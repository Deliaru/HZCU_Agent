"""Run the Stage 4 product acceptance through the real HTTP API surface."""

from __future__ import annotations

import json
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
QUESTIONS = (
    "我是工程学院智能制造专业的学生，我想参加竞赛，学院有什么竞赛推荐的吗？"
    "国创呢？另外，我想知道我们学校一般什么时候放寒假。",
    "一般什么时候可以开始申请奖学金，怎么申请？",
)
ROOT = Path(__file__).resolve().parents[3]


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
    *,
    timeout_seconds: float = 360,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    accepted = client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={"message": question},
    )
    accepted.raise_for_status()
    task_id = accepted.json()["task_id"]
    deadline = time.monotonic() + timeout_seconds
    last_status = ""
    while time.monotonic() < deadline:
        task = client.get(f"/api/v1/tasks/{task_id}")
        task.raise_for_status()
        task_payload = task.json()
        if task_payload["status"] != last_status:
            print(
                json.dumps(
                    {"task_id": task_id, "status": task_payload["status"]},
                    ensure_ascii=False,
                ),
                file=sys.stderr,
                flush=True,
            )
            last_status = task_payload["status"]
        if task_payload["status"] == "failed":
            raise RuntimeError(
                f"任务 {task_id} 失败：{task_payload.get('error_code') or 'unknown'}"
            )
        if task_payload["status"] == "completed":
            answer = client.get(f"/api/v1/answers/{task_payload['answer_id']}")
            answer.raise_for_status()
            channel = client.app.state.broker._channels[task_id]
            events = [
                {"sequence": event.sequence, "event": event.event, "data": event.data}
                for event in channel.history
            ]
            return answer.json(), events
        time.sleep(1)
    raise TimeoutError(f"任务 {task_id} 在 {timeout_seconds:.0f} 秒内未完成")


def _turn_report(
    index: int,
    question: str,
    answer: dict[str, Any],
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    tool_starts = [event for event in events if event["event"] == "tool.started"]
    tool_completions = [event for event in events if event["event"] == "tool.completed"]
    first_completion = min(
        (event["sequence"] for event in tool_completions),
        default=10**9,
    )
    parallel_start_count = sum(event["sequence"] < first_completion for event in tool_starts)
    return {
        "turn": index,
        "question": question,
        "headline": answer["headline"],
        "answer_markdown": answer["answer_markdown"],
        "confidence": answer["confidence"],
        "verification_mode": answer["verification_mode"],
        "evidence": [
            {
                "title": item["title"],
                "publisher": item["publisher"],
                "canonical_url": item["canonical_url"],
                "published_at": item["published_at"],
            }
            for item in answer["evidence"]
        ],
        "event_summary": {
            "semantic_signals": next(
                (
                    event["data"].get("signals", {})
                    for event in events
                    if event["event"] == "perception.completed"
                ),
                {},
            ),
            "planned_steps": [
                {
                    "tool": step["tool"],
                    "purpose": step["purpose"],
                    "arguments": step.get("arguments", {}),
                }
                for event in events
                if event["event"] == "plan.created"
                for step in event["data"]["steps"]
            ],
            "tool_calls": [event["data"]["tool"] for event in tool_starts],
            "parallel_start_count": parallel_start_count,
            "investigation_rounds": sum(
                event["event"] == "investigation.round.started" for event in events
            ),
            "reviews": [event["data"] for event in events if event["event"] == "evidence.assessed"],
        },
    }


def main() -> int:
    base_url, api_key = _load_api_config()
    source_database = ROOT / "data" / "hzcu_agent.db"
    if not source_database.is_file():
        raise RuntimeError(f"未找到校园记忆数据库：{source_database}")

    with tempfile.TemporaryDirectory(prefix="hzcu-stage4-") as temp_dir:
        temp_root = Path(temp_dir)
        acceptance_database = temp_root / "acceptance.db"
        shutil.copy2(source_database, acceptance_database)
        settings = Settings(
            environment="test",
            database_url=f"sqlite+aiosqlite:///{acceptance_database.as_posix()}",
            snapshot_directory=str(temp_root / "snapshots"),
            model_provider="openai",
            openai_api_key=api_key,
            openai_base_url=base_url,
            agent_model=MODEL,
            utility_model=MODEL,
            reasoning_effort="medium",
            model_timeout_seconds=180,
            max_tool_rounds=2,
            max_tool_calls=8,
            auth_mode="anonymous",
            log_level="WARNING",
        )
        with TestClient(create_app(settings)) as client:
            health = client.get("/api/v1/health")
            health.raise_for_status()
            conversation = client.post(
                "/api/v1/conversations",
                json={"title": "阶段4真实API验收"},
            )
            conversation.raise_for_status()
            conversation_id = conversation.json()["conversation_id"]

            turns = []
            for index, question in enumerate(QUESTIONS, start=1):
                answer, events = _wait_for_answer(client, conversation_id, question)
                turns.append(_turn_report(index, question, answer, events))

    first_answer = turns[0]["answer_markdown"]
    second_answer = turns[1]["answer_markdown"]
    first_evidence_titles = [item["title"] for item in turns[0]["evidence"]]
    second_evidence_titles = [item["title"] for item in turns[1]["evidence"]]
    checks = {
        "same_conversation": True,
        "both_answers_have_evidence": all(turn["evidence"] for turn in turns),
        "both_answers_have_citations": all("[来源" in turn["answer_markdown"] for turn in turns),
        "compound_question_covered": all(
            keyword in first_answer for keyword in ("竞赛", "国创", "寒假")
        ),
        "innovation_answer_is_substantive": "创新创业训练计划" in first_answer,
        "innovation_evidence_found": any(
            "创新创业训练计划" in title for title in first_evidence_titles
        ),
        "winter_timing_is_answered": any(
            marker in first_answer
            for marker in ("1月下旬", "1 月下旬", "一月下旬", "1月20", "1月21")
        ),
        "winter_evidence_found": any("寒假" in title for title in first_evidence_titles),
        "scholarship_question_covered": "奖学金" in second_answer,
        "scholarship_timing_is_answered": any(
            marker in second_answer for marker in ("9月", "10月", "九月", "十月")
        ),
        "scholarship_rules_evidence_found": any(
            "学生手册" in title or "奖学金" in title for title in second_evidence_titles
        ),
        "compound_question_used_multiple_tools": len(turns[0]["event_summary"]["tool_calls"]) >= 2,
        "time_sensitive_question_used_live_search": "search_official_live"
        in turns[0]["event_summary"]["tool_calls"],
        "parallel_investigation_observed": turns[0]["event_summary"]["parallel_start_count"] >= 2,
    }
    report = {
        "stage": 4,
        "model": MODEL,
        "api_config_file": "APT.txt" if (ROOT / "APT.txt").is_file() else "API.txt",
        "passed": all(checks.values()),
        "checks": checks,
        "turns": turns,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
