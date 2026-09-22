"""A directory or a file, archived as it stands.

The plainest type, and the one most machines need most of: configuration under
/etc and /opt, mail spools, uploaded files. It produces one tar stream through
zstd and nothing else, so restoring it needs only tar and zstd, on any machine,
years later.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Any

from ..model import Artifact, BackupError, BuildContext, Component


@dataclass(frozen=True)
class PathComponent(Component):
    path: str = ""
    exclude: tuple[str, ...] = ()

    type = "path"

    @classmethod
    def from_config(cls, table: dict[str, Any]) -> PathComponent:
        name = cls._name(table)
        path = cls._required(table, "path", name)
        if not path.startswith("/"):
            raise BackupError(
                f"component {name!r}: path has to be absolute, and {path!r} is not; "
                "what it would be relative to depends on who started the backup"
            )
        excludes = table.get("exclude", [])
        if not isinstance(excludes, list):
            raise BackupError(f"component {name!r}: exclude has to be a list")
        return cls(
            name=name,
            path=path.rstrip("/") or "/",
            exclude=tuple(str(item) for item in excludes),
        )

    def artifacts(self, ctx: BuildContext) -> list[Artifact]:
        # --warning=no-file-changed and --ignore-failed-read are not politeness:
        # a live server rewrites logs while tar reads them, and without these
        # any busy file ends the backup with nothing to show for it.
        tar = ["tar", "--warning=no-file-changed", "--ignore-failed-read"]
        tar += [f"--exclude={shlex.quote(p)}" for p in self.exclude]
        tar += ["-cf", "-", "-C", "/", shlex.quote(self.path.lstrip("/"))]
        zstd = f"zstd -T{ctx.zstd_threads} -{ctx.zstd_level} -q"
        # --ignore-failed-read forgives the path itself being missing too, and
        # tar then writes an empty archive and exits 0. ls says what is gone.
        present = f"ls -d -- {shlex.quote(self.path)} >/dev/null"
        return [
            Artifact(
                name=f"{self.name}.tar.zst",
                produce=f"{present} && {' '.join(tar)} | {zstd}",
                recipe={"type": "path", "target": "/"},
                # Reading the table of contents proves the archive is whole,
                # which a checksum over the encrypted bytes does not.
                check="zstd -dc | tar -tf - >/dev/null",
            )
        ]

    def describe(self) -> dict[str, Any]:
        return {**super().describe(), "path": self.path, "exclude": list(self.exclude)}


COMPONENT = PathComponent
