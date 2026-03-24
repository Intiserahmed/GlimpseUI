"""
Action planning: vision call + response parsing.
Uses OpenRouter (OpenAI-compatible API) — works with any vision model.
"""

import re
import json
import base64
from dataclasses import dataclass
from typing import Optional
from io import BytesIO

import openai
from PIL import Image

from .config import OPENROUTER_API_KEY, OPENROUTER_BASE_URL, OPENROUTER_MODEL
from .logger import get_logger

logger = get_logger(__name__)

AI_TIMEOUT  = 30.0  # seconds per call
AI_RETRIES  = 3     # attempts before giving up
AI_BACKOFF  = 2.0   # seconds — doubles each attempt (2s, 4s)

# Errors worth retrying (transient). Auth/quota errors are not retried.
_RETRYABLE = (
    openai.RateLimitError,
    openai.APITimeoutError,
    openai.APIConnectionError,
    openai.InternalServerError,
)


async def _call_with_retry(coro_fn, label: str):
    """Call an async OpenAI coroutine with exponential backoff on transient errors."""
    import asyncio
    last_err = None
    for attempt in range(1, AI_RETRIES + 1):
        try:
            return await coro_fn()
        except _RETRYABLE as e:
            last_err = e
            if attempt < AI_RETRIES:
                wait = AI_BACKOFF * (2 ** (attempt - 1))
                logger.warning("%s transient error (attempt %d/%d), retrying in %.0fs: %s",
                               label, attempt, AI_RETRIES, wait, e)
                await asyncio.sleep(wait)
        except openai.OpenAIError:
            raise  # auth, bad request, etc — don't retry
    raise last_err


# ── Client ────────────────────────────────────────────────────────────────────

def get_client() -> openai.AsyncOpenAI:
    return openai.AsyncOpenAI(
        api_key=OPENROUTER_API_KEY,
        base_url=OPENROUTER_BASE_URL,
    )


# ── System prompts ────────────────────────────────────────────────────────────

_ACTIONS_BLOCK = """
## Coordinate System
- The screenshot uses a normalized coordinate system: width=1000, height=1000
- BBox format: [x1, y1, x2, y2] — top-left corner (x1,y1), bottom-right corner (x2,y2)
- x goes left→right (0 to 1000), y goes top→bottom (0 to 1000)

## Response Format
ALWAYS respond with this exact XML structure:

<action-type>ActionName</action-type>
<action-param-json>{"param": "value"}</action-param-json>

## Available Actions

**Tap** - Tap / click a visible element
<action-type>Tap</action-type>
<action-param-json>{"locate": {"prompt": "search input field", "bbox": [100, 45, 700, 75]}}</action-param-json>

**DoubleClick** - Double-tap (open files, select words)
<action-type>DoubleClick</action-type>
<action-param-json>{"locate": {"prompt": "file icon", "bbox": [200, 300, 260, 360]}}</action-param-json>

**Type** - Type text into the focused input
<action-type>Type</action-type>
<action-param-json>{"text": "text to type here"}</action-param-json>

**TypeWord** - Type a full word + Enter in one shot (Wordle guesses, game input, search — never type letter-by-letter)
<action-type>TypeWord</action-type>
<action-param-json>{"word": "CRANE"}</action-param-json>

**KeyCombo** - Modifier key combo (Ctrl+A, Cmd+Z, Ctrl+Shift+T, Alt+F4, etc.)
<action-type>KeyCombo</action-type>
<action-param-json>{"keys": "Ctrl+A"}</action-param-json>

**ClearAndType** - Select all text in focused field and replace it (avoids triple-click + type)
<action-type>ClearAndType</action-type>
<action-param-json>{"text": "new value"}</action-param-json>

**FillField** - Tap a field, type text, optionally advance to next field — one step per form field
<action-type>FillField</action-type>
<action-param-json>{"locate": {"prompt": "email input", "bbox": [100, 200, 600, 230]}, "text": "user@example.com", "advance": true}</action-param-json>

**SelectOption** - Open a dropdown and select an option by visible text — one step, no re-screenshot
<action-type>SelectOption</action-type>
<action-param-json>{"locate": {"prompt": "Country dropdown", "bbox": [100, 300, 400, 330]}, "option": "United States"}</action-param-json>

**DragTo** - Drag from one element to another (Trello cards, sliders, file upload, Figma)
<action-type>DragTo</action-type>
<action-param-json>{"from": [100, 200, 160, 240], "to": [500, 200, 560, 240]}</action-param-json>

**SwipeSequence** - Multiple directional swipes without screenshots between (2048, carousels, mobile games)
<action-type>SwipeSequence</action-type>
<action-param-json>{"moves": ["up", "right", "up", "left", "down"]}</action-param-json>

**Navigate** - Open a URL directly
<action-type>Navigate</action-type>
<action-param-json>{"url": "https://example.com"}</action-param-json>

**KeyPress** - Press a single key
<action-type>KeyPress</action-type>
<action-param-json>{"key": "Enter"}</action-param-json>
Keys: Enter, Escape, Tab, Backspace, ArrowUp/Down/Left/Right

**Scroll** - Scroll the view
<action-type>Scroll</action-type>
<action-param-json>{"direction": "down"}</action-param-json>

**Wait** - Wait for animation/load
<action-type>Wait</action-type>
<action-param-json>{"ms": 1500}</action-param-json>

**Finished** - Task complete
<action-type>Finished</action-type>
<action-param-json>{"success": true, "message": "Describe what was accomplished"}</action-param-json>

## Rules
1. ONE action per response — no exceptions
2. ALWAYS prefer batch actions over multiple single steps:
   - Word/game input → TypeWord (not KeyPress per letter)
   - Form field → FillField (not Tap + Type separately)
   - Dropdown → SelectOption (not Tap + Tap)
   - Replace text → ClearAndType (not triple-click + Type)
   - Drag gesture → DragTo (not mouse steps)
   - Game swipes → SwipeSequence (not Scroll per move)
   - Keyboard shortcut → KeyCombo (not modifier + key)
3. If element not visible: Scroll to find it
4. Multi-step task: complete ALL steps before Finished
5. Call Finished once goal is visually confirmed — do NOT keep exploring
6. If stuck 3 times on same step: Finished with success=false
"""

SYSTEM_PROMPT = f"""You are an AI agent that controls a web browser to complete tasks.
You observe screenshots and decide the next action.
{_ACTIONS_BLOCK}
## Extra Rules
- NEVER navigate to google.com — always use https://duckduckgo.com for any web search
- If you need to search the web, go directly to https://duckduckgo.com and type the query there
- RightClick and Hover are available for browser context menus
"""

SYSTEM_PROMPT_IOS = f"""You are an AI agent controlling an iPhone via touch. You see the screen and tap/swipe to complete tasks.
{_ACTIONS_BLOCK}
## iOS-Specific Rules
- To open an app: tap its icon on the home screen. NEVER open Safari and search for the app name.
- If the app icon is not visible: swipe left/right on the home screen to find it, OR swipe DOWN from the middle of the home screen to open Spotlight search, then type the app name.
- Spotlight search: swipe down on home screen → type app name → tap the app result (NOT a web search result).
- To go home: use the Home gesture (swipe up from bottom) or press the Home button area.
- Settings app icon looks like grey gears. Safari is the blue compass icon.
- Scroll by swiping up (scroll down) or swiping down (scroll up).
- Never use Navigate (no browser URL bar on home screen).
"""

SYSTEM_PROMPT_ANDROID = f"""You are an AI agent controlling an Android device via touch. You see the screen and tap/swipe to complete tasks.
{_ACTIONS_BLOCK}
## Android-Specific Rules
- To open an app: tap its icon on the home screen or app drawer. NEVER search for it in a browser.
- If app icon not visible: swipe up to open the app drawer, then find the app.
- Or use the search bar at the top of the launcher / Google search widget.
- Settings app is the gear icon.
- Scroll by swiping up (to scroll down content) or swiping down.
- Never use Navigate unless you are inside a browser app.
"""

SYSTEM_PROMPT_DESKTOP = f"""You are an AI agent controlling a desktop computer. You observe screenshots and click/type to complete tasks.
{_ACTIONS_BLOCK}
## Desktop-Specific Rules
- To open an app: look for it in the Dock, Desktop, or use Spotlight (Cmd+Space on Mac).
- Right-click for context menus. Double-click to open files/folders.
- For web search use https://duckduckgo.com — never google.com.
"""


def get_system_prompt(platform: str = "web") -> str:
    return {
        "ios":     SYSTEM_PROMPT_IOS,
        "android": SYSTEM_PROMPT_ANDROID,
        "desktop": SYSTEM_PROMPT_DESKTOP,
    }.get(platform, SYSTEM_PROMPT)


CONV_LIMIT = 12  # max turns to keep in context (excluding system)


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class LocatedElement:
    prompt: str
    bbox: list
    center_x: int = 0
    center_y: int = 0


@dataclass
class ParsedAction:
    action_type: str
    params: dict
    thought: str = ""
    located: Optional[LocatedElement] = None
    raw_response: str = ""


# ── Coordinate math ───────────────────────────────────────────────────────────

def bbox_to_pixels(bbox, viewport_w, viewport_h):
    x1, y1, x2, y2 = bbox[0], bbox[1], bbox[2], bbox[3]
    px1 = round((x1 * viewport_w) / 1000)
    py1 = round((y1 * viewport_h) / 1000)
    px2 = round((x2 * viewport_w) / 1000)
    py2 = round((y2 * viewport_h) / 1000)
    return px1, py1, px2, py2


def bbox_center(px1, py1, px2, py2):
    return (px1 + px2) // 2, (py1 + py2) // 2


# ── Response parser ───────────────────────────────────────────────────────────

def extract_tag(text: str, tag: str) -> Optional[str]:
    m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", text, re.DOTALL | re.IGNORECASE)
    return m.group(1).strip() if m else None


def parse_response(response_text: str, viewport_w: int, viewport_h: int) -> Optional[ParsedAction]:
    thought     = extract_tag(response_text, "thought") or ""
    action_type = extract_tag(response_text, "action-type")
    param_json  = extract_tag(response_text, "action-param-json")

    if not action_type:
        return None

    action_type = action_type.strip()

    try:
        params = json.loads(param_json) if param_json else {}
    except json.JSONDecodeError:
        params = {}

    located = None
    if action_type in ("Tap", "DoubleClick", "RightClick", "Hover") and "locate" in params:
        loc      = params["locate"]
        bbox_raw = loc.get("bbox", [0, 0, 100, 100])
        px1, py1, px2, py2 = bbox_to_pixels(bbox_raw, viewport_w, viewport_h)
        cx, cy   = bbox_center(px1, py1, px2, py2)
        located  = LocatedElement(
            prompt=loc.get("prompt", ""),
            bbox=bbox_raw,
            center_x=cx,
            center_y=cy,
        )

    return ParsedAction(
        action_type=action_type,
        params=params,
        thought=thought,
        located=located,
        raw_response=response_text,
    )


# ── Screenshot optimizer ──────────────────────────────────────────────────────

def resize_screenshot(screenshot_b64: str, max_px: int = 1024) -> str:
    """Resize + convert screenshot to JPEG base64."""
    img_bytes = base64.b64decode(screenshot_b64)
    img = Image.open(BytesIO(img_bytes)).convert("RGB")
    w, h = img.size
    if max(w, h) > max_px:
        scale = max_px / max(w, h)
        img   = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=60)
    return base64.b64encode(buf.getvalue()).decode()


# ── Conversation builders (OpenAI message format) ─────────────────────────────

def build_first_turn(task: str, screenshot_b64: str) -> dict:
    screenshot_b64 = resize_screenshot(screenshot_b64)
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": f"Task: {task}\n\nHere is the current screenshot. What is the first action to take?"},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{screenshot_b64}"}},
        ],
    }


def build_continuation_turn(step: int, screenshot_b64: str, last_action: str, last_result: str) -> dict:
    screenshot_b64 = resize_screenshot(screenshot_b64)
    result_note    = f"Result: {last_result}" if last_result else "Action executed."
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": f"Step {step}: I executed '{last_action}'. {result_note}\nHere is the updated screenshot. What is the next action?"},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{screenshot_b64}"}},
        ],
    }


def build_retry_turn(step: int, screenshot_b64: str, error: str) -> dict:
    screenshot_b64 = resize_screenshot(screenshot_b64)
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": f"Step {step}: The previous action FAILED with error: {error}\nHere is the current screenshot. Try a different approach."},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{screenshot_b64}"}},
        ],
    }


def build_assistant_turn(text: str) -> dict:
    return {"role": "assistant", "content": text}


# ── Visual assertion ──────────────────────────────────────────────────────────

async def check_assertion(screenshot_b64: str, condition: str) -> tuple[bool, str]:
    """
    Ask the AI whether `condition` is visually true in the screenshot.
    Returns (passed: bool, reason: str).
    """
    screenshot_b64 = resize_screenshot(screenshot_b64)
    client = get_client()

    response = await _call_with_retry(
        lambda: client.chat.completions.create(
            model=OPENROUTER_MODEL,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": (
                        f"Look at this screenshot carefully.\n"
                        f"Is the following statement TRUE or FALSE?\n\n"
                        f"Statement: {condition}\n\n"
                        f"Reply with exactly one line: 'TRUE: <brief reason>' or 'FALSE: <brief reason>'"
                    )},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{screenshot_b64}"}},
                ],
            }],
            temperature=0,
            max_tokens=100,
            timeout=AI_TIMEOUT,
        ),
        label="check_assertion",
    )

    text   = response.choices[0].message.content or ""
    passed = text.upper().startswith("TRUE")
    reason = text.split(":", 1)[-1].strip() if ":" in text else text
    return passed, reason


# ── Multi-turn call ───────────────────────────────────────────────────────────

async def call_ai(conversation: list, platform: str = "web") -> tuple[str, dict]:
    """
    Call the AI with full conversation history (list of OpenAI message dicts).
    Returns (response_text, assistant_turn_to_append).
    """
    client = get_client()

    # Trim: keep first turn + most recent turns
    trimmed = conversation
    if len(conversation) > CONV_LIMIT:
        trimmed = [conversation[0]] + conversation[-(CONV_LIMIT - 1):]

    messages = [{"role": "system", "content": get_system_prompt(platform)}] + trimmed

    logger.debug("call_ai: %d turns, model=%s", len(messages), OPENROUTER_MODEL)
    response = await _call_with_retry(
        lambda: client.chat.completions.create(
            model=OPENROUTER_MODEL,
            messages=messages,
            temperature=0.1,
            max_tokens=1024,
            timeout=AI_TIMEOUT,
        ),
        label="call_ai",
    )

    text           = response.choices[0].message.content or ""
    assistant_turn = build_assistant_turn(text)
    return text, assistant_turn


# ── Legacy alias ──────────────────────────────────────────────────────────────

async def call_gemini(conversation: list, platform: str = "web") -> tuple[str, dict]:
    """Backwards-compatible alias for call_ai."""
    return await call_ai(conversation, platform)
