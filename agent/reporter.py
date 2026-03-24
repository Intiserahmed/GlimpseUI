"""
HTML Test Reporter — generate a self-contained HTML report with screenshots.

Produces a single HTML file (no external dependencies) that shows:
  - Pass/fail summary with timing
  - Per-test expandable sections
  - Inline screenshots (base64)
  - Pixel-diff image on snapshot failures

Usage:
    from agent.reporter import Reporter

    r = Reporter(output_path="reports/run.html")
    r.begin_suite("My Test Suite")

    r.begin_test("Login flow")
    r.add_step("tap", "Tap login button", screenshot_b64="...", passed=True)
    r.end_test(passed=True, duration=2.3)

    r.end_suite()
    r.write()
    print(f"Report saved to {r.output_path}")
"""

import base64
import html
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class StepRecord:
    action:    str
    label:     str
    passed:    bool
    error:     str  = ""
    duration:  float = 0.0
    screenshot: Optional[str] = None   # base64 JPEG
    diff_image: Optional[str] = None   # base64 JPEG (snapshot diff)


@dataclass
class TestRecord:
    name:      str
    passed:    bool = True
    duration:  float = 0.0
    steps:     list = field(default_factory=list)
    error:     str  = ""


@dataclass
class SuiteRecord:
    name:     str
    tests:    list = field(default_factory=list)
    started:  float = field(default_factory=time.time)
    finished: float = 0.0


class Reporter:
    def __init__(self, output_path: str = "reports/run.html"):
        self.output_path = Path(output_path)
        self.output_path.parent.mkdir(parents=True, exist_ok=True)

        self._suite: Optional[SuiteRecord] = None
        self._current_test: Optional[TestRecord] = None

    # ── Suite lifecycle ───────────────────────────────────────────────────────

    def begin_suite(self, name: str):
        self._suite = SuiteRecord(name=name)

    def end_suite(self):
        if self._suite:
            self._suite.finished = time.time()

    # ── Test lifecycle ────────────────────────────────────────────────────────

    def begin_test(self, name: str):
        self._current_test = TestRecord(name=name)
        self._test_start = time.time()

    def end_test(self, passed: bool, duration: float = None, error: str = ""):
        if not self._current_test:
            return
        self._current_test.passed   = passed
        self._current_test.duration = duration if duration is not None \
                                      else (time.time() - self._test_start)
        self._current_test.error    = error
        if self._suite:
            self._suite.tests.append(self._current_test)
        self._current_test = None

    # ── Step recording ────────────────────────────────────────────────────────

    def add_step(
        self,
        action:     str,
        label:      str,
        passed:     bool = True,
        error:      str  = "",
        duration:   float = 0.0,
        screenshot: Optional[str] = None,
        diff_image: Optional[str] = None,
    ):
        if not self._current_test:
            return
        self._current_test.steps.append(StepRecord(
            action    = action,
            label     = label,
            passed    = passed,
            error     = error,
            duration  = duration,
            screenshot= screenshot,
            diff_image= diff_image,
        ))

    # ── Shorthand helpers ─────────────────────────────────────────────────────

    def pass_step(self, action: str, label: str, **kw):
        self.add_step(action, label, passed=True, **kw)

    def fail_step(self, action: str, label: str, error: str = "", **kw):
        self.add_step(action, label, passed=False, error=error, **kw)

    # ── Write HTML ────────────────────────────────────────────────────────────

    def write(self):
        html_content = self._render()
        self.output_path.write_text(html_content, encoding="utf-8")
        return str(self.output_path)

    def _render(self) -> str:
        suite = self._suite
        if not suite:
            suite = SuiteRecord(name="Test Run")

        total    = len(suite.tests)
        passed   = sum(1 for t in suite.tests if t.passed)
        failed   = total - passed
        duration = suite.finished - suite.started if suite.finished else 0.0
        pct      = int(passed / total * 100) if total else 0
        bar_color = "#4caf50" if failed == 0 else "#f44336"

        tests_html = "\n".join(self._render_test(t, i) for i, t in enumerate(suite.tests))

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(suite.name)} — Test Report</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
          margin: 0; background: #f5f5f5; color: #333; }}
  header {{ background: #1a1a2e; color: #fff; padding: 24px 32px; }}
  header h1 {{ margin: 0 0 8px; font-size: 22px; }}
  .meta {{ opacity: .7; font-size: 13px; }}
  .summary {{ display: flex; gap: 24px; padding: 20px 32px;
              background: #fff; border-bottom: 1px solid #e0e0e0; flex-wrap: wrap; }}
  .stat {{ text-align: center; }}
  .stat .val {{ font-size: 32px; font-weight: 700; line-height: 1; }}
  .stat .lbl {{ font-size: 12px; color: #888; margin-top: 4px; }}
  .green {{ color: #4caf50; }} .red {{ color: #f44336; }} .gray {{ color: #888; }}
  .bar-wrap {{ flex: 1; display: flex; align-items: center; min-width: 200px; }}
  .bar {{ height: 8px; background: #eee; border-radius: 4px; flex: 1; overflow: hidden; }}
  .bar-fill {{ height: 100%; background: {bar_color}; width: {pct}%; transition: width .4s; }}
  .tests {{ padding: 16px 32px; max-width: 1100px; margin: 0 auto; }}
  details {{ background: #fff; border-radius: 8px; margin-bottom: 10px;
             box-shadow: 0 1px 3px rgba(0,0,0,.08); overflow: hidden; }}
  summary {{ padding: 14px 18px; cursor: pointer; display: flex;
             align-items: center; gap: 12px; list-style: none; }}
  summary::-webkit-details-marker {{ display: none; }}
  .badge {{ width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; }}
  .badge.pass {{ background: #4caf50; }} .badge.fail {{ background: #f44336; }}
  .test-name {{ font-weight: 600; flex: 1; }}
  .dur {{ color: #888; font-size: 13px; }}
  .steps {{ border-top: 1px solid #f0f0f0; }}
  .step {{ display: flex; align-items: flex-start; gap: 12px;
           padding: 10px 18px; border-bottom: 1px solid #f8f8f8; font-size: 13px; }}
  .step:last-child {{ border-bottom: none; }}
  .step-icon {{ font-size: 14px; width: 18px; text-align: center; flex-shrink: 0; margin-top: 1px; }}
  .step-body {{ flex: 1; }}
  .step-action {{ font-family: monospace; background: #f0f0f0;
                  padding: 1px 5px; border-radius: 3px; font-size: 11px; }}
  .step-label {{ margin-top: 2px; color: #555; }}
  .step-error {{ color: #c62828; margin-top: 4px; font-size: 12px; }}
  .step-ss {{ margin-top: 8px; }}
  .step-ss img {{ max-width: 280px; border-radius: 4px; border: 1px solid #ddd;
                  cursor: pointer; transition: max-width .2s; }}
  .step-ss img:focus, .step-ss img:hover {{ max-width: 100%; outline: none; }}
  .diff-lbl {{ font-size: 11px; color: #e65100; margin-top: 4px; }}
  .error-box {{ padding: 14px 18px; background: #fff3f3;
                border-top: 1px solid #ffcdd2; color: #c62828; font-size: 13px; }}
</style>
</head>
<body>
<header>
  <h1>{html.escape(suite.name)}</h1>
  <div class="meta">
    {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(suite.started))}
    &nbsp;·&nbsp; {duration:.1f}s total
  </div>
</header>
<div class="summary">
  <div class="stat"><div class="val">{total}</div><div class="lbl">TOTAL</div></div>
  <div class="stat"><div class="val green">{passed}</div><div class="lbl">PASSED</div></div>
  <div class="stat"><div class="val {'red' if failed else 'gray'}">{failed}</div><div class="lbl">FAILED</div></div>
  <div class="bar-wrap"><div class="bar"><div class="bar-fill"></div></div></div>
  <div class="stat"><div class="val">{pct}%</div><div class="lbl">PASS RATE</div></div>
</div>
<div class="tests">
{tests_html}
</div>
</body>
</html>"""

    def _render_test(self, test: TestRecord, idx: int) -> str:
        badge  = "pass" if test.passed else "fail"
        icon   = "✓" if test.passed else "✗"
        steps  = "\n".join(self._render_step(s) for s in test.steps)
        err_box = f'<div class="error-box">{html.escape(test.error)}</div>' \
                  if test.error else ""
        open_attr = "" if test.passed else " open"

        return f"""<details{open_attr}>
  <summary>
    <div class="badge {badge}"></div>
    <span class="test-name">{html.escape(test.name)}</span>
    <span class="dur">{test.duration:.2f}s</span>
    <span style="color:{'#4caf50' if test.passed else '#f44336'};font-weight:700">{icon}</span>
  </summary>
  <div class="steps">{steps}</div>
  {err_box}
</details>"""

    def _render_step(self, step: StepRecord) -> str:
        icon  = "✓" if step.passed else "✗"
        color = "#4caf50" if step.passed else "#f44336"
        err   = f'<div class="step-error">⚠ {html.escape(step.error)}</div>' \
                if step.error else ""

        ss_html = ""
        if step.screenshot:
            ss_html = f'''<div class="step-ss">
              <img src="data:image/jpeg;base64,{step.screenshot}" alt="screenshot" tabindex="0">
            </div>'''
        if step.diff_image:
            ss_html += f'''<div class="diff-lbl">Pixel diff:</div>
            <div class="step-ss">
              <img src="data:image/jpeg;base64,{step.diff_image}" alt="diff" tabindex="0">
            </div>'''

        return f"""<div class="step">
  <div class="step-icon" style="color:{color}">{icon}</div>
  <div class="step-body">
    <span class="step-action">{html.escape(step.action)}</span>
    <div class="step-label">{html.escape(step.label)}</div>
    {err}
    {ss_html}
  </div>
</div>"""
