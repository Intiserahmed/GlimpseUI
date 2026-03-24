"""
Deterministic script executor.

Runs a cached action script against a browser (web) or mobile device
(iOS/Android) with NO AI calls. Pure DOM/accessibility-tree execution.

This is what makes tests fast and free after the first AI-compiled run.

Web step format:
  {"action": "navigate", "url": "https://..."}
  {"action": "click",    "selector": "#submit", "label": "Submit"}
  {"action": "fill",     "selector": "input#email", "text": "..."}
  {"action": "press",    "key": "Enter"}
  {"action": "wait",     "ms": 1000}
  {"action": "scroll",   "direction": "down"}
  {"action": "wait_stable"}

Mobile step format:
  {"action": "tap",        "accessibilityId": "login-btn",
                           "label": "Sign In", "coords": [196, 480]}
  {"action": "type",       "text": "admin@example.com"}
  {"action": "keypress",   "key": "Return"}
  {"action": "scroll",     "direction": "down"}
  {"action": "wait",       "ms": 1000}
  {"action": "wait_stable"}
  {"action": "navigate",   "url": "https://..."}   # web-in-app / deeplink
"""

import asyncio
import time
from typing import Optional

import requests


# ── Web executor (Playwright via browser_use) ─────────────────────────────────

async def run_web_script(
    page,
    script: list[dict],
) -> tuple[bool, str, int]:
    """
    Execute a deterministic web script.
    Returns (success, error_message, failed_step_index).
    -1 means all steps passed.
    """
    for i, step in enumerate(script):
        action = step.get("action", "")
        try:
            if action == "navigate":
                await page.goto(step["url"])
                await asyncio.sleep(1.0)

            elif action == "click":
                selector = step.get("selector", "")
                try:
                    await page.click(selector, timeout=5000)
                except Exception:
                    # Fallback: find by text label
                    if label := step.get("label"):
                        await page.click(f"text={label}", timeout=5000)
                    else:
                        raise

            elif action == "fill":
                selector = step.get("selector", "")
                await page.fill(selector, step.get("text", ""))

            elif action == "press":
                await page.keyboard.press(step["key"])

            elif action == "wait":
                await asyncio.sleep(step.get("ms", 500) / 1000)

            elif action == "wait_stable":
                # Wait for network idle as a proxy for "stable"
                try:
                    await page.wait_for_load_state("networkidle", timeout=3000)
                except Exception:
                    pass  # continue if networkidle times out

            elif action == "scroll":
                direction = step.get("direction", "down")
                delta     = 300 if direction == "down" else -300
                await page.evaluate(f"window.scrollBy(0, {delta})")

            elif action == "hover":
                await page.hover(step.get("selector", "body"))

        except Exception as e:
            return False, f"Step {i} ({action}): {e}", i

    return True, "", -1


# ── Mobile executor (iOS via XCTest bridge) ───────────────────────────────────

def run_ios_script(
    script:     list[dict],
    bridge_url: str = "http://localhost:22087",
    app_bundle: str = "com.apple.mobilesafari",
) -> tuple[bool, str, int]:
    """
    Execute a deterministic iOS script using XCTest bridge + mobile_element.
    Returns (success, error_message, failed_step_index).
    """
    from .mobile_element import resolve_element
    from .mobile_wait   import wait_for_stable, wait_for_element
    from .retry         import sync_retry

    def bridge(path: str, data: dict = {}) -> dict:
        try:
            r = requests.post(f"{bridge_url}{path}", json=data, timeout=15)
            return r.json()
        except Exception as e:
            return {"ok": False, "error": str(e)}

    for i, step in enumerate(script):
        action = step.get("action", "")

        if action == "tap":
            el = resolve_element(step, platform="ios", bridge_url=bridge_url)
            if el is None:
                return False, f"Step {i}: element not found — {step}", i

            ok, _, err = sync_retry(
                lambda: bridge("/tap", {"x": el.cx, "y": el.cy}),
                max_attempts=3,
                label=f"tap '{el.label}'",
            )
            if not ok:
                return False, f"Step {i} (tap '{el.label}'): {err}", i

            wait_for_stable(bridge_url=bridge_url)

        elif action == "doubletap":
            el = resolve_element(step, platform="ios", bridge_url=bridge_url)
            if el is None:
                return False, f"Step {i}: element not found — {step}", i
            bridge("/doubletap", {"x": el.cx, "y": el.cy})
            wait_for_stable(bridge_url=bridge_url)

        elif action == "type":
            text = step.get("text", "")
            ok, _, err = sync_retry(
                lambda: bridge("/type", {"text": text, "bundleId": app_bundle}),
                max_attempts=2,
                label="type",
            )
            if not ok:
                return False, f"Step {i} (type): {err}", i

        elif action == "keypress":
            key = step.get("key", "Return")
            bridge("/keypress", {"key": key, "bundleId": app_bundle})
            wait_for_stable(bridge_url=bridge_url)

        elif action == "scroll":
            import subprocess
            direction = step.get("direction", "down")
            vw, vh    = step.get("viewport_w", 393), step.get("viewport_h", 852)
            cx        = vw // 2
            y1, y2    = (int(vh * 0.7), int(vh * 0.3)) if direction == "down" \
                        else (int(vh * 0.3), int(vh * 0.7))
            bridge("/swipe", {"x1": cx, "y1": y1, "x2": cx, "y2": y2})
            wait_for_stable(bridge_url=bridge_url)

        elif action == "navigate":
            import subprocess
            url = step.get("url", "")
            subprocess.run(["xcrun", "simctl", "openurl", "booted", url],
                           capture_output=True)
            wait_for_stable(timeout=3.0, bridge_url=bridge_url)

        elif action == "wait":
            time.sleep(step.get("ms", 500) / 1000)

        elif action == "wait_stable":
            wait_for_stable(bridge_url=bridge_url)

        elif action == "wait_for":
            label = step.get("label", "")
            el    = wait_for_element(label, timeout=step.get("timeout", 5.0),
                                     bridge_url=bridge_url)
            if el is None:
                return False, f"Step {i}: '{label}' never appeared", i

    return True, "", -1


# ── Android executor (ADB + uiautomator2) ────────────────────────────────────

def run_android_script(
    script:        list[dict],
    device_serial: str = None,
) -> tuple[bool, str, int]:
    """
    Execute a deterministic Android script using uiautomator2.
    Returns (success, error_message, failed_step_index).
    """
    from .mobile_element import resolve_element

    try:
        import uiautomator2 as u2
        d = u2.connect(device_serial)
    except ImportError:
        return False, "uiautomator2 not installed: pip install uiautomator2", -1
    except Exception as e:
        return False, f"Cannot connect to Android device: {e}", -1

    for i, step in enumerate(script):
        action = step.get("action", "")

        try:
            if action == "tap":
                el = resolve_element(step, platform="android",
                                     device_serial=device_serial)
                if el is None:
                    return False, f"Step {i}: element not found — {step}", i
                d.click(el.cx, el.cy)
                time.sleep(0.5)

            elif action == "type":
                d.send_keys(step.get("text", ""))

            elif action == "keypress":
                key_map = {
                    "Return": "KEYCODE_ENTER",   "Enter": "KEYCODE_ENTER",
                    "Backspace": "KEYCODE_DEL",  "Back":  "KEYCODE_BACK",
                    "Home": "KEYCODE_HOME",      "Tab":   "KEYCODE_TAB",
                }
                d.press(key_map.get(step.get("key", "Enter"), "KEYCODE_ENTER"))

            elif action == "scroll":
                direction = step.get("direction", "down")
                d.swipe_ext(direction, scale=0.5)
                time.sleep(0.5)

            elif action == "navigate":
                import subprocess
                subprocess.run(["adb", "shell", "am", "start",
                                "-a", "android.intent.action.VIEW",
                                "-d", step.get("url", "")],
                               capture_output=True)
                time.sleep(1.5)

            elif action == "wait":
                time.sleep(step.get("ms", 500) / 1000)

            elif action == "wait_for":
                label   = step.get("label", "")
                timeout = step.get("timeout", 5.0)
                try:
                    d(text=label).wait(timeout=timeout)
                except Exception:
                    return False, f"Step {i}: '{label}' never appeared", i

        except Exception as e:
            return False, f"Step {i} ({action}): {e}", i

    return True, "", -1
