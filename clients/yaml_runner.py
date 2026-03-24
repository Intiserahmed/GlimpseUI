"""
GlimpseUI — YAML Test Runner (mobile/desktop CLI)

Runs structured test files against iOS (XCTest bridge) or Android (ADB).

Usage:
  python yaml_runner.py tests/login.yaml
  python yaml_runner.py tests/login.yaml --platform android
  python yaml_runner.py tests/login.yaml --server http://my-server.com

YAML format:
  name: "Login Flow"
  platform: ios            # ios | android (default: ios)
  app: com.apple.mobilesafari   # iOS bundle ID for keyboard events
  steps:
    - task: "Tap the Login button"
    - task: "Enter email test@example.com"
    - assert: "Dashboard is visible"     # AI visual assertion
    - check: page_contains "Welcome"     # falls back to AI assert on mobile
    - wait: 2000                         # milliseconds

Note: `check:` steps use deterministic DOM checks on web (server-side).
On iOS/Android they fall back to an AI visual assertion via /assert.
"""

import argparse
import base64
import subprocess
import sys
import time
import os
from io import BytesIO

import requests
import yaml
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agent.mobile_wait import wait_for_stable as _wait_stable

# ── Config ────────────────────────────────────────────────────────────────────

SERVER_URL  = os.getenv("GLIMPSEUI_URL", "http://localhost:8080")
BRIDGE_URL  = "http://localhost:22087"
LOOP_DELAY  = 0.5   # kept as fallback

def _auth_headers() -> dict:
    key = os.getenv("GLIMPSEUI_API_KEY", "")
    return {"Authorization": f"Bearer {key}"} if key else {}

# uiautomator2 device — connected lazily for Android runs
_u2_device = None

def _get_u2(device_serial: str | None = None):
    """Return a uiautomator2 device, connecting once and reusing."""
    global _u2_device
    if _u2_device is None:
        try:
            import uiautomator2 as u2
            _u2_device = u2.connect(device_serial) if device_serial else u2.connect()
        except Exception:
            pass
    return _u2_device
MAX_STEPS   = 20

# iPhone 17 Pro
IOS_W, IOS_H = 393, 852
# Pixel 7
AND_W, AND_H = 412, 915


# ── Screenshot ────────────────────────────────────────────────────────────────

def ios_screenshot() -> str:
    """Try XCTest bridge first, fall back to simctl."""
    try:
        r = requests.post(f"{BRIDGE_URL}/screenshot", timeout=5)
        ss = r.json().get("screenshot", "")
        if ss:
            return ss
    except Exception:
        pass
    path = "/tmp/ui_nav_ios.png"
    subprocess.run(["xcrun", "simctl", "io", "booted", "screenshot", path],
                   check=True, capture_output=True)
    img = Image.open(path).convert("RGB").resize((IOS_W, IOS_H), Image.LANCZOS)
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=60)
    return base64.b64encode(buf.getvalue()).decode()


def android_screenshot() -> str:
    subprocess.run(["adb", "shell", "screencap", "-p", "/sdcard/ui_nav.png"],
                   capture_output=True)
    subprocess.run(["adb", "pull", "/sdcard/ui_nav.png", "/tmp/ui_nav_and.png"],
                   capture_output=True)
    img = Image.open("/tmp/ui_nav_and.png").convert("RGB").resize((AND_W, AND_H), Image.LANCZOS)
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=60)
    return base64.b64encode(buf.getvalue()).decode()


def take_screenshot(platform: str) -> str:
    return ios_screenshot() if platform == "ios" else android_screenshot()


# ── Bridge / ADB execute ──────────────────────────────────────────────────────

def bridge_call(path: str, data: dict = {}) -> dict:
    try:
        r = requests.post(f"{BRIDGE_URL}{path}", json=data, timeout=15)
        return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}


def adb(*args) -> subprocess.CompletedProcess:
    return subprocess.run(["adb"] + list(args), capture_output=True, text=True)


def execute_action(action: str, params: dict, located: dict | None,
                   platform: str, app_bundle: str) -> tuple[bool, str]:
    if platform == "ios":
        if action in ("Tap", "DoubleClick", "RightClick") and located:
            path = "/doubletap" if action == "DoubleClick" else "/tap"
            r = bridge_call(path, {"x": located["cx"], "y": located["cy"]})
            return r.get("ok", False), r.get("error", "")
        elif action == "Type":
            r = bridge_call("/type", {"text": params.get("text", ""), "bundleId": app_bundle})
            return r.get("ok", False), r.get("error", "")
        elif action == "KeyPress":
            r = bridge_call("/keypress", {"key": params.get("key", "Return"), "bundleId": app_bundle})
            return r.get("ok", False), r.get("error", "")
        elif action == "Scroll":
            direction = params.get("direction", "down")
            cx = IOS_W // 2
            y1, y2 = (int(IOS_H * 0.7), int(IOS_H * 0.3)) if direction == "down" \
                else (int(IOS_H * 0.3), int(IOS_H * 0.7))
            r = bridge_call("/swipe", {"x1": cx, "y1": y1, "x2": cx, "y2": y2})
            return r.get("ok", False), r.get("error", "")
        elif action == "Navigate":
            subprocess.run(["xcrun", "simctl", "openurl", "booted", params.get("url", "")],
                           capture_output=True)
            return True, ""
        elif action == "Wait":
            time.sleep(params.get("ms", 1000) / 1000)
            return True, ""

    else:  # android — use uiautomator2 when available, fall back to ADB
        d = _get_u2()

        if action in ("Tap", "DoubleClick") and located:
            cx, cy = located["cx"], located["cy"]
            try:
                if d:
                    if action == "DoubleClick":
                        d.double_click(cx, cy)
                    else:
                        d.click(cx, cy)
                else:
                    if action == "DoubleClick":
                        adb("shell", "input", "tap", str(cx), str(cy))
                        time.sleep(0.1)
                    adb("shell", "input", "tap", str(cx), str(cy))
                return True, ""
            except Exception as e:
                return False, str(e)

        elif action == "Type":
            text = params.get("text", "")
            try:
                if d:
                    d.send_keys(text)   # handles unicode & spaces natively
                else:
                    # ADB fallback: escape spaces and special chars
                    safe = text.replace("\\", "\\\\").replace(" ", "%s") \
                               .replace("'", "\\'").replace("\"", "\\\"") \
                               .replace("&", "\\&").replace(";", "\\;") \
                               .replace("<", "\\<").replace(">", "\\>") \
                               .replace("(", "\\(").replace(")", "\\)")
                    adb("shell", "input", "text", safe)
                return True, ""
            except Exception as e:
                return False, str(e)

        elif action == "KeyPress":
            key_map_u2  = {"Return": "enter", "Enter": "enter", "Backspace": "del",
                            "Escape": "back",  "Tab": "tab"}
            key_map_adb = {"Return": "66", "Enter": "66", "Backspace": "67",
                           "Escape": "111", "Tab": "61",
                           "ArrowUp": "19", "ArrowDown": "20",
                           "ArrowLeft": "21", "ArrowRight": "22"}
            key = params.get("key", "Return")
            try:
                if d:
                    d.press(key_map_u2.get(key, "enter"))
                else:
                    adb("shell", "input", "keyevent", key_map_adb.get(key, "66"))
                return True, ""
            except Exception as e:
                return False, str(e)

        elif action == "Scroll":
            direction = params.get("direction", "down")
            cx = AND_W // 2
            if direction == "down":
                y1, y2 = int(AND_H * 0.7), int(AND_H * 0.3)
            else:
                y1, y2 = int(AND_H * 0.3), int(AND_H * 0.7)
            try:
                if d:
                    d.swipe(cx, y1, cx, y2, duration=0.3)
                else:
                    adb("shell", "input", "swipe", str(cx), str(y1), str(cx), str(y2), "300")
                return True, ""
            except Exception as e:
                return False, str(e)

        elif action == "Navigate":
            url = params.get("url", "")
            try:
                if d:
                    d.shell(["am", "start", "-a", "android.intent.action.VIEW", "-d", url])
                else:
                    adb("shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", url)
                return True, ""
            except Exception as e:
                return False, str(e)

        elif action == "Wait":
            time.sleep(params.get("ms", 1000) / 1000)
            return True, ""

    return False, f"Unknown action: {action}"


# ── Run a single task step ────────────────────────────────────────────────────

def run_task_step(task: str, platform: str, app_bundle: str, server: str,
                  viewport_w: int, viewport_h: int) -> tuple[bool, str]:
    """Run one task: step — loop until AI says Finished. Returns (success, message)."""
    session_id   = None
    last_action  = None
    last_success = True
    last_error   = None

    for step in range(1, MAX_STEPS + 1):
        screenshot = take_screenshot(platform)

        payload = {
            "task":         task,
            "screenshot":   screenshot,
            "session_id":   session_id,
            "last_action":  last_action,
            "last_success": last_success,
            "last_error":   last_error,
            "viewport_w":   viewport_w,
            "viewport_h":   viewport_h,
        }

        try:
            resp = requests.post(f"{server}/next-action", json=payload,
                                 headers=_auth_headers(), timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            return False, f"API error: {e}"

        session_id = data.get("session_id")
        action     = data.get("action")
        params     = data.get("params", {})
        located    = data.get("located")
        finished   = data.get("finished", False)

        print(f"    step {step}: {action}", end="")
        if located:
            print(f" → ({located['cx']}, {located['cy']})", end="")
        print()

        if finished:
            success = data.get("success", True)
            message = data.get("message", "")
            return success, message

        ok, err = execute_action(action, params, located, platform, app_bundle)
        last_action  = action
        last_success = ok
        last_error   = err if not ok else None

        if not ok:
            print(f"    ⚠  {err}")

        # Condition-based wait: settle before next screenshot
        if platform == "ios":
            if not _wait_stable(bridge_url=BRIDGE_URL, timeout=3.0):
                time.sleep(LOOP_DELAY)
        elif platform == "android":
            d = _get_u2()
            if d:
                from clients.android_client import wait_for_stable as _android_stable
                if not _android_stable(d, timeout=3.0):
                    time.sleep(LOOP_DELAY)
            else:
                time.sleep(LOOP_DELAY)
        else:
            time.sleep(LOOP_DELAY)

    return False, f"Task did not finish within {MAX_STEPS} steps"


# ── Run an assert step ────────────────────────────────────────────────────────

def run_assert_step(condition: str, platform: str, server: str) -> tuple[bool, str]:
    """Check a visual assertion against the current screen."""
    screenshot = take_screenshot(platform)
    try:
        resp = requests.post(f"{server}/assert",
                             json={"screenshot": screenshot, "condition": condition},
                             headers=_auth_headers(), timeout=20)
        resp.raise_for_status()
        data = resp.json()
        return data.get("passed", False), data.get("reason", "")
    except Exception as e:
        return False, f"Assert API error: {e}"


# ── Run a check step ──────────────────────────────────────────────────────────

def run_check_step(condition: str, platform: str, server: str) -> tuple[bool, str]:
    """
    Deterministic check where possible; falls back to AI visual assertion
    on mobile platforms where direct DOM access is unavailable.

    Supported (all platforms via fallback, web via server-side DOM):
      url_contains <value>
      url_equals   <value>
      page_contains <value>
    """
    cond = condition.strip()

    # On mobile we have no direct DOM access from the CLI runner.
    # Route all check: steps through the AI assert endpoint as a fallback.
    # The server-side suite_runner.py handles these deterministically for web.
    if platform in ("ios", "android", "desktop"):
        return run_assert_step(cond, platform, server)

    # For web: the server handles DOM checks if you use the suite runner.
    # The CLI web runner has no DOM access either, so also falls back.
    return run_assert_step(cond, platform, server)


# ── Main runner ───────────────────────────────────────────────────────────────

def run_yaml(yaml_path: str, platform_override: str | None,
             server: str, app_override: str | None):

    with open(yaml_path) as f:
        spec = yaml.safe_load(f)

    name     = spec.get("name", yaml_path)
    platform = platform_override or spec.get("platform", "ios")
    app      = app_override or spec.get("app", "com.apple.mobilesafari")
    steps    = spec.get("steps", [])
    vw       = IOS_W if platform == "ios" else AND_W
    vh       = IOS_H if platform == "ios" else AND_H

    print(f"\n{'─'*50}")
    print(f"  {name}")
    print(f"  platform: {platform}  |  steps: {len(steps)}")
    print(f"{'─'*50}\n")

    results = []

    for i, step in enumerate(steps, 1):
        if "task" in step:
            task = step["task"]
            print(f"[{i}/{len(steps)}] TASK: {task}")
            ok, msg = run_task_step(task, platform, app, server, vw, vh)
            status = "✅ PASS" if ok else "❌ FAIL"
            print(f"  {status}  {msg}\n")
            results.append({"type": "task", "description": task, "passed": ok, "reason": msg})

        elif "assert" in step:
            condition = step["assert"]
            print(f"[{i}/{len(steps)}] ASSERT: {condition}")
            ok, reason = run_assert_step(condition, platform, server)
            status = "✅ PASS" if ok else "❌ FAIL"
            print(f"  {status}  {reason}\n")
            results.append({"type": "assert", "description": condition, "passed": ok, "reason": reason})

        elif "check" in step:
            condition = step["check"]
            print(f"[{i}/{len(steps)}] CHECK: {condition}")
            ok, reason = run_check_step(condition, platform, server)
            status = "✅ PASS" if ok else "❌ FAIL"
            print(f"  {status}  {reason}\n")
            results.append({"type": "check", "description": condition, "passed": ok, "reason": reason})

        elif "wait" in step:
            ms = int(step["wait"])
            print(f"[{i}/{len(steps)}] WAIT: {ms}ms")
            time.sleep(ms / 1000)
            results.append({"type": "wait", "description": f"{ms}ms", "passed": True, "reason": ""})

    # ── Summary ───────────────────────────────────────────────────────────────
    passed = sum(1 for r in results if r["passed"])
    total  = len(results)
    failed = [r for r in results if not r["passed"]]

    print(f"{'─'*50}")
    print(f"  Results: {passed}/{total} passed")
    if failed:
        print("\n  Failed steps:")
        for r in failed:
            print(f"    ✗ [{r['type']}] {r['description']}")
            print(f"      {r['reason']}")
    print(f"{'─'*50}\n")

    return len(failed) == 0


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="GlimpseUI — YAML Test Runner")
    parser.add_argument("yaml_file",                    help="Path to test YAML file")
    parser.add_argument("--platform", choices=["ios", "android"], default=None)
    parser.add_argument("--server",   default=SERVER_URL)
    parser.add_argument("--app",      default=None,
                        help="Override bundle ID for keyboard events (iOS)")
    args = parser.parse_args()

    ok = run_yaml(args.yaml_file, args.platform, args.server, args.app)
    sys.exit(0 if ok else 1)
