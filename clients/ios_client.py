"""
iOS client for GlimpseUI.

Uses the GlimpseUIBridge XCTest runner (port 22087) for device interaction.
The cloud AI sees the screen and decides actions. This script executes them.

Setup:
  1. cd xctest-bridge && ./build_and_run.sh
  2. python ios_client.py --task "Open Settings and enable Dark Mode"

Requirements:
  pip install requests pillow
"""

import argparse
import base64
import subprocess
import time
import os
import sys
from io import BytesIO

import requests
import yaml
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agent.mobile_wait import wait_for_stable as _wait_stable

# ── Config ────────────────────────────────────────────────────────────────────

SERVER_URL   = os.getenv("GLIMPSEUI_URL", "http://localhost:8080")
BRIDGE_URL   = "http://localhost:22087"
LOOP_DELAY   = 0.5   # kept as fallback; smart wait used first

def _auth_headers() -> dict:
    key = os.getenv("GLIMPSEUI_API_KEY", "")
    return {"Authorization": f"Bearer {key}"} if key else {}

# iPhone 17 Pro logical points
VIEWPORT_W   = 393
VIEWPORT_H   = 852

# Bundle ID of the app under test — used for keyboard events.
# Override with --app flag. Common values:
#   com.apple.mobilesafari   (Safari)
#   com.apple.Preferences    (Settings)
#   com.apple.springboard    (home screen / unknown)
TYPE_BUNDLE  = "com.apple.mobilesafari"


# ── Bridge commands ───────────────────────────────────────────────────────────

def bridge(path: str, data: dict = {}) -> dict:
    try:
        r = requests.post(f"{BRIDGE_URL}{path}", json=data, timeout=30)
        return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}


def take_screenshot() -> str:
    """Get screenshot from XCTest bridge as base64 JPEG."""
    # Try bridge first (higher quality, exact screen content)
    result = bridge("/screenshot")
    if result.get("ok") and result.get("screenshot"):
        return result["screenshot"]

    # Fallback: simctl screenshot
    path = "/tmp/glimpseui_ios.png"
    subprocess.run(["xcrun", "simctl", "io", "booted", "screenshot", path],
                   check=True, capture_output=True)
    img = Image.open(path).convert("RGB")
    img = img.resize((VIEWPORT_W, VIEWPORT_H), Image.LANCZOS)
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=60)
    return base64.b64encode(buf.getvalue()).decode()


def check_bridge() -> bool:
    try:
        r = requests.get(f"{BRIDGE_URL}/health", timeout=3)
        return r.json().get("ok", False)
    except Exception:
        return False


# ── Hybrid: accessibility tree ────────────────────────────────────────────────

def get_element_tree() -> list:
    """Fetch accessibility tree from XCTest bridge."""
    result = bridge("/viewHierarchy")
    return result.get("elements", []) if result.get("ok") else []


def find_in_tree(prompt: str, elements: list) -> dict | None:
    """
    Fuzzy-find element in accessibility tree by natural language prompt.
    Priority: exact → contains → partial word match.
    """
    if not prompt or not elements:
        return None
    p = prompt.lower().strip()
    # 1. Exact match
    for el in elements:
        label = el.get("label", "").lower()
        ident = el.get("identifier", "").lower()
        if p == label or p == ident:
            return el
    # 2. Prompt contained in label
    for el in elements:
        label = el.get("label", "").lower()
        ident = el.get("identifier", "").lower()
        if p in label or p in ident:
            return el
    # 3. Label contained in prompt
    for el in elements:
        label = el.get("label", "").lower()
        ident = el.get("identifier", "").lower()
        if label and label in p:
            return el
        if ident and ident in p:
            return el
    return None


def tree_center(el: dict) -> tuple[int, int]:
    return int(el["x"] + el["w"] / 2), int(el["y"] + el["h"] / 2)


# ── Action executor ───────────────────────────────────────────────────────────

def execute(action: str, params: dict, located: dict | None, app_bundle: str = TYPE_BUNDLE) -> tuple[bool, str]:
    if action in ("Tap", "DoubleClick", "RightClick") and located:
        cx, cy = located["cx"], located["cy"]
        path = "/doubletap" if action == "DoubleClick" else "/tap"
        r = bridge(path, {"x": cx, "y": cy})
        return r.get("ok", False), r.get("error", "")

    elif action == "Type":
        text = params.get("text", "")
        r = bridge("/type", {"text": text, "bundleId": app_bundle})
        return r.get("ok", False), r.get("error", "")

    elif action == "KeyPress":
        key = params.get("key", "Return")
        r = bridge("/keypress", {"key": key, "bundleId": app_bundle})
        return r.get("ok", False), r.get("error", "")

    elif action == "Scroll":
        direction = params.get("direction", "down")
        cx = VIEWPORT_W // 2
        if direction == "down":
            y1, y2 = int(VIEWPORT_H * 0.7), int(VIEWPORT_H * 0.3)
        else:
            y1, y2 = int(VIEWPORT_H * 0.3), int(VIEWPORT_H * 0.7)
        r = bridge("/swipe", {"x1": cx, "y1": y1, "x2": cx, "y2": y2})
        return r.get("ok", False), r.get("error", "")

    elif action == "Navigate":
        url = params.get("url", "")
        subprocess.run(["xcrun", "simctl", "openurl", "booted", url], capture_output=True)
        return True, ""

    elif action == "Wait":
        ms = params.get("ms", 1000)
        time.sleep(ms / 1000)
        return True, ""

    return False, f"Unknown action: {action}"


# ── YAML save ─────────────────────────────────────────────────────────────────

def auto_assertion(screenshot_b64: str, task: str, server: str) -> str | None:
    """Ask the AI to generate one assertion for the completed task."""
    try:
        resp = requests.post(f"{server}/assert", json={
            "screenshot": screenshot_b64,
            "condition": f"The task '{task}' was completed successfully and the result is visible on screen",
        }, timeout=15)
        data = resp.json()
        if data.get("passed"):
            # Ask for a specific assertion description
            resp2 = requests.post(f"{server}/assert", json={
                "screenshot": screenshot_b64,
                "condition": "Describe in one short sentence what is currently visible on screen",
            }, timeout=15)
            reason = resp2.json().get("reason", "")
            return reason if reason else None
    except Exception:
        pass
    return None


def save_yaml(path: str, task: str, platform: str, app_bundle: str,
              final_screenshot: str | None, server: str):
    """Save the completed task as a reusable YAML test file."""
    steps = [{"task": task}]

    # Auto-generate an assertion from the final screen state
    if final_screenshot:
        assertion = auto_assertion(final_screenshot, task, server)
        if assertion:
            steps.append({"assert": assertion})

    spec = {
        "name": task[:60] + ("..." if len(task) > 60 else ""),
        "platform": platform,
        "app": app_bundle,
        "steps": steps,
    }

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        yaml.dump(spec, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    print(f"\n💾 Saved as: {path}")
    print(f"   Replay: python clients/yaml_runner.py {path}")


# ── Main loop ─────────────────────────────────────────────────────────────────

def run(task: str, server: str, app_bundle: str = TYPE_BUNDLE,
        save_path: str | None = None, platform: str = "ios"):
    print(f"\n🤖 Task: {task}")
    print(f"📡 Server: {server}")
    print(f"📱 Bridge: {BRIDGE_URL}")

    if not check_bridge():
        print("❌ XCTest bridge not running.")
        print("   Run: cd xctest-bridge && ./build_and_run.sh")
        sys.exit(1)

    print("✅ Bridge connected\n")

    session_id      = None
    last_action     = None
    last_success    = True
    last_error      = None
    step            = 0
    last_screenshot = None

    while True:
        step += 1
        print(f"── Step {step} ──────────────────────────")

        screenshot      = take_screenshot()
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
            "platform":     "ios",
        }

        try:
            resp = requests.post(f"{server}/next-action", json=payload,
                                 headers=_auth_headers(), timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"❌ API error: {e}")
            break

        session_id = data.get("session_id")
        action     = data.get("action")
        params     = data.get("params", {})
        located    = data.get("located")
        finished   = data.get("finished", False)

        # ── Hybrid: try accessibility tree before using AI vision coords ──────
        if action in ("Tap", "DoubleClick", "RightClick") and located:
            prompt = located.get("prompt", "")
            elements = get_element_tree()
            tree_el = find_in_tree(prompt, elements)
            if tree_el:
                cx, cy = tree_center(tree_el)
                located = {**located, "cx": cx, "cy": cy}
                print(f"🌳 Tree match: '{prompt}' → ({cx}, {cy})")
            else:
                print(f"👁  Vision coords: '{prompt}' → ({located['cx']}, {located['cy']})")

        print(f"🎯 {action}", end="")
        if located:
            print(f" → ({located['cx']}, {located['cy']})", end="")
        print()

        if finished:
            success = data.get("success", True)
            message = data.get("message", "")
            print(f"\n{'✅' if success else '❌'} {message}")
            bridge("/stop")
            if save_path and success:
                save_yaml(save_path, task, platform, app_bundle, last_screenshot, server)
            break

        ok, err = execute(action, params, located, app_bundle)
        last_action  = action
        last_success = ok
        last_error   = err if not ok else None

        if not ok:
            print(f"  ⚠️  {err}")

        # Wait for UI to settle rather than sleeping a fixed amount
        if not _wait_stable(bridge_url=BRIDGE_URL, timeout=3.0):
            time.sleep(LOOP_DELAY)   # fallback if bridge unreachable


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="GlimpseUI — iOS Client")
    parser.add_argument("--task",   required=True)
    parser.add_argument("--server", default=SERVER_URL)
    parser.add_argument("--app",    default=TYPE_BUNDLE,
                        help="Bundle ID of the app under test (for keyboard input)")
    parser.add_argument("--save",   default=None, metavar="PATH",
                        help="Save completed task as a reusable YAML test (e.g. tests/my_test.yaml)")
    args = parser.parse_args()
    run(args.task, args.server, args.app, save_path=args.save)
