"""What auto-discovery is and is not allowed to touch on its own.

`rule.py` turns `backup.exclude` and the components the operator already wrote
into a decision auto-discovery can consult before adding anything: `Skip`
records why one candidate was left out, `Exclusions` is the operator's own
opt-outs, and `Declared` is what a hand-written `[[component]]` already
covers, so discovery never doubles a volume, a mount, or a database the
operator already named.

`plan` is the rule itself. Its picture of the machine is one `docker inspect`
of every container, running or stopped (`machine.parse_inspect`), plus the
list of volumes Docker knows; it reads nothing else off the host, so what it
decides is exactly what Docker says the machine runs. From that it takes four
groups, because each needs a different way of copying to be restorable:

- databases, as dumps, since copying a live data directory gives files the
  server may not be able to open again;
- compose project directories, whole, since that is where the compose file,
  its `.env` and whatever the project keeps beside them live;
- volumes, named or anonymous, since nothing on the host names them otherwise;
- bind mounts outside the project directories, since that data is somewhere
  the project directory does not reach.

Everything it considers and does not take goes into `Plan.skipped` with the
reason, instead of silently falling away. A backup that quietly omits a
volume looks exactly like one that took it, until the restore; a written
reason is something the operator can read tonight and disagree with.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from .machine import Container
from .model import BackupError, Component, slug

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
            # Normalised here, once: GNU tar still archives a folder named in
            # --exclude with a trailing slash, and "//" would never compare
            # equal to the paths Docker reports.
            paths.append(posixpath.normpath("/" + value.lstrip("/")))
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
                volume = component.volume  # type: ignore[attr-defined]
                container = component.container  # type: ignore[attr-defined]
                destination = component.destination  # type: ignore[attr-defined]
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


# Checked in this order, the first one present wins: the MariaDB name before
# the MySQL one, the password before a file holding it.
_MYSQL_PASSWORD_NAMES = (
    "MARIADB_ROOT_PASSWORD",
    "MYSQL_ROOT_PASSWORD",
    "MARIADB_ROOT_PASSWORD_FILE",
    "MYSQL_ROOT_PASSWORD_FILE",
)
_MYSQL_EMPTY_PASSWORD_NAMES = (
    "MARIADB_ALLOW_EMPTY_ROOT_PASSWORD",
    "MYSQL_ALLOW_EMPTY_PASSWORD",
)
# Docker names an anonymous volume with 64 hex characters and nothing else.
_ANONYMOUS_VOLUME = re.compile(r"[0-9a-f]{64}")
_SYSTEM_FILES = frozenset({"/", "/etc/localtime", "/etc/timezone"})
_SYSTEM_TREES = ("/proc", "/sys", "/dev", "/run", "/var/run", "/var/lib/docker")


@dataclass(frozen=True)
class Plan:
    """What the rule decided: tables to back up, and everything it left out.

    `measure` pairs each table name with what it will read ("path" or
    "volume"), so the room a backup needs can be summed before it starts.
    """

    tables: list[dict[str, Any]]  # [[component]] tables, in order
    skipped: list[Skip]
    measure: list[tuple[str, str, str]]  # (component name, "path"|"volume", what)
    warnings: list[str] = field(default_factory=list)


def _data_dir(container: Container, kind: str) -> str:
    if kind == "mysql":
        return "/var/lib/mysql"
    return container.env.get("PGDATA") or "/var/lib/postgresql/data"


def _password_env(container: Container) -> str:
    """The variable holding MySQL's root password, by name, never by value.

    "" means the image was told to allow an empty root password. With
    neither, the rule has no way in, and only the operator can give one.
    """
    for name in _MYSQL_PASSWORD_NAMES:
        if name in container.env_names:
            return name
    if any(container.env.get(name) for name in _MYSQL_EMPTY_PASSWORD_NAMES):
        return ""
    raise BackupError(
        f"container {container.name!r}: its root password is not in its "
        "environment, so holdfast cannot reach the database; declare a mysql "
        "component for it in holdfast.toml with defaults_file, or exclude it "
        f'with "container:{container.name}"'
    )


def _system(source: str) -> bool:
    return (
        source in _SYSTEM_FILES
        or source.endswith(".sock")
        or any(under(source, tree) for tree in _SYSTEM_TREES)
    )


def plan(
    containers: Sequence[Container],
    volumes: Sequence[str],
    *,
    exclude: Exclusions,
    declared: Declared,
    backup_root: str,
    exists: Callable[[str], bool],
) -> Plan:
    """Decide what a Docker host keeps, from what it runs.

    Pure: `exists` is the one question it asks of the host, so tests can
    answer it. Databases go first, because their data mounts decide what the
    three file groups must leave to the dump; that is remembered even for an
    excluded database, whose data files are still no substitute for a dump.
    """
    tables: list[dict[str, Any]] = []
    skipped: list[Skip] = []
    measure: list[tuple[str, str, str]] = []
    used = set(declared.names)

    def name_for(base: str) -> str:
        name, n = base, 2
        while name in used:
            name, n = f"{base}-{n}", n + 1
        used.add(name)
        return name

    containers = sorted(containers, key=lambda c: c.name)

    # Databases.
    data_volumes: set[str] = set()
    data_binds: set[str] = set()
    for c in containers:
        kind = database_kind(c.image)
        if not kind:
            continue
        data_dir = _data_dir(c, kind)
        data = [
            m
            for m in c.mounts
            if m.kind in ("volume", "bind") and under(data_dir, m.destination)
        ]
        data_volumes.update(m.name for m in data if m.kind == "volume")
        data_binds.update(m.source for m in data if m.kind == "bind")

        what = f"database container {c.name}"
        if c.name in exclude.containers:
            # No dump is taken for an excluded container, but its data files
            # are still remembered as "data": a bind under a compose project
            # directory must stay out of that project's own archive either
            # way. The volume loop checks exclusion before "covered by the
            # dump" so this reads "excluded by you" there; bind mounts have
            # no such generic check - they are gathered per non-excluded
            # container - so it is said here instead.
            for m in data:
                if m.kind == "bind":
                    skipped.append(Skip(f"bind mount {m.source}", "excluded by you"))
            skipped.append(Skip(what, "excluded by you"))
            continue
        if c.name in declared.databases:
            skipped.append(Skip(what, "declared by hand"))
            continue
        name = name_for(slug(c.name))
        if kind == "mysql":
            table: dict[str, Any] = {
                "type": "mysql",
                "name": name,
                "container": c.name,
                "user": "root",
                "databases": ["*"],
                "credentials": "container_env",
                "password_env": _password_env(c),
            }
        else:
            table = {
                "type": "postgres",
                "name": name,
                "container": c.name,
                "user": c.env.get("POSTGRES_USER") or "postgres",
                "databases": ["*"],
                "globals": True,
            }
        excluded = exclude.databases_of(c.name)
        if excluded:
            table["exclude_databases"] = excluded
        tables.append(table)
        measure.extend(
            (name, "volume", m.name) if m.kind == "volume" else (name, "path", m.source)
            for m in data
        )

    # Compose project directories, one per working_dir.
    projects: dict[str, str] = {}  # working_dir -> project name
    for c in containers:
        if c.working_dir and c.working_dir not in projects:
            projects[c.working_dir] = c.project or posixpath.basename(
                posixpath.normpath(c.working_dir)
            )
    taken: list[str] = []  # bind sources kept as path tables, filled below
    reported: set[str] = set()  # excluded paths that already have a Skip line

    def carve(kept: str) -> list[str]:
        """What a kept path table has to leave out of its own archive.

        Everything below it that the rule reports as not taken, or taken by
        another table: a database's live data (dumped, declared or excluded,
        a copy of its files is no backup either way), the operator's path
        exclusions, the other project directories, the snapshots, and the
        other kept binds. Without this a bind of /root would archive
        /root/myapp a second time with its raw database files, and a bind of
        /opt would archive the backups being written into /opt/backups.
        """
        root = posixpath.normpath(kept)
        inside = {
            posixpath.normpath(p)
            for p in [*data_binds, *exclude.paths, *projects, backup_root, *taken]
            if under(p, root) and posixpath.normpath(p) != root
        }
        for p in exclude.paths:
            if p in inside and p not in reported:
                reported.add(p)
                skipped.append(Skip(f"path {p}", "excluded by you"))
        return sorted({p.lstrip("/") for p in inside})

    for wd in sorted(projects):
        what = f"compose project {projects[wd]} ({wd})"
        if exclude.path(wd):
            skipped.append(Skip(what, "excluded by you"))
        elif declared.path(wd):
            skipped.append(Skip(what, "declared by hand"))
        elif not exists(wd):
            skipped.append(Skip(what, "the project directory is not on this machine"))
        else:
            name = name_for("project-" + slug(projects[wd]))
            tables.append(
                {"type": "path", "name": name, "path": wd, "exclude": carve(wd)}
            )
            measure.append((name, "path", wd))

    # Volumes, named and anonymous; one table however many containers share it.
    users: dict[str, list[tuple[str, str]]] = {}  # volume -> (container, dest)
    for c in containers:
        for m in c.mounts:
            if m.kind == "volume" and m.name:
                users.setdefault(m.name, []).append((c.name, m.destination))
    for v in sorted(set(volumes) | set(users)):
        mounted = sorted(users.get(v, []))
        what = f"volume {v}"
        if not mounted:
            skipped.append(Skip(what, "no container uses it"))
        elif v in exclude.volumes or all(
            container in exclude.containers for container, _ in mounted
        ):
            # Checked before "covered by the dump": an excluded database's
            # own data volume is excluded too, not dumped, so it must not
            # claim a dump that never runs.
            skipped.append(Skip(what, "excluded by you"))
        elif v in data_volumes:
            skipped.append(Skip(what, "database data, covered by the dump"))
        elif (
            declared.every_volume
            or v in declared.volumes
            or any(user in declared.mounts for user in mounted)
        ):
            skipped.append(Skip(what, "declared by hand"))
        else:
            # Name the volume after a container that is kept: one exists,
            # since a volume whose every user is excluded was skipped above.
            container, destination = next(
                user for user in mounted if user[0] not in exclude.containers
            )
            if _ANONYMOUS_VOLUME.fullmatch(v):
                name = name_for(f"volume-{slug(container)}-{slug(destination)}")
                tables.append(
                    {
                        "type": "docker_volume",
                        "name": name,
                        "container": container,
                        "destination": destination,
                    }
                )
            else:
                name = name_for("volume-" + slug(v))
                tables.append({"type": "docker_volume", "name": name, "volume": v})
            measure.append((name, "volume", v))

    # Bind mounts outside the project directories, shortest first, so a
    # parent is taken before anything inside it is looked at.
    sources = sorted(
        {
            m.source
            for c in containers
            if c.name not in exclude.containers
            for m in c.mounts
            if m.kind == "bind"
        },
        key=lambda s: (len(s), s),
    )
    for source in sources:
        if source in data_binds:
            reason = "database data, covered by the dump"
        elif _system(source):
            reason = "system"
        elif under(source, backup_root):
            reason = "the snapshots themselves"
        elif any(under(source, wd) for wd in projects):
            reason = "inside a compose project directory"
        elif any(under(source, parent) for parent in taken):
            reason = "inside another bind mount already taken"
        elif not exists(source):
            reason = "not on this machine"
        elif exclude.path(source):
            reason = "excluded by you"
        elif declared.path(source):
            reason = "declared by hand"
        else:
            name = name_for("mount-" + slug(source))
            tables.append(
                {"type": "path", "name": name, "path": source, "exclude": carve(source)}
            )
            measure.append((name, "path", source))
            taken.append(source)
            continue
        skipped.append(Skip(f"bind mount {source}", reason))

    # An exclusion that matches nothing is most likely a typo, and a typo
    # here means something the operator wanted left out is being kept.
    places = [
        *projects,
        *(m.source for c in containers for m in c.mounts if m.kind == "bind"),
    ]
    known_volumes = set(volumes) | set(users)
    names = {c.name for c in containers}
    database_containers = {c.name for c in containers if database_kind(c.image)}
    unmatched = [
        *(f"volume:{v}" for v in sorted(exclude.volumes) if v not in known_volumes),
        *(
            f"path:{p}"
            for p in exclude.paths
            if not any(under(p, where) or under(where, p) for where in places)
        ),
        *(
            f"database:{c}/{d}"
            for c, d in sorted(exclude.databases)
            if c not in database_containers
        ),
        *(f"container:{c}" for c in sorted(exclude.containers) if c not in names),
    ]
    warnings = [
        f"backup.exclude: {item!r} matches nothing on this machine"
        for item in unmatched
    ]
    return Plan(tables=tables, skipped=skipped, measure=measure, warnings=warnings)
