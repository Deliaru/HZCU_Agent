"""Audit the refactored web UI through Microsoft Edge CDP directly.

This script intentionally avoids Playwright. It checks desktop and mobile
layouts, opens the existing drawers and product panels, visits every route,
captures screenshots, and reports console or horizontal-overflow failures.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
from pathlib import Path
from typing import Any

from accept_stage6_edge import EdgeCdp, _target_websocket


def _write_screenshot(output: Path, encoded_data: str) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(base64.b64decode(encoded_data))


def _list_screenshots(output_dir: Path) -> list[str]:
    return [str(path) for path in sorted(output_dir.glob("*.png"))]


async def _capture(edge: EdgeCdp, output: Path) -> None:
    screenshot = await edge.command(
        "Page.captureScreenshot",
        {"format": "png", "captureBeyondViewport": False},
    )
    await asyncio.to_thread(
        _write_screenshot,
        output,
        screenshot["result"]["data"],
    )


async def _navigate(edge: EdgeCdp, url: str, ready: str) -> None:
    await edge.command("Page.navigate", {"url": url})
    await edge.wait_for(
        f"document.readyState === 'complete' && ({ready})",
        wait_seconds=40,
    )
    await asyncio.sleep(0.35)


async def _metrics(edge: EdgeCdp, selector: str) -> dict[str, Any]:
    return json.loads(
        await edge.evaluate(
            "JSON.stringify((() => {"
            f"const target = document.querySelector({json.dumps(selector)});"
            "const rect = target?.getBoundingClientRect();"
            "return {"
            "width: innerWidth, height: innerHeight,"
            "scrollWidth: document.documentElement.scrollWidth,"
            "clientWidth: document.documentElement.clientWidth,"
            "target: rect ? {left: rect.left, right: rect.right, top: rect.top, "
            "bottom: rect.bottom, width: rect.width, height: rect.height} : null,"
            "display: target ? getComputedStyle(target).display : null"
            "};"
            "})())"
        )
    )


async def _run(
    *,
    cdp_base_url: str,
    origin: str,
    target_origin: str | None,
    output_dir: Path,
    require_answer: bool,
) -> dict[str, Any]:
    websocket_url = _target_websocket(cdp_base_url, target_origin or origin)
    async with EdgeCdp(websocket_url) as edge:
        for domain in ("Runtime", "Log", "Page", "Network"):
            await edge.command(f"{domain}.enable")

        had_running_task = bool(await edge.evaluate("!!document.querySelector('.live-trace')"))
        if had_running_task:
            await edge.evaluate("document.querySelector('.cancel-task')?.click()")
            await asyncio.sleep(0.6)

        await edge.command(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": 1440,
                "height": 900,
                "deviceScaleFactor": 1,
                "mobile": False,
            },
        )
        await _navigate(
            edge,
            f"{origin}/?edge-refactor=desktop",
            "!!document.querySelector('textarea[aria-label=\"输入校园问题\"]')",
        )

        onboarding = bool(
            await edge.evaluate(
                "!![...document.querySelectorAll('button')].find("
                "button => (button.innerText || '').includes('先跳过'))"
            )
        )
        if onboarding:
            await edge.evaluate(
                "[...document.querySelectorAll('button')].find("
                "button => (button.innerText || '').includes('先跳过')).click()"
            )
            await asyncio.sleep(0.4)

        desktop_home = await _metrics(edge, ".workspace")
        answer_present = bool(
            await edge.evaluate("!!document.querySelector('.agent-message .markdown')")
        )
        answer_evidence_count = int(
            await edge.evaluate("document.querySelectorAll('.evidence-index button').length")
        )
        desktop_columns = await edge.evaluate(
            "getComputedStyle(document.querySelector('.workspace')).gridTemplateColumns"
        )
        desktop_rail = await edge.evaluate(
            "getComputedStyle(document.querySelector('.app-rail')).display"
        )
        desktop_composer = await _metrics(edge, ".composer")
        await _capture(edge, output_dir / "hzcu-refactor-desktop-1440x900.png")

        await _navigate(
            edge,
            f"{origin}/sources?edge-refactor=desktop",
            "document.querySelectorAll('.ledger-list > button').length > 0",
        )
        source_count = int(
            await edge.evaluate("document.querySelectorAll('.ledger-list > button').length")
        )
        try:
            await edge.wait_for(
                "document.querySelectorAll('.resource-row > button').length > 0",
                wait_seconds=20,
            )
        except TimeoutError:
            pass
        resource_count = int(
            await edge.evaluate("document.querySelectorAll('.resource-row > button').length")
        )
        version_workbench_open = False
        version_count = 0
        if resource_count:
            await edge.evaluate("document.querySelector('.resource-row > button').click()")
            try:
                await edge.wait_for(
                    "!!document.querySelector('.version-workbench')",
                    wait_seconds=12,
                )
                version_workbench_open = True
                try:
                    await edge.wait_for(
                        "document.querySelectorAll('.version-timeline > button').length > 0",
                        wait_seconds=12,
                    )
                except TimeoutError:
                    pass
                version_count = int(
                    await edge.evaluate(
                        "document.querySelectorAll('.version-timeline > button').length"
                    )
                )
            except TimeoutError:
                pass
        desktop_sources = await _metrics(edge, ".registry-workspace")
        await _capture(edge, output_dir / "hzcu-refactor-sources-desktop-1440x900.png")

        await _navigate(
            edge,
            f"{origin}/admin?edge-refactor=desktop",
            "!!document.querySelector('.admin-denied, .admin-content, .login-shell')",
        )
        admin_state = str(
            await edge.evaluate(
                "document.querySelector('.login-shell') ? 'login' : "
                "document.querySelector('.admin-denied') ? 'denied' : 'authorized'"
            )
        )
        desktop_admin = await _metrics(edge, ".admin-denied, .admin-content, .login-shell")
        await _capture(edge, output_dir / "hzcu-refactor-admin-desktop-1440x900.png")

        await edge.command(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": 390,
                "height": 844,
                "deviceScaleFactor": 1,
                "mobile": True,
            },
        )
        await _navigate(
            edge,
            f"{origin}/?edge-refactor=mobile",
            "!!document.querySelector('textarea[aria-label=\"输入校园问题\"]')",
        )
        mobile_home = await _metrics(edge, ".workspace")
        mobile_composer = await _metrics(edge, ".composer")
        mobile_rail_hidden = await edge.evaluate(
            "getComputedStyle(document.querySelector('.conversation-rail')).transform"
        )
        await edge.evaluate("document.querySelector('button[aria-label=\"打开会话历史\"]').click()")
        await asyncio.sleep(0.25)
        mobile_rail_open = str(
            await edge.evaluate("document.querySelector('.conversation-rail').className")
        )

        await edge.evaluate(
            "[...document.querySelectorAll('.conversation-rail button')].find("
            "button => (button.innerText || '').includes('我的空间'))?.click()"
        )
        space_open = False
        space_tabs: list[str] = []
        try:
            await edge.wait_for("!!document.querySelector('.space-panel')", wait_seconds=8)
            space_open = True
            space_tabs = json.loads(
                await edge.evaluate(
                    "JSON.stringify([...document.querySelectorAll("
                    "'.space-panel nav button')].map(button => button.innerText.trim()))"
                )
            )
            for label in ("待办", "数据", "画像"):
                await edge.evaluate(
                    "[...document.querySelectorAll('.space-panel nav button')].find("
                    f"button => (button.innerText || '').includes({json.dumps(label)}))?.click()"
                )
            await edge.evaluate(
                "document.querySelector('button[aria-label=\"关闭我的空间\"]')?.click()"
            )
        except TimeoutError:
            pass

        if "rail-open" not in mobile_rail_open:
            await edge.evaluate(
                "document.querySelector('button[aria-label=\"打开会话历史\"]')?.click()"
            )
        await edge.evaluate(
            "document.querySelector('button[aria-label=\"关闭会话历史\"]')?.click()"
        )
        await edge.evaluate("document.querySelector('.mobile-evidence-trigger')?.click()")
        await asyncio.sleep(0.2)
        evidence_open = "evidence-open" in str(
            await edge.evaluate("document.querySelector('.evidence-desk')?.className || ''")
        )
        await _capture(edge, output_dir / "hzcu-refactor-evidence-mobile-390x844.png")
        await edge.evaluate("document.querySelector('.desk-close')?.click()")
        await asyncio.sleep(0.5)

        credential_available = bool(
            await edge.evaluate("!!document.querySelector('.identity-vpn-trigger')")
        )
        credential_open = False
        if credential_available:
            await edge.evaluate("document.querySelector('.identity-vpn-trigger').click()")
            await asyncio.sleep(0.2)
            credential_open = bool(
                await edge.evaluate("!!document.querySelector('.credential-panel')")
            )
            await edge.evaluate(
                "document.querySelector('button[aria-label=\"关闭登录窗口\"]')?.click()"
            )

        await _capture(edge, output_dir / "hzcu-refactor-mobile-390x844.png")

        await _navigate(
            edge,
            f"{origin}/sources?edge-refactor=mobile",
            "document.querySelectorAll('.ledger-list > button').length > 0",
        )
        mobile_sources = await _metrics(edge, ".registry-workspace")
        await _capture(edge, output_dir / "hzcu-refactor-sources-mobile-390x844.png")

        await _navigate(
            edge,
            f"{origin}/admin?edge-refactor=mobile",
            "!!document.querySelector('.admin-denied, .admin-content, .login-shell')",
        )
        mobile_admin = await _metrics(edge, ".admin-denied, .admin-content, .login-shell")
        await _capture(edge, output_dir / "hzcu-refactor-admin-mobile-390x844.png")

        await edge.command(
            "Storage.clearDataForOrigin",
            {"origin": origin, "storageTypes": "all"},
        )
        await edge.command("Network.clearBrowserCookies")
        await edge.command(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": 1440,
                "height": 900,
                "deviceScaleFactor": 1,
                "mobile": False,
            },
        )
        await _navigate(
            edge,
            f"{origin}/?edge-refactor=fresh-desktop",
            "!!document.querySelector('textarea[aria-label=\"输入校园问题\"]')",
        )
        fresh_onboarding = bool(
            await edge.evaluate(
                "!![...document.querySelectorAll('button')].find("
                "button => (button.innerText || '').includes('先跳过'))"
            )
        )
        if fresh_onboarding:
            await edge.evaluate(
                "[...document.querySelectorAll('button')].find("
                "button => (button.innerText || '').includes('先跳过')).click()"
            )
            await asyncio.sleep(0.4)
        fresh_desktop = await _metrics(edge, ".hero-copy")
        await _capture(edge, output_dir / "hzcu-refactor-hero-desktop-1440x900.png")

        await edge.command(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": 390,
                "height": 844,
                "deviceScaleFactor": 1,
                "mobile": True,
            },
        )
        await _navigate(
            edge,
            f"{origin}/?edge-refactor=fresh-mobile",
            "!!document.querySelector('.hero-copy')",
        )
        fresh_mobile = await _metrics(edge, ".hero-copy")
        await _capture(edge, output_dir / "hzcu-refactor-hero-mobile-390x844.png")

        await edge.evaluate("true")
        errors = list(dict.fromkeys(edge.console_errors))
        warnings = list(dict.fromkeys(edge.console_warnings))

        checks = {
            "desktop_home_no_overflow": desktop_home["scrollWidth"]
            <= desktop_home["clientWidth"] + 1,
            "desktop_three_column_workspace": len(str(desktop_columns).split()) == 3,
            "desktop_global_rail_visible": desktop_rail != "none",
            "desktop_composer_in_view": desktop_composer["target"] is not None
            and desktop_composer["target"]["bottom"] <= 901,
            "completed_answer_restored": answer_present or not require_answer,
            "completed_answer_has_evidence": answer_evidence_count > 0 or not require_answer,
            "desktop_sources_no_overflow": desktop_sources["scrollWidth"]
            <= desktop_sources["clientWidth"] + 1,
            "source_registry_populated": source_count >= 40,
            "source_resources_populated": resource_count > 0,
            "version_workbench_open": version_workbench_open,
            "version_timeline_populated": version_count > 0,
            "admin_state_rendered": admin_state in {"login", "denied", "authorized"},
            "desktop_admin_no_overflow": desktop_admin["scrollWidth"]
            <= desktop_admin["clientWidth"] + 1,
            "mobile_home_no_overflow": mobile_home["scrollWidth"] <= mobile_home["clientWidth"] + 1,
            "mobile_composer_in_view": mobile_composer["target"] is not None
            and mobile_composer["target"]["bottom"] <= 845,
            "mobile_history_drawer": "rail-open" in mobile_rail_open,
            "mobile_evidence_drawer": evidence_open,
            "space_panel_and_tabs": space_open and len(space_tabs) == 3,
            "credential_state": not credential_available or credential_open,
            "mobile_sources_no_overflow": mobile_sources["scrollWidth"]
            <= mobile_sources["clientWidth"] + 1,
            "mobile_admin_no_overflow": mobile_admin["scrollWidth"]
            <= mobile_admin["clientWidth"] + 1,
            "fresh_onboarding_rendered": fresh_onboarding,
            "fresh_desktop_hero": fresh_desktop["target"] is not None,
            "fresh_mobile_hero_no_overflow": fresh_mobile["target"] is not None
            and fresh_mobile["scrollWidth"] <= fresh_mobile["clientWidth"] + 1,
            "console_clean": not errors and not warnings,
        }
        return {
            "passed": all(checks.values()),
            "browser": "Microsoft Edge --headless=new (direct CDP)",
            "had_running_task_and_canceled": had_running_task,
            "answer_required": require_answer,
            "checks": checks,
            "desktop": {
                "home": desktop_home,
                "columns": desktop_columns,
                "sources": desktop_sources,
                "admin": desktop_admin,
            },
            "mobile": {
                "home": mobile_home,
                "composer": mobile_composer,
                "rail_transform_before_open": mobile_rail_hidden,
                "sources": mobile_sources,
                "admin": mobile_admin,
            },
            "source_count": source_count,
            "resource_count": resource_count,
            "version_count": version_count,
            "version_workbench_open": version_workbench_open,
            "answer_present": answer_present,
            "answer_evidence_count": answer_evidence_count,
            "fresh_onboarding": fresh_onboarding,
            "admin_state": admin_state,
            "space_tabs": space_tabs,
            "console_errors": errors,
            "console_warnings": warnings,
            "screenshots": await asyncio.to_thread(_list_screenshots, output_dir),
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cdp-url", default="http://127.0.0.1:19345")
    parser.add_argument("--origin", default="http://127.0.0.1:13000")
    parser.add_argument(
        "--target-origin",
        help="Origin of an existing Edge page to navigate when origin is not open yet.",
    )
    parser.add_argument(
        "--allow-empty-answer",
        action="store_true",
        help="Skip answer restoration checks for a clean, new browser profile.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output/edge/frontend-refactor"),
    )
    args = parser.parse_args()
    report = asyncio.run(
        _run(
            cdp_base_url=args.cdp_url,
            origin=args.origin.rstrip("/"),
            target_origin=args.target_origin.rstrip("/") if args.target_origin else None,
            output_dir=args.output_dir.resolve(),
            require_answer=not args.allow_empty_answer,
        )
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
