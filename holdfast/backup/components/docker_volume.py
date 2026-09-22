"""Docker volumes, archived from wherever Docker says they live.

One component, many artifacts: a machine's volumes are a set that changes, and
declaring each one by hand would mean the backup quietly stops covering the
volume added last month. The declaration is a rule instead - everything, minus
these names, minus anything larger than this.

Where a volume lives is asked, not assumed. Building
`/var/lib/docker/volumes/<name>/_data` by hand is wrong under a non-default
data-root and under rootless Docker, and wrong without saying so.

A database volume does not belong here even when it fits: a copy of a running
PostgreSQL data directory is not a backup of it. Name it in `exclude` and
declare a `postgres` component instead.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..model import Artifact, BackupError, BuildContext, Component

VOLUME_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


@dataclass(frozen=True)
class DockerVolumeComponent(Component):
    exclude: tuple[str, ...] = ()
    max_mb: int = 512

    type = "docker_volume"

    @classmethod
    def from_config(cls, table: dict[str, Any]) -> DockerVolumeComponent:
        name = cls._name(table)
        excluded = table.get("exclude", [])
        if not isinstance(excluded, list):
            raise BackupError(f"component {name!r}: exclude has to be a list")
        return cls(
            name=name,
            exclude=tuple(str(item) for item in excluded),
            max_mb=int(table.get("max_mb", 512)),
        )

    def artifacts(self, ctx: BuildContext) -> list[Artifact]:
        zstd = f"zstd -T{ctx.zstd_threads} -{ctx.zstd_level} -q"
        found: list[Artifact] = []
        for volume in ctx.probe.volumes():
            if volume in self.exclude:
                continue
            if not VOLUME_NAME.match(volume):
                raise BackupError(
                    f"component {self.name!r}: the volume name {volume!r} cannot "
                    "be stored safely - it becomes a file name inside the snapshot"
                )
            where = ctx.probe.volume_mountpoint(volume)
            if not Path(where).is_dir():
                raise BackupError(
                    f"component {self.name!r}: docker says the volume {volume!r} "
                    f"is at {where}, and that is not a directory this can read. "
                    "Under rootless Docker or a non-local volume driver it will "
                    "not be one; name the volume in exclude if it is not wanted "
                    "here."
                )
            if ctx.probe.directory_size_mb(where) > self.max_mb:
                # The operator's own rule, not something going wrong. The rule
                # is in the manifest, which is how its absence is explained.
                continue
            found.append(
                Artifact(
                    name=f"docker-volumes/{volume}.tar.zst",
                    produce=(
                        "tar --warning=no-file-changed --ignore-failed-read "
                        f"-cf - -C {shlex.quote(where)} . | {zstd}"
                    ),
                    recipe={"type": "docker_volume", "volume": volume},
                    check="zstd -dc | tar -tf - >/dev/null",
                )
            )
        return found

    def describe(self) -> dict[str, Any]:
        return {
            **super().describe(),
            "exclude": list(self.exclude),
            "max_mb": self.max_mb,
        }


COMPONENT = DockerVolumeComponent
