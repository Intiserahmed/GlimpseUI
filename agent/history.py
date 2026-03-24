"""
Run history — stores every task run in ~/.glimpseui/history.db (SQLite).

Uses a single persistent connection with WAL mode so concurrent reads
don't block writes. The DB is always accessed from the asyncio event-loop
thread, so check_same_thread=False is safe.
"""

import json
import os
import sqlite3
import time
from typing import Optional

from .logger import get_logger

logger = get_logger(__name__)

DB_PATH = os.path.join(os.path.expanduser("~"), ".glimpseui", "history.db")

_con: Optional[sqlite3.Connection] = None


def _conn() -> sqlite3.Connection:
    """Return the shared SQLite connection, opening it once."""
    global _con
    if _con is None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        _con = sqlite3.connect(DB_PATH, check_same_thread=False)
        _con.row_factory = sqlite3.Row
        _con.execute("PRAGMA journal_mode=WAL")
        _con.execute("PRAGMA synchronous=NORMAL")
        _con.commit()
        logger.debug("SQLite opened: %s", DB_PATH)
    return _con


def init_db():
    con = _conn()
    con.execute("""
        CREATE TABLE IF NOT EXISTS runs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            task        TEXT    NOT NULL,
            platform    TEXT    NOT NULL DEFAULT 'web',
            start_time  REAL    NOT NULL,
            end_time    REAL,
            success     INTEGER,
            steps       INTEGER DEFAULT 0,
            message     TEXT,
            screenshot  TEXT,
            steps_json  TEXT
        )
    """)
    # One-time schema migration — only catches the "already exists" OperationalError
    try:
        con.execute("ALTER TABLE runs ADD COLUMN steps_json TEXT")
        con.commit()
        logger.info("DB migrated: added steps_json column")
    except sqlite3.OperationalError:
        pass  # column already exists — expected on every run after first migration
    con.commit()


def start_run(task: str, platform: str = "web") -> int:
    init_db()
    con = _conn()
    cur = con.execute(
        "INSERT INTO runs (task, platform, start_time) VALUES (?, ?, ?)",
        (task, platform, time.time()),
    )
    con.commit()
    return cur.lastrowid


def finish_run(
    run_id: int,
    success: bool,
    steps_data: list,
    message: str,
    screenshot: Optional[str] = None,
):
    steps_json = json.dumps(steps_data)
    con = _conn()
    con.execute(
        """UPDATE runs
           SET end_time=?, success=?, steps=?, message=?, screenshot=?, steps_json=?
           WHERE id=?""",
        (time.time(), int(success), len(steps_data), message,
         screenshot, steps_json, run_id),
    )
    con.commit()


def get_runs(limit: int = 50, platform: Optional[str] = None) -> list[dict]:
    init_db()
    con = _conn()
    if platform:
        rows = con.execute(
            "SELECT * FROM runs WHERE platform=? ORDER BY start_time DESC LIMIT ?",
            (platform, limit),
        ).fetchall()
    else:
        rows = con.execute(
            "SELECT * FROM runs ORDER BY start_time DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_run(run_id: int) -> Optional[dict]:
    init_db()
    con = _conn()
    row = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    return dict(row) if row else None


def delete_run(run_id: int):
    con = _conn()
    con.execute("DELETE FROM runs WHERE id=?", (run_id,))
    con.commit()


def clear_history():
    con = _conn()
    con.execute("DELETE FROM runs")
    con.commit()
