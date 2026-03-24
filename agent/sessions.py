"""
Stateful session store for /next-action (assisted mode).
Each session maintains its own Gemini conversation history.
Sessions expire after IDLE_TIMEOUT seconds of inactivity.
"""

import asyncio
import time
import uuid
from .planner import build_first_turn, build_continuation_turn, build_retry_turn, CONV_LIMIT

IDLE_TIMEOUT = 600  # 10 minutes


class AssistSession:
    def __init__(self, session_id: str, task: str):
        self.session_id   = session_id
        self.task         = task
        self.step         = 0
        self.conversation: list[dict] = []
        self.last_action  = ""
        self.last_result  = ""
        self.created_at   = time.time()
        self.last_active  = time.time()

    def touch(self):
        self.last_active = time.time()

    def is_expired(self) -> bool:
        return time.time() - self.last_active > IDLE_TIMEOUT

    def add_first_turn(self, screenshot_b64: str):
        self.conversation.append(build_first_turn(self.task, screenshot_b64))

    def add_continuation(self, screenshot_b64: str):
        self.conversation.append(
            build_continuation_turn(self.step + 1, screenshot_b64, self.last_action, self.last_result)
        )

    def add_retry(self, screenshot_b64: str, error: str):
        self.conversation.append(
            build_retry_turn(self.step, screenshot_b64, error)
        )

    def add_assistant(self, turn):
        self.conversation.append(turn)
        # Trim: keep first + recent
        if len(self.conversation) > CONV_LIMIT:
            self.conversation = [self.conversation[0]] + self.conversation[-(CONV_LIMIT - 1):]

    def record_action(self, action_label: str, success: bool, error: str = ""):
        self.last_action = action_label
        self.last_result = "success" if success else f"failed: {error}"
        self.step += 1


# ── Global store ──────────────────────────────────────────────────────────────

_store: dict[str, AssistSession] = {}


def create_session(task: str, session_id: str = None) -> AssistSession:
    sid = session_id or str(uuid.uuid4())[:12]
    session = AssistSession(sid, task)
    _store[sid] = session
    return session


def get_session(session_id: str) -> AssistSession | None:
    return _store.get(session_id)


def delete_session(session_id: str):
    _store.pop(session_id, None)


def list_sessions() -> list[dict]:
    return [
        {
            "session_id": s.session_id,
            "task": s.task,
            "step": s.step,
            "age_s": round(time.time() - s.created_at),
        }
        for s in _store.values()
    ]


async def cleanup_loop():
    """Background task — evict expired sessions every 60s."""
    while True:
        await asyncio.sleep(60)
        expired = [sid for sid, s in _store.items() if s.is_expired()]
        for sid in expired:
            _store.pop(sid, None)
