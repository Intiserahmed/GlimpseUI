"""
Self-healing: fix a stale cached step without re-running the full AI loop.

When a cached script step fails (element not found), the healer tries to
locate the element using cheaper strategies before escalating to AI.

Healing cost:
  Structural fallbacks  →  $0      (accessibility tree queries)
  AI vision heal        →  ~$0.001 (one targeted API call, result cached)

Usage:
    from agent.healer import heal_step

    healed = await heal_step(broken_step, platform="ios",
                              bridge_url="http://localhost:22087",
                              browser=browser_session)
    if healed:
        broken_step.update(healed)   # update cache
"""

import base64
from typing import Optional

from .mobile_element import resolve_element, _get_tree, _center
from .config import GEMINI_MODEL, GEMINI_API_KEY


# ── iOS / Android healer ──────────────────────────────────────────────────────

async def heal_step(
    step:          dict,
    platform:      str  = "ios",
    bridge_url:    str  = "http://localhost:22087",
    browser=None,                   # BrowserSession (web only)
    device_serial: str  = None,     # Android only
) -> Optional[dict]:
    """
    Try to heal a failed step.
    Returns an updated step dict with working selectors, or None if healing failed.
    """
    if platform == "web":
        return await _heal_web(step, browser)
    return await _heal_mobile(step, platform, bridge_url, device_serial)


# ── Web healing ───────────────────────────────────────────────────────────────

async def _heal_web(step: dict, browser) -> Optional[dict]:
    """
    Try CSS selector variants, then text-based selectors,
    then AI vision as a last resort.
    """
    if not browser:
        return None

    page     = await browser.get_current_page()
    selector = step.get("selector", "")
    label    = step.get("label", "")

    # Try selector variants
    for variant in _web_selector_variants(selector, label):
        try:
            await page.wait_for_selector(variant, timeout=2000)
            return {**step, "selector": variant}
        except Exception:
            continue

    # Try text-based selector
    if label:
        for text_sel in [f"text={label}", f"[aria-label='{label}']",
                         f"[placeholder='{label}']", f"[title='{label}']"]:
            try:
                await page.wait_for_selector(text_sel, timeout=2000)
                return {**step, "selector": text_sel}
            except Exception:
                continue

    # AI vision: locate element by description
    if label:
        new_selector = await _ai_locate_web(label, page, browser)
        if new_selector:
            return {**step, "selector": new_selector}

    return None


def _web_selector_variants(selector: str, label: str = "") -> list[str]:
    """Generate fallback selectors from a broken one."""
    variants = []

    if "data-testid" in selector:
        # Test ID changed — try text match
        value = selector.split("=")[-1].strip("\"']")
        variants += [f"text={value}", f"[aria-label='{value}']"]

    if selector.startswith("#"):
        # ID changed — try class or tag
        tag_guess = "button" if "btn" in selector.lower() else "input"
        variants.append(tag_guess)

    return variants


async def _ai_locate_web(label: str, page, browser) -> Optional[str]:
    """
    One AI call: find the CSS selector for `label` in the current page.
    Returns selector string or None.
    """
    from .session_manager import take_screenshot_b64
    from .planner import resize_screenshot
    from google import genai
    from google.genai import types

    try:
        screenshot_b64   = await take_screenshot_b64(browser)
        screenshot_b64   = resize_screenshot(screenshot_b64)
        screenshot_bytes = base64.b64decode(screenshot_b64)

        client   = genai.Client(api_key=GEMINI_API_KEY)
        response = await client.aio.models.generate_content(
            model=GEMINI_MODEL,
            contents=[types.Content(role="user", parts=[
                types.Part(text=(
                    f"Look at this screenshot of a web page.\n"
                    f"Find the element described as: '{label}'\n"
                    f"Reply with ONLY a CSS selector that would uniquely identify it.\n"
                    f"Prefer: data-testid > id > aria-label > text content.\n"
                    f"If not found, reply: NOT_FOUND"
                )),
                types.Part.from_bytes(data=screenshot_bytes, mime_type="image/jpeg"),
            ])],
            config=types.GenerateContentConfig(temperature=0, max_output_tokens=50),
        )

        result = response.candidates[0].content.parts[0].text.strip()
        if result and result != "NOT_FOUND" and not result.startswith("NOT"):
            return result
    except Exception:
        pass

    return None


# ── Mobile healing ────────────────────────────────────────────────────────────

async def _heal_mobile(
    step:          dict,
    platform:      str,
    bridge_url:    str,
    device_serial: str = None,
) -> Optional[dict]:
    """
    Try multi-strategy resolution first, then AI vision heal.
    Returns updated step dict or None.
    """
    # multi-strategy already tried by executor before calling healer,
    # so here we go straight to AI vision.
    healed = await _ai_locate_mobile(step, platform, bridge_url, device_serial)
    return healed


async def _ai_locate_mobile(
    step:          dict,
    platform:      str,
    bridge_url:    str,
    device_serial: str = None,
) -> Optional[dict]:
    """
    One AI call: find the element in the current screenshot.
    Returns updated step dict with new coords + accessibilityId, or None.
    """
    import requests as req
    from .planner import resize_screenshot
    from google import genai
    from google.genai import types

    label = step.get("label", step.get("accessibilityId", "unknown element"))

    # Take screenshot
    try:
        if platform == "ios":
            r  = req.post(f"{bridge_url}/screenshot", timeout=5)
            ss = r.json().get("screenshot", "")
        else:
            import subprocess, tempfile
            subprocess.run(["adb", "shell", "screencap", "-p", "/sdcard/heal.png"],
                           capture_output=True)
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
                tmppath = f.name
            subprocess.run(["adb", "pull", "/sdcard/heal.png", tmppath],
                           capture_output=True)
            from PIL import Image
            from io import BytesIO
            img = Image.open(tmppath).convert("RGB")
            buf = BytesIO()
            img.save(buf, format="JPEG", quality=60)
            ss  = base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return None

    if not ss:
        return None

    ss_bytes = base64.b64decode(resize_screenshot(ss))

    try:
        client   = genai.Client(api_key=GEMINI_API_KEY)
        response = await client.aio.models.generate_content(
            model=GEMINI_MODEL,
            contents=[types.Content(role="user", parts=[
                types.Part(text=(
                    f"Look at this mobile screenshot.\n"
                    f"Find the element: '{label}'\n"
                    f"Reply with ONLY two numbers: the x and y pixel coordinates "
                    f"of the CENTER of that element.\n"
                    f"Format: x,y\n"
                    f"If not found, reply: NOT_FOUND"
                )),
                types.Part.from_bytes(data=ss_bytes, mime_type="image/jpeg"),
            ])],
            config=types.GenerateContentConfig(temperature=0, max_output_tokens=20),
        )

        text = response.candidates[0].content.parts[0].text.strip()
        if text and text != "NOT_FOUND" and "," in text:
            parts = text.split(",")
            cx, cy = int(parts[0].strip()), int(parts[1].strip())
            # Also try to find accessibilityId at these coords
            tree   = _get_tree(platform, bridge_url, device_serial)
            acc_id = _find_id_near(tree, cx, cy)
            healed = {**step, "coords": [cx, cy]}
            if acc_id:
                healed["accessibilityId"] = acc_id
            return healed
    except Exception:
        pass

    return None


def _find_id_near(elements: list, cx: int, cy: int, radius: int = 40) -> Optional[str]:
    """Find the accessibilityId of the element closest to (cx, cy)."""
    best_dist, best_id = float("inf"), None
    for el in elements:
        ecx = int(el["x"] + el["w"] / 2)
        ecy = int(el["y"] + el["h"] / 2)
        dist = abs(ecx - cx) + abs(ecy - cy)
        if dist < radius and dist < best_dist and el.get("identifier"):
            best_dist, best_id = dist, el["identifier"]
    return best_id
