"""
Condition-based waiting for mobile platforms.

Replaces every hardcoded sleep() in the codebase with smart waits that
check actual device state. This is the single biggest fix for flakiness.

Usage:
    from agent.mobile_wait import wait_for_stable, wait_for_element

    bridge("/tap", {"x": 196, "y": 400})
    wait_for_stable()                    # replaces time.sleep(0.8)

    el = wait_for_element("Sign In")     # replaces sleep(1.0) + hope
"""

import hashlib
import time
from typing import Optional

import requests

BRIDGE_URL = "http://localhost:22087"

# How many consecutive identical tree snapshots = "stable"
STABLE_REQUIRED = 2
# Interval between tree polls
POLL_INTERVAL   = 0.15


# ── Tree helpers ──────────────────────────────────────────────────────────────

def _get_tree(bridge_url: str = BRIDGE_URL) -> list:
    try:
        r = requests.post(f"{bridge_url}/viewHierarchy", timeout=5)
        return r.json().get("elements", [])
    except Exception:
        return []


def _tree_hash(elements: list) -> str:
    """Fingerprint the accessibility tree by element labels + positions."""
    key = str(sorted([
        (e.get("label", ""), e.get("identifier", ""),
         round(e.get("x", 0) / 10) * 10,   # round to 10px grid
         round(e.get("y", 0) / 10) * 10)
        for e in elements
    ]))
    return hashlib.md5(key.encode()).hexdigest()


# ── Public wait functions ─────────────────────────────────────────────────────

def wait_for_stable(
    timeout: float = 4.0,
    bridge_url: str = BRIDGE_URL,
) -> bool:
    """
    Wait until the accessibility tree stops changing.
    Use this after EVERY tap/swipe instead of sleep().

    Returns True when stable, False if timed out (test continues anyway).
    """
    prev_hash    = None
    stable_count = 0
    deadline     = time.time() + timeout

    while time.time() < deadline:
        tree = _get_tree(bridge_url)
        h    = _tree_hash(tree)

        if h == prev_hash:
            stable_count += 1
            if stable_count >= STABLE_REQUIRED:
                return True
        else:
            stable_count = 0

        prev_hash = h
        time.sleep(POLL_INTERVAL)

    return False  # timed out — caller continues


def wait_for_element(
    label: str,
    timeout: float = 5.0,
    bridge_url: str = BRIDGE_URL,
) -> Optional[dict]:
    """
    Wait until an element with this label appears.
    Use before tapping something that may take time to load.

    Returns the element dict, or None if not found within timeout.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        tree = _get_tree(bridge_url)
        for el in tree:
            if label.lower() in el.get("label", "").lower():
                return el
            if label.lower() in el.get("identifier", "").lower():
                return el
        time.sleep(POLL_INTERVAL)
    return None


def wait_for_screen_change(
    prev_hash: str,
    timeout: float = 5.0,
    bridge_url: str = BRIDGE_URL,
) -> bool:
    """
    Wait until the screen is different from prev_hash.
    Use to confirm a tap actually triggered a navigation.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        tree = _get_tree(bridge_url)
        if _tree_hash(tree) != prev_hash:
            return True
        time.sleep(POLL_INTERVAL)
    return False


def current_screen_hash(bridge_url: str = BRIDGE_URL) -> str:
    """Get a fingerprint of the current screen state."""
    return _tree_hash(_get_tree(bridge_url))


def wait_for_element_gone(
    label: str,
    timeout: float = 5.0,
    bridge_url: str = BRIDGE_URL,
) -> bool:
    """
    Wait until a loading indicator or modal disappears.
    E.g. wait_for_element_gone("Loading...")
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        tree = _get_tree(bridge_url)
        found = any(
            label.lower() in el.get("label", "").lower()
            for el in tree
        )
        if not found:
            return True
        time.sleep(POLL_INTERVAL)
    return False
