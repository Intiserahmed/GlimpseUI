"""
Central configuration — single source of truth for AI model and API key.

Supports OpenRouter (recommended) via OpenAI-compatible API.
Falls back to GEMINI_API_KEY / GEMINI_MODEL env vars for backwards compat.
"""

import os

# ── OpenRouter (primary) ──────────────────────────────────────────────────────
# Accept OPENROUTER_API_KEY; fall back to GEMINI_API_KEY so existing .env files
# that already have the OpenRouter key stored under the old name still work.
OPENROUTER_API_KEY  = os.getenv("OPENROUTER_API_KEY",  os.getenv("GEMINI_API_KEY", ""))
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
OPENROUTER_MODEL    = os.getenv("OPENROUTER_MODEL",    os.getenv("GEMINI_MODEL", "google/gemini-2.0-flash-exp:free"))

# ── Legacy aliases (so old imports keep working) ──────────────────────────────
GEMINI_API_KEY = OPENROUTER_API_KEY
GEMINI_MODEL   = OPENROUTER_MODEL
