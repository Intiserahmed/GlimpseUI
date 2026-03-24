"""
Test sharding for parallel CI execution.

Splits a test list across N CI runners so they run in parallel.
Uses consistent hashing so the same test always goes to the same shard
(important for cache locality — each shard's cache warms up faster).

Usage (CI):
    # Runner 1 of 4:
    python -m agent.runner --suite tests/ --shard 1/4

    # Runner 2 of 4:
    python -m agent.runner --suite tests/ --shard 2/4

Usage (Python):
    from agent.sharding import get_shard, parse_shard_arg

    shard_index, total = parse_shard_arg("2/4")
    my_tests = get_shard(all_tests, shard_index, total)
"""

import hashlib
from pathlib import Path
from typing import Optional


def get_shard(
    tests:       list,
    shard_index: int,
    total:       int,
    key_fn=None,
) -> list:
    """
    Return the subset of tests assigned to this shard.

    Uses consistent hashing (not round-robin) so test-to-shard assignment
    is stable even if tests are added/removed from the suite.

    Args:
        tests:       full list of test dicts
        shard_index: 1-based shard number (1..total)
        total:       total number of shards
        key_fn:      optional function to extract a string key from a test dict
                     defaults to test["name"]
    """
    if total <= 1:
        return tests

    if key_fn is None:
        key_fn = lambda t: t.get("name", str(t))

    result = []
    for test in tests:
        key       = key_fn(test)
        hash_val  = int(hashlib.md5(key.encode()).hexdigest(), 16)
        assigned  = (hash_val % total) + 1   # 1-based
        if assigned == shard_index:
            result.append(test)

    return result


def parse_shard_arg(shard_arg: Optional[str]) -> tuple[int, int]:
    """
    Parse a shard argument like "2/4" → (2, 4).
    Returns (1, 1) if shard_arg is None (no sharding, run all tests).
    """
    if not shard_arg:
        return 1, 1
    try:
        parts = shard_arg.strip().split("/")
        return int(parts[0]), int(parts[1])
    except (ValueError, IndexError):
        raise ValueError(f"Invalid shard format '{shard_arg}'. Use 'N/TOTAL', e.g. '2/4'")


def shard_files(
    yaml_files:  list[Path],
    shard_index: int,
    total:       int,
) -> list[Path]:
    """Shard a list of YAML test file paths."""
    return get_shard(
        yaml_files,
        shard_index,
        total,
        key_fn=lambda p: str(p),
    )
