# GlimpseUI

AI-powered cross-platform UI testing. Describe a task in plain English — the agent navigates the UI, records every action once, then replays from a deterministic script with zero AI cost on every future run.

```
"Go to google.com and search for AI news"
→ compiles once → ⚡ cached forever
```

<video src="https://github.com/Intiserahmed/GlimpseUI/raw/main/demo.mp4" autoplay loop muted playsinline width="100%"></video>

---

## Why vision-based testing?

Traditional tools (Playwright, Cypress, Selenium) query the DOM — they need selectors, IDs, class names. GlimpseUI sees the screen like a human does.

| Capability | DOM tools | GlimpseUI |
|---|---|---|
| Sites you don't own / no source access | ✗ | ✓ |
| Catch visual bugs (overflow, overlap, wrong color) | ✗ | ✓ |
| Canvas, WebGL, PDF viewers | ✗ | ✓ |
| Survives DOM refactors automatically | ✗ | ✓ |
| Design vs reality diff (Figma → live app) | ✗ | ✓ |
| Natural language intent | ✗ | ✓ |
| Zero AI cost on repeat runs | — | ✓ (compile-once) |

---

## How it works

| Mode | What happens |
|---|---|
| **Web** | OpenRouter AI + vision loop controls a real Chromium browser via Playwright |
| **iOS** | XCTest bridge + vision loop — agent sees the screen, taps via accessibility tree |
| **Android** | uiautomator2 HTTP + vision loop — ~5× faster than raw ADB |
| **Compile-once** | AI runs once per new task, generates a deterministic action script, all future CI runs execute from cache for **$0** |
| **Self-healing** | If a cached step breaks (UI changed), the healer patches just that step and re-saves |

---

## Quick start

**Prerequisites:** Python 3.11+, an [OpenRouter API key](https://openrouter.ai/keys) (free tier available)

```bash
git clone https://github.com/YOUR_USERNAME/glimpseui
cd glimpseui

python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
playwright install chromium --with-deps   # one-time

cp .env.example .env
# edit .env → OPENROUTER_API_KEY=sk-or-v1-...

python main.py
# → opens http://localhost:8080
```

The web UI opens automatically. Enter a URL + task, hit Run.

---

## Running tests

```bash
# All web tests (sharded across 3 workers)
python -m agent.runner --suite tests/web/ --platform web

# Single suite
python -m agent.runner --suite tests/suites/web_smoke.yaml

# iOS (requires simulator + XCTest bridge)
python clients/yaml_runner.py tests/ios/safari_search.yaml --platform ios

# Android (requires adb device connected)
python clients/yaml_runner.py tests/android/chrome_search.yaml --platform android
```

---

## Mobile setup

### iOS
```bash
# Start the XCTest bridge (keeps the simulator accessible)
cd xctest-bridge && ./build_and_run.sh

# Or from the web UI: iOS tab → Start Bridge
```

### Android
```bash
# Install uiautomator2 server on the device (one-time)
python -m uiautomator2 init

# Verify connection
adb devices
```

---

## Project structure

```
glimpseui/
├── main.py                  # FastAPI server + all endpoints
├── seer_app.py              # Desktop app entry point (pywebview)
├── static/index.html        # Web UI (single-file, no build step)
│
├── agent/
│   ├── config.py            # Central config (OpenRouter keys/model)
│   ├── loop.py              # Autonomous web agent (vision loop)
│   ├── planner.py           # AI calls + XML response parsing
│   ├── computer_use.py      # Action executor (CDP + browser-use)
│   ├── session_manager.py   # Shared BrowserSession singleton
│   ├── cache.py             # Compile-once script cache
│   ├── compiler.py          # AI → deterministic script compiler
│   ├── executor.py          # Script executor (zero AI cost)
│   ├── healer.py            # Self-healing for broken cached steps
│   ├── suite_runner.py      # YAML test suite runner
│   ├── reporter.py          # HTML report generator
│   ├── junit_reporter.py    # JUnit XML for CI
│   ├── notify.py            # Slack webhook notifications
│   ├── runner.py            # CLI entry point (python -m agent.runner)
│   ├── auth.py              # API key auth (set GLIMPSEUI_API_KEY)
│   └── history.py           # SQLite run history
│
├── clients/
│   ├── ios_client.py        # iOS assisted-mode client
│   ├── android_client.py    # Android client (uiautomator2)
│   ├── desktop_client.py    # Desktop client (pyautogui)
│   └── yaml_runner.py       # YAML test runner for mobile platforms
│
├── tests/
│   ├── web/                 # Web test YAML files
│   ├── ios/                 # iOS test YAML files
│   ├── android/             # Android test YAML files
│   ├── suites/              # Smoke suites (web_smoke, ios_smoke, android_smoke)
│   └── .cache/              # Compiled scripts (commit this for $0 CI)
│
├── xctest-bridge/           # Swift XCTest bridge for iOS
├── Dockerfile               # Production container
├── deploy.sh                # Google Cloud Run deploy
├── build_dmg.sh             # macOS .app + .dmg builder
└── GlimpseUI.spec           # PyInstaller spec
```

---

## Writing tests

Tests are plain YAML. Two formats supported:

**Flat steps (preferred):**
```yaml
name: "Google Search"
platform: web
url: https://google.com

steps:
  - task: "Search for OpenAI"
  - check: page_contains "OpenAI"
  - assert: "Search results are visible"
  - task: "Click the first result"
  - check: url_contains "openai"
```

**Legacy (also supported):**
```yaml
name: "Google Search"
platform: web
url: https://google.com
tests:
  - name: "Search test"
    task: "Search for OpenAI"
    assert: "Results are visible"
```

`check:` steps are deterministic — no AI call, no cost. Use them over `assert:` wherever possible.

---

## Deployment

### Docker (Railway, Render, Fly.io, EC2)
```bash
docker build -t glimpseui .
docker run -p 8080:8080 -e OPENROUTER_API_KEY=sk-or-v1-... glimpseui
```

### Google Cloud Run
```bash
# Store key as a secret first
gcloud secrets create openrouter-api-key --data-file=- <<< "sk-or-v1-..."
./deploy.sh your-gcp-project-id
```

### macOS app
```bash
brew install create-dmg
./build_dmg.sh
# → dist/GlimpseUI-1.0.0.dmg
```

### Security
Set `GLIMPSEUI_API_KEY` in your environment to lock the server. All clients read this automatically.
```bash
export GLIMPSEUI_API_KEY=your-secret-token
```

---

## CI/CD

GitHub Actions workflows are included (`.github/workflows/ui-tests.yml`):
- Web tests run sharded across 3 parallel runners
- iOS tests run on `macos-14`
- Cache is saved/restored per branch — subsequent runs cost $0 in AI calls
- Slack notification on failure

**Required secrets** (Settings → Secrets → Actions):
```
OPENROUTER_API_KEY    your OpenRouter API key (https://openrouter.ai/keys)
SLACK_WEBHOOK_URL     optional, for failure notifications
```

---

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `OPENROUTER_API_KEY` | Yes | OpenRouter API key — supports Gemini, Claude, GPT-4o, and more |
| `OPENROUTER_MODEL` | No | Default: `google/gemini-2.0-flash-exp:free` |
| `OPENROUTER_BASE_URL` | No | Default: `https://openrouter.ai/api/v1` |
| `GLIMPSEUI_API_KEY` | No | Set to lock the server (empty = open/dev mode) |
| `GLIMPSEUI_CACHE_DIR` | No | Default: `tests/.cache` |
| `PORT` | No | Server port, default `8080` |

> **Legacy:** `GEMINI_API_KEY` and `GEMINI_MODEL` are still accepted as fallbacks so existing `.env` files keep working.

---

## License

MIT
