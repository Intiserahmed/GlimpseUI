"""
Slack / webhook notifications for test run results.

Sends a summary message to a Slack webhook (or any compatible endpoint)
after a test suite completes. Includes pass rate, duration, and failure list.

Config via environment variables:
    SLACK_WEBHOOK_URL   — Slack incoming webhook URL
    SLACK_CHANNEL       — override channel (optional)
    CI_JOB_URL          — link back to CI job (optional, auto-detected for GitHub Actions)

Usage:
    from agent.notify import notify_slack

    await notify_slack(
        suite_name="Nightly Mobile Tests",
        total=20, passed=18, failed=2,
        duration=142.3,
        failures=["Login flow", "Checkout step 3"],
    )
"""

import os
import json
import asyncio
from typing import Optional


def _job_url() -> str:
    """Auto-detect CI job URL from environment."""
    # GitHub Actions
    server = os.getenv("GITHUB_SERVER_URL", "")
    repo   = os.getenv("GITHUB_REPOSITORY", "")
    run_id = os.getenv("GITHUB_RUN_ID", "")
    if server and repo and run_id:
        return f"{server}/{repo}/actions/runs/{run_id}"

    # GitLab CI
    gl = os.getenv("CI_JOB_URL", "")
    if gl:
        return gl

    return os.getenv("CI_JOB_URL", "")


def _build_payload(
    suite_name: str,
    total:      int,
    passed:     int,
    failed:     int,
    duration:   float,
    failures:   list[str],
    channel:    Optional[str] = None,
) -> dict:
    pct       = int(passed / total * 100) if total else 0
    status    = "✅ All tests passed" if failed == 0 else f"❌ {failed} test(s) failed"
    color     = "#4caf50" if failed == 0 else "#f44336"
    job_url   = _job_url()
    dur_str   = f"{duration:.1f}s"

    failure_text = ""
    if failures:
        items = "\n".join(f"• {f}" for f in failures[:10])
        if len(failures) > 10:
            items += f"\n• … and {len(failures) - 10} more"
        failure_text = f"\n*Failed tests:*\n{items}"

    link_text = f"\n<{job_url}|View CI run>" if job_url else ""

    attachment = {
        "color": color,
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"{status} — {suite_name}"},
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Total:* {total}"},
                    {"type": "mrkdwn", "text": f"*Passed:* {passed} ({pct}%)"},
                    {"type": "mrkdwn", "text": f"*Failed:* {failed}"},
                    {"type": "mrkdwn", "text": f"*Duration:* {dur_str}"},
                ],
            },
        ],
    }

    if failure_text or link_text:
        attachment["blocks"].append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": (failure_text + link_text).strip()},
        })

    payload: dict = {"attachments": [attachment]}
    if channel:
        payload["channel"] = channel

    return payload


async def notify_slack(
    suite_name: str,
    total:      int,
    passed:     int,
    failed:     int,
    duration:   float,
    failures:   Optional[list[str]] = None,
    webhook_url: Optional[str] = None,
    channel:    Optional[str] = None,
    only_on_failure: bool = False,
) -> bool:
    """
    Post a Slack notification.
    Returns True on success, False on error (never raises).

    Args:
        webhook_url: Slack incoming webhook URL (falls back to SLACK_WEBHOOK_URL env var)
        only_on_failure: if True, skip notification when all tests pass
    """
    if only_on_failure and failed == 0:
        return True

    url = webhook_url or os.getenv("SLACK_WEBHOOK_URL", "")
    if not url:
        print("[notify] SLACK_WEBHOOK_URL not set — skipping notification")
        return False

    ch      = channel or os.getenv("SLACK_CHANNEL")
    payload = _build_payload(
        suite_name, total, passed, failed, duration,
        failures or [], ch,
    )

    try:
        import urllib.request
        data = json.dumps(payload).encode()
        req  = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, urllib.request.urlopen, req)
        return True
    except Exception as e:
        print(f"[notify] Slack notification failed: {e}")
        return False


def notify_slack_sync(
    suite_name: str,
    total:      int,
    passed:     int,
    failed:     int,
    duration:   float,
    failures:   Optional[list[str]] = None,
    webhook_url: Optional[str] = None,
    channel:    Optional[str] = None,
    only_on_failure: bool = False,
) -> bool:
    """Synchronous version for use in non-async contexts."""
    import urllib.request

    if only_on_failure and failed == 0:
        return True

    url = webhook_url or os.getenv("SLACK_WEBHOOK_URL", "")
    if not url:
        print("[notify] SLACK_WEBHOOK_URL not set — skipping notification")
        return False

    ch      = channel or os.getenv("SLACK_CHANNEL")
    payload = _build_payload(
        suite_name, total, passed, failed, duration,
        failures or [], ch,
    )

    try:
        data = json.dumps(payload).encode()
        req  = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req)
        return True
    except Exception as e:
        print(f"[notify] Slack notification failed: {e}")
        return False
