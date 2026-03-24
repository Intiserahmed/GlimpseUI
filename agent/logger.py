"""
Central logging setup for GlimpseUI.

Usage in any module:
    from .logger import get_logger
    logger = get_logger(__name__)
    logger.info("something happened")
    logger.warning("watch out: %s", detail)
    logger.error("failed: %s", err, exc_info=True)

Set DEBUG=1 in env to enable verbose output.
"""

import logging
import os
import sys


def _setup_root() -> logging.Logger:
    root = logging.getLogger("glimpseui")
    if root.handlers:
        return root  # already configured (e.g. in tests)

    level = logging.DEBUG if os.getenv("DEBUG") else logging.INFO
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(fmt)
    root.addHandler(handler)
    root.setLevel(level)
    return root


_setup_root()


def get_logger(name: str) -> logging.Logger:
    """Return a child logger under the glimpseui namespace."""
    # Strip leading 'agent.' so names stay short: glimpseui.loop, glimpseui.planner, etc.
    short = name.replace("agent.", "").replace("__main__", "main")
    return logging.getLogger(f"glimpseui.{short}")
