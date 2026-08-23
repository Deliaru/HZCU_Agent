"""Exercise the Stage 6 pilot isolation and concurrent-message gate.

This is intentionally an HTTP-level probe.  It creates independent visitor
cookie jars for the simulated devices, submits a bounded concurrent burst,
and checks that every task and conversation remains attributable to one
device.  Run it against an isolated pilot API database.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import threading
import time
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass
class Device:
    index: int
    client: httpx.Client
    csrf: str
    conversation_id: str


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:18001")
    parser.add_argument("--devices", type=int, default=50)
    parser.add_argument("--concurrent-messages", type=int, default=10)
    parser.add_argument("--timeout-seconds", type=float, default=45.0)
    return parser.parse_args()


def _request_json(
    response: httpx.Response,
    *,
    expected: set[int],
    context: str,
) -> Any:
    if response.status_code not in expected:
        raise RuntimeError(f"{context}: HTTP {response.status_code}: {response.text[:240]}")
    return response.json()


def _create_device(base_url: str, index: int) -> Device:
    client = httpx.Client(
        base_url=base_url.rstrip("/"),
        timeout=30.0,
        trust_env=False,
    )
    try:
        _request_json(
            client.get("/api/v1/auth/me"),
            expected={200},
            context=f"device {index} identity",
        )
        csrf = client.cookies.get("hzcu_csrf")
        if not csrf:
            raise RuntimeError(f"device {index}: API did not issue a CSRF cookie")
        conversation = _request_json(
            client.post(
                "/api/v1/conversations",
                json={"title": f"stage6-load-device-{index:02d}"},
                headers={"X-CSRF-Token": csrf},
            ),
            expected={201},
            context=f"device {index} conversation",
        )
        return Device(
            index=index,
            client=client,
            csrf=csrf,
            conversation_id=conversation["conversation_id"],
        )
    except Exception:
        client.close()
        raise


def _verify_isolation(devices: list[Device]) -> None:
    known_conversations = {device.conversation_id for device in devices}
    for device in devices:
        payload = _request_json(
            device.client.get("/api/v1/conversations"),
            expected={200},
            context=f"device {device.index} conversation list",
        )
        visible = {item["conversation_id"] for item in payload["items"]}
        if visible != {device.conversation_id}:
            raise RuntimeError(
                f"device {device.index}: isolation mismatch; "
                f"visible={sorted(visible)} expected={device.conversation_id}"
            )
        if visible & (known_conversations - {device.conversation_id}):
            raise RuntimeError(f"device {device.index}: cross-device conversation leak")


def _send_message(device: Device, barrier: threading.Barrier) -> str:
    barrier.wait()
    payload = {
        "message": f"stage6-load-device-{device.index:02d}-unique-question",
        "client_message_id": f"stage6-load-message-{device.index:02d}",
    }
    response = device.client.post(
        f"/api/v1/conversations/{device.conversation_id}/messages",
        json=payload,
        headers={"X-CSRF-Token": device.csrf},
    )
    accepted = _request_json(
        response,
        expected={202},
        context=f"device {device.index} message",
    )
    return accepted["task_id"]


def _wait_for_task(device: Device, task_id: str, timeout_seconds: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        task = _request_json(
            device.client.get(f"/api/v1/tasks/{task_id}"),
            expected={200},
            context=f"device {device.index} task",
        )
        if task["status"] in {"completed", "failed", "canceled"}:
            return task
        time.sleep(0.05)
    raise RuntimeError(f"device {device.index}: task {task_id} did not finish")


def run_probe(
    *,
    base_url: str,
    device_count: int,
    concurrent_messages: int,
    timeout_seconds: float,
) -> dict[str, Any]:
    if device_count < 1:
        raise ValueError("--devices must be positive")
    if not 1 <= concurrent_messages <= device_count:
        raise ValueError("--concurrent-messages must be between 1 and --devices")

    started = time.perf_counter()
    devices: list[Device] = []
    try:
        devices = [_create_device(base_url, index) for index in range(device_count)]
        _verify_isolation(devices)

        selected = devices[:concurrent_messages]
        barrier = threading.Barrier(concurrent_messages)
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=concurrent_messages,
            thread_name_prefix="stage6-load",
        ) as pool:
            futures = [pool.submit(_send_message, device, barrier) for device in selected]
            task_ids = [future.result() for future in futures]

        if len(set(task_ids)) != concurrent_messages:
            raise RuntimeError("concurrent burst returned duplicate task IDs")
        tasks = [
            _wait_for_task(device, task_id, timeout_seconds)
            for device, task_id in zip(selected, task_ids, strict=True)
        ]
        if any(task["status"] != "completed" or not task.get("answer_id") for task in tasks):
            raise RuntimeError(f"task completion gate failed: {tasks}")

        for device in selected:
            detail = _request_json(
                device.client.get(f"/api/v1/conversations/{device.conversation_id}"),
                expected={200},
                context=f"device {device.index} conversation detail",
            )
            messages = [item for item in detail["messages"] if item["role"] == "user"]
            expected_message = f"stage6-load-device-{device.index:02d}-unique-question"
            if [item["content"] for item in messages] != [expected_message]:
                raise RuntimeError(
                    f"device {device.index}: message attribution mismatch {messages}"
                )

        return {
            "status": "passed",
            "devices": device_count,
            "concurrent_messages": concurrent_messages,
            "unique_conversations": len({device.conversation_id for device in devices}),
            "unique_tasks": len(set(task_ids)),
            "task_statuses": [task["status"] for task in tasks],
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        }
    finally:
        for device in devices:
            device.client.close()


def main() -> int:
    args = _parse_args()
    result = run_probe(
        base_url=args.base_url,
        device_count=args.devices,
        concurrent_messages=args.concurrent_messages,
        timeout_seconds=args.timeout_seconds,
    )
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
