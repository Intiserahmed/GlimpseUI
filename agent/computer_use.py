"""
Google Gemini Computer Use API — web browser automation.

Uses Gemini with ComputerUseTool (ENVIRONMENT_BROWSER).
Returns structured FunctionCall objects instead of XML.
"""

import asyncio
import base64
from typing import AsyncGenerator

from google import genai
from google.genai import types

from .config import GEMINI_MODEL, GEMINI_API_KEY

MODEL = GEMINI_MODEL


# ── Client ────────────────────────────────────────────────────────────────────

def get_client() -> genai.Client:
    return genai.Client(api_key=GEMINI_API_KEY)


def get_config() -> types.GenerateContentConfig:
    return types.GenerateContentConfig(
        tools=[types.Tool(
            computer_use=types.ComputerUse(
                environment=types.Environment.ENVIRONMENT_BROWSER,
            )
        )],
        temperature=0,
        max_output_tokens=2048,
    )


# ── Content builders ──────────────────────────────────────────────────────────

def make_initial_content(task: str, screenshot_b64: str, url: str = "") -> list:
    screenshot_bytes = base64.b64decode(screenshot_b64)
    return [
        types.Content(
            role="user",
            parts=[
                types.Part(text=f"Task: {task}\nCurrent URL: {url}"),
                types.Part.from_bytes(data=screenshot_bytes, mime_type="image/png"),
            ],
        )
    ]


def make_function_response(
    func_name: str,
    screenshot_b64: str,
    url: str,
    error: str = "",
) -> types.Content:
    screenshot_bytes = base64.b64decode(screenshot_b64)
    response_data = {"url": url, "status": "error" if error else "success"}
    if error:
        response_data["error"] = error

    return types.Content(
        role="user",
        parts=[
            types.Part(
                function_response=types.FunctionResponse(
                    name=func_name,
                    response=response_data,
                )
            ),
            types.Part.from_bytes(data=screenshot_bytes, mime_type="image/png"),
        ],
    )


# ── Response helpers ──────────────────────────────────────────────────────────

def get_function_calls(response) -> list:
    calls = []
    for part in response.candidates[0].content.parts:
        if hasattr(part, "function_call") and part.function_call:
            calls.append(part.function_call)
    return calls


def get_thought(response) -> str:
    for part in response.candidates[0].content.parts:
        if hasattr(part, "text") and part.text:
            return part.text.strip()
    return ""


def has_function_calls(response) -> bool:
    return bool(get_function_calls(response))


# ── Action executor (FunctionCall → Playwright) ───────────────────────────────

async def execute_computer_use_action(
    page,
    func_call,
    viewport_w: int,
    viewport_h: int,
) -> tuple[bool, str]:
    """
    Execute a Computer Use FunctionCall via browser_use actor Page (CDP-based).
    Coordinates are 0-999 normalized → scale to actual pixels.
    Returns (ok, error_message).
    """
    name = func_call.name
    args = dict(func_call.args) if func_call.args else {}

    def scale_x(x): return int(x / 1000 * viewport_w)
    def scale_y(y): return int(y / 1000 * viewport_h)

    try:
        if name == "open_web_browser":
            pass  # browser already open

        elif name == "navigate":
            url = args.get("url", "")
            await page.goto(url)
            await asyncio.sleep(1.5)

        elif name == "click_at":
            x, y  = scale_x(args["x"]), scale_y(args["y"])
            mouse = await page.mouse
            await mouse.click(x, y)
            await asyncio.sleep(0.3)  # let UI react after tap

        elif name == "type_text_at":
            x, y  = scale_x(args["x"]), scale_y(args["y"])
            text  = args.get("text", "")
            clear = args.get("clear_before_typing", False)
            mouse = await page.mouse
            if clear:
                await mouse.click(x, y, click_count=3)
            else:
                await mouse.click(x, y)
            await asyncio.sleep(0.1)
            session_id = await page._ensure_session()
            await page._client.send.Input.insertText({"text": text}, session_id=session_id)
            if args.get("press_enter", False):
                await page.press("Return")

        elif name == "hover_at":
            x, y  = scale_x(args["x"]), scale_y(args["y"])
            mouse = await page.mouse
            await mouse.move(x, y)

        elif name == "scroll_document":
            direction = args.get("direction", "down")
            delta = {"down": (0, 300), "up": (0, -300),
                     "left": (-300, 0), "right": (300, 0)}.get(direction, (0, 300))
            mouse = await page.mouse
            await mouse.scroll(delta_x=delta[0], delta_y=delta[1])

        elif name == "scroll_at":
            x, y      = scale_x(args["x"]), scale_y(args["y"])
            direction  = args.get("direction", "down")
            magnitude  = args.get("magnitude", 1)
            delta_map  = {"down": (0, 150), "up": (0, -150),
                          "left": (-150, 0), "right": (150, 0)}
            dx, dy = delta_map.get(direction, (0, 150))
            mouse  = await page.mouse
            await mouse.move(x, y)
            await mouse.scroll(delta_x=dx * magnitude, delta_y=dy * magnitude)

        elif name == "key_combination":
            await page.press(args.get("keys", ""))

        elif name == "go_back":
            await page.go_back()
            await asyncio.sleep(1.0)

        elif name == "go_forward":
            await page.go_forward()
            await asyncio.sleep(1.0)

        elif name == "wait_5_seconds":
            await asyncio.sleep(5)

        elif name == "drag_and_drop":
            x, y   = scale_x(args["x"]), scale_y(args["y"])
            dx, dy = scale_x(args["destination_x"]), scale_y(args["destination_y"])
            mouse  = await page.mouse
            await mouse.move(x, y)
            await mouse.down()
            await asyncio.sleep(0.1)
            await mouse.move(dx, dy)
            await mouse.up()

        else:
            return False, f"Unknown action: {name}"

        return True, ""

    except Exception as e:
        return False, str(e)


# ── Map FunctionCall → our UI event format ────────────────────────────────────

def func_call_to_ui_event(func_call, viewport_w: int, viewport_h: int) -> dict:
    """Convert a FunctionCall to the format expected by the web UI."""
    name = func_call.name
    args = dict(func_call.args) if func_call.args else {}

    def scale_x(x): return int(x / 1000 * viewport_w)
    def scale_y(y): return int(y / 1000 * viewport_h)

    # Map Computer Use action names → our action names for the UI
    action_map = {
        "click_at":       "Tap",
        "type_text_at":   "Type",
        "navigate":       "Navigate",
        "scroll_document":"Scroll",
        "scroll_at":      "Scroll",
        "key_combination":"KeyPress",
        "hover_at":       "Hover",
        "go_back":        "KeyPress",
        "go_forward":     "KeyPress",
        "wait_5_seconds": "Wait",
        "drag_and_drop":  "Drag",
        "open_web_browser": "Navigate",
    }

    action = action_map.get(name, name)
    params = {}
    located = None

    if name == "click_at":
        cx, cy = scale_x(args.get("x", 500)), scale_y(args.get("y", 500))
        located = {
            "prompt": f"({args.get('x')}, {args.get('y')})",
            "bbox":   [args.get("x",0)-20, args.get("y",0)-10,
                       args.get("x",0)+20, args.get("y",0)+10],
            "cx": cx, "cy": cy,
        }

    elif name == "type_text_at":
        params = {"text": args.get("text", "")}
        cx, cy = scale_x(args.get("x", 500)), scale_y(args.get("y", 500))
        located = {
            "prompt": f"text field at ({args.get('x')}, {args.get('y')})",
            "bbox":   [args.get("x",0)-50, args.get("y",0)-10,
                       args.get("x",0)+50, args.get("y",0)+10],
            "cx": cx, "cy": cy,
        }

    elif name == "navigate":
        params = {"url": args.get("url", "")}

    elif name in ("scroll_document", "scroll_at"):
        params = {"direction": args.get("direction", "down"), "amount": args.get("magnitude", 1)}

    elif name == "key_combination":
        params = {"key": args.get("keys", "")}

    elif name == "hover_at":
        cx, cy = scale_x(args.get("x", 500)), scale_y(args.get("y", 500))
        located = {"prompt": "element", "bbox": [args.get("x",0)-20, args.get("y",0)-10,
                                                  args.get("x",0)+20, args.get("y",0)+10],
                   "cx": cx, "cy": cy}

    elif name == "wait_5_seconds":
        params = {"ms": 5000}

    elif name == "go_back":
        params = {"key": "Alt+Left"}

    elif name == "go_forward":
        params = {"key": "Alt+Right"}

    return {"action": action, "params": params, "located": located, "raw_name": name}
