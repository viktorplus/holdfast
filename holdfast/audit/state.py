"""The one thing the watchdog writes.

Four checks answer "is this the same state as last time", which means the last
report has to survive between runs. It is a single JSON file: readable by eye,
readable by the next run, and requiring no schema, no migration and no daemon.

The write goes through `holdfast.atomic`, which explains why it is atomic: the
short version is that a run interrupted halfway would otherwise leave a
truncated file, and the run after that would silently lose its baseline - the
failure mode where the watchdog stops watching and still reports PASS.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..atomic import write_json

STATE_FILE = "last.json"


def load_last(state_dir: Path) -> dict[str, Any] | None:
    """The previous report, or None when there is not a usable one."""
    path = Path(state_dir) / STATE_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        report = json.loads(text)
    except ValueError:
        # A corrupt file means the drift checks have no baseline. They say so
        # themselves, as UNKNOWN. Refusing to run would be worse.
        return None
    return report if isinstance(report, dict) else None


def save_last(state_dir: Path, report: dict[str, Any]) -> Path:
    return write_json(Path(state_dir) / STATE_FILE, report)
