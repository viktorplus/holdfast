"""Looking at a snapshot, checking one, and putting one back.

Three commands over one snapshot object, and they differ in how much they
claim. `list` opens no key and touches nothing. `verify` reads every artifact
through the key and the format check and writes nothing but the journal.
`restore` carries out the recipes.
"""

from __future__ import annotations

import shlex
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import jobs
from .components.mysql import CONTAINER_ENV, in_container
from .decrypt import Identity, Run
from .model import RestoreError
from .services import Services, owners
from .snapshot import Record, Snapshot


@dataclass(frozen=True)
class VerifyResult:
    snapshot: str
    checked: int
    failures: list[str]

    @property
    def ok(self) -> bool:
        return not self.failures


def render_list(snapshot: Snapshot) -> str:
    """What is in this copy, answered without a key."""
    encryption = snapshot.encryption
    sealed = (
        f"encrypted with {encryption.get('tool')}"
        if encryption.get("enabled")
        else "not encrypted"
    )
    lines = [
        f"snapshot {snapshot.snapshot} from {snapshot.host_label or 'an unnamed machine'}",
        f"taken {snapshot.created_at}, {sealed}",
        "",
    ]
    if not snapshot.records:
        # Saying it, rather than leaving a gap where the list would be: a
        # snapshot nothing can be restored from is a thing worth reading twice.
        lines.append("  nothing in this snapshot has a recipe to put it back")
    for record in snapshot.records:
        lines.append(
            f"  {record.size:>12}  {record.component:<16} "
            f"{record.kind:<16} {record.path}"
        )
    if snapshot.unrestorable:
        lines.append("")
        lines.append(
            "  these are in the snapshot, and nothing here knows how to put them back:"
        )
        for record in snapshot.unrestorable:
            lines.append(
                f"  {record.size:>12}  {record.component:<16} {'-':<16} {record.path}"
            )
    return "\n".join(lines)


def verify(
    snapshot: Snapshot,
    identity: Identity,
    *,
    jobs_dir: Path,
    run: Run,
) -> VerifyResult:
    """Read every artifact back, and write down that it was read.

    Every artifact, including the ones no recipe can put back: those bytes are
    in the copy too, and a copy is either whole or it is not.

    And every one of them even after the first failure. A second run in the
    middle of an incident is another half hour, and the answer wanted then is
    the whole list.
    """
    failures: list[str] = []
    everything = [*snapshot.records, *snapshot.unrestorable]
    for record in everything:
        failure = _check(record, identity, run)
        if failure:
            failures.append(failure)

    jobs.record(
        jobs_dir,
        "verify",
        ok=not failures,
        snapshot=snapshot.snapshot,
        checked=len(everything),
        failures=failures,
    )
    return VerifyResult(snapshot.snapshot, len(everything), failures)


def _check(record: Record, identity: Identity, run: Run) -> str | None:
    if not record.checksum_ok():
        return f"{record.path}: the checksum does not match what the manifest records"
    # The key is consulted before the format check, so that a run stops on a
    # wrong key rather than reporting every artifact as unreadable.
    line = f"{identity.stream(record.file)} | {record.check}"
    if run(line) != 0:
        return (
            f"{record.path}: the bytes decrypt but do not read as what they claim to be"
        )
    return None


# --------------------------------------------------------------------------
# restore
# --------------------------------------------------------------------------

# The order the recipes run in, and it is not the order of the manifest.
# Files and volumes go in while the services are down; the dumps go in after
# they are back up, because a dump is loaded by a running server. Globals
# before databases: a database restored before its roles exist belongs to
# nobody.
BEFORE = ("path", "docker_volume")
AFTER = ("pg_globals", "pg_database", "mysql_database")


@dataclass(frozen=True)
class RestoreResult:
    snapshot: str
    done: list[str]
    skipped: list[Record]
    warnings: list[str]


def restore(
    snapshot: Snapshot,
    identity: Identity,
    *,
    jobs_dir: Path,
    probe,
    run: Run,
    root: str = "/",
    component: str | None = None,
    dry_run: bool = False,
    assume_yes: bool = False,
    stop: bool = True,
    from_other_host: bool = False,
    this_label: str = "",
    pg_container: str | None = None,
    pg_user: str | None = None,
    say: Callable[[str], None] = print,
    ask: Callable[[str], str] | None = None,
) -> RestoreResult:
    """Carry out every recipe in order, live data first with services down.

    A dry run journals nothing: it restored nothing, and a recorded success
    would push the memory of a real restore out of the file ``restore_tested``
    reads.
    """
    chosen = snapshot.select(component)
    _same_machine(snapshot, this_label, from_other_host)
    _rebasable(chosen, root)

    # Everything checkable, checked, while nothing has been touched. Leaving
    # this to whoever remembered to run verify first means writing a corrupt
    # artifact into production the one time they were in a hurry.
    for record in chosen:
        if not record.checksum_ok():
            raise RestoreError(
                f"{record.path}: the checksum does not match the manifest. "
                "Nothing has been changed."
            )
    identity.probe(chosen, run)

    ordered = _in_order(chosen)
    _announce(snapshot, ordered, root, component, dry_run, say)
    _confirm(snapshot, dry_run, assume_yes, ask)

    services = Services(
        owners(ordered, probe, pg_container=pg_container),
        probe,
        dry_run=dry_run,
        enabled=stop,
    )
    done: list[str] = []
    try:
        services.stop()
        _carry_out(
            [r for r in ordered if r.kind in BEFORE],
            identity,
            probe,
            run,
            root,
            pg_container,
            pg_user,
            dry_run,
            done,
        )
        services.start()
        _carry_out(
            [r for r in ordered if r.kind in AFTER],
            identity,
            probe,
            run,
            root,
            pg_container,
            pg_user,
            dry_run,
            done,
        )
    # Anything at all, an interrupt included: whatever stopped the recipes,
    # the containers stopped above must not stay down because of it.
    except BaseException as exc:  # noqa: BLE001
        complaints = services.bring_up()
        cause = "interrupted" if isinstance(exc, KeyboardInterrupt) else str(exc)
        if not dry_run:
            jobs.record(
                jobs_dir,
                "restore",
                ok=False,
                snapshot=snapshot.snapshot,
                error=cause,
            )
        raise RestoreError(
            f"{cause}\n"
            "The system is PARTIALLY RESTORED: some recipes ran and some did "
            "not. Fix the cause and run the same command again - the recipes "
            "can be repeated safely."
            + ("\n" + "\n".join(complaints) if complaints else "")
        ) from None

    if not dry_run:
        jobs.record(
            jobs_dir,
            "restore",
            ok=True,
            snapshot=snapshot.snapshot,
            artifacts=len(done),
        )
    return RestoreResult(snapshot.snapshot, done, list(snapshot.unrestorable), [])


def _same_machine(snapshot: Snapshot, this_label: str, allowed: bool) -> None:
    """Restoring one machine's data onto another is almost never meant."""
    if allowed or (this_label and this_label == snapshot.host_label):
        return
    if not this_label:
        raise RestoreError(
            f"this snapshot was taken on {snapshot.host_label!r}, and this "
            "machine has no host_label, so there is nothing to check it "
            "against. Pass --from-other-host if this is the right machine."
        )
    raise RestoreError(
        f"this snapshot was taken on {snapshot.host_label!r}, and this machine "
        f"is {this_label!r}. Restoring one machine's data onto another is "
        "almost never meant; pass --from-other-host if it is."
    )


def _rebasable(records: list[Record], root: str) -> None:
    """--root moves files; a volume or a database has no scratch copy to go to.

    Carried on regardless, a rehearsal would overwrite the live ones.
    """
    if root in ("", "/"):
        return
    live = sorted({r.component for r in records if r.kind != "path"})
    if live:
        raise RestoreError(
            f"--root moves only files, and {', '.join(live)} would go into the "
            "live volumes and databases. Rehearse one file component at a time "
            "with --component. Nothing has been changed."
        )


def _in_order(records: list[Record]) -> list[Record]:
    phase = {kind: i for i, kind in enumerate((*BEFORE, *AFTER))}
    return sorted(records, key=lambda r: phase.get(r.kind, len(phase)))


def _announce(
    snapshot: Snapshot,
    ordered: list[Record],
    root: str,
    component: str | None,
    dry_run: bool,
    say: Callable[[str], None],
) -> None:
    say(f"This will OVERWRITE live data on {snapshot.host_label or 'this machine'}.")
    say(f"  snapshot:   {snapshot.snapshot}")
    say(f"  root:       {root}")
    say(f"  scope:      {component or 'all components'}")
    for record in ordered:
        say(f"  {record.kind:<16} {record.path}")
    for record in snapshot.unrestorable:
        say(f"  {'not restored':<16} {record.path} (no recipe was declared for it)")
    if dry_run:
        say("dry run: nothing will be changed")


def _confirm(
    snapshot: Snapshot,
    dry_run: bool,
    assume_yes: bool,
    ask: Callable[[str], str] | None,
) -> None:
    if dry_run or assume_yes:
        return
    if ask is None:
        raise RestoreError(
            "this is not an interactive session, so there is nobody to confirm "
            "with; pass --yes if that is what you meant"
        )
    label = snapshot.host_label or "the machine"
    if ask(f"Type the label of this machine ({label}) to continue: ").strip() != label:
        raise RestoreError("that did not match, and nothing was restored")


def _carry_out(
    records: list[Record],
    identity: Identity,
    probe,
    run: Run,
    root: str,
    pg_container: str | None,
    pg_user: str | None,
    dry_run: bool,
    done: list[str],
) -> None:
    for record in records:
        for line in _lines_for(record, identity, probe, root, pg_container, pg_user):
            if dry_run:
                continue
            if run(line) != 0:
                raise RestoreError(f"{record.path}: the restore command failed")
        done.append(record.path)


def _lines_for(
    record: Record,
    identity: Identity,
    probe,
    root: str,
    pg_container: str | None,
    pg_user: str | None,
) -> list[str]:
    body = identity.stream(record.file)
    recipe = record.recipe
    if record.kind == "path":
        return _path_lines(body, str(recipe.get("target") or "/"), root)
    if record.kind == "docker_volume":
        return _volume_lines(body, str(recipe["volume"]), probe)
    container = pg_container or str(recipe.get("container") or "")
    if record.kind == "pg_globals":
        user = pg_user or str(recipe.get("user") or "")
        return _globals_lines(body, container, user)
    if record.kind == "pg_database":
        user = pg_user or str(recipe.get("user") or "")
        return _database_lines(body, container, user, str(recipe["database"]))
    return _mysql_lines(body, recipe)


def _inside(target: str, root: str) -> str:
    """Where a path recipe lands, once --root has had its say.

    Re-basing is what makes a rehearsal into a scratch directory possible, and
    a rehearsal is the only way anybody finds out a snapshot restores before
    the day they need it to.
    """
    if root in ("", "/"):
        return target
    return root.rstrip("/") + "/" + target.lstrip("/")


def _exec(container: str, command: str, *, stdin: bool = False) -> str:
    if not container:
        return command
    flag = "-i " if stdin else ""
    return f"docker exec {flag}{shlex.quote(container)} {command}"


def _path_lines(body: str, target: str, root: str) -> list[str]:
    dest = shlex.quote(_inside(target, root))
    return [f"mkdir -p -- {dest} && {body} | zstd -dc | tar -xf - -C {dest}"]


def _volume_lines(body: str, volume: str, probe) -> list[str]:
    probe.capture(
        ["docker", "volume", "create", volume],
        what=f"making sure the volume {volume!r} is there",
        timeout=30,
    )
    where = probe.volume_mountpoint(volume)
    if not Path(where).is_dir():
        raise RestoreError(
            f"docker says the volume {volume!r} is at {where}, and that is not a "
            "directory this can write to. Under rootless Docker or a non-local "
            "driver it will not be one."
        )
    quoted = shlex.quote(where)
    # Emptied first, and refused if it could not be: unpacking on top of what
    # is left is a merge of the old data and the new one, not a restore. rm
    # ignores what it could not remove - immutable, busy, read-only - so the
    # emptiness has to be checked rather than assumed.
    complaint = "the volume would not empty; refusing to merge old data with new"
    empty = (
        f"rm -rf -- {quoted}/..?* {quoted}/.[!.]* {quoted}/* 2>/dev/null; "
        f'[ -z "$(find {quoted} -mindepth 1 -print -quit)" ] || '
        f'{{ echo "{complaint}" >&2; exit 1; }}'
    )
    return [empty, f"{body} | zstd -dc | tar -xf - -C {quoted}"]


def _ready(container: str, command: str) -> str:
    """Wait for a server to answer before talking to it.

    A container that has just been started is not a server that is accepting
    connections yet, and the difference is the whole restore.
    """
    probe_line = _exec(container, command)
    return (
        f"i=0; while [ $i -lt 90 ]; do {probe_line} >/dev/null 2>&1 && break; "
        f"i=$((i+1)); sleep 1; done; {probe_line} >/dev/null 2>&1"
    )


def _globals_lines(body: str, container: str, user: str) -> list[str]:
    quoted = shlex.quote(user)
    # ON_ERROR_STOP is deliberately not set: roles that already exist make psql
    # noisy, and that is not a reason to abandon a restore.
    return [
        _ready(container, f"pg_isready -U {quoted}"),
        f"{body} | zstd -dc | "
        + _exec(container, f"psql -U {quoted} -d postgres -q", stdin=True),
    ]


def _database_lines(body: str, container: str, user: str, database: str) -> list[str]:
    user_q, db_q = shlex.quote(user), shlex.quote(database)
    create = _exec(
        container,
        f"psql -U {user_q} -d postgres -q -c "
        + shlex.quote(f'create database "{database}";'),
    )
    return [
        _ready(container, f"pg_isready -U {user_q}"),
        f"{create} >/dev/null 2>&1 || true",
        # No zstd here: pg_dump -Fc carries its own compression.
        f"{body} | "
        + _exec(
            container,
            f"pg_restore -U {user_q} -d {db_q} --clean --if-exists --no-owner",
            stdin=True,
        ),
    ]


def _mysql_lines(body: str, recipe: dict[str, Any]) -> list[str]:
    """The same credentials the dump was taken with.

    A dump that needed a defaults file or the container's own password to come
    out needs the same to go back in; without it the one restore that matters
    ends in Access denied.
    """
    container = str(recipe.get("container") or "")
    user = str(recipe.get("user") or "")
    if recipe.get("credentials") == CONTAINER_ENV:
        password_env = str(recipe.get("password_env") or "")
        login = f"-u{shlex.quote(user or 'root')}"
        ping = "sh -c " + shlex.quote(
            in_container("admin", password_env, f"{login} ping")
        )
        load = "sh -c " + shlex.quote(in_container("client", password_env, login))
    else:
        defaults_file = str(recipe.get("defaults_file") or "")
        # --defaults-file first, for the same reason as in the dump.
        flags = (
            f"--defaults-file={shlex.quote(defaults_file)} " if defaults_file else ""
        )
        flags += f"-u {shlex.quote(user)} " if user else ""
        ping = f"mysqladmin {flags}ping"
        load = f"mysql {flags}".rstrip()
    return [
        _ready(container, ping),
        f"{body} | zstd -dc | " + _exec(container, load, stdin=True),
    ]
