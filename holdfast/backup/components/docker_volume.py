"""Docker volumes, archived from wherever Docker says they live.

One component, many artifacts: a machine's volumes are a set that changes, and
declaring each one by hand would mean the backup quietly stops covering the
volume added last month. The declaration is a rule instead - everything, minus
these names, minus anything larger than this.

There is also a form that takes exactly one volume: by its name, or - for a
volume compose created without one - by the container that mounts it and the
path it is mounted at. Such a volume's name is random and different on every
machine compose brings the application up on, so the container and the path
are the only description of it that survives a move.

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

from ..model import Artifact, BackupError, BuildContext, Component, slug

VOLUME_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
CHECK = "zstd -dc | tar -tf - >/dev/null"


@dataclass(frozen=True)
class DockerVolumeComponent(Component):
    exclude: tuple[str, ...] = ()
    max_mb: int = 512
    volume: str = ""
    container: str = ""
    destination: str = ""

    type = "docker_volume"

    @classmethod
    def from_config(cls, table: dict[str, Any]) -> DockerVolumeComponent:
        name = cls._name(table)
        if any(key in table for key in ("volume", "container", "destination")):
            return cls._one(name, table)
        excluded = table.get("exclude", [])
        if not isinstance(excluded, list):
            raise BackupError(f"component {name!r}: exclude has to be a list")
        return cls(
            name=name,
            exclude=tuple(str(item) for item in excluded),
            max_mb=int(table.get("max_mb", 512)),
        )

    @classmethod
    def _one(cls, name: str, table: dict[str, Any]) -> DockerVolumeComponent:
        if "exclude" in table or "max_mb" in table:
            # A size limit on the one volume the operator named would quietly
            # drop the very thing they asked for.
            raise BackupError(
                f"component {name!r}: exclude and max_mb belong to the form that "
                "takes every volume, not to one named volume"
            )
        volume = str(table.get("volume") or "")
        container = str(table.get("container") or "")
        destination = str(table.get("destination") or "")
        if volume and (container or destination):
            raise BackupError(
                f"component {name!r}: name the volume, or its container and "
                "destination - not both"
            )
        if not volume and not (container and destination):
            raise BackupError(
                f"component {name!r}: a container and a destination go together; "
                "one of them alone does not say which volume"
            )
        chosen = volume or container
        if not VOLUME_NAME.match(chosen):
            raise BackupError(
                f"component {name!r}: the name {chosen!r} cannot be stored "
                "safely - it becomes part of the recipe inside the snapshot"
            )
        if container and (
            not destination.startswith("/") or ".." in Path(destination).parts
        ):
            raise BackupError(
                f"component {name!r}: the destination {destination!r} has to be "
                "an absolute path inside the container, without '..'"
            )
        return cls(
            name=name, volume=volume, container=container, destination=destination
        )

    def artifacts(self, ctx: BuildContext) -> list[Artifact]:
        if self.volume or self.container:
            return [self._explicit(ctx)]
        found: list[Artifact] = []
        for volume in ctx.probe.volumes():
            if volume in self.exclude:
                continue
            if not VOLUME_NAME.match(volume):
                raise BackupError(
                    f"component {self.name!r}: the volume name {volume!r} cannot "
                    "be stored safely - it becomes a file name inside the snapshot"
                )
            where = self._where(volume, ctx)
            if ctx.probe.directory_size_mb(where) > self.max_mb:
                # The operator's own rule, not something going wrong. The rule
                # is in the manifest, which is how its absence is explained.
                continue
            found.append(
                Artifact(
                    name=f"docker-volumes/{volume}.tar.zst",
                    produce=self._archive(where, ctx),
                    recipe={"type": "docker_volume", "volume": volume},
                    check=CHECK,
                )
            )
        return found

    def _explicit(self, ctx: BuildContext) -> Artifact:
        if self.volume:
            name = f"docker-volumes/{self.volume}.tar.zst"
            where = self._where(self.volume, ctx)
            recipe = {"type": "docker_volume", "volume": self.volume}
        else:
            # The recipe keeps the container and the path, not the volume's
            # name: that name means nothing on the machine the snapshot goes
            # back to.
            volume = ctx.probe.mounted_volume(self.container, self.destination)
            name = (
                f"docker-volumes/{slug(self.container)}--"
                f"{slug(self.destination)}.tar.zst"
            )
            where = self._where(volume, ctx)
            recipe = {
                "type": "docker_volume",
                "container": self.container,
                "destination": self.destination,
            }
        return Artifact(
            name=name, produce=self._archive(where, ctx), recipe=recipe, check=CHECK
        )

    def _where(self, volume: str, ctx: BuildContext) -> str:
        where = ctx.probe.volume_mountpoint(volume)
        if not Path(where).is_dir():
            raise BackupError(
                f"component {self.name!r}: docker says the volume {volume!r} "
                f"is at {where}, and that is not a directory this can read. "
                "Under rootless Docker or a non-local volume driver it will "
                "not be one; name the volume in exclude if it is not wanted "
                "here."
            )
        return where

    @staticmethod
    def _archive(where: str, ctx: BuildContext) -> str:
        zstd = f"zstd -T{ctx.zstd_threads} -{ctx.zstd_level} -q"
        return (
            "tar --warning=no-file-changed --ignore-failed-read "
            f"-cf - -C {shlex.quote(where)} . | {zstd}"
        )

    def describe(self) -> dict[str, Any]:
        if self.volume or self.container:
            return {
                **super().describe(),
                "volume": self.volume,
                "container": self.container,
                "destination": self.destination,
            }
        return {
            **super().describe(),
            "exclude": list(self.exclude),
            "max_mb": self.max_mb,
        }

    def containers(self) -> list[str]:
        return [self.container] if self.container else []


COMPONENT = DockerVolumeComponent
