"""
GlimpseUI — FastAPI server

Endpoints:
  GET  /                  → web UI
  GET  /health            → health check
  GET  /sessions          → active sessions

  -- Autonomous web mode (cloud runs everything) --
  POST /run-task/stream   → SSE streaming live steps
  POST /run-task          → sync, wait for result

  -- Assisted mode (client is the hands, cloud is the brain) --
  POST /next-action       → send screenshot, get next action back
  POST /end-session       → close an assisted session
"""

import asyncio
import json
import os
import sys
import uuid
from contextlib import asynccontextmanager
from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, HTTPException, Depends
from fastapi.responses import StreamingResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Optional

from agent.auth import require_auth, require_local_or_auth
from agent.logger import get_logger

logger = get_logger("main")

from agent.config import GEMINI_MODEL
from agent.loop import run_task, get_sessions
from agent.dom_runner import run_dom_task
from agent.session_manager import reset_session
from agent.sessions import (
    create_session, get_session, delete_session,
    list_sessions, cleanup_loop,
)
from agent.planner import call_gemini, parse_response, check_assertion
from agent.history import start_run, finish_run, get_runs, get_run, delete_run, clear_history
from agent.suite_runner import run_suite
from agent.cache import list_entries as cache_list_entries, invalidate_all as cache_invalidate_all


# ── Lifespan: start background cleanup ───────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(cleanup_loop())
    yield
    await reset_session()   # clean up shared browser on shutdown

app = FastAPI(title="GlimpseUI", version="1.0.0", lifespan=lifespan)

# Allow origins: localhost by default; set GLIMPSEUI_ALLOWED_ORIGINS=https://yourdomain.com
# for production deployments (comma-separated list).
_raw_origins = os.getenv("GLIMPSEUI_ALLOWED_ORIGINS", "")
_allowed_origins = (
    [o.strip() for o in _raw_origins.split(",") if o.strip()]
    if _raw_origins
    else ["http://localhost:8080", "http://127.0.0.1:8080"]
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Authorization", "X-API-Key", "Content-Type"],
)


# ── Request models ────────────────────────────────────────────────────────────

class TaskRequest(BaseModel):
    task:       str            = Field(..., min_length=1, max_length=2000)
    url:        Optional[str]  = Field(default="about:blank", max_length=2048)
    start_url:  Optional[str]  = Field(default=None, max_length=2048)
    session_id: Optional[str]  = Field(default=None, max_length=64)
    mode:       Optional[str]  = Field(default="vision", pattern="^(vision|dom|auto)$")


class NextActionRequest(BaseModel):
    """Assisted mode — client sends screenshot, gets action back."""
    screenshot:   str           = Field(..., max_length=2_000_000)  # ~1.5 MB base64
    task:         str           = Field(..., min_length=1, max_length=2000)
    session_id:   Optional[str] = Field(default=None, max_length=64)
    last_action:  Optional[str] = Field(default=None, max_length=200)
    last_success: Optional[bool] = True
    last_error:   Optional[str] = Field(default=None, max_length=500)
    viewport_w:   Optional[int] = Field(default=1280, ge=100, le=4096)
    viewport_h:   Optional[int] = Field(default=800,  ge=100, le=4096)
    platform:     Optional[str] = Field(default="web", pattern="^(web|ios|android|desktop)$")


class AssertRequest(BaseModel):
    """Check whether a condition is visually true in a screenshot."""
    screenshot: str = Field(..., max_length=2_000_000)
    condition:  str = Field(..., min_length=1, max_length=1000)


class EndSessionRequest(BaseModel):
    session_id: str = Field(..., min_length=1, max_length=64)


class RunClientRequest(BaseModel):
    platform: str           = Field(..., pattern="^(ios|android|desktop)$")
    task:     str           = Field(..., min_length=1, max_length=2000)
    device:   Optional[str] = Field(default=None, max_length=128)


# Active client subprocesses keyed by run_id
_client_procs: dict = {}

# XCTest bridge subprocess
_bridge_proc = None


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "glimpseui",
        "model": GEMINI_MODEL,
        "mode": "autonomous (Computer Use API) + assisted (mobile)",
    }


@app.get("/devices")
async def devices():
    """Detect connected iOS / Android devices and report availability."""
    result = {
        "web": True,
        "desktop": True,
        "ios": False,
        "android": False,
        "android_ids": [],
    }

    # Android — ADB
    try:
        proc = await asyncio.create_subprocess_exec(
            "adb", "devices",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=3.0)
        lines = [
            l for l in stdout.decode().split("\n")[1:]
            if l.strip() and "\tdevice" in l
        ]
        result["android"] = len(lines) > 0
        result["android_ids"] = [l.split("\t")[0] for l in lines]
    except Exception:
        pass

    # iOS — XCTest bridge on :22087
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", 22087), timeout=1.0
        )
        writer.close()
        await writer.wait_closed()
        result["ios"] = True
    except Exception:
        pass

    return result


@app.get("/sessions")
async def sessions_list():
    return {
        "autonomous": get_sessions(),
        "assisted": list_sessions(),
    }


@app.get("/", response_class=HTMLResponse)
async def root():
    html_path = os.path.join(os.path.dirname(__file__), "static", "index.html")
    with open(html_path) as f:
        return f.read()


# ── Autonomous web mode ───────────────────────────────────────────────────────

def _pick_runner(req: TaskRequest):
    url = req.start_url or req.url or "about:blank"
    if req.mode == "dom":
        return run_dom_task(req.task, url, req.session_id)
    return run_task(req.task, url, req.session_id)


@app.post("/run-task/stream")
async def run_task_stream(req: TaskRequest, _=Depends(require_auth)):
    """SSE — streams live step events while Playwright runs on the server."""

    async def event_generator():
        run_id = start_run(req.task, "web")
        steps_data = []
        async for event in _pick_runner(req):
            if event["type"] == "step":
                steps_data.append({
                    "step":       event["step"],
                    "action":     event.get("action", ""),
                    "thought":    event.get("thought", ""),
                    "params":     event.get("params", {}),
                    "located":    event.get("located"),
                    "screenshot": event.get("screenshot", ""),
                })
            elif event["type"] == "done":
                finish_run(run_id, event["success"], steps_data,
                           event.get("message", ""), event.get("screenshot"))
            elif event["type"] == "error":
                finish_run(run_id, False, steps_data, event.get("message", "Error"))
            yield f"data: {json.dumps(event)}\n\n"
        yield 'data: {"type":"end"}\n\n'

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/run-task")
async def run_task_sync(req: TaskRequest, _=Depends(require_auth)):
    """Sync — waits for full completion, returns all steps + final screenshot."""
    steps, result = [], None

    async for event in _pick_runner(req):
        if event["type"] == "step":
            steps.append({
                "step":    event["step"],
                "thought": event["thought"],
                "action":  event["action"],
                "params":  event["params"],
                "located": event.get("located"),
            })
        elif event["type"] == "done":
            result = event
        elif event["type"] == "error":
            raise HTTPException(status_code=500, detail=event["message"])

    if not result:
        raise HTTPException(status_code=500, detail="No completion event received")

    return {
        "success":          result["success"],
        "message":          result["message"],
        "steps":            steps,
        "total_steps":      len(steps),
        "final_screenshot": result.get("screenshot"),
    }


# ── Assisted mode ─────────────────────────────────────────────────────────────

@app.post("/next-action")
async def next_action(req: NextActionRequest, _=Depends(require_auth)):
    """
    Assisted mode endpoint.
    Client (desktop/mobile) sends a screenshot → cloud returns next action.

    Flow:
      1. First call: no session_id → creates session, builds first Gemini turn
      2. Subsequent calls: session_id → appends continuation turn
      3. Call Gemini → parse → return action
      4. Client executes action locally, loops back with new screenshot
    """
    vw = req.viewport_w or 1280
    vh = req.viewport_h or 800

    # Get or create session
    session = get_session(req.session_id) if req.session_id else None

    if session is None:
        # New session
        session = create_session(req.task, req.session_id)
        session.add_first_turn(req.screenshot)
    else:
        # Existing session — record result of last action
        if req.last_action:
            session.record_action(
                req.last_action,
                req.last_success if req.last_success is not None else True,
                req.last_error or "",
            )
        if req.last_error and not req.last_success:
            session.add_retry(req.screenshot, req.last_error)
        else:
            session.add_continuation(req.screenshot)

    session.touch()

    # Call Gemini
    raw_response, assistant_turn = await call_gemini(session.conversation, req.platform or "web")
    session.add_assistant(assistant_turn)

    # Parse action
    action = parse_response(raw_response, vw, vh)
    if not action:
        raise HTTPException(
            status_code=422,
            detail=f"Could not parse Gemini response: {raw_response[:300]}",
        )

    # If Finished, clean up session
    if action.action_type == "Finished":
        delete_session(session.session_id)

    return {
        "session_id":  session.session_id,
        "step":        session.step + 1,
        "thought":     action.thought,
        "action":      action.action_type,
        "params":      action.params,
        "located": {
            "prompt": action.located.prompt,
            "bbox":   action.located.bbox,
            "cx":     action.located.center_x,
            "cy":     action.located.center_y,
        } if action.located else None,
        "finished": action.action_type == "Finished",
        "success":  action.params.get("success", True) if action.action_type == "Finished" else None,
        "message":  action.params.get("message", "") if action.action_type == "Finished" else None,
    }


@app.post("/assert")
async def assert_condition(req: AssertRequest, _=Depends(require_auth)):
    """
    Visual assertion — check whether a condition is true in a screenshot.
    Used by YAML test runner for assert: steps.
    """
    passed, reason = await check_assertion(req.screenshot, req.condition)
    return {"passed": passed, "reason": reason, "condition": req.condition}


@app.post("/end-session")
async def end_session(req: EndSessionRequest, _=Depends(require_auth)):
    delete_session(req.session_id)
    return {"ok": True, "session_id": req.session_id}


@app.get("/start-bridge/stream")
async def start_bridge_stream(_=Depends(require_auth)):
    """Build and run the XCTest bridge, streaming output as SSE."""
    global _bridge_proc
    base = os.path.dirname(os.path.abspath(__file__))
    script = os.path.join(base, "xctest-bridge", "build_and_run.sh")

    async def event_gen():
        global _bridge_proc
        proc = await asyncio.create_subprocess_exec(
            "bash", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=os.path.join(base, "xctest-bridge"),
        )
        _bridge_proc = proc
        yield f"data: {json.dumps({'type': 'log', 'text': '🔨 Building XCTest bridge…'})}\n\n"
        try:
            async for line in proc.stdout:
                text = line.decode(errors="replace").rstrip()
                if text:
                    yield f"data: {json.dumps({'type': 'log', 'text': text})}\n\n"
        finally:
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    proc.kill()
            # check if bridge is up
            try:
                r, w = await asyncio.wait_for(
                    asyncio.open_connection("127.0.0.1", 22087), timeout=2.0
                )
                w.close()
                await w.wait_closed()
                yield f"data: {json.dumps({'type': 'ready'})}\n\n"
            except Exception:
                yield f"data: {json.dumps({'type': 'end', 'exit_code': proc.returncode})}\n\n"

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/run-client/stream")
async def run_client_stream(req: RunClientRequest, _=Depends(require_auth)):
    """Spawn an ios/android/desktop client and stream its output as SSE."""
    scripts = {
        "ios":     "clients/ios_client.py",
        "android": "clients/android_client.py",
        "desktop": "clients/desktop_client.py",
    }
    if req.platform not in scripts:
        raise HTTPException(400, f"Unknown platform: {req.platform}")

    base = os.path.dirname(os.path.abspath(__file__))
    script = os.path.join(base, scripts[req.platform])
    port = os.getenv("PORT", "8080")
    server_url = f"http://127.0.0.1:{port}"

    cmd = [sys.executable, "-u", script, "--task", req.task, "--server", server_url]
    if req.platform == "android" and req.device:
        cmd += ["--device", req.device]

    run_id = uuid.uuid4().hex[:8]

    async def event_gen():
        hist_id = start_run(req.task, req.platform)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=base,
        )
        _client_procs[run_id] = proc
        yield f"data: {json.dumps({'type': 'start', 'run_id': run_id})}\n\n"
        try:
            async for line in proc.stdout:
                text = line.decode(errors="replace").rstrip()
                if text:
                    yield f"data: {json.dumps({'type': 'log', 'text': text})}\n\n"
        finally:
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    proc.kill()
            _client_procs.pop(run_id, None)
            success = proc.returncode == 0
            finish_run(hist_id, success, [], "Done" if success else "Failed")
            yield f"data: {json.dumps({'type': 'end', 'exit_code': proc.returncode})}\n\n"

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/stop-client/{run_id}")
async def stop_client(run_id: str):
    proc = _client_procs.get(run_id)
    if proc:
        proc.terminate()
        _client_procs.pop(run_id, None)
        return {"ok": True}
    return {"ok": False, "error": "Not found"}


@app.post("/snap-windows")
async def snap_windows(_=Depends(require_auth)):
    """Snap our app to the left half and iOS Simulator to the right half."""
    script = """
    tell application "Finder"
        set screenBounds to bounds of window of desktop
        set sw to item 3 of screenBounds
        set sh to item 4 of screenBounds
    end tell
    set hw to sw / 2
    tell application "System Events"
        if exists (first process whose name is "Simulator") then
            tell application "Simulator" to activate
            set position of first window of (first process whose name is "Simulator") to {hw, 0}
            set size    of first window of (first process whose name is "Simulator") to {hw, sh}
        end if
        repeat with pname in {"GlimpseUI", "GlimpseUI", "Python"}
            if exists (first process whose name is pname) then
                set position of first window of (first process whose name is pname) to {0, 0}
                set size    of first window of (first process whose name is pname) to {hw, sh}
                tell application (pname as string) to activate
                exit repeat
            end if
        end repeat
    end tell
    """
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await asyncio.wait_for(proc.communicate(), timeout=8.0)
    if proc.returncode != 0:
        return {"ok": False, "error": stderr.decode().strip()}
    return {"ok": True}


@app.post("/reset-browser")
async def reset_browser(_=Depends(require_auth)):
    """Close and recreate the shared browser instance (clears cookies/state)."""
    await reset_session()
    return {"ok": True, "message": "Browser session reset"}


async def _simctl_screenshot() -> bytes | None:
    """Capture one PNG frame from the booted iOS simulator. Returns None on failure."""
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        tmppath = f.name
    try:
        proc = await asyncio.create_subprocess_exec(
            "xcrun", "simctl", "io", "booted", "screenshot", tmppath,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=5.0)
        if proc.returncode != 0:
            return None
        with open(tmppath, "rb") as f:
            return f.read()
    except Exception:
        return None
    finally:
        try:
            os.unlink(tmppath)
        except OSError:
            pass


@app.get("/simulator/screenshot")
async def simulator_screenshot():
    """Single PNG frame from the booted iOS simulator."""
    from fastapi.responses import Response
    data = await _simctl_screenshot()
    if data is None:
        raise HTTPException(status_code=503, detail="No booted simulator found")
    return Response(content=data, media_type="image/png")


@app.get("/simulator/stream")
async def simulator_stream():
    """MJPEG stream of the booted iOS simulator via simctl."""
    first = await _simctl_screenshot()
    if first is None:
        raise HTTPException(status_code=503, detail="No booted simulator found")

    BOUNDARY = b"--frame"

    def make_frame(data: bytes) -> bytes:
        return (
            BOUNDARY + b"\r\n"
            b"Content-Type: image/png\r\n"
            b"Content-Length: " + str(len(data)).encode() + b"\r\n\r\n" +
            data + b"\r\n"
        )

    async def generator():
        yield make_frame(first)
        while True:
            await asyncio.sleep(1.0)
            data = await _simctl_screenshot()
            if data is None:
                break
            yield make_frame(data)

    return StreamingResponse(
        generator(),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-cache"},
    )


class SuiteRequest(BaseModel):
    yaml: str
    suite_id: Optional[str] = None


@app.post("/run-suite/stream")
async def run_suite_stream(req: SuiteRequest, _=Depends(require_auth)):
    """SSE — run a YAML test suite, streaming per-test results."""

    async def event_generator():
        run_id = None
        all_steps: list = []
        passed = failed = 0

        async for event in run_suite(req.yaml, req.suite_id):
            if event["type"] == "suite_start":
                run_id = start_run(f"[Suite] {event['name']}", event.get("platform", "web"))
            elif event["type"] == "test_done":
                all_steps.extend(event.get("steps_data", []))
                if event["status"] == "pass":
                    passed += 1
                else:
                    failed += 1
            elif event["type"] in ("suite_done", "error") and run_id:
                success = failed == 0 and event["type"] != "error"
                msg = f"{passed} passed, {failed} failed"
                finish_run(run_id, success, all_steps, msg)

            # strip heavy steps_data from SSE (already saved to DB)
            out = {k: v for k, v in event.items() if k != "steps_data"}
            yield f"data: {json.dumps(out)}\n\n"

        yield 'data: {"type":"end"}\n\n'

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class ConfigRequest(BaseModel):
    openrouter_api_key: Optional[str] = Field(default=None, max_length=200)
    openrouter_model:   Optional[str] = Field(default=None, max_length=200)
    # Legacy field names accepted for backwards compatibility
    gemini_api_key: Optional[str] = Field(default=None, max_length=200)
    gemini_model:   Optional[str] = Field(default=None, max_length=200)


@app.post("/save-config")
async def save_config(req: ConfigRequest, _=Depends(require_local_or_auth)):
    """Persist API keys to ~/.glimpseui/.env so they survive app restarts."""
    config_dir = os.path.join(os.path.expanduser("~"), ".glimpseui")
    os.makedirs(config_dir, exist_ok=True)
    env_path = os.path.join(config_dir, ".env")

    # Read existing lines, replace or append keys
    lines: dict[str, str] = {}
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    lines[k.strip()] = v.strip()

    api_key = req.openrouter_api_key or req.gemini_api_key
    model   = req.openrouter_model   or req.gemini_model

    if api_key is not None:
        lines["OPENROUTER_API_KEY"] = api_key
        os.environ["OPENROUTER_API_KEY"] = api_key
    if model is not None:
        lines["OPENROUTER_MODEL"] = model
        os.environ["OPENROUTER_MODEL"] = model

    with open(env_path, "w") as f:
        for k, v in lines.items():
            f.write(f"{k}={v}\n")

    return {"ok": True}


@app.get("/history")
async def history(limit: int = 50, platform: Optional[str] = None):
    return {"runs": get_runs(limit, platform)}

@app.get("/history/{run_id}")
async def history_run(run_id: int):
    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return run

@app.delete("/history/{run_id}")
async def history_delete(run_id: int):
    delete_run(run_id)
    return {"ok": True}

@app.delete("/history")
async def history_clear():
    clear_history()
    return {"ok": True}


@app.get("/cache")
async def get_cache():
    """List all cached compiled scripts."""
    return {"entries": cache_list_entries()}


@app.delete("/cache")
async def clear_cache(platform: Optional[str] = None, _=Depends(require_auth)):
    """Wipe all cached scripts (or just one platform)."""
    cache_invalidate_all(platform)
    return {"ok": True}


@app.get("/get-config")
async def get_config():
    """Return current config (masks the API key)."""
    key = os.getenv("GEMINI_API_KEY", "")
    return {
        "gemini_api_key_set": bool(key),
        "gemini_api_key_preview": (key[:8] + "…") if key else "",
        "gemini_model": GEMINI_MODEL,
    }


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import threading
    import webbrowser
    import uvicorn

    port = int(os.getenv("PORT", 8080))

    def _open_browser():
        import time
        time.sleep(1.2)
        webbrowser.open(f"http://localhost:{port}")

    threading.Thread(target=_open_browser, daemon=True).start()
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
