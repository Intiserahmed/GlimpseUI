"""
Seer — Desktop Client

Controls any desktop app (macOS, Windows, Linux) using vision + accessibility tree.
Hybrid approach: tries platform a11y tree first (precise), falls back to AI vision coords.

Setup:
  pip install pyautogui pillow requests pyyaml
  macOS:   pip install atomacos
  Windows: pip install pywinauto
  Linux:   pip install pyatspi

  python desktop_client.py --task "Open Safari and search for AI news"

No selectors. No code. Works on any desktop app.
"""

import argparse
import base64
import os
import platform
import sys
import time
from io import BytesIO

try:
    import pyautogui
except ImportError:
    print("ERROR: pyautogui not installed. Run: pip install pyautogui")
    sys.exit(1)

import requests
import yaml
from PIL import Image

# ── Config ────────────────────────────────────────────────────────────────────

SERVER_URL  = os.getenv("SEER_URL", "http://localhost:8080")
LOOP_DELAY  = 0.6
_PLATFORM   = platform.system()  # "Darwin" | "Windows" | "Linux"

# Disable pyautogui failsafe (move mouse to corner to abort)
pyautogui.FAILSAFE = True
pyautogui.PAUSE    = 0.05


# ── Hybrid: accessibility tree ────────────────────────────────────────────────

def get_element_tree() -> list:
    """
    Fetch all interactive elements from the platform accessibility tree.
    Returns list of dicts with: label, identifier, cx, cy, enabled.
    Falls back to [] if the library is not installed or the call fails.
    """
    try:
        if _PLATFORM == "Darwin":
            return _tree_macos()
        elif _PLATFORM == "Windows":
            return _tree_windows()
        else:
            return _tree_linux()
    except Exception:
        return []


def _tree_macos() -> list:
    """macOS: AXUIElement via atomacos."""
    import atomacos  # pip install atomacos
    app = atomacos.getFrontmostApp()
    elements = []
    _walk_ax(app, elements)
    return elements


def _walk_ax(node, out: list):
    """Recursively walk AX tree, collect labelled elements."""
    try:
        role  = getattr(node, "AXRole", "") or ""
        title = getattr(node, "AXTitle", "") or ""
        value = getattr(node, "AXValue", "")
        desc  = getattr(node, "AXDescription", "") or ""
        ident = getattr(node, "AXIdentifier", "") or ""
        label = title or desc or (str(value) if isinstance(value, str) else "")
        frame = getattr(node, "AXFrame", None)
        enabled = bool(getattr(node, "AXEnabled", True))
        if label and frame:
            x, y, w, h = frame.x, frame.y, frame.size.width, frame.size.height
            if w > 0 and h > 0:
                out.append({
                    "label":      label,
                    "identifier": ident,
                    "cx": int(x + w / 2),
                    "cy": int(y + h / 2),
                    "enabled":    enabled,
                })
        children = getattr(node, "AXChildren", None) or []
        for child in children:
            _walk_ax(child, out)
    except Exception:
        pass


def _tree_windows() -> list:
    """Windows: Microsoft UIA via pywinauto."""
    from pywinauto import Desktop  # pip install pywinauto
    elements = []
    try:
        desk = Desktop(backend="uia")
        wins = desk.windows(visible_only=True)
        for win in wins:
            try:
                for el in win.descendants():
                    try:
                        name   = el.window_text() or ""
                        ident  = getattr(el, "automation_id", lambda: "")() or ""
                        rect   = el.rectangle()
                        w, h   = rect.width(), rect.height()
                        if not name or w <= 0 or h <= 0:
                            continue
                        elements.append({
                            "label":      name,
                            "identifier": ident,
                            "cx": rect.left + w // 2,
                            "cy": rect.top  + h // 2,
                            "enabled": el.is_enabled(),
                        })
                    except Exception:
                        pass
            except Exception:
                pass
    except Exception:
        pass
    return elements


def _tree_linux() -> list:
    """Linux: AT-SPI2 via pyatspi."""
    import pyatspi  # pip install pyatspi
    elements = []
    desktop = pyatspi.Registry.getDesktop(0)
    for app in desktop:
        if not app:
            continue
        try:
            _walk_atspi(app, elements)
        except Exception:
            pass
    return elements


def _walk_atspi(node, out: list):
    try:
        name  = node.name or ""
        role  = node.getRole()
        comp  = node.queryComponent()
        ext   = comp.getExtents(pyatspi.DESKTOP_COORDS)
        w, h  = ext.width, ext.height
        if name and w > 0 and h > 0:
            out.append({
                "label":      name,
                "identifier": "",
                "cx": ext.x + w // 2,
                "cy": ext.y + h // 2,
                "enabled": node.getState().contains(pyatspi.STATE_ENABLED),
            })
        for i in range(node.childCount):
            _walk_atspi(node.getChildAtIndex(i), out)
    except Exception:
        pass


def find_in_tree(prompt: str, elements: list) -> dict | None:
    """Fuzzy-find element by natural language prompt. Same logic as iOS/Android clients."""
    if not prompt or not elements:
        return None
    p = prompt.lower().strip()
    # 1. Exact match
    for el in elements:
        if p == el.get("label", "").lower() or p == el.get("identifier", "").lower():
            return el
    # 2. Prompt contained in label
    for el in elements:
        if p in el.get("label", "").lower() or p in el.get("identifier", "").lower():
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


# ── Screenshot ────────────────────────────────────────────────────────────────

def take_screenshot() -> tuple[str, int, int]:
    """Take full desktop screenshot. Returns (base64_jpeg, width, height)."""
    img = pyautogui.screenshot().convert("RGB")
    w, h = img.size

    # Resize to max 1280px on longest side for token efficiency
    max_px = 1280
    if max(w, h) > max_px:
        scale = max_px / max(w, h)
        img   = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

    buf = BytesIO()
    img.save(buf, format="JPEG", quality=60)
    b64 = base64.b64encode(buf.getvalue()).decode()
    return b64, w, h


# ── Action executor ───────────────────────────────────────────────────────────

def execute(action: str, params: dict, located: dict | None,
            screen_w: int, screen_h: int) -> tuple[bool, str]:
    try:
        if action in ("Tap", "DoubleClick", "RightClick") and located:
            # located coords are in original screen pixels
            cx, cy = located["cx"], located["cy"]
            if action == "DoubleClick":
                pyautogui.doubleClick(cx, cy)
            elif action == "RightClick":
                pyautogui.rightClick(cx, cy)
            else:
                pyautogui.click(cx, cy)
            return True, ""

        elif action == "Hover" and located:
            pyautogui.moveTo(located["cx"], located["cy"], duration=0.2)
            return True, ""

        elif action == "Type":
            text = params.get("text", "")
            pyautogui.typewrite(text, interval=0.03)
            return True, ""

        elif action == "KeyPress":
            key = params.get("key", "enter")
            mods = params.get("modifiers", [])
            key_lower = key.lower()
            # Map common key names
            key_map = {
                "return": "enter", "escape": "esc",
                "arrowup": "up", "arrowdown": "down",
                "arrowleft": "left", "arrowright": "right",
            }
            key_lower = key_map.get(key_lower, key_lower)
            if mods:
                mod_map = {"control": "ctrl", "meta": "command"}
                mapped_mods = [mod_map.get(m.lower(), m.lower()) for m in mods]
                pyautogui.hotkey(*mapped_mods, key_lower)
            else:
                pyautogui.press(key_lower)
            return True, ""

        elif action == "Scroll":
            direction = params.get("direction", "down")
            amount    = params.get("amount", 3)
            clicks    = amount * 3
            if direction == "down":
                pyautogui.scroll(-clicks)
            elif direction == "up":
                pyautogui.scroll(clicks)
            elif direction == "left":
                pyautogui.hscroll(-clicks)
            elif direction == "right":
                pyautogui.hscroll(clicks)
            return True, ""

        elif action == "Wait":
            ms = params.get("ms", 1000)
            time.sleep(ms / 1000)
            return True, ""

        elif action == "Navigate":
            # On desktop, "navigate" opens a URL in the default browser
            import subprocess, platform
            url = params.get("url", "")
            if platform.system() == "Darwin":
                subprocess.run(["open", url], capture_output=True)
            elif platform.system() == "Windows":
                subprocess.run(["start", url], shell=True, capture_output=True)
            else:
                subprocess.run(["xdg-open", url], capture_output=True)
            return True, ""

        return False, f"Unknown action: {action}"

    except Exception as e:
        return False, str(e)


# ── macOS accessibility permission check ──────────────────────────────────────

def check_accessibility() -> bool:
    """
    On macOS, pyautogui requires Accessibility permission.
    Returns True if trusted (or non-macOS). Prints guidance if not.
    """
    if _PLATFORM != "Darwin":
        return True
    try:
        import ctypes, ctypes.util, subprocess as _sp
        lib = ctypes.cdll.LoadLibrary(ctypes.util.find_library("ApplicationServices"))
        lib.AXIsProcessTrustedWithOptions.restype = ctypes.c_bool
        trusted = lib.AXIsProcessTrustedWithOptions(None)
        if not trusted:
            # Identify the app that needs permission (the one that launched us)
            try:
                parent_pid = os.getppid()
                result = _sp.run(
                    ["ps", "-p", str(parent_pid), "-o", "comm="],
                    capture_output=True, text=True,
                )
                parent_app = result.stdout.strip().split("/")[-1] or "your terminal app"
            except Exception:
                parent_app = "your terminal app"
            print(f"  ⚠️  Accessibility permission not granted.")
            print(f"  Open: System Settings → Privacy & Security → Accessibility")
            print(f"  Add and enable: {parent_app}")
            print(f"  Then re-run this task.\n")
            # Open System Settings to the right pane automatically
            try:
                _sp.run(["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"],
                        capture_output=True)
            except Exception:
                pass
        return trusted
    except Exception:
        return True  # can't check — proceed anyway


# ── Bridge check ──────────────────────────────────────────────────────────────

def check_server() -> bool:
    try:
        r = requests.get(f"{SERVER_URL}/health", timeout=3)
        return r.json().get("status") == "ok"
    except Exception:
        return False


# ── YAML save ─────────────────────────────────────────────────────────────────

def save_yaml(path: str, task: str, final_screenshot: str | None, server: str):
    steps = [{"task": task}]
    if final_screenshot:
        try:
            resp = requests.post(f"{server}/assert", json={
                "screenshot": final_screenshot,
                "condition":  f"The task '{task}' was completed successfully",
            }, timeout=15)
            if resp.json().get("passed"):
                resp2 = requests.post(f"{server}/assert", json={
                    "screenshot": final_screenshot,
                    "condition":  "Describe in one short sentence what is visible on screen",
                }, timeout=15)
                reason = resp2.json().get("reason", "")
                if reason:
                    steps.append({"assert": reason})
        except Exception:
            pass

    spec = {
        "name":     task[:60] + ("..." if len(task) > 60 else ""),
        "platform": "desktop",
        "steps":    steps,
    }
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        yaml.dump(spec, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    print(f"\n  Saved: {path}")


# ── Main loop ─────────────────────────────────────────────────────────────────

def run(task: str, server: str, save_path: str | None = None):
    print(f"\n  Task:   {task}")
    print(f"  Server: {server}")
    print(f"  Mode:   Desktop (pyautogui)\n")

    if not check_server():
        print("  Seer server not running.")
        print(f"  Start it: python main.py")
        sys.exit(1)

    print("  Server connected\n")

    if not check_accessibility():
        sys.exit(1)

    session_id      = None
    last_action     = None
    last_success    = True
    last_error      = None
    step            = 0
    last_screenshot = None

    while True:
        step += 1
        print(f"  Step {step} ".ljust(40, "-"))

        screenshot, screen_w, screen_h = take_screenshot()
        last_screenshot = screenshot

        payload = {
            "task":         task,
            "screenshot":   screenshot,
            "session_id":   session_id,
            "last_action":  last_action,
            "last_success": last_success,
            "last_error":   last_error,
            "viewport_w":   screen_w,
            "viewport_h":   screen_h,
            "platform":     "desktop",
        }

        try:
            resp = requests.post(f"{server}/next-action", json=payload, timeout=30)
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
        thought    = data.get("thought", "")

        if thought:
            print(f"  Thought: {thought[:80]}")

        # ── Hybrid: try accessibility tree before using AI vision coords ──────
        if action in ("Tap", "DoubleClick", "RightClick", "Hover") and located:
            prompt   = located.get("prompt", "")
            elements = get_element_tree()
            tree_el  = find_in_tree(prompt, elements)
            if tree_el:
                located = {**located, "cx": tree_el["cx"], "cy": tree_el["cy"]}
                print(f"  Tree: '{prompt}' → ({tree_el['cx']}, {tree_el['cy']})")
            else:
                print(f"  Vision: '{prompt}' → ({located['cx']}, {located['cy']})")

        print(f"  Action: {action}", end="")
        if located:
            print(f" → ({located['cx']}, {located['cy']})", end="")
        print()

        if finished:
            success = data.get("success", True)
            message = data.get("message", "")
            print(f"\n  {'DONE' if success else 'FAILED'}: {message}")
            if save_path and success:
                save_yaml(save_path, task, last_screenshot, server)
            break

        ok, err = execute(action, params, located, screen_w, screen_h)
        last_action  = action
        last_success = ok
        last_error   = err if not ok else None

        if not ok:
            print(f"  Warning: {err}")

        time.sleep(LOOP_DELAY)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seer — Desktop Client")
    parser.add_argument("--task",   required=True,  help="What to do in plain English")
    parser.add_argument("--server", default=SERVER_URL)
    parser.add_argument("--save",   default=None, metavar="PATH",
                        help="Save completed task as YAML (e.g. tests/my_test.yaml)")
    args = parser.parse_args()
    run(args.task, args.server, save_path=args.save)
