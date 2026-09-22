"""Writing a small JSON file so that an interrupted run cannot truncate it.

Everything holdfast remembers between runs - the watchdog's previous report,
the journal of what each job last did - is a file of this shape. A plain write
that dies halfway leaves a half-written file, and the next run reads it as
nothing: the failure mode where the tool quietly stops remembering and still
reports success. Writing beside the target and renaming makes the swap atomic,
so a reader sees either the old file or the new one.

The mode is 0600 because the same function writes both, and one of them will
eventually hold something worth not sharing.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any


def write_text(path: Path, text: str) -> Path:
    """Write ``text`` to ``path`` atomically, mode 0600.

    What write_json below does with a dict, components.toml needs to do with
    a string it already built (TOML, not JSON), so the atomic swap moved
    here and write_json became a one-line caller of it, rather than growing
    a second copy of the same replace-in-place dance.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.new"
    temporary.write_text(text, encoding="utf-8")
    try:
        temporary.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        # Windows and some mounts do not carry POSIX modes. Worth noting rather
        # than failing over: the machines this runs on for real are Linux.
        pass
    os.replace(temporary, path)
    return path


def write_json(path: Path, data: Any) -> Path:
    """Write ``data`` to ``path`` as JSON, atomically, mode 0600."""
    return write_text(path, json.dumps(data, indent=2, sort_keys=True))
