"""
DOM-aware agent loop using browser-use with the shared BrowserSession.
Same browser instance as vision runner — cookies, tabs, state all preserved.
"""

import uuid
from typing import AsyncGenerator

from browser_use import Agent
from langchain_openai import ChatOpenAI

from .session_manager import get_session
from .config import OPENROUTER_MODEL, OPENROUTER_API_KEY, OPENROUTER_BASE_URL


def _make_llm() -> ChatOpenAI:
    return ChatOpenAI(
        model=OPENROUTER_MODEL,
        api_key=OPENROUTER_API_KEY,
        base_url=OPENROUTER_BASE_URL,
        temperature=0,
    )


async def run_dom_task(
    task: str,
    start_url: str = "about:blank",
    session_id: str = None,
) -> AsyncGenerator[dict, None]:
    sid = session_id or str(uuid.uuid4())[:8]
    yield {"type": "start", "session_id": sid, "task": task, "url": start_url, "mode": "dom"}

    try:
        # Get shared session — same browser as vision runner
        browser = await get_session()

        if start_url and start_url != "about:blank":
            await browser.navigate_to(start_url)

        agent = Agent(
            task=task,
            llm=_make_llm(),
            browser_session=browser,
        )

        history = await agent.run(max_steps=20)

        # Extract steps
        thoughts = history.model_thoughts() or []
        actions  = history.action_names()   or []
        total    = max(len(thoughts), len(actions))
        step = 0

        for i in range(total):
            step += 1
            thought     = str(thoughts[i]) if i < len(thoughts) else ""
            action_name = str(actions[i])  if i < len(actions)  else "Action"
            yield {
                "type":       "step",
                "session_id": sid,
                "step":       step,
                "thought":    thought,
                "action":     action_name,
                "params":     {},
                "located":    None,
                "screenshot": None,
            }

        final   = history.final_result()
        success = history.is_successful() if hasattr(history, "is_successful") else history.is_done()
        message = str(final) if final else "Task completed"

        yield {
            "type":       "done",
            "session_id": sid,
            "step":       step,
            "success":    bool(success),
            "message":    message,
            "screenshot": None,
        }

    except Exception as e:
        yield {"type": "error", "session_id": sid, "message": str(e)}
