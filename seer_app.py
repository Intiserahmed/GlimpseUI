"""
GlimpseUI — Desktop App entry point

Starts the FastAPI server in a background thread, then opens a native
window via pywebview. Works both in dev and when bundled with PyInstaller.

Build DMG:
  ./build_dmg.sh
"""

import os
import socket
import sys
import threading

import uvicorn
import webview


def find_free_port() -> int:
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def _add_bundle_path():
    """When frozen by PyInstaller, add _MEIPASS to sys.path so imports work."""
    if getattr(sys, "frozen", False):
        base = sys._MEIPASS  # type: ignore[attr-defined]
        if base not in sys.path:
            sys.path.insert(0, base)
        # Also set working dir so relative file opens (static/, xctest-bridge/) work
        os.chdir(base)


def _load_env():
    """
    Load .env from (in priority order):
      1. ~/.glimpseui/.env   — user's persistent config (used by bundled app)
      2. <script dir>/.env     — dev fallback

    On first run of the bundled app, if ~/.glimpseui/.env doesn't exist yet,
    copy the bundled .env (which contains the hardcoded keys) there.
    """
    import shutil
    from dotenv import load_dotenv  # noqa: PLC0415

    config_dir = os.path.join(os.path.expanduser("~"), ".glimpseui")
    user_env   = os.path.join(config_dir, ".env")

    if getattr(sys, "frozen", False):
        # Bundled app: seed ~/.glimpseui/.env from the bundled .env if needed
        bundled_env = os.path.join(sys._MEIPASS, ".env")  # type: ignore[attr-defined]
        if not os.path.exists(user_env) and os.path.exists(bundled_env):
            os.makedirs(config_dir, exist_ok=True)
            shutil.copy(bundled_env, user_env)

    if os.path.exists(user_env):
        load_dotenv(user_env, override=True)
    else:
        dev_env = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
        load_dotenv(dev_env, override=True)


def start_server(port: int):
    os.environ["PORT"] = str(port)
    _add_bundle_path()
    _load_env()
    # Import app after path is set up
    from main import app  # noqa: PLC0415
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    port = find_free_port()
    t = threading.Thread(target=start_server, args=(port,), daemon=True)
    t.start()

    # Wait until server is actually ready (poll instead of fixed sleep)
    import time
    import urllib.request
    for _ in range(300):   # up to 30s
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=0.5)
            break
        except Exception:
            time.sleep(0.1)

    webview.create_window(
        title="GlimpseUI",
        url=f"http://127.0.0.1:{port}",
        width=1280,
        height=800,
        min_size=(800, 600),
        text_select=True,
    )
    webview.start()
