"""The declared seam, for whatever the other types do not cover.

The honest answer to "it has to back up any software": five types are supported
properly, and for the sixth there is a place to stand. The operator's command
is used exactly as written - holdfast does not wrap it in its own compression,
because then the manifest would describe something other than what was stored.

What holdfast cannot supply is knowledge of the format, so it claims none. With
no declared recipe the artifact says so, and the restore leaves it alone rather
than guessing. With no declared check, all it claims is that the bytes were
readable end to end, which after decryption is a real statement and not much of
one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..model import Artifact, BackupError, BuildContext, Component

NO_RECIPE = {"type": "none", "note": "declared by the operator"}


@dataclass(frozen=True)
class CommandComponent(Component):
    produce: str = ""
    artifact: str = ""
    check: str = "cat >/dev/null"
    restore: dict[str, Any] = field(default_factory=dict)

    type = "command"

    @classmethod
    def from_config(cls, table: dict[str, Any]) -> CommandComponent:
        name = cls._name(table)
        restore = table.get("restore", {})
        if not isinstance(restore, dict):
            raise BackupError(f"component {name!r}: restore has to be a table")
        return cls(
            name=name,
            produce=cls._required(table, "produce", name),
            artifact=str(table.get("artifact") or f"{name}.bin"),
            check=str(table.get("check") or "cat >/dev/null"),
            restore=dict(restore),
        )

    def artifacts(self, ctx: BuildContext) -> list[Artifact]:
        return [
            Artifact(
                name=self.artifact,
                produce=self.produce,
                # Copied through untouched. The restore reads it; this does not
                # interpret it, because it was not written here.
                recipe=dict(self.restore) if self.restore else dict(NO_RECIPE),
                check=self.check,
            )
        ]

    def describe(self) -> dict[str, Any]:
        # The command itself stays out: it can hold a path and the name of an
        # internal tool, and the manifest sits in the snapshot in plaintext.
        return {**super().describe(), "artifact": self.artifact}


COMPONENT = CommandComponent
