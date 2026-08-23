"""Inspect the CA login and administrator model console through Edge CDP.

The local pilot may intentionally run without a registered CAS callback.  The
login page is exercised against the real local API.  For the administrator-only
visual state, this script mocks read-only browser responses before page load;
backend authorization and persistence are covered by API tests.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
from pathlib import Path

from accept_stage6_edge import EdgeCdp, _target_websocket


def _write_screenshot(path: Path, encoded_data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(base64.b64decode(encoded_data))


def _list_screenshots(output_dir: Path) -> list[str]:
    return [str(path) for path in sorted(output_dir.glob("*.png"))]


async def _capture(edge: EdgeCdp, path: Path) -> None:
    result = await edge.command(
        "Page.captureScreenshot",
        {"format": "png", "captureBeyondViewport": False},
    )
    await asyncio.to_thread(_write_screenshot, path, result["result"]["data"])


async def _navigate(edge: EdgeCdp, url: str, selector: str) -> None:
    await edge.command("Page.navigate", {"url": url})
    await edge.wait_for(
        f"document.readyState === 'complete' && !!document.querySelector({json.dumps(selector)})",
        wait_seconds=30,
    )
    await asyncio.sleep(0.3)


def _admin_fetch_mock() -> str:
    responses = {
        "/api/v1/auth/me": {
            "authenticated": True,
            "auth_mode": "optional_cas",
            "cas_enabled": True,
            "subject_hint": "••••1024",
            "visibility_scopes": ["campus", "public"],
            "mirror_visibility_scopes": ["campus", "public"],
            "login_url": None,
            "service_registration_required": False,
            "query_access": "direct",
            "query_access_expires_at": None,
            "credential_handoff_available": False,
            "read_only_capability": "campus_notice.read",
            "subject_kind": "campus",
            "role": "admin",
            "visitor_data_available": False,
        },
        "/api/v1/admin/model-config": {
            "protocol": "openai_responses",
            "base_url": "https://relay.example/v1",
            "agent_model": "gpt-5.6-sol",
            "utility_model": "gpt-5.6-terra",
            "reasoning_effort": "medium",
            "utility_reasoning_effort": "low",
            "timeout_seconds": 180,
            "api_key_configured": True,
            "api_key_hint": "••••7K2M",
            "source": "environment",
            "updated_at": None,
        },
        "/api/v1/admin/overview": {
            "task_count": 50,
            "completed_count": 49,
            "failed_count": 1,
            "success_rate": 0.98,
            "median_duration_ms": 3200,
            "p95_duration_ms": 7400,
            "feedback_count": 8,
            "source_alert_count": 0,
        },
        "/api/v1/admin/task-health": {"items": []},
        "/api/v1/admin/feedback": [],
        "/api/v1/sources/alerts": [],
    }
    encoded = json.dumps(responses, ensure_ascii=False)
    return f"""
      (() => {{
        const fixtures = {encoded};
        const originalFetch = window.fetch.bind(window);
        window.fetch = async (input, init) => {{
          const url = new URL(typeof input === 'string' ? input : input.url, location.href);
          const fixture = fixtures[url.pathname];
          if (fixture !== undefined) {{
            let body = fixture;
            if (url.pathname.endsWith('/admin/model-config') &&
                (init?.method || 'GET').toUpperCase() === 'PUT') {{
              const submitted = JSON.parse(init?.body || '{{}}');
              body = {{...fixture, ...submitted, api_key: undefined,
                api_key_configured: true, api_key_hint: '••••7K2M', source: 'database',
                updated_at: new Date().toISOString()}};
            }}
            return new Response(JSON.stringify(body), {{
              status: 200,
              headers: {{'Content-Type': 'application/json'}},
            }});
          }}
          return originalFetch(input, init);
        }};
      }})();
    """


async def _run(cdp_url: str, origin: str, output_dir: Path) -> dict:
    websocket_url = _target_websocket(cdp_url, origin)
    async with EdgeCdp(websocket_url) as edge:
        for domain in ("Runtime", "Log", "Page"):
            await edge.command(f"{domain}.enable")

        await edge.command(
            "Emulation.setDeviceMetricsOverride",
            {"width": 1440, "height": 900, "deviceScaleFactor": 1, "mobile": False},
        )
        await _navigate(edge, f"{origin}/login?edge=ca", ".login-shell")
        await _capture(edge, output_dir / "hzcu-ca-login-desktop-1440x900.png")
        login_desktop_overflow = await edge.evaluate(
            "document.documentElement.scrollWidth > document.documentElement.clientWidth + 1"
        )

        await edge.command(
            "Emulation.setDeviceMetricsOverride",
            {"width": 390, "height": 844, "deviceScaleFactor": 1, "mobile": True},
        )
        await _navigate(edge, f"{origin}/login?edge=ca-mobile", ".login-shell")
        await _capture(edge, output_dir / "hzcu-ca-login-mobile-390x844.png")
        login_mobile_overflow = await edge.evaluate(
            "document.documentElement.scrollWidth > document.documentElement.clientWidth + 1"
        )

        await edge.command(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": _admin_fetch_mock()},
        )
        await edge.command(
            "Emulation.setDeviceMetricsOverride",
            {"width": 1440, "height": 900, "deviceScaleFactor": 1, "mobile": False},
        )
        await _navigate(edge, f"{origin}/admin?edge=admin", ".model-config-workspace")
        await _capture(edge, output_dir / "hzcu-admin-model-desktop-1440x900.png")
        admin_desktop_overflow = await edge.evaluate(
            "document.documentElement.scrollWidth > document.documentElement.clientWidth + 1"
        )
        key_value = await edge.evaluate("document.querySelector('#model-api-key').value")

        await edge.command(
            "Emulation.setDeviceMetricsOverride",
            {"width": 390, "height": 844, "deviceScaleFactor": 1, "mobile": True},
        )
        await _navigate(edge, f"{origin}/admin?edge=admin-mobile", ".model-config-workspace")
        await edge.evaluate("document.querySelector('.model-config-form').scrollIntoView()")
        await asyncio.sleep(0.2)
        await _capture(edge, output_dir / "hzcu-admin-model-mobile-390x844.png")
        admin_mobile_overflow = await edge.evaluate(
            "document.documentElement.scrollWidth > document.documentElement.clientWidth + 1"
        )

        checks = {
            "login_desktop_no_overflow": not login_desktop_overflow,
            "login_mobile_no_overflow": not login_mobile_overflow,
            "admin_desktop_no_overflow": not admin_desktop_overflow,
            "admin_mobile_no_overflow": not admin_mobile_overflow,
            "saved_key_not_rendered": key_value == "",
            "console_clean": not edge.console_errors and not edge.console_warnings,
        }
        return {
            "passed": all(checks.values()),
            "browser": "Microsoft Edge --headless=new (direct CDP)",
            "checks": checks,
            "console_errors": edge.console_errors,
            "console_warnings": edge.console_warnings,
            "screenshots": await asyncio.to_thread(_list_screenshots, output_dir),
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cdp-url", default="http://127.0.0.1:19347")
    parser.add_argument("--origin", default="http://127.0.0.1:13000")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output/edge/admin-console"),
    )
    args = parser.parse_args()
    report = asyncio.run(_run(args.cdp_url, args.origin.rstrip("/"), args.output_dir.resolve()))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
