"""Reading a snapshot, and refusing one that cannot be honestly restored.

This is the only place that interprets a manifest, and all three restore
commands go through it, so what one of them refuses the others refuse too.

Every recipe is checked, including the ones a narrowed run will not touch. A
snapshot carrying an instruction this version cannot carry out is not one it
can restore part of: doing so leaves an operator believing the machine is back
when some of it never came back at all.

There is no counting of "artifacts parsed against artifacts claimed" here. The
source needed it because it read the manifest with sed, and a dropped line
would have vanished from the restore while the run still reported success.
`json.loads` either reads the whole document or raises.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .manifest import FORMAT, digest
from .model import RestoreError

MANIFEST = "manifest.json"

DOCKER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
DATABASE_NAME = re.compile(r"^[A-Za-z0-9_.][A-Za-z0-9_.-]*$")
USER_NAME = re.compile(r"^[A-Za-z0-9_.-]*$")

NOTHING = "none"


@dataclass(frozen=True)
class Record:
    """One artifact, as the manifest describes it and as it is on disk."""

    path: str
    component: str
    size: int
    sha256: str
    recipe: dict[str, Any]
    check: str
    file: Path

    @property
    def kind(self) -> str:
        return str(self.recipe.get("type", NOTHING))

    def checksum_ok(self) -> bool:
        actual, _ = digest(self.file)
        return actual == self.sha256


@dataclass(frozen=True)
class Snapshot:
    directory: Path
    snapshot: str
    host_label: str
    created_at: str
    encryption: dict[str, Any]
    records: list[Record]
    unrestorable: list[Record] = field(default_factory=list)

    def select(self, component: str | None) -> list[Record]:
        if component is None:
            return list(self.records)
        chosen = [r for r in self.records if r.component == component]
        if not chosen:
            known = sorted({r.component for r in self.records})
            raise RestoreError(
                f"this snapshot holds nothing for a component called "
                f"{component!r}; it holds: {', '.join(known) or 'nothing'}"
            )
        return chosen


def load_snapshot(directory: Path) -> Snapshot:
    directory = Path(directory)
    manifest = _read_manifest(directory)

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise RestoreError(f"{directory} lists no artifacts; there is nothing in it")

    records: list[Record] = []
    unrestorable: list[Record] = []
    for entry in artifacts:
        record = _record(directory, entry)
        # Every recipe, not only the selected ones.
        _validate(record)
        (unrestorable if record.kind == NOTHING else records).append(record)

    return Snapshot(
        directory=directory,
        snapshot=str(manifest.get("snapshot") or directory.name),
        host_label=str(manifest.get("host_label") or ""),
        created_at=str(manifest.get("created_at") or ""),
        encryption=dict(manifest.get("encryption") or {}),
        records=records,
        unrestorable=unrestorable,
    )


def _read_manifest(directory: Path) -> dict[str, Any]:
    path = directory / MANIFEST
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RestoreError(
            f"{directory} does not look like a snapshot: {exc}"
        ) from None
    try:
        manifest = json.loads(text)
    except ValueError as exc:
        raise RestoreError(
            f"the manifest in {directory} does not parse: {exc}"
        ) from None
    if not isinstance(manifest, dict):
        raise RestoreError(f"the manifest in {directory} is not an object")

    version = manifest.get("format")
    if version != FORMAT:
        raise RestoreError(
            f"this snapshot is format {version!r} and this holdfast reads "
            f"{FORMAT}; it needs a newer holdfast, and reading the parts it "
            "happens to understand would restore some of the machine and "
            "quietly leave the rest"
        )
    return manifest


def _record(directory: Path, entry: Any) -> Record:
    if not isinstance(entry, dict):
        raise RestoreError("an entry in the manifest's artifact list is not an object")
    path = str(entry.get("path") or "")
    if not path:
        raise RestoreError("an artifact in the manifest has no path")
    sha = str(entry.get("sha256") or "")
    if not sha:
        raise RestoreError(
            f"the manifest records no sha256 for {path}; nothing can vouch for "
            "those bytes, so they are not going to be written anywhere"
        )
    file = directory / path
    if not file.is_file():
        raise RestoreError(f"the manifest lists {path}, and it is not in the snapshot")
    recipe = entry.get("restore")
    if not isinstance(recipe, dict):
        raise RestoreError(f"the manifest gives {path} no restore recipe")
    return Record(
        path=path,
        component=str(entry.get("component") or ""),
        size=int(entry.get("size") or 0),
        sha256=sha,
        recipe=recipe,
        check=str(entry.get("check") or "cat >/dev/null"),
        file=file,
    )


# -- recipe validation -----------------------------------------------------


def _text(recipe: dict[str, Any], key: str, path: str) -> str:
    value = recipe.get(key)
    if not isinstance(value, str) or not value:
        raise RestoreError(f"the {recipe.get('type')} recipe for {path} has no {key}")
    return value


def _matching(value: str, pattern: re.Pattern[str], what: str, path: str) -> None:
    if not pattern.match(value):
        raise RestoreError(f"{what} {value!r} in the recipe for {path} is not usable")


def _path_recipe(recipe: dict[str, Any], path: str) -> None:
    target = _text(recipe, "target", path)
    if not target.startswith("/"):
        raise RestoreError(
            f"the path recipe for {path} has a relative target {target!r}; "
            "where it would land would depend on who ran the restore"
        )
    if ".." in Path(target).parts:
        raise RestoreError(
            f"the path recipe for {path} climbs out of its target: {target!r}"
        )


def _volume_recipe(recipe: dict[str, Any], path: str) -> None:
    _matching(_text(recipe, "volume", path), DOCKER_NAME, "the volume name", path)


def _postgres_recipe(recipe: dict[str, Any], path: str) -> None:
    container = recipe.get("container") or ""
    if container:
        _matching(str(container), DOCKER_NAME, "the container name", path)
    _matching(str(recipe.get("user") or ""), USER_NAME, "the user name", path)
    if recipe.get("type") == "pg_database":
        _matching(_text(recipe, "database", path), DATABASE_NAME, "the database", path)


def _mysql_recipe(recipe: dict[str, Any], path: str) -> None:
    container = recipe.get("container") or ""
    if container:
        _matching(str(container), DOCKER_NAME, "the container name", path)
    _matching(_text(recipe, "database", path), DATABASE_NAME, "the database", path)


RECIPES: dict[str, Callable[[dict[str, Any], str], None]] = {
    "path": _path_recipe,
    "docker_volume": _volume_recipe,
    "pg_globals": _postgres_recipe,
    "pg_database": _postgres_recipe,
    "mysql_database": _mysql_recipe,
    NOTHING: lambda recipe, path: None,
}


def _validate(record: Record) -> None:
    check = RECIPES.get(record.kind)
    if check is None:
        raise RestoreError(
            f"the recipe {record.kind!r} for {record.path} is one this holdfast "
            "does not know how to carry out; it will not restore part of a "
            "snapshot and call that a restore"
        )
    check(record.recipe, record.path)
