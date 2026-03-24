"""
Vision agent loop — XML-format actions via OpenRouter-compatible AI.
Replaces the previous Gemini Computer Use API (ENVIRONMENT_BROWSER) approach.
"""

import asyncio
import hashlib
import json
import uuid
from typing import AsyncGenerator

from .logger import get_logger
from .session_manager import get_session, take_screenshot_b64, viewport_size, viewport_size_live
from .planner import (
    call_ai,
    parse_response,
    ParsedAction,
    build_first_turn,
    build_continuation_turn,
    build_retry_turn,
)

logger = get_logger(__name__)

MAX_STEPS   = 30
MAX_REPEATS = 3

_sessions: dict[str, dict] = {}


def get_sessions() -> list[dict]:
    return list(_sessions.values())


# ── Action fingerprint ────────────────────────────────────────────────────────

def _action_fingerprint(parsed: ParsedAction) -> str:
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


# ── Low-level CDP helpers ─────────────────────────────────────────────────────

_COMBO_MODS = {"ctrl": 2, "control": 2, "alt": 1, "shift": 8, "meta": 4, "cmd": 4, "command": 4}

_KEY_INFO: dict[str, dict] = {
    "Enter":      {"key": "Enter",     "code": "Enter",      "keyCode": 13},
    "Escape":     {"key": "Escape",    "code": "Escape",     "keyCode": 27},
    "Tab":        {"key": "Tab",       "code": "Tab",        "keyCode": 9},
    "Backspace":  {"key": "Backspace", "code": "Backspace",  "keyCode": 8},
    "Delete":     {"key": "Delete",    "code": "Delete",     "keyCode": 46},
    "Space":      {"key": " ",         "code": "Space",      "keyCode": 32},
    " ":          {"key": " ",         "code": "Space",      "keyCode": 32},
    "ArrowUp":    {"key": "ArrowUp",   "code": "ArrowUp",    "keyCode": 38},
    "ArrowDown":  {"key": "ArrowDown", "code": "ArrowDown",  "keyCode": 40},
    "ArrowLeft":  {"key": "ArrowLeft", "code": "ArrowLeft",  "keyCode": 37},
    "ArrowRight": {"key": "ArrowRight","code": "ArrowRight", "keyCode": 39},
}


def _key_params(key: str) -> dict:
    if key in _KEY_INFO:
        return _KEY_INFO[key]
    ch = key[0] if key else "?"
    return {"key": ch, "code": f"Key{ch.upper()}", "keyCode": ord(ch.upper())}


async def _dispatch_key(page, key: str, session_id=None) -> None:
    """Fire keyDown + char + keyUp via CDP — works for both inputs and canvas/game pages."""
    if session_id is None:
        session_id = await page._ensure_session()
    kp = _key_params(key)
    base = {
        "key":                  kp["key"],
        "code":                 kp["code"],
        "windowsVirtualKeyCode": kp["keyCode"],
        "nativeVirtualKeyCode":  kp["keyCode"],
    }
    await page._client.send.Input.dispatchKeyEvent(
        {**base, "type": "keyDown"}, session_id=session_id
    )
    # "char" event is what actually inserts the character into a focused input
    if len(kp["key"]) == 1:
        await page._client.send.Input.dispatchKeyEvent(
            {**base, "type": "char", "text": kp["key"]}, session_id=session_id
        )
    await page._client.send.Input.dispatchKeyEvent(
        {**base, "type": "keyUp"}, session_id=session_id
    )


async def _dispatch_text(page, text: str, session_id=None) -> None:
    """Type text char-by-char via CDP — triggers keydown listeners AND fills inputs."""
    if session_id is None:
        session_id = await page._ensure_session()
    for ch in text:
        await _dispatch_key(page, ch, session_id)
        await asyncio.sleep(0.08)  # 80ms matches MidScene's proven delay


async def _dispatch_combo(page, combo: str, session_id=None) -> None:
    """Fire a modifier+key combo e.g. 'Ctrl+A', 'Cmd+Shift+Z'."""
    if session_id is None:
        session_id = await page._ensure_session()
    parts = combo.split("+")
    key   = parts[-1]
    mods  = sum(_COMBO_MODS.get(m.lower(), 0) for m in parts[:-1])
    kp    = _key_params(key)
    base  = {"key": kp["key"], "code": kp["code"],
             "windowsVirtualKeyCode": kp["keyCode"],
             "nativeVirtualKeyCode":  kp["keyCode"],
             "modifiers": mods}
    await page._client.send.Input.dispatchKeyEvent({**base, "type": "keyDown"}, session_id=session_id)
    await page._client.send.Input.dispatchKeyEvent({**base, "type": "keyUp"},   session_id=session_id)


async def _drag(page, fx: int, fy: int, tx: int, ty: int, session_id=None) -> None:
    """Smooth mouse drag from (fx,fy) to (tx,ty) via CDP mouse events."""
    if session_id is None:
        session_id = await page._ensure_session()
    steps = max(abs(tx - fx), abs(ty - fy), 10)
    await page._client.send.Input.dispatchMouseEvent(
        {"type": "mouseMoved", "x": fx, "y": fy}, session_id=session_id)
    await page._client.send.Input.dispatchMouseEvent(
        {"type": "mousePressed", "x": fx, "y": fy, "button": "left", "clickCount": 1},
        session_id=session_id)
    for i in range(1, steps + 1):
        ix = fx + round((tx - fx) * i / steps)
        iy = fy + round((ty - fy) * i / steps)
        await page._client.send.Input.dispatchMouseEvent(
            {"type": "mouseMoved", "x": ix, "y": iy, "button": "left"}, session_id=session_id)
        await asyncio.sleep(0.008)
    await page._client.send.Input.dispatchMouseEvent(
        {"type": "mouseReleased", "x": tx, "y": ty, "button": "left", "clickCount": 1},
        session_id=session_id)


async def _focus_nearest_input(page, cx: int, cy: int, radius: int = 200) -> str | None:
    """Find the nearest visible input/textarea to (cx,cy) and focus it via DOM.
    Returns the focused element's type string, or None if nothing found within radius.
    Uses direct DOM .click()/.focus() — bypasses coordinate/overlay issues entirely."""
    return await page.evaluate(f"""
        (() => {{
            const cx = {cx}, cy = {cy}, radius = {radius};
            const els = Array.from(document.querySelectorAll(
                'input:not([type=hidden]), textarea, [contenteditable="true"]'
            ));
            let best = null, bestDist = Infinity;
            for (const el of els) {{
                const r = el.getBoundingClientRect();
                if (r.width === 0 || r.height === 0) continue;
                const dist = Math.hypot(r.left + r.width/2 - cx, r.top + r.height/2 - cy);
                if (dist < bestDist) {{ bestDist = dist; best = el; }}
            }}
            if (!best || bestDist > radius) return null;
            best.scrollIntoView({{block: 'nearest', inline: 'nearest'}});
            best.click();
            best.focus();
            return best.tagName + ':' + (best.type || '');
        }})()
    """)


# ── Action executor (ParsedAction → Playwright) ───────────────────────────────

async def execute_parsed_action(page, parsed: ParsedAction, vw: int, vh: int) -> tuple[bool, str]:
    try:
        at = parsed.action_type
        p  = parsed.params

        if at in ("Tap", "DoubleClick", "RightClick", "Hover") and parsed.located:
            cx, cy = parsed.located.center_x, parsed.located.center_y
            logger.debug("Tap bbox=%s → pixel cx=%d cy=%d (viewport %dx%d)",
                         parsed.located.bbox, cx, cy, vw, vh)
            mouse  = await page.mouse
            if at == "DoubleClick":
                await mouse.click(cx, cy, click_count=2)
            elif at == "RightClick":
                await mouse.click(cx, cy, button="right")
            elif at == "Hover":
                await mouse.move(cx, cy)
            else:
                await mouse.click(cx, cy)
            await asyncio.sleep(0.3)

        elif at == "Type":
            text = p.get("text", "")
            session_id = await page._ensure_session()
            has_inputs = await page.evaluate(
                "() => document.querySelectorAll('input:not([type=hidden]), textarea').length > 0"
            )
            if has_inputs:
                # MidScene approach: Ctrl+A clears field and confirms focus, then type char-by-char
                await _dispatch_combo(page, "Ctrl+A", session_id)
                await asyncio.sleep(0.05)
                await _dispatch_key(page, "Backspace", session_id)
                await asyncio.sleep(0.05)
                await _dispatch_text(page, text, session_id)
            else:
                # Canvas/game page (no inputs) — key events go to document listeners
                await _dispatch_text(page, text, session_id)

        elif at == "TypeWord":
            # Batch: type every letter then Enter — zero extra AI round-trips
            word = p.get("word", "").upper()
            session_id = await page._ensure_session()
            await _dispatch_text(page, word, session_id)
            await _dispatch_key(page, "Enter", session_id)

        elif at == "KeyCombo":
            # Modifier combos: Ctrl+A, Cmd+Shift+Z, Alt+F4, etc.
            session_id = await page._ensure_session()
            await _dispatch_combo(page, p.get("keys", ""), session_id)

        elif at == "ClearAndType":
            # Select-all + replace — no intermediate screenshot needed
            session_id = await page._ensure_session()
            await _dispatch_combo(page, "Ctrl+A", session_id)
            await asyncio.sleep(0.05)
            text = p.get("text", "")
            try:
                await page._client.send.Input.insertText({"text": text}, session_id=session_id)
            except Exception:
                await _dispatch_text(page, text, session_id)

        elif at == "FillField":
            from .planner import bbox_to_pixels, bbox_center
            loc = p.get("locate", {})
            bbox_raw = loc.get("bbox", [0, 0, 100, 100])
            px1, py1, px2, py2 = bbox_to_pixels(bbox_raw, vw, vh)
            cx, cy = bbox_center(px1, py1, px2, py2)
            mouse = await page.mouse
            await mouse.click(cx, cy)
            await asyncio.sleep(0.2)
            session_id = await page._ensure_session()
            await _dispatch_combo(page, "Ctrl+A", session_id)
            await asyncio.sleep(0.05)
            await _dispatch_key(page, "Backspace", session_id)
            await asyncio.sleep(0.05)
            await _dispatch_text(page, p.get("text", ""), session_id)
            if p.get("advance", False):
                await _dispatch_key(page, "Tab", session_id)

        elif at == "SelectOption":
            # Open a <select> or custom dropdown and pick an option by text
            option_text = p.get("option", "")
            # Try native <select> first (instant, no clicks needed)
            selected = await page.evaluate(f"""
                (() => {{
                    for (const sel of document.querySelectorAll('select')) {{
                        const opt = Array.from(sel.options).find(o =>
                            o.text.toLowerCase().includes({json.dumps(option_text.lower())}) ||
                            o.value.toLowerCase().includes({json.dumps(option_text.lower())})
                        );
                        if (opt) {{
                            sel.value = opt.value;
                            sel.dispatchEvent(new Event('input',  {{bubbles: true}}));
                            sel.dispatchEvent(new Event('change', {{bubbles: true}}));
                            return true;
                        }}
                    }}
                    return false;
                }})()
            """)
            if not selected and parsed.located:
                # Custom dropdown: click to open, then look for option text via JS
                cx, cy = parsed.located.center_x, parsed.located.center_y
                mouse  = await page.mouse
                await mouse.click(cx, cy)
                await asyncio.sleep(0.3)
                await page.evaluate(f"""
                    (() => {{
                        const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
                        while (walker.nextNode()) {{
                            const el = walker.currentNode;
                            if (el.innerText && el.innerText.trim().toLowerCase() ===
                                    {json.dumps(option_text.lower())} &&
                                    el.offsetParent !== null) {{
                                el.click(); return;
                            }}
                        }}
                    }})()
                """)

        elif at == "DragTo":
            # Single gesture: mousedown → smooth move → mouseup
            from .planner import bbox_to_pixels, bbox_center
            def _resolve(key):
                raw = p.get(key, [0, 0, 100, 100])
                x1, y1, x2, y2 = bbox_to_pixels(raw, vw, vh)
                return bbox_center(x1, y1, x2, y2)
            fx, fy = _resolve("from")
            tx, ty = _resolve("to")
            session_id = await page._ensure_session()
            await _drag(page, fx, fy, tx, ty, session_id)

        elif at == "SwipeSequence":
            # Multiple directional swipes without a screenshot between each
            moves = p.get("moves", [])
            delta_map = {
                "down":  (0,  400), "up":    (0, -400),
                "left": (-400, 0),  "right": (400,  0),
            }
            mouse = await page.mouse
            for move in moves:
                dx, dy = delta_map.get(move, (0, 400))
                await mouse.scroll(delta_x=dx, delta_y=dy)
                await asyncio.sleep(0.15)

        elif at == "Navigate":
            await page.goto(p.get("url", ""))
            await asyncio.sleep(1.5)

        elif at == "KeyPress":
            key = p.get("key", "")
            session_id = await page._ensure_session()
            await _dispatch_key(page, key, session_id)

        elif at == "Scroll":
            direction = p.get("direction", "down")
            delta = {
                "down":  (0,  300), "up":    (0, -300),
                "left": (-300, 0),  "right": (300,  0),
            }.get(direction, (0, 300))
            mouse = await page.mouse
            await mouse.scroll(delta_x=delta[0], delta_y=delta[1])

        elif at == "Wait":
            await asyncio.sleep(p.get("ms", 1000) / 1000)

        elif at == "Finished":
            return True, "finished"

        return True, ""

    except Exception as e:
        return False, str(e)


# ── Main task loop ────────────────────────────────────────────────────────────

async def run_task(
    task: str,
    start_url: str = "about:blank",
    session_id: str = None,
) -> AsyncGenerator[dict, None]:
    sid = session_id or str(uuid.uuid4())[:8]
    _sessions[sid] = {"session_id": sid, "status": "running", "task": task, "steps": 0}

    try:
        browser = await get_session()
        nav_url = start_url if (start_url and start_url != "about:blank") else "about:blank"
        try:
            await browser.navigate_to(nav_url)
        except Exception as nav_err:
            logger.warning("Initial navigation to %s failed: %s", nav_url, nav_err)
        page = await browser.get_current_page()

        yield {"type": "start", "session_id": sid, "task": task, "url": start_url, "mode": "vision"}

        vw, vh = await viewport_size_live(browser)
        screenshot   = await take_screenshot_b64(browser)
        conversation = [build_first_turn(task, screenshot)]
        fingerprint_counts: dict[str, int] = {}

        for step in range(1, MAX_STEPS + 1):
            _sessions[sid]["steps"] = step

            response_text, assistant_turn = await call_ai(conversation)
            conversation.append(assistant_turn)

            parsed = parse_response(response_text, vw, vh)
            if not parsed:
                screenshot = await take_screenshot_b64(browser)
                yield {
                    "type": "done", "session_id": sid, "step": step,
                    "success": False,
                    "message": "Could not parse AI response",
                    "screenshot": screenshot,
                }
                break

            # Task complete
            if parsed.action_type == "Finished":
                screenshot = await take_screenshot_b64(browser)
                yield {
                    "type": "done", "session_id": sid, "step": step,
                    "success": parsed.params.get("success", True),
                    "message": parsed.params.get("message", "Task completed"),
                    "screenshot": screenshot,
                }
                break

            # Loop detection
            fp = _action_fingerprint(parsed)
            fingerprint_counts[fp] = fingerprint_counts.get(fp, 0) + 1
            if fingerprint_counts[fp] >= MAX_REPEATS:
                screenshot = await take_screenshot_b64(browser)
                yield {
                    "type": "done", "session_id": sid, "step": step,
                    "success": False,
                    "message": (
                        f"Stuck: '{parsed.action_type}' repeated {MAX_REPEATS}× — "
                        f"{parsed.thought or 'no thought recorded'}"
                    ),
                    "screenshot": screenshot,
                }
                return

            screenshot = await take_screenshot_b64(browser)

            yield {
                "type":       "step",
                "session_id": sid,
                "step":       step,
                "thought":    parsed.thought,
                "action":     parsed.action_type,
                "params":     parsed.params,
                "located": {
                    "prompt": parsed.located.prompt,
                    "bbox":   parsed.located.bbox,
                    "cx":     parsed.located.center_x,
                    "cy":     parsed.located.center_y,
                } if parsed.located else None,
                "screenshot": screenshot,
            }

            ok, error = await execute_parsed_action(page, parsed, vw, vh)

            await asyncio.sleep(0.5)

            screenshot  = await take_screenshot_b64(browser)
            current_url = await page.get_url()
            vw, vh      = await viewport_size_live(browser)

            if error and error != "finished":
                conversation.append(build_retry_turn(step, screenshot, error))
            else:
                conversation.append(
                    build_continuation_turn(step + 1, screenshot, parsed.action_type, "")
                )

            await asyncio.sleep(0.3)

        else:
            screenshot = await take_screenshot_b64(browser)
            yield {
                "type": "done", "session_id": sid, "step": MAX_STEPS,
                "success": False,
                "message": f"Reached max steps ({MAX_STEPS}) without completing task",
                "screenshot": screenshot,
            }

    except Exception as e:
        logger.error("run_task failed [sid=%s]: %s", sid, e, exc_info=True)
        yield {"type": "error", "session_id": sid, "message": str(e)}

    finally:
        _sessions.pop(sid, None)
