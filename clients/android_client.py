"""
GlimpseUI — Android Client

Uses uiautomator2 (HTTP to UIAutomator2 server on device) for all actions.
No ADB subprocess per tap — ~5x faster than adb shell input.

Setup:
  pip install uiautomator2 requests pillow
  python -m uiautomator2 init          ← installs server APK on device (once)
  adb devices                           ← confirm device is listed

Usage:
  python clients/android_client.py --task "Open Settings and enable Dark Mode"
  python clients/android_client.py --task "Search for shoes" --app com.amazon.mShop.android.shopping
  python clients/android_client.py --task "..." --save tests/my_test.yaml
"""

import argparse
import base64
import hashlib
import os
import sys
import time
import xml.etree.ElementTree as ET
from io import BytesIO

import requests
import uiautomator2 as u2
import yaml
from PIL import Image

# ── Config ────────────────────────────────────────────────────────────────────

SERVER_URL = os.getenv("GLIMPSEUI_URL", "http://localhost:8080")

def _auth_headers() -> dict:
    key = os.getenv("GLIMPSEUI_API_KEY", "")
    return {"Authorization": f"Bearer {key}"} if key else {}

# Logical viewport (Pixel 7 — override with --viewport if needed)
VIEWPORT_W = 412
VIEWPORT_H = 915

POLL_INTERVAL   = 0.15   # seconds between hierarchy polls
STABLE_REQUIRED = 2      # consecutive identical hashes = stable
STABLE_TIMEOUT  = 4.0    # max seconds to wait for stability


# ── Device connection ─────────────────────────────────────────────────────────

def connect(device_id: str | None = None) -> u2.Device:
    d = u2.connect(device_id) if device_id else u2.connect()
    # Verify connection
    try:
        info = d.info
        print(f"  Device: {info.get('productName', 'unknown')} "
              f"Android {info.get('sdkInt', '?')} "
              f"({info.get('displayWidth')}×{info.get('displayHeight')})")
    except Exception as e:
        print(f"  Device connection failed: {e}")
        print("  Make sure:")
        print("    1. adb devices lists your device")
        print("    2. python -m uiautomator2 init  (install server APK)")
        sys.exit(1)
    return d


# ── Screenshot ────────────────────────────────────────────────────────────────

def take_screenshot(d: u2.Device) -> str:
    """Screenshot via uiautomator2 HTTP, returns resized JPEG base64."""
    img = d.screenshot()
    img = img.convert("RGB").resize((VIEWPORT_W, VIEWPORT_H), Image.LANCZOS)
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=60)
    return base64.b64encode(buf.getvalue()).decode()


# ── Accessibility tree ────────────────────────────────────────────────────────

def get_element_tree(d: u2.Device) -> list:
    """
    Dump hierarchy via uiautomator2 HTTP.
    Returns list of {label, identifier, cx, cy, enabled}.
    """
    try:
        xml_str   = d.dump_hierarchy(pretty=False)
        info      = d.info
        screen_w  = info.get("displayWidth", 1080)
        screen_h  = info.get("displayHeight", 2400)
        root      = ET.fromstring(xml_str)
        elements  = []

        import re
        for node in root.iter("node"):
            text    = node.get("text", "").strip()
            cd      = node.get("content-desc", "").strip()
            res_id  = node.get("resource-id", "").split("/")[-1]
            bounds  = node.get("bounds", "")
            enabled = node.get("enabled", "false") == "true"

            if not (text or cd or res_id):
                continue

            m = re.findall(r"\d+", bounds)
            if len(m) < 4:
                continue

            x1, y1, x2, y2 = int(m[0]), int(m[1]), int(m[2]), int(m[3])
            if x2 <= x1 or y2 <= y1:
                continue

            cx = int((x1 + x2) / 2 * VIEWPORT_W / screen_w)
            cy = int((y1 + y2) / 2 * VIEWPORT_H / screen_h)
            elements.append({
                "label":      text or cd,
                "identifier": res_id,
                "cx": cx, "cy": cy,
                "enabled": enabled,
            })
        return elements
    except Exception:
        return []


def find_in_tree(prompt: str, elements: list) -> dict | None:
    """Fuzzy-find element by natural language prompt."""
    if not prompt or not elements:
        return None
    p = prompt.lower().strip()

    # Exact match
    for el in elements:
        if p in (el.get("label", "").lower(), el.get("identifier", "").lower()):
            return el
    # Prompt contained in label
    for el in elements:
        if p in el.get("label", "").lower() or p in el.get("identifier", "").lower():
            return el
    # Label contained in prompt (handles "Login button" → find "Login")
    for el in elements:
        lbl = el.get("label", "").lower()
        idn = el.get("identifier", "").lower()
        if (lbl and lbl in p) or (idn and idn in p):
            return el
    return None


# ── Smart wait ────────────────────────────────────────────────────────────────

def _hierarchy_hash(d: u2.Device) -> str:
    try:
        xml_str = d.dump_hierarchy(pretty=False)
        return hashlib.md5(xml_str.encode()).hexdigest()
    except Exception:
        return ""


def wait_for_stable(d: u2.Device, timeout: float = STABLE_TIMEOUT) -> bool:
    """
    Poll uiautomator2 hierarchy until it stops changing.
    Replaces time.sleep() after every tap/swipe.
    Returns True when stable, False on timeout (caller continues anyway).
    """
    prev_hash    = None
    stable_count = 0
    deadline     = time.time() + timeout

    while time.time() < deadline:
        h = _hierarchy_hash(d)
        if h == prev_hash:
            stable_count += 1
            if stable_count >= STABLE_REQUIRED:
                return True
        else:
            stable_count = 0
        prev_hash = h
        time.sleep(POLL_INTERVAL)

    return False


# ── Action executor ───────────────────────────────────────────────────────────

def execute(d: u2.Device, action: str, params: dict,
            located: dict | None) -> tuple[bool, str]:
    try:
        if action in ("Tap", "DoubleClick") and located:
            cx, cy = located["cx"], located["cy"]
            if action == "DoubleClick":
                d.double_click(cx, cy)
            else:
                d.click(cx, cy)
            return True, ""

        if action == "Type":
            text = params.get("text", "")
            d.send_keys(text)           # uiautomator2 handles unicode correctly
            return True, ""

        if action == "KeyPress":
            key_map = {
                "Return":    "enter",
                "Enter":     "enter",
                "Backspace": "del",
                "Escape":    "back",
                "Tab":       "tab",
                "ArrowUp":   "up",
                "ArrowDown": "down",
                "ArrowLeft": "left",
                "ArrowRight":"right",
            }
            key = key_map.get(params.get("key", "Return"), "enter")
            d.press(key)
            return True, ""

        if action == "Scroll":
            direction = params.get("direction", "down")
            cx = VIEWPORT_W // 2
            if direction == "down":
                d.swipe(cx, int(VIEWPORT_H * 0.7), cx, int(VIEWPORT_H * 0.3), duration=0.3)
            elif direction == "up":
                d.swipe(cx, int(VIEWPORT_H * 0.3), cx, int(VIEWPORT_H * 0.7), duration=0.3)
            elif direction == "left":
                d.swipe(int(VIEWPORT_W * 0.8), VIEWPORT_H // 2,
                        int(VIEWPORT_W * 0.2), VIEWPORT_H // 2, duration=0.3)
            elif direction == "right":
                d.swipe(int(VIEWPORT_W * 0.2), VIEWPORT_H // 2,
                        int(VIEWPORT_W * 0.8), VIEWPORT_H // 2, duration=0.3)
            return True, ""

        if action == "Navigate":
            url = params.get("url", "")
            # Use list args — no shell injection risk
            d.shell(["am", "start", "-a", "android.intent.action.VIEW", "-d", url])
            return True, ""

        if action == "Wait":
            time.sleep(params.get("ms", 1000) / 1000)
            return True, ""

        return False, f"Unknown action: {action}"

    except Exception as e:
        return False, str(e)


# ── YAML save ─────────────────────────────────────────────────────────────────

def _auto_assertion(screenshot_b64: str, task: str, server: str) -> str | None:
    try:
        resp = requests.post(f"{server}/assert", json={
            "screenshot": screenshot_b64,
            "condition":  f"The task '{task}' completed successfully and the result is visible",
        }, timeout=15)
        if resp.json().get("passed"):
            resp2 = requests.post(f"{server}/assert", json={
                "screenshot": screenshot_b64,
                "condition":  "Describe in one short sentence what is currently visible",
            }, timeout=15)
            return resp2.json().get("reason") or None
    except Exception:
        pass
    return None


def save_yaml(path: str, task: str, app_package: str,
              final_screenshot: str | None, server: str):
    steps = [{"task": task}]
    if final_screenshot:
        assertion = _auto_assertion(final_screenshot, task, server)
        if assertion:
            steps.append({"assert": assertion})

    spec = {
        "name":     task[:60] + ("..." if len(task) > 60 else ""),
        "platform": "android",
        "app":      app_package,
        "steps":    steps,
    }
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        yaml.dump(spec, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    print(f"\n  Saved: {path}")
    print(f"  Replay: python clients/yaml_runner.py {path} --platform android")


# ── Main loop ─────────────────────────────────────────────────────────────────

def run(task: str, server: str,
        device_id: str | None = None,
        app_package: str | None = None,
        save_path: str | None = None):

    print(f"\n  Task:   {task}")
    print(f"  Server: {server}")

    d = connect(device_id)

    # Launch specific app if requested
    if app_package:
        try:
            d.app_start(app_package)
            wait_for_stable(d, timeout=3.0)
            print(f"  Launched: {app_package}")
        except Exception as e:
            print(f"  Could not launch {app_package}: {e}")

    session_id    = None
    last_action   = None
    last_success  = True
    last_error    = None
    step          = 0
    last_screenshot = None

    while True:
        step += 1
        print(f"\n  Step {step} ".ljust(40, "─"))

        screenshot      = take_screenshot(d)
        last_screenshot = screenshot

        payload = {
            "task":         task,
            "screenshot":   screenshot,
            "session_id":   session_id,
            "last_action":  last_action,
            "last_success": last_success,
            "last_error":   last_error,
            "viewport_w":   VIEWPORT_W,
            "viewport_h":   VIEWPORT_H,
            "platform":     "android",
        }

        try:
            resp = requests.post(f"{server}/next-action", json=payload,
                                 headers=_auth_headers(), timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"  API error: {e}")
            break

        session_id = data.get("session_id")
        action     = data.get("action")
        params     = data.get("params", {})
        located    = data.get("located")
        finished   = data.get("finished", False)

        # Hybrid: try accessibility tree before using AI vision coords
        if action in ("Tap", "DoubleClick") and located:
            prompt   = located.get("prompt", "")
            elements = get_element_tree(d)
            tree_el  = find_in_tree(prompt, elements)
            if tree_el:
                located = {**located, "cx": tree_el["cx"], "cy": tree_el["cy"]}
                print(f"  Tree: '{prompt}' → ({tree_el['cx']}, {tree_el['cy']})")
            else:
                print(f"  Vision: '{prompt}' → ({located['cx']}, {located['cy']})")

        thought = data.get("thought", "")
        if thought:
            print(f"  Think: {thought[:80]}")
        print(f"  Act:   {action}", end="")
        if located:
            print(f" @ ({located['cx']}, {located['cy']})", end="")
        print()

        if finished:
            success = data.get("success", True)
            message = data.get("message", "")
            print(f"\n  {'✓ DONE' if success else '✗ FAILED'}: {message}")
            if save_path and success:
                save_yaml(save_path, task, app_package or "unknown", last_screenshot, server)
            break

        ok, err = execute(d, action, params, located)
        last_action  = action
        last_success = ok
        last_error   = err if not ok else None

        if not ok:
            print(f"  Warning: {err}")

        # Condition-based wait — replaces fixed sleep
        if not wait_for_stable(d, timeout=STABLE_TIMEOUT):
            time.sleep(0.3)   # fallback if uiautomator2 unreachable


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="GlimpseUI — Android Client")
    parser.add_argument("--task",   required=True, help="Natural language task")
    parser.add_argument("--server", default=SERVER_URL)
    parser.add_argument("--device", default=None,
                        help="ADB device serial (e.g. emulator-5554 or R58M123456)")
    parser.add_argument("--app",    default=None,
                        help="App package to launch before running task "
                             "(e.g. com.android.settings)")
    parser.add_argument("--save",   default=None, metavar="PATH",
                        help="Save completed task as YAML test (e.g. tests/my_test.yaml)")
    args = parser.parse_args()
    run(args.task, args.server,
        device_id=args.device,
        app_package=args.app,
        save_path=args.save)
