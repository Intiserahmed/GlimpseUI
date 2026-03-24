"""
Shared BrowserSession singleton.

Both vision runner (loop.py) and DOM runner (dom_runner.py) share one browser
instance through this module. State (cookies, login, current page, tabs) is
preserved across mode switches.

Usage:
    from agent.session_manager import get_session, reset_session

    session = await get_session()           # creates or returns existing
    page    = await session.get_current_page()
    await reset_session()                   # close + recreate
"""

import asyncio
import base64
import os
from io import BytesIO
from typing import Optional

from browser_use import BrowserSession
from PIL import Image

from .logger import get_logger

logger = get_logger(__name__)

_session: Optional[BrowserSession] = None
_lock = asyncio.Lock()

# ── Config ────────────────────────────────────────────────────────────────────

VIEWPORT = {"width": 1280, "height": 800}
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


async def get_session() -> BrowserSession:
    """Return the shared BrowserSession, creating it if needed."""
    global _session
    async with _lock:
        if _session is not None and not _session.is_cdp_connected:
            # Stale session — stop it before replacing to avoid leaking browser process
            logger.warning("Browser session disconnected — restarting")
            try:
                await _session.stop()
            except Exception:
                pass
            _session = None
        if _session is None:
            logger.info("Creating new browser session")
            _session = await _create_session()
    return _session


async def reset_session():
    """Close the current session and force a fresh one next call."""
    global _session
    async with _lock:
        if _session is not None:
            try:
                await _session.stop()
            except Exception:
                pass
            _session = None


async def _create_session() -> BrowserSession:
    session = BrowserSession(
        headless=True,
        keep_alive=True,
        screen={"width": VIEWPORT["width"], "height": VIEWPORT["height"]},
        user_agent=USER_AGENT,
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-blink-features=AutomationControlled",
            "--disable-features=IsolateOrigins,site-per-process",
            "--no-proxy-server",
        ],
        wait_between_actions=0.3,
        minimum_wait_page_load_time=0.3,
        wait_for_network_idle_page_load_time=2.0,
    )
    await session.start()
    return session


# ── Screenshot helper (JPEG, resized) ────────────────────────────────────────

async def take_screenshot_b64(session: BrowserSession, max_px: int = 1024) -> str:
    """Take screenshot from shared session, return resized JPEG base64."""
    png_bytes = await session.take_screenshot(format="png")
    img = Image.open(BytesIO(png_bytes)).convert("RGB")
    w, h = img.size
    logger.debug("screenshot raw=%dx%d viewport=%s", w, h, VIEWPORT)
    if max(w, h) > max_px:
        scale = max_px / max(w, h)
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        logger.debug("screenshot resized=%dx%d scale=%.3f", int(w*scale), int(h*scale), scale)
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=60)
    return base64.b64encode(buf.getvalue()).decode()


def viewport_size(session: BrowserSession) -> tuple[int, int]:
    # Return the configured viewport — source of truth since we set it on creation.
    # browser-use doesn't expose a sync viewport getter; any resize goes through here.
    return VIEWPORT["width"], VIEWPORT["height"]


async def viewport_size_live(session: BrowserSession) -> tuple[int, int]:
    """Read actual rendered viewport from the live page (catches zoom/resize edge cases)."""
    try:
        page = await session.get_current_page()
        dims = await page.evaluate("() => [window.innerWidth, window.innerHeight]")
        if dims and dims[0] > 0:
            return int(dims[0]), int(dims[1])
    except Exception:
        pass
    return VIEWPORT["width"], VIEWPORT["height"]
