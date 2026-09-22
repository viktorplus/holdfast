"""What each job last did, and when it last did it successfully.

Successful backups already leave a trace: every snapshot is a directory with a
manifest, and `backup_freshness` reads them. Three things leave none - a run
that failed, a snapshot that was verified, and a copy that was sent to shared
storage - and this journal exists for those.

Two fields rather than one. A single "last run" means tonight's failure erases
the memory of last night's success, and the check that asks "when was a restore
last proven to work" would answer "never" for a machine that has proven it every
week for a year.

There is no history here. If the web interface or the digests need one later,
that is something added beside this, not a change to it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .atomic import write_json

KINDS = ("backup", "verify", "restore", "offsite")


def _path(jobs_dir: Path, kind: str) -> Path:
    if kind not in KINDS:
        raise ValueError(f"unknown job kind {kind!r}; known kinds: {', '.join(KINDS)}")
    return Path(jobs_dir) / f"{kind}.json"


def last(jobs_dir: Path, kind: str) -> dict[str, Any] | None:
    """The journal for ``kind``, or None when there is not a usable one."""
    try:
        text = _path(jobs_dir, kind).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        entry = json.loads(text)
    except ValueError:
        return None
    return entry if isinstance(entry, dict) else None


def record(jobs_dir: Path, kind: str, ok: bool, **fields: Any) -> Path:
    """Note that ``kind`` has just run, and whether it worked."""
    path = _path(jobs_dir, kind)
    entry = {
        "at": datetime.now(UTC).isoformat(),
        "ok": ok,
        **fields,
    }
    previous = last(jobs_dir, kind) or {}
    return write_json(
        path,
        {
            "kind": kind,
            "last_run": entry,
            "last_success": entry if ok else previous.get("last_success"),
        },
    )


def note(jobs_dir: Path, kind: str, **fields: Any) -> None:
    """Record `kind`'s outcome without letting the record fail the caller.

    Losing the journal must not turn a finished run into a failure, nor hide
    the failure that brought the caller here in the first place - both
    `backup/engine.py` and `backup/offsite.py` call this only after a run has
    already succeeded or already failed on its own terms.
    """
    try:
        record(jobs_dir, kind, **fields)
    except OSError:
        pass
