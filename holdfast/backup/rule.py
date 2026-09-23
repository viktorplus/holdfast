"""What auto-discovery is and is not allowed to touch on its own.

`rule.py` turns `backup.exclude` and the components the operator already wrote
into a decision auto-discovery can consult before adding anything: `Skip`
records why one candidate was left out, `Exclusions` is the operator's own
opt-outs, and `Declared` is what a hand-written `[[component]]` already
covers, so discovery never doubles a volume, a mount, or a database the
operator already named.
"""

from __future__ import annotations

import posixpath
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from .model import BackupError, Component

EXCLUDE_KINDS = ("volume", "path", "database", "container")

_MYSQL_IMAGES = frozenset({"mysql", "mariadb", "percona", "percona-server"})
_POSTGRES_IMAGES = frozenset({"postgres", "postgis"})


@dataclass(frozen=True)
class Skip:
    """One candidate auto-discovery decided not to add, and why."""

    what: str
    reason: str

    def as_dict(self) -> dict[str, str]:
        return {"what": self.what, "reason": self.reason}


@dataclass(frozen=True)
class Exclusions:
    """The operator's own `backup.exclude`, parsed into its four kinds."""

    volumes: frozenset[str] = frozenset()
    paths: tuple[str, ...] = ()
    databases: frozenset[tuple[str, str]] = frozenset()  # (container, database)
    containers: frozenset[str] = frozenset()

    def path(self, path: str) -> bool:
        return any(under(path, excluded) for excluded in self.paths)

    def databases_of(self, container: str) -> list[str]:
        return sorted(
            database
            for excluded_container, database in self.databases
            if excluded_container == container
        )


def parse_exclusions(items: Any) -> Exclusions:
    """Turn `backup.exclude` into `Exclusions`, or say precisely why not.

    Every rejection names the offending item, verbatim, because the operator
    wrote this list by hand and the fix is in the same line the error quotes.
    """
    if not isinstance(items, list):
        raise BackupError("backup.exclude has to be a list")

    volumes: set[str] = set()
    paths: list[str] = []
    databases: set[tuple[str, str]] = set()
    containers: set[str] = set()

    for item in items:
        kind, sep, value = (
            item.partition(":") if isinstance(item, str) else ("", "", "")
        )
        if not sep or kind not in EXCLUDE_KINDS or not value:
            raise BackupError(
                f"backup.exclude: {item!r} is not one of "
                "volume:<...>, path:<...>, database:<...>, container:<...>"
            )
        if kind == "path":
            if not value.startswith("/"):
                raise BackupError(
                    f"backup.exclude: {item!r} has to name an absolute path"
                )
            paths.append(value)
        elif kind == "database":
            container, has_slash, database = value.partition("/")
            if not has_slash:
                raise BackupError(
                    f"backup.exclude: {item!r} has to be "
                    "database:<container>/<database>"
                )
            databases.add((container, database))
        elif kind == "volume":
            volumes.add(value)
        else:  # container
            containers.add(value)

    return Exclusions(
        volumes=frozenset(volumes),
        paths=tuple(paths),
        databases=frozenset(databases),
        containers=frozenset(containers),
    )


@dataclass(frozen=True)
class Declared:
    """What the operator already wrote in `[[component]]`.

    Auto-discovery reads this before adding anything, so a hand-written
    component is never doubled by a guessed one for the same volume, mount,
    or database.
    """

    names: frozenset[str] = frozenset()
    paths: tuple[str, ...] = ()
    volumes: frozenset[str] = frozenset()
    mounts: frozenset[tuple[str, str]] = frozenset()  # (container, destination)
    databases: frozenset[str] = frozenset()  # container names
    every_volume: bool = False

    @classmethod
    def of(cls, components: Iterable[Component]) -> Declared:
        names: set[str] = set()
        paths: list[str] = []
        volumes: set[str] = set()
        mounts: set[tuple[str, str]] = set()
        databases: set[str] = set()
        every_volume = False

        for component in components:
            names.add(component.name)
            if component.type == "path":
                paths.append(component.path)  # type: ignore[attr-defined]
            elif component.type == "docker_volume":
                # `volume`/`container`/`destination` are added to
                # DockerVolumeComponent in task 6; read them defensively so
                # this task can be built and tested before that lands.
                volume = getattr(component, "volume", "")
                container = getattr(component, "container", "")
                destination = getattr(component, "destination", "")
                if volume:
                    volumes.add(volume)
                elif container:
                    mounts.add((container, destination))
                else:
                    every_volume = True
            elif component.type in ("mysql", "postgres"):
                container = getattr(component, "container", "")
                if container:
                    databases.add(container)

        return cls(
            names=frozenset(names),
            paths=tuple(paths),
            volumes=frozenset(volumes),
            mounts=frozenset(mounts),
            databases=frozenset(databases),
            every_volume=every_volume,
        )

    def path(self, path: str) -> bool:
        return any(under(path, declared) for declared in self.paths)


def under(path: str, parent: str) -> bool:
    """Whether `path` is `parent` itself or something inside it.

    Both sides go through `posixpath.normpath` first, because a container's
    paths and the operator's config are Linux paths even when holdfast runs
    somewhere else.
    """
    child = posixpath.normpath(path)
    root = posixpath.normpath(parent)
    if root == "/":
        return child.startswith("/")
    return child == root or child.startswith(root + "/")


def database_kind(image: str) -> str:
    """ "mysql", "postgres", or "" for whatever else an image might be.

    Reads only the image name: strip a `@digest`, take the last `/`-segment,
    strip a `:tag`. Good enough to route a container to the right component
    type without pulling the image or asking it anything.
    """
    name = image.split("@", 1)[0].rsplit("/", 1)[-1].split(":", 1)[0]
    if name in _MYSQL_IMAGES:
        return "mysql"
    if name in _POSTGRES_IMAGES:
        return "postgres"
    return ""
