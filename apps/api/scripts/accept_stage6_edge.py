"""Run the Stage 6 mobile journey through an existing headless Edge CDP port.

This intentionally uses Edge's DevTools Protocol directly. It has no
Playwright dependency and never reads browser credentials or deployment
secrets.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import time
from pathlib import Path
from typing import Any
from urllib.request import urlopen

import websockets


class EdgeCdp:
    def __init__(self, websocket_url: str) -> None:
        self.websocket_url = websocket_url
        self.sequence = 0
        self.socket: Any = None
        self.console_errors: list[str] = []
        self.console_warnings: list[str] = []

    async def __aenter__(self) -> EdgeCdp:
        self.socket = await websockets.connect(
            self.websocket_url,
            max_size=40_000_000,
        )
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.socket.close()

    async def command(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout_seconds: float = 40,
    ) -> dict[str, Any]:
        self.sequence += 1
        command_id = self.sequence
        await self.socket.send(
            json.dumps(
                {
                    "id": command_id,
                    "method": method,
                    "params": params or {},
                }
            )
        )
        while True:
            message = json.loads(
                await asyncio.wait_for(
                    self.socket.recv(),
                    timeout=timeout_seconds,
                )
            )
            if message.get("id") == command_id:
                return message
            self._record_event(message)

    async def evaluate(self, expression: str) -> Any:
        response = await self.command(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "userGesture": True,
            },
        )
        body = response.get("result", {})
        if body.get("exceptionDetails"):
            raise RuntimeError(body["exceptionDetails"])
        return body.get("result", {}).get("value")

    async def wait_for(
        self,
        expression: str,
        *,
        wait_seconds: float = 20,
    ) -> Any:
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            try:
                value = await self.evaluate(expression)
                if value:
                    return value
            except RuntimeError:
                pass
            await asyncio.sleep(0.25)
        raise TimeoutError(f"Edge condition timed out: {expression}")

    def _record_event(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        params = message.get("params", {})
        if method == "Runtime.consoleAPICalled":
            level = params.get("type")
            text = " ".join(
                str(item.get("value", item.get("description", "")))
                for item in params.get("args", [])
            )
        elif method == "Runtime.exceptionThrown":
            level = "error"
            text = params.get("exceptionDetails", {}).get("text", "exception")
        elif method == "Log.entryAdded":
            entry = params.get("entry", {})
            level = entry.get("level")
            text = entry.get("text", "")
        else:
            return
        if not text:
            return
        if level == "error":
            self.console_errors.append(text)
        elif level == "warning":
            self.console_warnings.append(text)


def _target_websocket(cdp_base_url: str, origin: str) -> str:
    with urlopen(f"{cdp_base_url.rstrip('/')}/json/list", timeout=5) as response:
        targets = json.load(response)
    target = next(
        (
            item
            for item in targets
            if item.get("type") == "page" and str(item.get("url", "")).startswith(origin)
        ),
        None,
    )
    if target is None:
        raise RuntimeError(f"Edge 中没有打开 {origin} 的页面")
    return str(target["webSocketDebuggerUrl"])


def _write_screenshot(output: Path, encoded_data: str) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(base64.b64decode(encoded_data))


async def _run(
    *,
    cdp_base_url: str,
    origin: str,
    output: Path,
    answer_timeout_seconds: float,
) -> dict[str, Any]:
    question = "商学院本科学生奖学金评定条件"
    websocket_url = _target_websocket(cdp_base_url, origin)
    async with EdgeCdp(websocket_url) as edge:
        for domain in ("Runtime", "Log", "Page", "Network"):
            await edge.command(f"{domain}.enable")
        await edge.command(
            "Storage.clearDataForOrigin",
            {"origin": origin, "storageTypes": "all"},
        )
        await edge.command("Network.clearBrowserCookies")
        await edge.command(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": 390,
                "height": 844,
                "deviceScaleFactor": 1,
                "mobile": True,
            },
        )
        await edge.command(
            "Page.navigate",
            {"url": f"{origin}/?edge-direct=final"},
        )
        await edge.wait_for(
            (
                "document.readyState === 'complete' && "
                "!!document.querySelector("
                "'textarea[aria-label=\"输入校园问题\"]')"
            ),
            wait_seconds=30,
        )

        onboarding_visible = bool(
            await edge.evaluate(
                "!![...document.querySelectorAll('button')].find("
                "button => (button.innerText || '').includes('先跳过'))"
            )
        )
        if onboarding_visible:
            await edge.evaluate(
                "[...document.querySelectorAll('button')].find("
                "button => (button.innerText || '').includes('先跳过')).click()"
            )
            await asyncio.sleep(0.4)

        encoded_question = json.dumps(question, ensure_ascii=False)
        await edge.evaluate(
            "(() => {"
            "const field = document.querySelector("
            "'textarea[aria-label=\"输入校园问题\"]');"
            "Object.getOwnPropertyDescriptor("
            "HTMLTextAreaElement.prototype, 'value'"
            f").set.call(field, {encoded_question});"
            "field.dispatchEvent(new InputEvent('input', {"
            "bubbles: true, inputType: 'insertText', "
            f"data: {encoded_question}"
            "}));"
            "return field.value;"
            "})()"
        )
        await edge.wait_for(
            "![...document.querySelectorAll('button')].find("
            "button => button.getAttribute('aria-label') === '发送').disabled",
            wait_seconds=5,
        )
        await edge.evaluate(
            "[...document.querySelectorAll('button')].find("
            "button => button.getAttribute('aria-label') === '发送').click()"
        )
        answer = await edge.wait_for(
            "document.querySelector('.agent-message .markdown')?.innerText || ''",
            wait_seconds=answer_timeout_seconds,
        )
        evidence_button = await edge.evaluate(
            "document.querySelector('.mobile-evidence-trigger')?.innerText || ''"
        )
        feedback_clicked = bool(
            await edge.evaluate(
                "!![...document.querySelectorAll('button')].find("
                "button => (button.innerText || '').includes('有帮助'))"
            )
        )
        if feedback_clicked:
            await edge.evaluate(
                "[...document.querySelectorAll('button')].find("
                "button => (button.innerText || '').includes('有帮助')).click()"
            )
            await asyncio.sleep(0.2)

        await edge.evaluate(
            "[...document.querySelectorAll('button')].find("
            "button => button.getAttribute('aria-label') === "
            "'打开会话历史').click()"
        )
        await asyncio.sleep(0.2)
        drawer_class = await edge.evaluate(
            "document.querySelector('.conversation-rail')?.className || ''"
        )
        history_completed = bool(
            await edge.evaluate(
                "document.querySelector('.thread-index')?.innerText.includes('completed')"
            )
        )
        await edge.evaluate(
            "[...document.querySelectorAll('button')].find("
            "button => button.getAttribute('aria-label') === "
            "'关闭会话历史')?.click()"
        )

        await edge.evaluate("document.querySelector('.mobile-evidence-trigger').click()")
        await asyncio.sleep(0.2)
        evidence_class = await edge.evaluate(
            "document.querySelector('.evidence-desk')?.className || ''"
        )
        await edge.evaluate("document.querySelector('.desk-close')?.click()")

        await edge.evaluate(
            "[...document.querySelectorAll('button')].find("
            "button => (button.innerText || '').includes('我的空间')).click()"
        )
        await edge.wait_for(
            "!![...document.querySelectorAll('button')].find("
            "button => /^待办 \\d+$/.test((button.innerText || '').trim()))",
            wait_seconds=8,
        )
        await edge.evaluate(
            "[...document.querySelectorAll('button')].find("
            "button => /^待办 \\d+$/.test((button.innerText || '').trim())).click()"
        )
        await edge.wait_for(
            "!!document.querySelector('input[placeholder=\"手动添加一项待办\"]')",
            wait_seconds=8,
        )
        todo_title = "检查新学期校历"
        encoded_todo = json.dumps(todo_title, ensure_ascii=False)
        await edge.evaluate(
            "(() => {"
            "const field = document.querySelector("
            "'input[placeholder=\"手动添加一项待办\"]');"
            "Object.getOwnPropertyDescriptor("
            "HTMLInputElement.prototype, 'value'"
            f").set.call(field, {encoded_todo});"
            "field.dispatchEvent(new InputEvent('input', {"
            "bubbles: true, inputType: 'insertText', "
            f"data: {encoded_todo}"
            "}));"
            "return field.value;"
            "})()"
        )
        await edge.wait_for(
            "![...document.querySelectorAll('button')].find("
            "button => (button.innerText || '').trim() === '添加').disabled",
            wait_seconds=5,
        )
        await edge.evaluate(
            "[...document.querySelectorAll('button')].find("
            "button => (button.innerText || '').trim() === '添加').click()"
        )
        await edge.wait_for(
            f"document.body.innerText.includes({encoded_todo})",
            wait_seconds=8,
        )
        await edge.evaluate(
            "[...document.querySelectorAll('button')].find("
            "button => button.getAttribute('aria-label') === '完成待办')?.click()"
        )
        await asyncio.sleep(0.3)
        todo_completed = bool(
            await edge.evaluate("!!document.querySelector('.space-todos article.done')")
        )
        delete_button = bool(
            await edge.evaluate(
                "!![...document.querySelectorAll('button')].find("
                "button => button.getAttribute('aria-label') === '删除待办')"
            )
        )
        if delete_button:
            await edge.evaluate(
                "[...document.querySelectorAll('button')].find("
                "button => button.getAttribute('aria-label') === '删除待办').click()"
            )
            await asyncio.sleep(0.2)
        todo_deleted = not bool(
            await edge.evaluate(f"document.body.innerText.includes({encoded_todo})")
        )

        await edge.command(
            "Page.navigate",
            {"url": f"{origin}/?edge-direct=restored"},
        )
        await edge.wait_for(
            (
                "document.readyState === 'complete' && "
                "!!document.querySelector("
                "'textarea[aria-label=\"输入校园问题\"]')"
            ),
            wait_seconds=30,
        )
        restored = bool(
            await edge.wait_for(
                "!!document.querySelector('.agent-message .markdown')",
                wait_seconds=20,
            )
        )
        viewport = json.loads(
            await edge.evaluate(
                "JSON.stringify({"
                "width: innerWidth, height: innerHeight, "
                "scrollWidth: document.documentElement.scrollWidth, "
                "clientWidth: document.documentElement.clientWidth"
                "})"
            )
        )
        screenshot = await edge.command(
            "Page.captureScreenshot",
            {"format": "png", "captureBeyondViewport": False},
        )
        await asyncio.to_thread(
            _write_screenshot,
            output,
            screenshot["result"]["data"],
        )

        await edge.command(
            "Page.navigate",
            {"url": f"{origin}/sources?edge-direct=final"},
        )
        await edge.wait_for(
            "document.readyState === 'complete'",
            wait_seconds=20,
        )
        source_count = int(
            await edge.wait_for(
                "document.querySelectorAll('.ledger-list > button').length",
                wait_seconds=30,
            )
        )
        source_viewport = json.loads(
            await edge.evaluate(
                "JSON.stringify({"
                "width: innerWidth, height: innerHeight, "
                "scrollWidth: document.documentElement.scrollWidth, "
                "clientWidth: document.documentElement.clientWidth"
                "})"
            )
        )
        # Flush any queued console events before taking the final counts.
        await edge.evaluate("true")

        errors = list(dict.fromkeys(edge.console_errors))
        warnings = list(dict.fromkeys(edge.console_warnings))
        checks = {
            "answer_present": bool(answer),
            "history_completed": history_completed,
            "feedback_clicked": feedback_clicked,
            "history_drawer_open": "rail-open" in drawer_class,
            "evidence_panel_open": "evidence-open" in evidence_class,
            "evidence_count_positive": evidence_button.strip() != "证据 0",
            "todo_completed": todo_completed,
            "todo_deleted": todo_deleted,
            "restored_after_navigation": restored,
            "home_no_horizontal_overflow": (viewport["scrollWidth"] <= viewport["clientWidth"] + 1),
            "source_count_at_least_40": source_count >= 40,
            "sources_no_horizontal_overflow": (
                source_viewport["scrollWidth"] <= source_viewport["clientWidth"] + 1
            ),
            "console_clean": not errors and not warnings,
        }
        return {
            "passed": all(checks.values()),
            "browser": "Microsoft Edge --headless=new (direct CDP)",
            "question": question,
            "onboarding_skipped": onboarding_visible,
            "checks": checks,
            "viewport": viewport,
            "source_viewport": source_viewport,
            "answer_excerpt": str(answer)[:160],
            "evidence_button": evidence_button,
            "source_count": source_count,
            "console_errors": errors,
            "console_warnings": warnings,
            "screenshot": str(output),
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--cdp-url",
        default="http://127.0.0.1:19222",
        help="Headless Edge remote-debugging HTTP endpoint.",
    )
    parser.add_argument(
        "--origin",
        default="http://127.0.0.1:13000",
        help="Origin already opened in Edge.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("output/playwright/hzcu-stage6-headless-edge-direct-390x844.png"),
    )
    parser.add_argument(
        "--answer-timeout",
        type=float,
        default=300,
        help="Seconds to wait for a real-model answer.",
    )
    args = parser.parse_args()
    report = asyncio.run(
        _run(
            cdp_base_url=args.cdp_url,
            origin=args.origin.rstrip("/"),
            output=args.output.resolve(),
            answer_timeout_seconds=args.answer_timeout,
        )
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
