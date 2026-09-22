"""The watchdog: a register of read-only checks and a report."""

from .context import Context
from .model import FAIL, GROUPS, PASS, UNKNOWN, WARN, Check, Finding
from .registry import all_checks, check
from .report import (
    Report,
    exit_code,
    render_baseline,
    render_checks_markdown,
    render_coverage,
    render_json,
    render_text,
    run_audit,
)
from .state import load_last, save_last

__all__ = [
    "FAIL",
    "GROUPS",
    "PASS",
    "UNKNOWN",
    "WARN",
    "Check",
    "Context",
    "Finding",
    "Report",
    "all_checks",
    "check",
    "exit_code",
    "load_last",
    "render_baseline",
    "render_checks_markdown",
    "render_coverage",
    "render_json",
    "render_text",
    "run_audit",
    "save_last",
]
