"""What a backup component is, and what it owes the engine.

A component declares one part of this machine worth keeping. It does three
things and no more: it describes itself for the manifest, it hands over the
shell line that produces its bytes, and it says how those bytes are checked.
Ordering, encryption, checksums, the manifest, rotation, free space and what to
do when something fails all stay in the engine, so that adding a type means
answering three questions rather than understanding a pipeline.

The shell line writes to stdout and touches no file. The engine opens the
destination itself, which is why no line here contains a redirect: nothing has
to be quoted, and a path with a space in it cannot break anything.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, ClassVar

NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

# A database user as a restore recipe may carry it. Here rather than in the
# snapshot reader so that the components can refuse the same names when the
# backup is declared, instead of the restore refusing them after the fact.
USER_NAME = re.compile(r"^[A-Za-z0-9_.-]*$")


def slug(text: str) -> str:
    """Turn a name a human or Docker chose into one NAME accepts.

    Auto-discovery reads container names, not operator-picked ones, so it
    cannot assume the result already fits: this is the one place that makes
    it fit, lower-cased with anything outside [a-z0-9_-] folded to a dash and
    the edges trimmed, falling back to a fixed name rather than an empty one.
    """
    cleaned = re.sub(r"[^a-z0-9_-]+", "-", text.lower()).strip("-_")
    return cleaned or "component"


class BackupError(Exception):
    """Something the operator can fix, in the configuration or on the machine."""


class BackupBusy(BackupError):
    """A backup is already running.

    Its own class because the command line answers it with 0, not 1: the
    nightly timer overlapping a manual run is not a failure, and reporting one
    every such night is how real failures stop being read.
    """


class RestoreError(Exception):
    """Something wrong with a snapshot, a key, or where it is being restored.

    Separate from BackupError because the two commands fail at different
    things and say so differently: a backup complains about this machine, a
    restore complains about a directory somebody handed it.
    """


def _default_probe() -> Any:
    # Imported here and not at the top: probe.py needs BackupError from this
    # module, and a module cannot import the module that imports it.
    from .probe import Probe

    return Probe()


@dataclass(frozen=True)
class BuildContext:
    """The settings every component shares, and nothing else.

    A component gets this rather than the whole configuration, so that a type
    cannot quietly start reading keys that belong to someone else.
    """

    zstd_level: int = 10
    zstd_threads: int = 0
    probe: Any = field(default_factory=_default_probe)
    """How a type asks the machine what it has. See probe.py.

    It lives here so that `artifacts(ctx)` keeps one argument: three of the
    five types need to ask something, and two do not.
    """


@dataclass(frozen=True)
class Artifact:
    """One file in the snapshot, described before it exists."""

    name: str
    """Path inside the snapshot, without the encryption suffix."""

    produce: str
    """A shell line writing the body to stdout and nothing to disk."""

    recipe: dict[str, Any]
    """How to put the body back. Copied into the manifest verbatim."""

    check: str
    """A shell line reading the body on stdin, non-zero if it is not readable."""


@dataclass(frozen=True)
class Component:
    name: str

    type: ClassVar[str] = ""

    @classmethod
    def from_config(cls, table: dict[str, Any]) -> Component:
        return cls(name=cls._name(table))

    @staticmethod
    def _name(table: dict[str, Any]) -> str:
        name = table.get("name")
        if not isinstance(name, str) or not name:
            raise BackupError("a component needs a name")
        if not NAME.match(name):
            # The name becomes part of a path inside the snapshot, so "../" in
            # it would write outside one.
            raise BackupError(
                f"{name!r} is not a usable component name: "
                "lowercase letters, digits, dash and underscore, "
                "starting with a letter or a digit"
            )
        return name

    @staticmethod
    def _required(table: dict[str, Any], key: str, name: str) -> str:
        value = table.get(key)
        if not isinstance(value, str) or not value:
            raise BackupError(f"component {name!r} needs {key}")
        return value

    def artifacts(self, ctx: BuildContext) -> list[Artifact]:
        raise NotImplementedError

    def describe(self) -> dict[str, Any]:
        """What the manifest says about this component.

        Never more than that. The manifest is plaintext inside the snapshot, so
        anything a describe() returns is readable by whoever holds the disk.
        """
        return {"type": self.type, "name": self.name}

    def containers(self) -> list[str]:
        """Containers this component owns.

        Nothing reads this during a backup. The restore does: it stops them
        before writing and starts them afterwards, because loading a dump into
        a running database is how half-restored data happens. Declared here
        because a type's contract is declared once, not appended to five
        classes later.
        """
        return []
