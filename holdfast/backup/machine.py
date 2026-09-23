"""Reading `docker inspect` into a picture of the machine.

Auto-discovery needs to know what is running before it can decide what to
back up, and the only source for that is Docker itself. `parse_inspect`
answers that question once per run, into a plain list of `Container`: every
later task in the plan (the rule, selection, the engine) reads that list
rather than shelling out to `docker inspect` again mid-run, so a container
that starts or stops while a backup is deciding what to do cannot make the
plan and the execution disagree with each other.

A container's environment can hold a database password, and the manifest
this project writes ends up readable by whoever holds the snapshot. So this
module keeps every variable's *name* (a rule needs to know that
`POSTGRES_PASSWORD` was set, to route around dumping with `-p`) but only a
short allow-list of *values*: `PGDATA` and `POSTGRES_USER` are paths and
usernames, not secrets, and anything named `*ALLOW_EMPTY*` is a boolean flag
a rule needs to read. Nothing else survives parsing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .model import BackupError

_KEPT_NAMES = {"PGDATA", "POSTGRES_USER"}


def _kept(name: str) -> bool:
    return name in _KEPT_NAMES or "ALLOW_EMPTY" in name


@dataclass(frozen=True)
class Mount:
    kind: str  # "volume" | "bind" | whatever Docker says
    name: str  # volume name, "" for a bind
    source: str  # host path
    destination: str  # path inside the container


@dataclass(frozen=True)
class Container:
    name: str  # without the leading "/"
    image: str  # Config.Image
    running: bool  # State.Running
    project: str = ""  # label com.docker.compose.project
    working_dir: str = ""  # label com.docker.compose.project.working_dir
    mounts: tuple[Mount, ...] = ()
    env_names: frozenset[str] = frozenset()
    env: dict[str, str] = field(default_factory=dict)  # only the kept values


def _parse_mount(entry: dict) -> Mount:
    return Mount(
        kind=entry.get("Type", ""),
        name=entry.get("Name", ""),
        source=entry.get("Source", ""),
        destination=entry.get("Destination", ""),
    )


def _parse_container(entry: dict) -> Container:
    config = entry.get("Config") or {}
    state = entry.get("State") or {}
    labels = config.get("Labels") or {}
    mounts = tuple(_parse_mount(m) for m in (entry.get("Mounts") or []))

    names: set[str] = set()
    kept: dict[str, str] = {}
    for line in config.get("Env") or []:
        name, _, value = line.partition("=")
        names.add(name)
        if _kept(name):
            kept[name] = value

    return Container(
        name=entry.get("Name", "").lstrip("/"),
        image=config.get("Image", ""),
        running=bool(state.get("Running", False)),
        project=labels.get("com.docker.compose.project", ""),
        working_dir=labels.get("com.docker.compose.project.working_dir", ""),
        mounts=mounts,
        env_names=frozenset(names),
        env=kept,
    )


def parse_inspect(text: str) -> list[Container]:
    """Turn the JSON `docker inspect` prints into `Container`s, sorted by name.

    Sorted so that the rest of the plan does not depend on the order Docker
    happened to answer in, which is the id order given to `docker inspect`
    and not otherwise meaningful.
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BackupError(f"docker inspect did not return JSON: {exc}") from None
    if not isinstance(data, list):
        raise BackupError("docker inspect did not return a list of containers")
    if not all(isinstance(entry, dict) for entry in data):
        raise BackupError("docker inspect returned an entry that is not a container")
    containers = [_parse_container(entry) for entry in data]
    return sorted(containers, key=lambda c: c.name)
