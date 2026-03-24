"""
CLI entry point for running UI test suites.

Supports:
  - YAML test files or directories
  - Test sharding for parallel CI execution
  - HTML + JUnit XML reports
  - Slack notifications on failure
  - iOS simulator pool for parallel mobile execution
  - Platform selection (web / ios / android)

Usage:
    # Run all tests in a directory
    python -m agent.runner --suite tests/

    # Shard 1 of 4 (for CI matrix strategy)
    python -m agent.runner --suite tests/ --shard 1/4

    # Run specific platform with report output
    python -m agent.runner --suite tests/mobile/ --platform ios \\
        --report reports/run.html --junit reports/junit.xml

    # With parallel simulator pool
    python -m agent.runner --suite tests/mobile/ --platform ios --pool-size 4

    # Notify Slack on failure only
    python -m agent.runner --suite tests/ --slack-on-failure
"""

import argparse
import asyncio
import sys
import time
from pathlib import Path
from typing import Optional

from .sharding       import parse_shard_arg, shard_files
from .reporter       import Reporter
from .junit_reporter import JUnitReporter
from .notify         import notify_slack_sync
from .cache          import load as cache_load, save as cache_save
from .compiler       import compile_web_task, compile_mobile_task
from .executor       import run_web_script, run_ios_script, run_android_script
from .healer         import heal_step
from .screenshot_policy import should_capture


# ── YAML loader ───────────────────────────────────────────────────────────────

def _collect_yaml_files(suite_path: str) -> list[Path]:
    p = Path(suite_path)
    if p.is_file():
        return [p]
    return sorted(p.rglob("*.yaml")) + sorted(p.rglob("*.yml"))


def _load_yaml(path: Path) -> dict:
    try:
        import yaml
        return yaml.safe_load(path.read_text()) or {}
    except Exception as e:
        print(f"[runner] Failed to load {path}: {e}")
        return {}


# ── Per-test runner ───────────────────────────────────────────────────────────

async def _run_test(
    test_name:  str,
    task:       str,
    platform:   str,
    reporter:   Reporter,
    junit:      JUnitReporter,
    # web
    browser=None,
    page=None,
    start_url: str = "",
    # mobile
    bridge_url:    str = "http://localhost:22087",
    device_serial: str = None,
    app_bundle:    str = "com.apple.mobilesafari",
    viewport_w:    int = 393,
    viewport_h:    int = 852,
    # options
    use_cache:  bool = True,
    no_cache:   bool = False,
) -> bool:
    reporter.begin_test(test_name)
    t_start = time.time()
    stdout_log = []

    def log(msg: str):
        print(f"  {msg}")
        stdout_log.append(msg)

    # ── Build cache key ───────────────────────────────────────────────────────
    cache_key = f"{platform}|{start_url}|{task}"

    script: Optional[list] = None
    if use_cache and not no_cache:
        script = cache_load(platform, start_url, task)
        if script:
            log(f"Cache hit — {len(script)} steps (no AI)")

    if script is None:
        log("Compiling task with AI...")
        try:
            if platform == "web":
                script = await compile_web_task(task, page, browser, start_url)
            elif platform == "ios":
                script = await compile_mobile_task(
                    task, "ios", bridge_url,
                    viewport_w=viewport_w, viewport_h=viewport_h,
                    app_bundle=app_bundle,
                )
            else:
                script = await compile_mobile_task(
                    task, "android", bridge_url,
                    device_serial=device_serial,
                    viewport_w=viewport_w, viewport_h=viewport_h,
                )
            if script and use_cache:
                cache_save(platform, start_url, task, script)
                log(f"Compiled {len(script)} steps — cached")
        except Exception as e:
            log(f"Compilation failed: {e}")
            reporter.fail_step("compile", task, error=str(e))
            reporter.end_test(passed=False, duration=time.time() - t_start, error=str(e))
            junit.add_test(test_name, passed=False, duration=time.time() - t_start,
                           error_msg=str(e), stdout="\n".join(stdout_log))
            return False

    if not script:
        msg = "No steps compiled"
        reporter.fail_step("compile", task, error=msg)
        reporter.end_test(passed=False, duration=time.time() - t_start, error=msg)
        junit.add_test(test_name, passed=False, duration=time.time() - t_start,
                       error_msg=msg, stdout="\n".join(stdout_log))
        return False

    # ── Execute ───────────────────────────────────────────────────────────────
    log(f"Running {len(script)} steps ({platform})...")
    try:
        if platform == "web":
            success, error, fail_idx = await run_web_script(page, script)
        elif platform == "ios":
            success, error, fail_idx = await run_ios_script(script, bridge_url, app_bundle)
        else:
            success, error, fail_idx = await run_android_script(script, device_serial)
    except Exception as e:
        success, error, fail_idx = False, str(e), -1

    # ── Record steps ──────────────────────────────────────────────────────────
    for i, step in enumerate(script):
        step_ok    = success or i < fail_idx
        step_err   = error if (not success and i == fail_idx) else ""
        step_label = step.get("label", step.get("selector", step.get("url", "")))
        reporter.add_step(step.get("action", "?"), step_label,
                          passed=step_ok, error=step_err)
        log(f"{'✓' if step_ok else '✗'} [{step.get('action')}] {step_label}")

    # ── Self-heal on failure ──────────────────────────────────────────────────
    if not success and fail_idx >= 0:
        log(f"Step {fail_idx} failed — attempting self-heal...")
        broken = script[fail_idx]
        healed = await heal_step(broken, platform=platform,
                                 bridge_url=bridge_url, browser=browser,
                                 device_serial=device_serial)
        if healed:
            script[fail_idx] = healed
            if use_cache:
                cache_save(platform, start_url, task, script)
            log("Healed — retry on next run (cache updated)")
        else:
            log("Healing failed")

    duration = time.time() - t_start
    reporter.end_test(passed=success, duration=duration,
                      error=error if not success else "")
    junit.add_test(test_name, passed=success, duration=duration,
                   error_msg=error if not success else "",
                   stdout="\n".join(stdout_log))
    return success


# ── Suite runner ──────────────────────────────────────────────────────────────

async def run_suite(args) -> int:
    yaml_files = _collect_yaml_files(args.suite)
    if not yaml_files:
        print(f"[runner] No YAML files found in {args.suite}")
        return 1

    # Sharding
    shard_index, total_shards = parse_shard_arg(args.shard)
    yaml_files = shard_files(yaml_files, shard_index, total_shards)
    if not yaml_files:
        print(f"[runner] Shard {args.shard}: no files assigned to this shard")
        return 0

    print(f"[runner] Shard {shard_index}/{total_shards}: {len(yaml_files)} file(s)")

    reporter = Reporter(output_path=args.report or "reports/run.html")
    junit    = JUnitReporter(output_path=args.junit or "reports/junit.xml")
    suite_name = f"UI Tests ({args.platform})"
    reporter.begin_suite(suite_name)
    junit.begin_suite(suite_name)

    failures = []
    total    = 0
    passed   = 0
    t_suite  = time.time()

    # ── iOS simulator pool ────────────────────────────────────────────────────
    pool = None
    if args.platform == "ios" and args.pool_size > 1:
        from .simulator_pool import SimulatorPool
        pool = SimulatorPool(size=args.pool_size, device=args.device)
        await pool.start()

    # ── Web browser session ───────────────────────────────────────────────────
    browser = None
    page    = None
    if args.platform == "web":
        try:
            from .session_manager import get_session
            browser = await get_session()
            page    = await browser.get_current_page()
        except Exception as e:
            print(f"[runner] Failed to start browser: {e}")
            return 1

    # ── Iterate files ─────────────────────────────────────────────────────────
    for yaml_path in yaml_files:
        data = _load_yaml(yaml_path)
        if not data:
            continue

        platform = args.platform or data.get("platform", "web")
        start_url = data.get("url", "")

        # Support both flat steps: and tests: format
        steps = data.get("steps", [])
        tests = data.get("tests",  [])

        if steps:
            # Flat format: group by task: entries
            task_groups = []
            current_task = None
            for step in steps:
                if "task" in step:
                    current_task = step["task"]
                    task_groups.append(current_task)
            if not task_groups:
                # Single unnamed task
                task_groups = [data.get("name", yaml_path.stem)]
        else:
            task_groups = [t.get("name", f"Test {i+1}") for i, t in enumerate(tests)]

        for task_name in task_groups:
            total += 1
            bridge = _pick_bridge(pool, args)
            ok = await _run_test(
                test_name  = task_name,
                task       = task_name,
                platform   = platform,
                reporter   = reporter,
                junit      = junit,
                browser    = browser,
                page       = page,
                start_url  = start_url,
                bridge_url = bridge,
                device_serial = args.device_serial,
                app_bundle    = args.app_bundle,
                use_cache  = not args.no_cache,
                no_cache   = args.no_cache,
            )
            if ok:
                passed += 1
            else:
                failures.append(task_name)

    # ── Teardown ──────────────────────────────────────────────────────────────
    if pool:
        await pool.shutdown()
    if browser:
        try:
            await browser.close()
        except Exception:
            pass

    duration = time.time() - t_suite
    reporter.end_suite()

    # ── Write reports ─────────────────────────────────────────────────────────
    if args.report:
        path = reporter.write()
        print(f"[runner] HTML report: {path}")
    if args.junit:
        path = junit.write()
        print(f"[runner] JUnit XML:   {path}")

    # ── Summary ───────────────────────────────────────────────────────────────
    failed = total - passed
    pct    = int(passed / total * 100) if total else 0
    print(f"\n{'='*50}")
    print(f"  {passed}/{total} passed ({pct}%)  |  {duration:.1f}s")
    if failures:
        print(f"  Failed:")
        for f in failures:
            print(f"    ✗ {f}")
    print(f"{'='*50}\n")

    # ── Slack notification ────────────────────────────────────────────────────
    if args.slack or args.slack_on_failure:
        notify_slack_sync(
            suite_name       = suite_name,
            total            = total,
            passed           = passed,
            failed           = failed,
            duration         = duration,
            failures         = failures,
            only_on_failure  = args.slack_on_failure and not args.slack,
        )

    return 0 if failed == 0 else 1


def _pick_bridge(pool, args) -> str:
    """Pick bridge URL — simplified (pool acquire done inside _run_test for now)."""
    return getattr(args, "bridge_url", "http://localhost:22087")


# ── CLI ───────────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m agent.runner",
        description="Run UI test suites with AI compile-once execution",
    )
    p.add_argument("--suite",    required=True,
                   help="Path to a YAML file or directory of YAML test files")
    p.add_argument("--platform", default="web",
                   choices=["web", "ios", "android"],
                   help="Target platform (default: web)")
    p.add_argument("--shard",    default=None,
                   help="Shard spec like '1/4' for CI matrix (default: run all)")
    p.add_argument("--report",   default=None,
                   help="Path for HTML report (default: reports/run.html)")
    p.add_argument("--junit",    default=None,
                   help="Path for JUnit XML report (default: reports/junit.xml)")
    p.add_argument("--no-cache", action="store_true",
                   help="Always re-compile with AI (ignore cached scripts)")
    p.add_argument("--pool-size", type=int, default=1,
                   help="iOS simulator pool size for parallel execution (default: 1)")
    p.add_argument("--device",   default="iPhone 15",
                   help="iOS device model name (default: 'iPhone 15')")
    p.add_argument("--device-serial", default=None,
                   help="Android device serial for adb")
    p.add_argument("--app-bundle", default="com.apple.mobilesafari",
                   help="iOS app bundle ID")
    p.add_argument("--bridge-url", default="http://localhost:22087",
                   help="XCTest bridge URL for iOS")
    p.add_argument("--slack",           action="store_true",
                   help="Send Slack notification after run")
    p.add_argument("--slack-on-failure", action="store_true",
                   help="Send Slack notification only on failure")
    return p


def main():
    parser = _build_parser()
    args   = parser.parse_args()

    # Default report paths if flags are given without values
    if args.report is None:
        args.report = "reports/run.html"
    if args.junit is None:
        args.junit = "reports/junit.xml"

    exit_code = asyncio.run(run_suite(args))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
