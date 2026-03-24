"""
JUnit XML Reporter — CI-compatible test results.

Produces a JUnit XML file that GitHub Actions, GitLab CI, Jenkins,
CircleCI and most other CI systems can parse natively.

Usage:
    from agent.junit_reporter import JUnitReporter

    r = JUnitReporter(output_path="reports/junit.xml")
    r.begin_suite("UI Tests", total=10)

    r.add_test("Login flow",   passed=True,  duration=2.3)
    r.add_test("Search works", passed=False, duration=1.1,
               error_msg="Element 'Search' not found",
               error_type="ElementNotFound")

    r.write()
"""

import html
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class CaseRecord:
    name:       str
    classname:  str
    duration:   float
    passed:     bool
    error_msg:  str  = ""
    error_type: str  = "AssertionError"
    stdout:     str  = ""   # captured step log


class JUnitReporter:
    def __init__(
        self,
        output_path: str  = "reports/junit.xml",
        suite_name:  str  = "GlimpseUI",
    ):
        self.output_path = Path(output_path)
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.suite_name  = suite_name
        self._cases: list[CaseRecord] = []
        self._started = time.time()

    def begin_suite(self, name: str):
        self.suite_name = name
        self._started   = time.time()

    def add_test(
        self,
        name:       str,
        passed:     bool,
        duration:   float = 0.0,
        classname:  str   = "",
        error_msg:  str   = "",
        error_type: str   = "AssertionError",
        stdout:     str   = "",
    ):
        self._cases.append(CaseRecord(
            name       = name,
            classname  = classname or self.suite_name,
            duration   = duration,
            passed     = passed,
            error_msg  = error_msg,
            error_type = error_type,
            stdout     = stdout,
        ))

    def write(self) -> str:
        xml = self._render()
        self.output_path.write_text(xml, encoding="utf-8")
        return str(self.output_path)

    def _render(self) -> str:
        total    = len(self._cases)
        failures = sum(1 for c in self._cases if not c.passed)
        duration = time.time() - self._started
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self._started))

        cases_xml = "\n".join(self._render_case(c) for c in self._cases)

        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<testsuites name="{_esc(self.suite_name)}" tests="{total}" '
            f'failures="{failures}" time="{duration:.3f}">\n'
            f'  <testsuite name="{_esc(self.suite_name)}" tests="{total}" '
            f'failures="{failures}" time="{duration:.3f}" timestamp="{timestamp}">\n'
            f'{cases_xml}\n'
            f'  </testsuite>\n'
            f'</testsuites>\n'
        )

    def _render_case(self, case: CaseRecord) -> str:
        lines = [
            f'    <testcase name="{_esc(case.name)}" classname="{_esc(case.classname)}" '
            f'time="{case.duration:.3f}">'
        ]

        if not case.passed:
            lines.append(
                f'      <failure type="{_esc(case.error_type)}" '
                f'message="{_esc(case.error_msg)}">'
                f'{_esc(case.error_msg)}'
                f'</failure>'
            )

        if case.stdout:
            lines.append(f'      <system-out>{_esc(case.stdout)}</system-out>')

        lines.append('    </testcase>')
        return "\n".join(lines)


def _esc(s: str) -> str:
    """XML-escape a string."""
    return (s
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&apos;"))
