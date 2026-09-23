"""components.toml: the list `holdfast backup --discover` writes, and nobody else.

holdfast.toml is the operator's file, comments and all, and `tomllib` reads
TOML without being able to give it back - so discovery never rewrites it. It
writes this second file instead, whole, every time, from a renderer small
enough to cover every value the rule produces and to refuse anything else. A
file that is replaced whole needs no merge logic, and the previous one is kept
beside it as `.prev`, so a run that took something away can be undone by hand.

The first line of `HEADER` is also how `select` tells this file from the draft
holdfast 0.2 told operators to redirect into the same path: that draft was
meant to be read and pasted, never backed up as it stood.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import tomllib
from pathlib import Path
from typing import Any

from .. import atomic
from .model import BackupError

HEADER = """\
# Written by `holdfast backup --discover`. Every run replaces this file whole,
# and keeps the one before it as components.toml.prev - edits made here will
# not survive. Your own components and exclusions belong in holdfast.toml.
"""


def written_by_discover(path: Path) -> bool:
    """True only when the file's first line is HEADER's first line exactly."""
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            first = handle.readline().rstrip("\r\n")
    except FileNotFoundError:
        return False
    except OSError as exc:
        # Most often /etc/holdfast read by a user who is not root: a sentence
        # naming the file, not a traceback.
        raise BackupError(f"reading {path}: {exc}") from exc
    return first == HEADER.splitlines()[0]


def read_tables(path: Path) -> list[dict[str, Any]]:
    """The `[[component]]` tables in ``path``; a file that is not there has none."""
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise BackupError(f"reading {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise BackupError(f"{path} is not valid TOML: {exc}") from exc
    tables = data.get("component", [])
    if not isinstance(tables, list) or not all(isinstance(t, dict) for t in tables):
        raise BackupError(f"{path}: component is not a list of [[component]] tables")
    return tables


def _value(key: str, value: Any) -> str:
    # bool first: True is an int to isinstance, and would be written as 1.
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        # A JSON string is a TOML basic string, except that TOML also forbids
        # a raw DEL, which JSON leaves alone.
        return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")
    if isinstance(value, list) and not any(isinstance(v, list) for v in value):
        return "[" + ", ".join(_value(key, v) for v in value) + "]"
    raise BackupError(f"cannot write {key} = {value!r} to components.toml")


def render(tables: list[dict[str, Any]]) -> str:
    """HEADER, then each table as `[[component]]` with its keys in order."""
    blocks = [HEADER]
    for table in tables:
        lines = ["[[component]]"]
        lines += [f"{key} = {_value(key, value)}" for key, value in table.items()]
        blocks.append("\n".join(lines) + "\n")
    return "\n".join(blocks)


def write(path: Path, tables: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Replace ``path`` with ``tables``; return what it held before.

    What it held is read before anything is touched, and a file that cannot
    be read as a table list held nothing as far as the difference is
    concerned - it is still kept as `.prev`, since it may be the only copy of
    somebody's work.
    """
    text = render(tables)
    try:
        previous = read_tables(path)
    except BackupError:
        previous = []
    try:
        if path.exists():
            kept = path.with_name(path.name + ".prev")
            shutil.copyfile(path, kept)
            # The same content as components.toml, so the same 0600; copyfile
            # would leave it at the umask. Windows ignores it, as in atomic.
            with contextlib.suppress(OSError):
                kept.chmod(0o600)
        atomic.write_text(path, text)
    except OSError as exc:
        # /etc/holdfast without root, most often.
        raise BackupError(f"writing {path}: {exc}") from exc
    return previous


def difference(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> list[str]:
    """What changed, by (type, name): added, then removed, then changed."""
    old = {(t.get("type"), t.get("name")): t for t in before}
    new = {(t.get("type"), t.get("name")): t for t in after}

    def lines(mark: str, keys) -> list[str]:
        return sorted(f"  {mark} {name} ({kind})" for kind, name in keys)

    return [
        *lines("+", new.keys() - old.keys()),
        *lines("-", old.keys() - new.keys()),
        *lines("~", (k for k in new.keys() & old.keys() if new[k] != old[k])),
    ]
