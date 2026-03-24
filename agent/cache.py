"""
Compile-once script cache.

AI runs ONCE per new task → generates a deterministic action script →
saved to tests/.cache/<fingerprint>.json.

All subsequent CI runs load the cached script and execute it for free.
Cache is committed to git so new devs get deterministic execution immediately.

Cache entry format:
  {
    "task":     "Enter email and tap login",
    "url":      "https://app.example.com",
    "platform": "ios",
    "steps": [
      {"action": "tap",      "accessibilityId": "email-field",
       "label": "Email",     "coords": [196, 320]},
      {"action": "type",     "text": "admin@example.com"},
      {"action": "tap",      "accessibilityId": "login-button",
       "label": "Sign In",   "coords": [196, 480]},
      {"action": "wait_stable"}
    ]
  }
"""

import hashlib
import json
import os
from pathlib import Path
from typing import Optional

CACHE_DIR = Path(os.getenv("GLIMPSEUI_CACHE_DIR", "tests/.cache"))


# ── Key ───────────────────────────────────────────────────────────────────────

def _key(task: str, url: str, platform: str) -> str:
    """Stable fingerprint for a (task, url, platform) triple."""
    raw = f"{platform}|{url.rstrip('/')}|{task.strip().lower()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _path(task: str, url: str, platform: str) -> Path:
    return CACHE_DIR / f"{platform}_{_key(task, url, platform)}.json"


# ── Public API ────────────────────────────────────────────────────────────────

def load(task: str, url: str = "", platform: str = "web") -> Optional[list[dict]]:
    """
    Load a cached script for this task.
    Returns list of step dicts, or None if not cached yet.
    """
    p = _path(task, url, platform)
    if p.exists():
        try:
            data = json.loads(p.read_text())
            return data.get("steps", [])
        except Exception:
            return None
    return None


def save(task: str, steps: list[dict], url: str = "", platform: str = "web"):
    """
    Persist a compiled script to disk.
    Creates the cache directory if needed.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    p = _path(task, url, platform)
    p.write_text(json.dumps({
        "task":     task,
        "url":      url,
        "platform": platform,
        "steps":    steps,
    }, indent=2))


def invalidate(task: str, url: str = "", platform: str = "web"):
    """Remove a cached script so it gets re-compiled on next run."""
    p = _path(task, url, platform)
    p.unlink(missing_ok=True)


def invalidate_all(platform: str = None):
    """Wipe the entire cache (or all entries for one platform)."""
    if not CACHE_DIR.exists():
        return
    for p in CACHE_DIR.glob("*.json"):
        if platform is None or p.name.startswith(f"{platform}_"):
            p.unlink()


def list_entries() -> list[dict]:
    """Return summary of all cached scripts (for debugging)."""
    entries = []
    if not CACHE_DIR.exists():
        return entries
    for p in sorted(CACHE_DIR.glob("*.json")):
        try:
            data = json.loads(p.read_text())
            entries.append({
                "file":     p.name,
                "task":     data.get("task", ""),
                "platform": data.get("platform", ""),
                "steps":    len(data.get("steps", [])),
            })
        except Exception:
            pass
    return entries
