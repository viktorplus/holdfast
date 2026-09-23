"""Producing one snapshot, and refusing early rather than halfway.

The order of this file is the order of the run, and it is chosen so that
everything that can be refused is refused before a single byte is written: a
bad encryption setting, a missing tool, a bad component, another backup already
running, too little disk. After that point the run has started, and from there
the rule is the opposite one - a snapshot directory exists only when it is
whole. The work happens under a temporary name and is renamed last, so nothing
that was interrupted can be mistaken for a backup.

The shell gets exactly one line per artifact, and no more than that. The
destination file is opened here, in Python, which is why those lines contain no
redirect: there is nothing to quote and nothing for a path with a space in it
to break.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .. import jobs
from ..config import Config
from .encrypt import Encryption
from .manifest import build_manifest, digest, write_sha256sums
from .model import Artifact, BackupBusy, BackupError, BuildContext, Component
from .offsite import send
from .probe import Probe
from .rule import Skip
from .selection import Selection, select

Runner = Callable[[str, Path], int]

GIGABYTE = 1024**3
TIMESTAMP = "%Y%m%d-%H%M%S"
SNAPSHOT_SUFFIX = ".tmp"
SNAPSHOT_NAME = re.compile(r"^\d{8}-\d{6}$")


@dataclass(frozen=True)
class BackupResult:
    snapshot: str
    directory: Path
    artifacts: list[dict[str, Any]]
    total_bytes: int
    warnings: list[str] = field(default_factory=list)
    skipped: list[Skip] = field(default_factory=list)


# bash, not sh: a pipe reports only its last command unless pipefail is on,
# and the dash that is /bin/sh on Ubuntu refuses `set -o pipefail` outright.
SHELL = ("bash", "-o", "pipefail", "-c")


def shell_runner(line: str, dest: Path) -> int:
    """Run one pipeline, its output going straight into ``dest``."""
    with dest.open("wb") as handle:
        return subprocess.run([*SHELL, line], stdout=handle, check=False).returncode


def run_line(line: str) -> int:
    """Run one pipeline and report only whether it worked.

    Two runners rather than one with a flag: the backup writes an artifact's
    body into a file it opened, and the restore asks whether a pipeline
    succeeded. The output going nowhere is the whole difference, and a
    parameter deciding between them would read as if it were an option.
    """
    return subprocess.run(
        [*SHELL, line],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode


def run_backup(
    cfg: Config,
    *,
    runner: Runner | None = None,
    now: datetime | None = None,
    probe: Any = None,
    components_file: Path | None = None,
) -> BackupResult:
    runner = shell_runner if runner is None else runner
    now = now or datetime.now(UTC)
    probe = Probe() if probe is None else probe

    encryption = Encryption.from_config(cfg)
    _require_tools(encryption, runner)
    selection = select(cfg, probe, components_file)

    root = Path(str(cfg.get("backup.root")))
    jobs_dir = Path(str(cfg.get("jobs.dir")))
    snapshot = now.strftime(TIMESTAMP)

    with _lock(str(cfg.get("backup.lock_file"))):
        root.mkdir(parents=True, exist_ok=True)
        minimum_gb = int(cfg.get("backup.min_free_gb") or 0)
        _require_space(root, minimum_gb)
        _require_room(root, selection.estimate_mb, minimum_gb)

        temporary = root / f".{snapshot}{SNAPSHOT_SUFFIX}"
        os.umask(0o077)
        temporary.mkdir()
        try:
            result = _fill(
                temporary, snapshot, now, cfg, encryption, selection, probe, runner
            )
        except BaseException as exc:
            _discard(temporary, root)
            jobs.note(jobs_dir, "backup", ok=False, snapshot=snapshot, error=str(exc))
            raise

        final = root / snapshot
        os.replace(temporary, final)
        result = _with_directory(result, final)
        result.warnings.extend(_point_latest(root, final))
        # Only now, with the snapshot already final. A run of failures is
        # exactly when the old copy is the only copy, so nothing above this
        # line ever reaches it.
        keep_days = int(cfg.get("backup.retention_days") or 0)
        _, complaints = rotate(root, keep_days, now=now)
        result.warnings.extend(complaints)
        jobs.note(
            jobs_dir,
            "backup",
            ok=True,
            snapshot=snapshot,
            artifacts=len(result.artifacts),
            total_bytes=result.total_bytes,
        )

    # Outside the lock on purpose - see holdfast/backup/offsite.py. An upload
    # that hangs must not be able to forbid tomorrow's backup.
    result.warnings.extend(send(result.directory, cfg))
    return result


def dry_run(cfg: Config, *, probe: Any, components_file: Path | None = None) -> str:
    """What a backup would do, as text, without writing anything.

    The same selection the run makes, so what the operator reads here is what
    tonight's backup takes - including where each component came from and
    what auto-discovery left out, which is what they need to judge the rule.
    """
    selection = select(cfg, probe, components_file)
    encryption = Encryption.from_config(cfg)
    # The snapshot directory is only a base for comparing destinations here;
    # nothing is created under it.
    planned = _plan(
        Path(str(cfg.get("backup.root"))) / "dry-run",
        selection.components,
        _context(cfg, probe),
        encryption,
    )
    lines = [f"mode: {selection.mode}"]
    for component in selection.components:
        about = f"{component.type}, from {selection.origins[component.name]}"
        if component.name in selection.sizes_mb:
            about += f", ~{selection.sizes_mb[component.name]} MB"
        lines.append(f"{component.name} ({about})")
        for artifact in (a for c, a in planned if c is component):
            lines.append(f"  {artifact.name}{encryption.suffix}")
            lines.append(f"    {encryption.wrap(artifact.produce)}")
    if selection.skipped:
        lines += ["", "not taken:"]
        lines += [f"  {skip.what}: {skip.reason}" for skip in selection.skipped]
    if selection.estimate_mb:
        lines += [
            "",
            f"estimated size before compression: {selection.estimate_mb} MB",
        ]
    return "\n".join(lines) + "\n"


def _fill(
    directory: Path,
    snapshot: str,
    now: datetime,
    cfg: Config,
    encryption: Encryption,
    selection: Selection,
    probe: Any,
    runner: Runner,
) -> BackupResult:
    ctx = _context(cfg, probe)
    components = selection.components
    planned = _plan(directory, components, ctx, encryption)
    records: list[dict[str, Any]] = []
    total = 0
    for component, artifact in planned:
        record = _produce(directory, component, artifact, encryption, runner)
        records.append(record)
        total += record["size"]
    if not records:
        raise BackupError(
            "nothing to back up: no [[component]] produced an artifact. An empty "
            "snapshot would count as the newest backup, and rotation would "
            "remove the real ones around it"
        )

    manifest = build_manifest(
        snapshot=snapshot,
        created_at=now.isoformat(),
        host_label=str(cfg.get("host_label") or ""),
        compression={"tool": "zstd", "level": ctx.zstd_level},
        encryption=encryption.describe(),
        components=[c.describe() for c in components],
        artifacts=records,
        skipped=[skip.as_dict() for skip in selection.skipped],
    )
    (directory / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    write_sha256sums(directory)
    return BackupResult(snapshot, directory, records, total, [], selection.skipped)


def _context(cfg: Config, probe: Any) -> BuildContext:
    return BuildContext(
        zstd_level=int(cfg.get("backup.zstd_level") or 0),
        zstd_threads=int(cfg.get("backup.zstd_threads") or 0),
        probe=probe,
    )


def _plan(
    directory: Path,
    components: list[Component],
    ctx: BuildContext,
    encryption: Encryption,
) -> list[tuple[Component, Artifact]]:
    """Every artifact the run will write, checked before the first one is.

    Shared by the backup and the dry run, so the dry run cannot pass what the
    backup would refuse.
    """
    planned = [
        (component, artifact)
        for component in components
        for artifact in component.artifacts(ctx)
    ]
    _require_distinct(directory, planned, encryption)
    return planned


def _require_distinct(
    directory: Path,
    planned: list[tuple[Component, Artifact]],
    encryption: Encryption,
) -> None:
    """Refuse two artifacts that would land on the same file.

    The second would quietly replace the first, and the manifest would then
    list a file that is no longer the one it describes. Auto-discovery makes
    this reachable: two containers whose names fold to the same slug. Checked
    on the whole list before the first byte, so the run fails with nothing
    half-written.
    """
    owners: dict[Path, str] = {}
    for component, artifact in planned:
        name = artifact.name + encryption.suffix
        destination = _inside(directory, name, component.name)
        if destination in owners:
            raise BackupError(
                f"two artifacts would both be written to {name!r}: one from "
                f"component {owners[destination]!r}, one from component "
                f"{component.name!r}"
            )
        owners[destination] = component.name


def _produce(
    directory: Path,
    component: Component,
    artifact: Artifact,
    encryption: Encryption,
    runner: Runner,
) -> dict[str, Any]:
    name = artifact.name + encryption.suffix
    destination = _inside(directory, name, component.name)
    destination.parent.mkdir(parents=True, exist_ok=True)
    part = destination.with_name(destination.name + ".part")

    code = runner(encryption.wrap(artifact.produce), part)
    if code != 0:
        raise BackupError(f"component {component.name!r} failed with exit {code}")
    if part.stat().st_size == 0:
        # The pipeline reported success and produced nothing, which for a
        # backup is failure wearing the other one's face.
        raise BackupError(f"component {component.name!r} produced an empty artifact")
    part.replace(destination)

    sha, size = digest(destination)
    return {
        "path": name,
        "component": component.name,
        "size": size,
        "sha256": sha,
        # Both describe the body, not the file around it: encryption changes
        # the file name and nothing about how the contents go back.
        "restore": artifact.recipe,
        "check": artifact.check,
    }


def _inside(directory: Path, name: str, component: str) -> Path:
    """The path this artifact gets, or a refusal if it is not in the snapshot.

    Component names are checked when they are parsed, but a `command` component
    names its own artifact, and that string comes from the configuration.
    """
    destination = (directory / name).resolve()
    if not destination.is_relative_to(directory.resolve()):
        raise BackupError(
            f"component {component!r} wants to write {name!r}, "
            "which is outside the snapshot"
        )
    return destination


def _require_tools(encryption: Encryption, runner: Runner) -> None:
    if runner is shell_runner and shutil.which("bash") is None:
        raise BackupError("there is no bash on PATH, and every artifact needs one")
    if encryption.enabled and shutil.which(encryption.tool) is None:
        raise BackupError(
            f"encryption is on but {encryption.tool} is not on PATH; "
            "nothing here writes a plaintext archive instead"
        )


def _require_space(root: Path, minimum_gb: int) -> None:
    free = shutil.disk_usage(root).free
    if free < minimum_gb * GIGABYTE:
        raise BackupError(
            f"{root} has {free / GIGABYTE:.1f} GB free and "
            f"backup.min_free_gb asks for {minimum_gb}"
        )


def _require_room(root: Path, estimate_mb: int, minimum_gb: int) -> None:
    """Refuse a snapshot that would leave less than the minimum behind.

    Free space alone passes a run that the rule already knows will eat most of
    it. The estimate is before compression, so this errs on the side of
    refusing; a machine that is short by that margin is short anyway.
    """
    if estimate_mb == 0:
        return
    free = shutil.disk_usage(root).free
    if free - estimate_mb * 1024**2 < minimum_gb * GIGABYTE:
        raise BackupError(
            f"this snapshot is estimated at up to {estimate_mb} MB before "
            f"compression, {root} has {free / GIGABYTE:.1f} GB free, and "
            f"backup.min_free_gb asks for {minimum_gb} to be left"
        )


@contextlib.contextmanager
def _lock(path: str) -> Iterator[None]:
    """Held for the run, so two backups cannot write at once.

    On Windows there is none: the perimeter is Linux with systemd, and the
    portable substitutes - a lock directory, a pid file - survive a kill -9 and
    then stop every backup afterwards, silently, which is worse than the
    overlap they prevent.
    """
    try:
        import fcntl
    except ImportError:
        yield
        return

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise BackupBusy("a backup is already running") from None
        yield


def _discard(temporary: Path, root: Path) -> None:
    """Remove the half-built snapshot, and only that.

    The guard is not ceremony: this runs while something has already gone
    wrong, and a path that is not what it should be is the moment to stop
    rather than the moment to try harder.
    """
    if temporary.parent != root or not temporary.name.endswith(SNAPSHOT_SUFFIX):
        return
    shutil.rmtree(temporary, ignore_errors=True)


def _point_latest(root: Path, final: Path) -> list[str]:
    latest = root / "latest"
    try:
        if latest.is_symlink() or latest.exists():
            latest.unlink()
        latest.symlink_to(final, target_is_directory=True)
    except OSError as exc:
        # A convenience, not the snapshot. Losing it is worth saying and not
        # worth undoing a backup that is already complete.
        return [f"could not point {latest} at this snapshot: {exc}"]
    return []


def _with_directory(result: BackupResult, directory: Path) -> BackupResult:
    return BackupResult(
        result.snapshot,
        directory,
        result.artifacts,
        result.total_bytes,
        result.warnings,
        result.skipped,
    )


def rotate(
    root: Path, retention_days: int, *, now: datetime
) -> tuple[list[Path], list[str]]:
    """Delete snapshots past the window, and say what would not go.

    Returns what was removed and what could not be, rather than raising: by the
    time this runs the new snapshot is already final, and a stuck directory is
    not a reason to report the night as a failure.

    `retention_days` means what it says - older than this many days goes. The
    older script kept them a day longer, because `find -mtime +N` is true only
    at N+1 full days, and the key-rotation runbook counted on that number. See
    docs/backup.md.
    """
    if retention_days <= 0:
        return [], []

    cutoff = now.timestamp() - retention_days * 86400
    snapshots = sorted(
        (
            path
            for path in root.iterdir()
            if path.is_dir()
            and not path.is_symlink()
            and SNAPSHOT_NAME.match(path.name)
        ),
        key=lambda path: path.stat().st_mtime,
    )
    # Never the newest one. A machine whose backup broke months ago would
    # otherwise lose its last copy exactly on schedule, and the rotation would
    # look like it had worked.
    removed: list[Path] = []
    warnings: list[str] = []
    for path in snapshots[:-1]:
        if path.stat().st_mtime >= cutoff:
            continue
        try:
            shutil.rmtree(path)
        except OSError as exc:
            warnings.append(f"could not remove the old snapshot {path.name}: {exc}")
            continue
        removed.append(path)
    return removed, warnings
