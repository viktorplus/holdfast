"""The backups, judged from the outside.

Five checks live here now. What can be answered from the filesystem is
answered from the filesystem, which means it works whoever wrote the
snapshots. `restore_tested` and `backup_offsite_copy` are the exceptions:
both read holdfast's own job journal, because no filesystem scan can tell
whether a restore was ever carried out or a copy ever sent - and
`backup_offsite_copy` is the one check here that reaches the network, to
confirm the copy the journal describes is still where it was sent.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ... import jobs, rclone
from ..context import Context
from ..model import FAIL, GROUP_BACKUPS, PASS, UNKNOWN, WARN
from ..registry import check

MANIFEST_NAME = "manifest.json"
ENCRYPTED_SUFFIXES = (".age", ".gpg")
RESTORE_STALE_DAYS = 90

# One listing, and it must not be able to hold the report open. A wedged
# remote is exactly the machine worth getting a report about. 30 was too
# tight: this stage's own real-rclone test needed raising past it twice,
# because starting rclone on a loaded machine can alone take longer than
# that. 60 still bounds the wait the report can be held open for.
LISTING_TIMEOUT = 60


def _backup_root(ctx: Context) -> tuple[Path | None, str]:
    """Which directory to look for snapshots in, and how to show it.

    ``audit.backup.root`` is the operator's explicit answer for this check and
    always wins. Left empty, ``backup.root`` - the engine's own setting - is a
    plausible fallback, except that its default is ``/opt/backups``, a
    directory most machines do not have. Falling back onto a directory that is
    not there would manufacture a FAIL out of a default nobody configured,
    which is worse than an honest "does not know" - so the fallback is taken
    only when the directory it names actually exists.
    """
    configured = str(ctx.conf("audit.backup.root") or "").strip()
    if configured:
        return ctx.path(configured), configured
    fallback = str(ctx.conf("backup.root") or "").strip()
    if fallback and ctx.path(fallback).is_dir():
        return ctx.path(fallback), fallback
    return None, ""


def _root_unknown_detail(ctx: Context) -> str:
    """Text for when neither setting resolves to a real directory."""
    fallback = str(ctx.conf("backup.root") or "").strip()
    if fallback:
        return (
            f"audit.backup.root is not set, and backup.root ({fallback}) "
            "is not a directory here"
        )
    return "audit.backup.root is not set, and backup.root is not set either"


def _snapshots(root: Path) -> list[Path]:
    """Snapshot directories under root, newest first.

    ``latest`` is a pointer to a snapshot, not one itself - and neither is
    any other symlink an operator may have left in the backup root, for the
    same reason. A name with a leading dot is a run still being written -
    the backup engine builds each snapshot under a temporary name and
    renames it when it is done, so counting it here would judge an
    unfinished backup as though it had already landed.

    Sorted by mtime, not by name: a snapshot that was restored, copied or
    renamed keeps a name from when it was written but gets a fresh mtime, and
    `backup_freshness` reads the age off whichever directory this returns
    first - naming order and mtime order would then point at two different
    directories, and the age reported would belong to neither.
    """
    if not root.is_dir():
        return []
    return sorted(
        (
            p
            for p in root.iterdir()
            if p.is_dir()
            and not p.is_symlink()
            and p.name != "latest"
            and not p.name.startswith(".")
        ),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )


@check(
    "backup_freshness",
    GROUP_BACKUPS,
    "How old the newest snapshot is",
    "T1490",
    "high",
)
def _backup_freshness(ctx: Context):
    root, shown = _backup_root(ctx)
    manual = f"ls -1t {shown or '<backup root>'} | head -3"
    if root is None:
        return UNKNOWN, _root_unknown_detail(ctx), manual
    if not root.is_dir():
        return UNKNOWN, f"the backup directory is not there: {shown}", manual
    snapshots = _snapshots(root)
    if not snapshots:
        return FAIL, f"there is not one snapshot in {shown}", manual
    newest = snapshots[0]
    age_hours = (time.time() - newest.stat().st_mtime) / 3600
    limit = ctx.conf_int("audit.backup.max_age_hours")
    detail = (
        f"newest: {newest.name}, {age_hours:.1f} hours old against a limit of {limit}"
    )
    if age_hours > limit * 2:
        return FAIL, detail + " - the backup is not running", manual
    if age_hours > limit:
        return WARN, detail, manual
    return PASS, detail, manual


def _read_manifest(snapshot: Path) -> dict[str, Any] | None:
    """The manifest's raw contents, read locally rather than through
    ``backup/snapshot.py``'s ``load_snapshot``.

    ``load_snapshot`` refuses a snapshot this holdfast cannot fully restore -
    the wrong format version, a recipe it does not know - and it is right to:
    a restore that carried out part of a manifest and called it done would
    leave an operator believing the machine was back when some of it never
    came back at all. This check answers a different question, and has to
    answer it for exactly the snapshots ``load_snapshot`` refuses too: the
    directory being judged is not necessarily holdfast's own, since this is a
    machine's backup directory, not holdfast's. A plain ``json.loads`` reads
    whatever is there without deciding whether it can be restored.
    """
    try:
        text = (snapshot / MANIFEST_NAME).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _has_encrypted_files(snapshot: Path) -> bool:
    return any(
        p.is_file() and p.suffix in ENCRYPTED_SUFFIXES for p in snapshot.rglob("*")
    )


@check(
    "backup_encrypted",
    GROUP_BACKUPS,
    "Whether the newest snapshot needs a key to read",
    "T1005",
    "high",
)
def _backup_encrypted(ctx: Context):
    root, shown = _backup_root(ctx)
    manual = f"ls {shown or '<backup root>'}/<newest>/{MANIFEST_NAME}"
    if root is None:
        return UNKNOWN, _root_unknown_detail(ctx), manual
    if not root.is_dir():
        return UNKNOWN, f"the backup directory is not there: {shown}", manual
    snapshots = _snapshots(root)
    if not snapshots:
        return UNKNOWN, f"there is not one snapshot in {shown} to judge", manual
    newest = snapshots[0]
    manifest_present = (newest / MANIFEST_NAME).is_file()
    manifest = _read_manifest(newest) if manifest_present else None
    if manifest is not None:
        encryption = manifest.get("encryption")
        if isinstance(encryption, dict) and "enabled" in encryption:
            if encryption["enabled"]:
                tool = encryption.get("tool") or "?"
                return (
                    PASS,
                    f"{newest.name}: encrypted with {tool}, per its manifest",
                    manual,
                )
            return (
                FAIL,
                f"{newest.name}: the manifest says this snapshot is in the clear",
                manual,
            )
        reason = "the manifest says nothing about encryption"
    elif manifest_present:
        reason = "the manifest did not parse"
    else:
        reason = "no manifest"

    encrypted = _has_encrypted_files(newest)
    if encrypted:
        return (
            PASS,
            f"{newest.name}: .age/.gpg files present ({reason}), judged encrypted",
            manual,
        )
    return (
        FAIL,
        f"{newest.name}: {reason} and nothing encrypted in it - it is in the clear",
        manual,
    )


def _age_days(at: str) -> float | None:
    """Age of an ISO timestamp in days, or None when it does not parse.

    A stamp that fails to parse must not manufacture a false age - an honest
    "recorded, age unknown" beats a number invented for a string that was
    never a date.
    """
    try:
        when = datetime.fromisoformat(at)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return (datetime.now(UTC) - when).total_seconds() / 86400


@check(
    "restore_tested",
    GROUP_BACKUPS,
    "Whether a restore has ever been carried out",
    "T1490",
    "high",
)
def _restore_tested(ctx: Context):
    """A verify reads the copy back; only a restore proves it goes back in.

    ``restore.json`` is what answers this, and its absence falls back onto
    ``verify.json`` - which can only soften a FAIL into a WARN, never turn it
    into a PASS: a copy that reads is not a copy proven to restore, and a
    journal with no successful restore in it is not missing evidence, it is
    the answer - nobody has ever restored from these backups.
    """
    jobs_dir = ctx.path(str(ctx.conf("jobs.dir")))
    manual = (
        "holdfast restore --snapshot <snapshot> --identity <key> "
        "--root /tmp/holdfast-restore-drill --component <a path component> --yes"
    )

    restore_entry = jobs.last(jobs_dir, "restore")
    if restore_entry is None:
        verify_entry = jobs.last(jobs_dir, "verify")
        verify_success = (verify_entry or {}).get("last_success")
        if verify_success:
            at = str(verify_success.get("at") or "")
            return (
                WARN,
                (
                    f"verify read the copy back cleanly on {at}, but this "
                    "machine has never had a restore carried out - reading "
                    "is not restoring"
                ),
                manual,
            )
        return (
            FAIL,
            (
                "this machine has never had a restore carried out, and "
                "there is no verify to soften that either"
            ),
            manual,
        )

    success = restore_entry.get("last_success")
    if not success:
        return (
            FAIL,
            (
                "a restore was attempted and did not succeed - an "
                "unsuccessful attempt is not proof this backup restores"
            ),
            manual,
        )

    at = str(success.get("at") or "")
    age = _age_days(at)
    if age is None:
        return (
            PASS,
            (
                f"last proven restore recorded at {at!r}, which is not a "
                "timestamp this can read the age of - still a recorded success"
            ),
            manual,
        )
    detail = f"last proven restore: {at} ({age:.0f} days ago)"
    if age > RESTORE_STALE_DAYS:
        return (
            WARN,
            detail + f" - over {RESTORE_STALE_DAYS} days, due for another rehearsal",
            manual,
        )
    return PASS, detail, manual


@check(
    "storage_credentials_scope",
    GROUP_BACKUPS,
    "Whether each machine has its own storage account",
    "T1078",
    "medium",
)
def _storage_credentials_scope(ctx: Context):
    """Answerable only at the storage end, so it says so.

    From one machine there is no way to see whether the machine next door uses
    the same key. The question still belongs in the report, because the answer
    decides whether one compromised machine erases everyone's backups.
    """
    return (
        UNKNOWN,
        "one machine cannot see whether another uses the same storage key",
        (
            "check this at the storage end: every machine should have its own "
            "user and its own directory, with no access to anyone else's - "
            "otherwise one compromised machine erases every backup at once"
        ),
    )


@check(
    "backup_offsite_copy",
    GROUP_BACKUPS,
    "Whether a second copy exists somewhere else",
    "T1490",
    "high",
)
def _backup_offsite_copy(ctx: Context):
    """Answered cheapest first, and any of the three steps can end it.

    "Not configured", "never sent" and "sent, and now gone" read as three
    different sentences on purpose - an operator acts differently on each,
    and collapsing them into one FAIL would erase the difference the check
    exists to draw. The network is asked only once we have run out of
    cheaper answers, and only when it is allowed to be asked at all.
    """
    manual = "rclone lsjson --dirs-only <offsite.remote>/<host_label>"
    remote = str(ctx.conf("offsite.remote") or "").strip()
    if not remote:
        return (
            FAIL,
            (
                "offsite.remote is not configured, so nothing is ever sent - "
                "there is nothing to ask the network about"
            ),
            manual,
        )

    jobs_dir = ctx.path(str(ctx.conf("jobs.dir")))
    entry = jobs.last(jobs_dir, "offsite")
    success = (entry or {}).get("last_success")
    if not success:
        return (
            FAIL,
            (
                "this machine has never had a copy sent offsite successfully "
                "- there is nothing to look for in the storage"
            ),
            manual,
        )

    sent_snapshot = str(success.get("snapshot") or "")
    sent_at = str(success.get("at") or "")

    if not ctx.offsite_enabled:
        return (
            WARN,
            (
                f"the journal says {sent_snapshot} was last sent offsite on "
                f"{sent_at} - the storage was not asked because --no-offsite "
                "is set for this run"
            ),
            manual,
        )

    if not ctx.on_host:
        return (
            UNKNOWN,
            (
                f"the journal says {sent_snapshot} was last sent offsite on "
                f"{sent_at}, but this run is auditing a mounted tree "
                "(--host) - offsite.remote and host_label come from the "
                "config of the machine running the audit, not the one being "
                "audited, so a mounted tree cannot be asked about its own "
                "shared storage from here; add --no-offsite to silence this "
                "deliberately"
            ),
            manual,
        )

    host_label = str(ctx.conf("host_label") or "")
    try:
        target = rclone.destination(remote, host_label)
    except rclone.RcloneError as exc:
        return UNKNOWN, f"could not name the offsite destination: {exc}", manual
    manual = f"rclone lsjson --dirs-only {target}"

    try:
        names = rclone.directories(target, timeout=LISTING_TIMEOUT)
    except rclone.RcloneError as exc:
        if exc.code == rclone.DIRECTORY_NOT_FOUND:
            return (
                FAIL,
                (
                    f"{target} does not exist - this machine has no "
                    "directory in the offsite storage at all"
                ),
                manual,
            )
        return UNKNOWN, f"could not list {target}: {exc}", manual

    if sent_snapshot not in names:
        return (
            FAIL,
            (
                f"the journal says {sent_snapshot} was sent offsite on "
                f"{sent_at}, but it is not among the directories in {target} "
                "- the copy is gone"
            ),
            manual,
        )

    detail = f"{sent_snapshot} (sent {sent_at}) is present in {target}"
    root, _ = _backup_root(ctx)
    if root is not None:
        snapshots = _snapshots(root)
        if snapshots and snapshots[0].name != sent_snapshot:
            # Not "is newer locally and has not gone out yet": `_snapshots`
            # sorts by mtime, so the name compared here can belong to a
            # snapshot that was restored or copied rather than freshly made,
            # and it may already have been sent, long ago, under this same
            # name. State only what was actually compared - names - which
            # is true whichever way the mtimes turn out to disagree.
            return (
                WARN,
                (
                    f"the newest local snapshot is {snapshots[0].name}, and "
                    f"the last one sent offsite is {sent_snapshot} (sent "
                    f"{sent_at})"
                ),
                manual,
            )
    return PASS, detail, manual
