"""
Retry with exponential backoff.

Use for any operation that can transiently fail:
  - Bridge API calls (bridge might be briefly unavailable)
  - ADB commands (USB connection hiccup)
  - Element resolution (element not rendered yet)
  - Screenshot capture
"""

import asyncio
import time
from typing import Callable, TypeVar, Any

T = TypeVar("T")


# ── Async retry ───────────────────────────────────────────────────────────────

async def async_retry(
    fn: Callable,
    max_attempts: int = 3,
    backoff: float = 0.5,
    label: str = "",
) -> tuple[bool, Any, str]:
    """
    Retry an async callable up to max_attempts times.
    Returns (success, result, last_error_message).

    Usage:
        ok, screenshot, err = await async_retry(
            lambda: take_screenshot(),
            max_attempts=3,
            label="screenshot"
        )
    """
    last_error = ""
    for attempt in range(1, max_attempts + 1):
        try:
            result = await fn()
            return True, result, ""
        except Exception as e:
            last_error = str(e)
            if attempt < max_attempts:
                await asyncio.sleep(backoff * attempt)

    name = f"'{label}' " if label else ""
    return False, None, f"{name}failed after {max_attempts} attempts: {last_error}"


# ── Sync retry ────────────────────────────────────────────────────────────────

def sync_retry(
    fn: Callable,
    max_attempts: int = 3,
    backoff: float = 0.5,
    label: str = "",
) -> tuple[bool, Any, str]:
    """
    Retry a sync callable up to max_attempts times.
    Returns (success, result, last_error_message).

    Usage:
        ok, result, err = sync_retry(
            lambda: bridge("/tap", {"x": 196, "y": 400}),
            max_attempts=3,
            label="tap login button"
        )
    """
    last_error = ""
    for attempt in range(1, max_attempts + 1):
        try:
            result = fn()
            # Treat bridge errors as failures worth retrying
            if isinstance(result, dict) and not result.get("ok", True):
                raise RuntimeError(result.get("error", "bridge returned ok=false"))
            return True, result, ""
        except Exception as e:
            last_error = str(e)
            if attempt < max_attempts:
                time.sleep(backoff * attempt)

    name = f"'{label}' " if label else ""
    return False, None, f"{name}failed after {max_attempts} attempts: {last_error}"
