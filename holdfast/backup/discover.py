"""A first draft of what this machine keeps, for the operator to cross out.

The same job as `audit --baseline`, at the other end of the tool, and answered
the same way: crossing things out of a proposal is work somebody will do, and
writing a component list from a blank file is work they will put off. It prints
and never writes, because `tomllib` reads TOML without giving it back, and
rewriting the file would erase the comments that explain it.

Nothing here refuses. A machine with no Docker gets a draft of `path`
components and a comment saying why there is nothing else in it - discovery is
advice, and advice has nothing to fail at. That is the one place where the
tolerant reading of a silent machine is right, and it is why the strict probe
is used everywhere else.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Protocol

from .model import BackupError

MAX_MB = 512

POSTGRES = re.compile(r"(^|/)postgres", re.IGNORECASE)
MYSQL = re.compile(r"(^|/)(mysql|mariadb|percona)", re.IGNORECASE)

HEADER = """\
# A draft, not a configuration. holdfast looked at this machine and guessed;
# read every line, cross out what does not belong, and fill in what it could
# not know. Nothing was written anywhere - this is printed for you to keep.
#
#   holdfast backup --discover > /etc/holdfast/components.toml
#
# Then paste it into holdfast.toml. `holdfast backup --dry-run` will show you
# the exact command each component runs, without running any of them.
"""


class Machine(Protocol):
    """The part of the probe discovery needs."""

    def containers(self) -> list[tuple[str, str]]: ...
    def volumes(self) -> list[str]: ...
    def volume_mountpoint(self, name: str) -> str: ...
    def directory_size_mb(self, path: str) -> int: ...


def discover(machine: Machine, *, host: Path = Path("/")) -> str:
    lines = [HEADER]
    notes: list[str] = []

    containers = _ask(machine.containers, notes)
    databases = _databases(containers)
    lines.extend(databases.text)

    volumes = _ask(machine.volumes, notes)
    if volumes:
        lines.append(_volumes(machine, volumes, databases.volume_hints))

    lines.extend(_paths(host))

    if notes:
        lines.append(
            "# Docker was not asked, so nothing that lives in it is here:\n"
            + "".join(f"#   {note}\n" for note in notes)
        )
    return "\n".join(part for part in lines if part).rstrip() + "\n"


class _Databases:
    def __init__(self) -> None:
        self.text: list[str] = []
        self.volume_hints: list[str] = []


def _ask(question, notes: list[str]):
    """Whatever the machine says, or nothing plus a note explaining the gap."""
    try:
        return question()
    except BackupError as exc:
        notes.append(str(exc))
        return []


def _databases(containers: list[tuple[str, str]]) -> _Databases:
    found = _Databases()
    for name, image in containers:
        if POSTGRES.search(image):
            found.text.append(
                f"""\
[[component]]
type = "postgres"
name = {_string(_slug(name))}
container = {_string(name)}
# Check this. holdfast cannot see which role may read every database.
user = "postgres"
databases = ["*"]
globals = true
"""
            )
            found.volume_hints.append(_stem(name))
        elif MYSQL.search(image):
            found.text.append(
                f"""\
[[component]]
type = "mysql"
name = {_string(_slug(name))}
container = {_string(name)}
databases = ["*"]
# A credentials file the server reads itself. holdfast never holds a password.
# defaults_file = "/etc/holdfast/mysql.cnf"
"""
            )
            found.volume_hints.append(_stem(name))
    return found


def _volumes(machine: Machine, volumes: list[str], hints: list[str]) -> str:
    excluded: list[str] = []
    for volume in volumes:
        if any(hint and hint in volume for hint in hints):
            # A copy of a running data directory is not a backup of the
            # database; the dump above covers it.
            excluded.append(volume)
            continue
        try:
            where = machine.volume_mountpoint(volume)
            if machine.directory_size_mb(where) > MAX_MB:
                excluded.append(volume)
        except BackupError:
            excluded.append(volume)
    listed = ", ".join(_string(name) for name in excluded)
    return f"""\
[[component]]
type = "docker_volume"
name = "volumes"
# Crossed out here rather than left out: a volume missing from the draft is one
# nobody asks about. These are the ones a dump already covers, or the ones over
# max_mb.
exclude = [{listed}]
max_mb = {MAX_MB}
"""


def _paths(host: Path) -> list[str]:
    opt = host / "opt"
    if not opt.is_dir():
        return []
    found = []
    # By name, not by Path: comparing paths is case-insensitive on Windows,
    # and the draft would come out in a different order there.
    for child in sorted((p for p in opt.iterdir() if p.is_dir()), key=lambda p: p.name):
        found.append(
            f"""\
[[component]]
type = "path"
name = {_string(_slug(child.name))}
path = "/opt/{child.name}"
exclude = []
"""
        )
    return found


def _slug(name: str) -> str:
    """A component name holdfast will accept, from one a human chose."""
    cleaned = re.sub(r"[^a-z0-9_-]+", "-", name.lower()).strip("-")
    return cleaned or "component"


def _stem(container: str) -> str:
    """The part of a container name a project's volumes are likely to share."""
    return container.split("-")[0].split("_")[0]


def _string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
