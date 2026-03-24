"""
Playwright headless browser control.
Handles launch, screenshot, and all action execution.
"""

import asyncio
import base64
from typing import Optional
from playwright.async_api import async_playwright, Browser, Page, BrowserContext

from .planner import ParsedAction


# ── Action delays (ms) ────────────────────────────────────────────────────────

ACTION_DELAYS = {
    "Tap":         400,
    "DoubleClick": 500,
    "RightClick":   30,
    "Hover":        30,
    "Type":         80,
    "Navigate":    300,
    "KeyPress":    150,
    "Scroll":      100,
    "Wait":          0,   # handled by params
    "Finished":      0,
}

# ── Browser manager ───────────────────────────────────────────────────────────

class BrowserManager:
    def __init__(self):
        self._playwright = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None

    async def launch(self):
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--headless=new",
            ]
        )
        self._context = await self._browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        self._page = await self._context.new_page()

    async def close(self):
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    @property
    def page(self) -> Page:
        return self._page

    async def screenshot_b64(self) -> str:
        """Take screenshot and return as base64 JPEG."""
        png_bytes = await self._page.screenshot(type="png")
        # Convert PNG → JPEG for smaller size
        from PIL import Image
        from io import BytesIO
        img = Image.open(BytesIO(png_bytes)).convert("RGB")
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=70)
        return base64.b64encode(buf.getvalue()).decode()

    def viewport_size(self) -> tuple[int, int]:
        vp = self._page.viewport_size
        return vp["width"], vp["height"]

    async def wait_for_stable(self):
        """Wait for page network to settle."""
        try:
            await self._page.wait_for_load_state("networkidle", timeout=3000)
        except Exception:
            pass  # timeout is fine, just move on


# ── Action executor ───────────────────────────────────────────────────────────

async def execute_action(page: Page, action: ParsedAction, browser_session=None) -> dict:
    """Execute a parsed action on the page. Returns result dict."""
    t = action.action_type
    p = action.params

    try:
        if t == "Tap":
            loc = action.located
            await page.mouse.click(loc.center_x, loc.center_y)
            return {"ok": True, "x": loc.center_x, "y": loc.center_y}

        elif t == "DoubleClick":
            loc = action.located
            await page.mouse.dblclick(loc.center_x, loc.center_y)
            return {"ok": True}

        elif t == "RightClick":
            loc = action.located
            await page.mouse.click(loc.center_x, loc.center_y, button="right")
            return {"ok": True}

        elif t == "Hover":
            loc = action.located
            await page.mouse.move(loc.center_x, loc.center_y)
            return {"ok": True}

        elif t == "Type":
            text = p.get("text", "")
            await page.keyboard.type(text, delay=30)
            return {"ok": True, "text": text}

        elif t == "Navigate":
            url = p.get("url", "")
            if browser_session is not None:
                await browser_session.navigate_to(url)
            else:
                await page.goto(url, wait_until="domcontentloaded", timeout=15000)
            return {"ok": True, "url": url}

        elif t == "KeyPress":
            key = p.get("key", "Enter")
            modifiers = p.get("modifiers", [])
            # Build Playwright key combo
            mod_map = {"Control": "Control", "Shift": "Shift", "Alt": "Alt", "Meta": "Meta"}
            combo = "+".join([mod_map.get(m, m) for m in modifiers] + [key])
            await page.keyboard.press(combo)
            return {"ok": True, "key": combo}

        elif t == "Scroll":
            direction = p.get("direction", "down")
            amount = p.get("amount", 3)
            delta = amount * 150 * (1 if direction == "down" else -1)
            await page.mouse.wheel(0, delta)
            return {"ok": True, "direction": direction, "amount": amount}

        elif t == "Wait":
            ms = p.get("ms", 1000)
            await asyncio.sleep(ms / 1000)
            return {"ok": True, "waited_ms": ms}

        elif t == "Finished":
            return {
                "ok": True,
                "finished": True,
                "success": p.get("success", True),
                "message": p.get("message", "Task completed"),
            }

        else:
            return {"ok": False, "error": f"Unknown action: {t}"}

    except Exception as e:
        return {"ok": False, "error": str(e)}
