"""
YAML test suite runner.

Suite format (flat steps — preferred):
  name: "My Suite"
  platform: web              # web | ios | android | desktop
  url: https://example.com   # base URL (web only)
  continue_on_failure: false # stop on first failure (default)
  reset_between_tests: false # reset browser between task groups (default)

  steps:
    - task: "Natural language task description"
    - assert: "Visual assertion checked by AI (costs 1 API call)"
    - check: url_contains "example"      # deterministic — no AI, no cost
    - check: url_equals "https://example.com/page"
    - check: page_contains "Welcome"
    - wait: 1500                         # milliseconds

Legacy format (still supported for backward compatibility):
  tests:
    - name: "Test name"
      task: "Natural language task"
      assert: "Visual assertion"         # optional
      url: "https://override.com"        # optional per-test URL

Notes:
  - Each `task:` in the flat steps format starts a new logical test group.
  - `assert:`, `check:`, and `wait:` following a task belong to that group.
  - `check:` steps are deterministic DOM/URL checks — use them instead of
    `assert:` whenever possible to save API cost and improve reliability.
  - iOS/Android suites must be run via the CLI:
      python clients/yaml_runner.py <test.yaml> --platform ios
"""

import asyncio
import hashlib
import json
import uuid
from typing import AsyncGenerator

import yaml

from .session_manager import get_session, take_screenshot_b64, viewport_size, reset_session
from .planner        import (
    call_ai, parse_response,
    build_first_turn, build_continuation_turn, build_retry_turn,
    check_assertion,
)
from .loop           import execute_parsed_action
from .cache          import load as _cache_load, save as _cache_save
from .executor       import run_web_script
from .compiler       import compile_web_task
from .healer         import heal_step

MAX_STEPS   = 20
MAX_REPEATS = 3


# ── Deterministic check ───────────────────────────────────────────────────────

async def run_check_step(condition: str, page) -> tuple[bool, str]:
    """
    Fast deterministic check — no AI call, no cost.

    Supported forms:
      url_contains <value>
      url_equals   <value>
      page_contains <value>
    """
    cond = condition.strip()

    if cond.lower().startswith("url_contains "):
        value = cond[len("url_contains "):].strip().strip("\"'")
        url   = await page.get_url()
        ok    = value in url
        return ok, f"URL {'contains' if ok else 'does not contain'} '{value}' (got: {url})"

    if cond.lower().startswith("url_equals "):
        value = cond[len("url_equals "):].strip().strip("\"'")
        url   = await page.get_url()
        ok    = url == value
        return ok, f"URL {'matches' if ok else 'does not match'} '{value}' (got: {url})"

    if cond.lower().startswith("page_contains "):
        value = cond[len("page_contains "):].strip().strip("\"'")
        try:
            text = await page.evaluate("document.body.innerText")
            ok   = value in text
            return ok, f"Page {'contains' if ok else 'does not contain'} '{value}'"
        except Exception as e:
            return False, f"Could not read page text: {e}"

    return False, (
        f"Unknown check: '{cond}'. "
        f"Supported: url_contains, url_equals, page_contains"
    )


# ── Action fingerprint (loop detection) ──────────────────────────────────────

def _action_fingerprint(parsed) -> str:
    """Hash (action_type + key params) to detect stuck loops."""
    key_params: dict = {}
    if parsed.action_type in ("Tap", "DoubleClick", "RightClick", "Hover") and parsed.located:
        key_params = {
            "x": round(parsed.located.center_x / 50) * 50,
            "y": round(parsed.located.center_y / 50) * 50,
        }
    elif parsed.action_type == "Scroll":
        key_params = {"direction": parsed.params.get("direction", "down")}
    elif parsed.action_type == "Navigate":
        key_params = {"url": parsed.params.get("url", "")}
    elif parsed.action_type == "Type":
        key_params = {"text": parsed.params.get("text", "")}
    key = f"{parsed.action_type}:{json.dumps(key_params, sort_keys=True)}"
    return hashlib.md5(key.encode()).hexdigest()


# ── Compile-once cached task runner ──────────────────────────────────────────

async def _run_task_cached(
    page,
    browser,
    task:      str,
    start_url: str = "",
) -> dict:
    """
    Run a task using the compile-once model:
      1. Check cache for a deterministic script → run with executor (no AI cost)
      2. Cache miss → compile with AI (one-time cost) → cache → run
      3. On executor failure → try healer → update cache

    Falls back to the full AI runtime loop only if compilation fails.
    """
    script    = _cache_load(task, start_url, "web")
    cache_hit = script is not None

    if script is None:
        # Compile: run AI loop once to record actions
        try:
            script = await compile_web_task(task, page, browser, start_url)
            if script:
                _cache_save(task, script, start_url, "web")
        except Exception:
            script = None

    if script:
        success, error, fail_idx = await run_web_script(page, script)

        # Self-heal on failure
        if not success and fail_idx >= 0:
            healed = await heal_step(script[fail_idx], platform="web", browser=browser)
            if healed:
                script[fail_idx] = healed
                _cache_save(task, script, start_url, "web")

        screenshot = await take_screenshot_b64(browser)
        return {
            "cache_hit":  cache_hit,
            "success":    success,
            "steps":      [{"action": s.get("action", ""), "thought": ""} for s in script],
            "message":    "" if success else error,
            "screenshot": screenshot,
        }

    # Fallback: full AI runtime (no cache)
    result = await run_task_in_session(page, browser, task, start_url)
    result["cache_hit"] = False
    return result


# ── Single task runner (AI runtime fallback) ──────────────────────────────────

async def run_task_in_session(page, browser, task: str, start_url: str = "") -> dict:
    """Run a single task using the shared browser session. Returns result dict."""
    vw, vh = viewport_size(browser)

    if start_url:
        await browser.navigate_to(start_url)

    screenshot   = await take_screenshot_b64(browser)
    conversation = [build_first_turn(task, screenshot)]

    fingerprint_counts: dict[str, int] = {}
    steps: list[dict] = []

    for step in range(1, MAX_STEPS + 1):
        response_text, assistant_turn = await call_ai(conversation)
        conversation.append(assistant_turn)

        parsed = parse_response(response_text, vw, vh)
        if not parsed:
            screenshot = await take_screenshot_b64(browser)
            return {"success": False, "steps": steps,
                    "message": "Could not parse AI response", "screenshot": screenshot}

        if parsed.action_type == "Finished":
            screenshot = await take_screenshot_b64(browser)
            return {
                "success":    parsed.params.get("success", True),
                "steps":      steps,
                "message":    parsed.params.get("message", "Done"),
                "screenshot": screenshot,
            }

        fp = _action_fingerprint(parsed)
        fingerprint_counts[fp] = fingerprint_counts.get(fp, 0) + 1
        if fingerprint_counts[fp] >= MAX_REPEATS:
            screenshot = await take_screenshot_b64(browser)
            return {
                "success": False, "steps": steps,
                "message": f"Stuck: '{parsed.action_type}' repeated {MAX_REPEATS}×",
                "screenshot": screenshot,
            }

        screenshot = await take_screenshot_b64(browser)
        steps.append({
            "step":    step,
            "action":  parsed.action_type,
            "thought": parsed.thought,
            "located": {
                "prompt": parsed.located.prompt,
                "bbox":   parsed.located.bbox,
                "cx":     parsed.located.center_x,
                "cy":     parsed.located.center_y,
            } if parsed.located else None,
            "screenshot": screenshot,
        })

        ok, error = await execute_parsed_action(page, parsed, vw, vh)
        await asyncio.sleep(0.5)

        screenshot = await take_screenshot_b64(browser)
        vw, vh     = viewport_size(browser)

        if error and error != "finished":
            conversation.append(build_retry_turn(step, screenshot, error))
        else:
            conversation.append(build_continuation_turn(step + 1, screenshot, parsed.action_type, ""))
        await asyncio.sleep(0.3)

    screenshot = await take_screenshot_b64(browser)
    last = steps[-1] if steps else {}
    return {
        "success": False, "steps": steps,
        "message": (
            f"Reached max steps ({MAX_STEPS}). "
            f"Last action: {last.get('action', 'unknown')}"
        ),
        "screenshot": screenshot,
    }


# ── YAML step splitter ────────────────────────────────────────────────────────

def _steps_to_tests(steps: list) -> list[dict]:
    """
    Convert a flat steps list into logical test groups.
    Each `task:` item starts a new group; following assert/check/wait
    items belong to that group.
    """
    tests: list[dict] = []
    current: dict | None = None

    for step in steps:
        if "task" in step:
            if current is not None:
                tests.append(current)
            current = {
                "name":     step.get("name", step["task"][:60]),
                "task":     step["task"],
                "url":      step.get("url", ""),
                "substeps": [],
            }
        elif current is not None:
            # assert / check / wait belong to the preceding task
            current["substeps"].append(step)

    if current is not None:
        tests.append(current)

    return tests


def _legacy_tests_to_groups(tests: list) -> list[dict]:
    """
    Convert the old `tests:` format (list of {name, task, assert, url}) into
    the internal test-group format used by the runner.
    """
    groups = []
    for t in tests:
        substeps = []
        if t.get("assert"):
            substeps.append({"assert": t["assert"]})
        groups.append({
            "name":     t.get("name", t.get("task", "")[:60]),
            "task":     t.get("task", ""),
            "url":      t.get("url", ""),
            "substeps": substeps,
        })
    return groups


# ── Suite runner ──────────────────────────────────────────────────────────────

async def run_suite(
    suite_yaml: str,
    suite_id: str = None,
) -> AsyncGenerator[dict, None]:
    sid = suite_id or str(uuid.uuid4())[:8]

    try:
        suite = yaml.safe_load(suite_yaml)
    except Exception as e:
        yield {"type": "error", "suite_id": sid, "message": f"Invalid YAML: {e}"}
        return

    name       = suite.get("name", "Unnamed Suite")
    platform   = suite.get("platform", "web")
    base_url   = suite.get("url", "about:blank")
    cont       = suite.get("continue_on_failure", False)
    reset_each = suite.get("reset_between_tests", False)

    # Support both schemas: flat `steps:` (new) and `tests:` (legacy)
    if "steps" in suite:
        tests = _steps_to_tests(suite["steps"])
    elif "tests" in suite:
        tests = _legacy_tests_to_groups(suite["tests"])
    else:
        yield {"type": "error", "suite_id": sid, "message": "No steps or tests defined"}
        return

    if not tests:
        yield {"type": "error", "suite_id": sid, "message": "No runnable tasks found"}
        return

    # Non-web platforms must use the CLI runner
    if platform != "web":
        yield {
            "type":     "error",
            "suite_id": sid,
            "message": (
                f"The web suite runner only supports platform: web. "
                f"For '{platform}', run via the CLI:\n"
                f"  python clients/yaml_runner.py <test.yaml> --platform {platform}"
            ),
        }
        return

    yield {
        "type": "suite_start", "suite_id": sid,
        "name": name, "platform": platform, "total": len(tests),
    }

    passed = failed = 0
    browser = await get_session()
    page    = await browser.get_current_page()

    for i, test in enumerate(tests):
        tname    = test["name"]
        task     = test["task"]
        substeps = test.get("substeps", [])
        url      = test.get("url") or (base_url if i == 0 else "")

        yield {
            "type": "test_start", "suite_id": sid,
            "index": i, "name": tname, "total": len(tests),
        }

        # Optional browser reset between tests
        if reset_each and i > 0:
            await reset_session()
            browser = await get_session()
            page    = await browser.get_current_page()

        # Run the task (compile-once cached path; falls back to AI runtime)
        result = await _run_task_cached(page, browser, task, url)

        # Run substeps (assert / check / wait) only if task succeeded
        if result["success"]:
            for sub in substeps:
                if not result["success"]:
                    break

                if "assert" in sub:
                    ss = result.get("screenshot") or await take_screenshot_b64(browser)
                    ok, reason = await check_assertion(ss, sub["assert"])
                    if not ok:
                        result["success"] = False
                        result["message"] = f"Assert failed: {reason}"

                elif "check" in sub:
                    ok, reason = await run_check_step(sub["check"], page)
                    if not ok:
                        result["success"] = False
                        result["message"] = f"Check failed: {reason}"

                elif "wait" in sub:
                    await asyncio.sleep(int(sub["wait"]) / 1000)

        status = "pass" if result["success"] else "fail"
        if result["success"]:
            passed += 1
        else:
            failed += 1

        yield {
            "type":       "test_done",
            "suite_id":   sid,
            "index":      i,
            "name":       tname,
            "status":     status,
            "message":    result.get("message", ""),
            "steps":      len(result.get("steps", [])),
            "screenshot": result.get("screenshot", ""),
            "steps_data": result.get("steps", []),
            "cache_hit":  result.get("cache_hit", False),
        }

        if not result["success"] and not cont:
            yield {
                "type": "suite_done", "suite_id": sid, "name": name,
                "passed": passed, "failed": failed,
                "total": len(tests), "aborted": True,
            }
            return

    yield {
        "type": "suite_done", "suite_id": sid, "name": name,
        "passed": passed, "failed": failed,
        "total": len(tests), "aborted": False,
    }
