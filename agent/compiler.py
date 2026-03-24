"""
AI Compiler: runs the AI loop ONCE for a new task and records every action
as a deterministic cached script.

On the first run of a new task:
  - AI plans each step (screenshot → decide action → execute)
  - Each action is recorded with: accessibilityId + label + coords
  - Script saved to cache

On all future runs:
  - executor.py loads the script and runs it with NO AI calls
  - Cost: $0, speed: 10x faster

The key insight vs just running the AI loop:
  - While executing, we query the accessibility tree to resolve
    pixel coordinates → stable accessibilityId
  - This means the cached script survives layout changes
"""

import asyncio
import base64
from typing import Optional

from .config         import GEMINI_MODEL
from .mobile_element import _get_tree, _find_id_near


# ── Web compiler (uses Gemini Computer Use API) ───────────────────────────────

async def compile_web_task(
    task:      str,
    page,
    browser,
    start_url: str = "",
) -> list[dict]:
    """
    Run the AI vision loop for a web task and record each action
    as a deterministic step (CSS selector instead of pixel coords).
    Returns the compiled script.
    """
    from .session_manager import take_screenshot_b64, viewport_size
    from .computer_use   import (
        get_client, get_config,
        make_initial_content, make_function_response,
        get_function_calls,
        execute_computer_use_action,
    )

    if start_url:
        await browser.navigate_to(start_url)

    client   = get_client()
    config   = get_config()
    vw, vh   = viewport_size(browser)
    ss       = await take_screenshot_b64(browser)
    url      = await page.get_url()
    contents = make_initial_content(task, ss, url)
    script   : list[dict] = []

    for _ in range(25):  # max AI steps during compilation
        response = await client.aio.models.generate_content(
            model=GEMINI_MODEL, contents=contents, config=config
        )
        contents.append(response.candidates[0].content)
        func_calls = _get_fc(response)

        if not func_calls:
            break  # AI said done

        for fc in func_calls:
            name = fc.name
            args = dict(fc.args) if fc.args else {}

            step = _fc_to_web_step(name, args, vw, vh, page)
            if step:
                script.append(step)

            ok, error = await execute_computer_use_action(page, fc, vw, vh)
            await asyncio.sleep(0.5)

            ss  = await take_screenshot_b64(browser)
            url = await page.get_url()
            vw, vh = viewport_size(browser)

            contents.append(make_function_response(
                name, ss, url, error if not ok else ""
            ))
            await asyncio.sleep(0.2)

    return script


async def _fc_to_web_step(name: str, args: dict, vw: int, vh: int, page) -> Optional[dict]:
    """Convert a Gemini FunctionCall to a deterministic web step dict."""
    if name == "navigate":
        return {"action": "navigate", "url": args.get("url", "")}

    if name == "click_at":
        x  = int(args.get("x", 500) / 1000 * vw)
        y  = int(args.get("y", 500) / 1000 * vh)
        sel = await _coords_to_selector(page, x, y)
        step = {"action": "click", "coords": [x, y]}
        if sel:
            step["selector"] = sel
        return step

    if name == "type_text_at":
        x   = int(args.get("x", 500) / 1000 * vw)
        y   = int(args.get("y", 500) / 1000 * vh)
        sel = await _coords_to_selector(page, x, y)
        step = {"action": "fill", "text": args.get("text", ""), "coords": [x, y]}
        if sel:
            step["selector"] = sel
        return step

    if name == "key_combination":
        return {"action": "press", "key": args.get("keys", "Enter")}

    if name == "scroll_document":
        return {"action": "scroll", "direction": args.get("direction", "down")}

    if name in ("wait_5_seconds",):
        return {"action": "wait", "ms": 5000}

    if name == "go_back":
        return {"action": "press", "key": "Alt+Left"}

    return None


async def _coords_to_selector(page, x: int, y: int) -> Optional[str]:
    """
    Given pixel coordinates, find the best CSS selector for the element
    at that position. Prefers stable selectors (data-testid > id > aria-label).
    """
    try:
        result = await page.evaluate(f"""
            (() => {{
                const el = document.elementFromPoint({x}, {y});
                if (!el) return null;
                if (el.dataset.testid)
                    return '[data-testid="' + el.dataset.testid + '"]';
                if (el.id)
                    return '#' + el.id;
                const aria = el.getAttribute('aria-label');
                if (aria)
                    return '[aria-label="' + aria + '"]';
                if (el.name)
                    return '[name="' + el.name + '"]';
                const text = el.innerText?.trim().slice(0, 30);
                if (text && el.tagName !== 'BODY')
                    return el.tagName.toLowerCase() + ':has-text("' + text + '")';
                return null;
            }})()
        """)
        return result
    except Exception:
        return None


# ── Mobile compiler (uses planner.py XML-format AI) ──────────────────────────

async def compile_mobile_task(
    task:          str,
    platform:      str = "ios",
    bridge_url:    str = "http://localhost:22087",
    server_url:    str = "http://localhost:8080",
    viewport_w:    int = 393,
    viewport_h:    int = 852,
    app_bundle:    str = "com.apple.mobilesafari",
    device_serial: str = None,
) -> list[dict]:
    """
    Run the AI loop for a mobile task once, recording each action
    with accessibilityId + label + coords for deterministic replay.
    Returns the compiled script.
    """
    import requests as req
    import time
    from .mobile_wait    import wait_for_stable
    from .mobile_element import _get_tree, _center
    from .planner        import (
        get_client, build_first_turn, build_continuation_turn,
        build_retry_turn, build_assistant_turn, call_gemini, parse_response,
    )

    def take_screenshot() -> str:
        try:
            r  = req.post(f"{bridge_url}/screenshot", timeout=5)
            ss = r.json().get("screenshot", "")
            if ss:
                return ss
        except Exception:
            pass
        import subprocess
        path = "/tmp/ui_compile.png"
        subprocess.run(["xcrun", "simctl", "io", "booted", "screenshot", path],
                       capture_output=True)
        from PIL import Image
        from io import BytesIO
        img = Image.open(path).convert("RGB").resize((viewport_w, viewport_h))
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=60)
        return base64.b64encode(buf.getvalue()).decode()

    def bridge(path, data={}):
        try:
            return req.post(f"{bridge_url}{path}", json=data, timeout=15).json()
        except Exception as e:
            return {"ok": False, "error": str(e)}

    conversation = [build_first_turn(task, take_screenshot())]
    script: list[dict] = []
    last_action = ""
    last_result = ""

    for step_num in range(1, 25):
        raw, assistant_turn = await call_gemini(conversation, platform)
        conversation.append(assistant_turn)

        action = parse_response(raw, viewport_w, viewport_h)
        if not action:
            break

        if action.action_type == "Finished":
            break

        # Build deterministic step from this AI action
        det_step = _mobile_action_to_step(
            action, platform, bridge_url, device_serial, viewport_w, viewport_h
        )
        if det_step:
            script.append(det_step)

        # Execute the action
        ok, err = _execute_mobile_action(
            action, bridge, app_bundle, viewport_w, viewport_h
        )
        wait_for_stable(bridge_url=bridge_url)

        last_action = action.action_type
        last_result = "success" if ok else f"failed: {err}"

        ss = take_screenshot()
        if ok:
            conversation.append(
                build_continuation_turn(step_num, ss, last_action, last_result)
            )
        else:
            conversation.append(build_retry_turn(step_num, ss, err))

    return script


def _mobile_action_to_step(
    action,
    platform:      str,
    bridge_url:    str,
    device_serial: str,
    vw: int, vh: int,
) -> Optional[dict]:
    """Convert a ParsedAction to a deterministic mobile step dict."""
    if action.action_type == "Tap" and action.located:
        cx, cy = action.located.center_x, action.located.center_y
        step   = {
            "action": "tap",
            "label":  action.located.prompt,
            "coords": [cx, cy],
        }
        # Try to resolve to accessibilityId now while the element is visible
        tree   = _get_tree(platform, bridge_url, device_serial)
        acc_id = _find_id_near(tree, cx, cy)
        if acc_id:
            step["accessibilityId"] = acc_id
        return step

    if action.action_type == "Type":
        return {"action": "type", "text": action.params.get("text", "")}

    if action.action_type == "KeyPress":
        return {"action": "keypress", "key": action.params.get("key", "Return")}

    if action.action_type == "Scroll":
        return {"action": "scroll", "direction": action.params.get("direction", "down")}

    if action.action_type == "Navigate":
        return {"action": "navigate", "url": action.params.get("url", "")}

    if action.action_type == "Wait":
        return {"action": "wait", "ms": action.params.get("ms", 1000)}

    return None


def _execute_mobile_action(action, bridge_fn, app_bundle, vw, vh) -> tuple[bool, str]:
    """Execute a ParsedAction on the device during compilation."""
    import subprocess, time

    t = action.action_type
    p = action.params
    l = action.located

    if t in ("Tap", "DoubleClick") and l:
        path = "/doubletap" if t == "DoubleClick" else "/tap"
        r    = bridge_fn(path, {"x": l.center_x, "y": l.center_y})
        return r.get("ok", False), r.get("error", "")

    if t == "Type":
        r = bridge_fn("/type", {"text": p.get("text", ""), "bundleId": app_bundle})
        return r.get("ok", False), r.get("error", "")

    if t == "KeyPress":
        r = bridge_fn("/keypress", {"key": p.get("key", "Return"), "bundleId": app_bundle})
        return r.get("ok", False), r.get("error", "")

    if t == "Scroll":
        direction = p.get("direction", "down")
        cx = vw // 2
        y1, y2 = (int(vh * 0.7), int(vh * 0.3)) if direction == "down" \
                 else (int(vh * 0.3), int(vh * 0.7))
        r = bridge_fn("/swipe", {"x1": cx, "y1": y1, "x2": cx, "y2": y2})
        return r.get("ok", False), r.get("error", "")

    if t == "Navigate":
        subprocess.run(["xcrun", "simctl", "openurl", "booted", p.get("url", "")],
                       capture_output=True)
        return True, ""

    if t == "Wait":
        time.sleep(p.get("ms", 1000) / 1000)
        return True, ""

    return False, f"Unknown action: {t}"


# ── Helper re-exported from mobile_element ────────────────────────────────────

def _get_fc(response):
    calls = []
    for part in response.candidates[0].content.parts:
        if hasattr(part, "function_call") and part.function_call:
            calls.append(part.function_call)
    return calls
