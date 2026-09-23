"""`holdfast backup --discover`: write down what the rule finds, in manual mode.

Manual mode backs up a fixed list, and this is how the rule's findings join
that list without the operator typing them: the rule runs once, here, and
what it found is written to components.toml, which every later backup reads
as it stands until `--discover` is run again. It is a file of its own because
holdfast.toml belongs to the operator - its comments cannot survive a round
trip through `tomllib` - and a file that is always rewritten whole is one
nobody has to merge. The printed difference is what makes rerunning it safe:
the operator sees what was added, removed or changed before the next backup
acts on it.

In auto mode the rule runs at every backup instead, so a written list would
be a second, stale answer to the same question; nothing is written, and the
message points at `--dry-run`, which shows what tonight's run will take.

An error from the rule - a database whose password is not in its container's
environment - fails `--discover` exactly as it would fail an auto backup,
before the file is touched. Writing the list without that database would
record a backup that silently leaves it out, which is the one outcome worse
than an error the operator has to answer.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..config import Config
from .components_file import difference, write
from .registry import components_from_tables
from .selection import backup_mode, plan_for

AUTO = (
    'backup.mode is "auto": what is kept is worked out again at every backup, '
    "so there is nothing to write down. `holdfast backup --dry-run` shows what "
    "the next run takes.\n"
)


def discover(cfg: Config, probe: Any, components_file: Path) -> str:
    """Run the rule and, in manual mode, write what it found; say what changed."""
    if backup_mode(cfg) == "auto":
        return AUTO

    declared = components_from_tables(cfg.get("component", []))
    found = plan_for(cfg, probe, declared)
    before = write(components_file, found.tables)

    lines = [f"wrote {components_file} ({len(found.tables)} components)", ""]
    changes = difference(before, found.tables)
    lines += ["changes:", *changes] if changes else ["no changes"]
    if found.skipped:
        lines += ["", "not taken:"]
        lines += [f"  {skip.what}: {skip.reason}" for skip in found.skipped]
    return "\n".join(lines) + "\n"
