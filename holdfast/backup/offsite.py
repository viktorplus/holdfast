"""Sending a finished snapshot to shared storage.

Two things about the order matter and both are load-bearing. The copy leaves
only after the snapshot directory has been renamed to its final name -
before that there is no point at which "the snapshot" and "what would be
copied" mean the same thing, since a run that fails halfway leaves nothing
behind to send. And it leaves outside the run lock: an upload can sit on a
link nobody is watching for hours, and a lock held that long would turn one
bad night into `BackupBusy` on every night after it, for a backup that had
already finished and succeeded.

A failed send is not a failed backup. The snapshot on this machine is
already whole; what is missing is the second copy, which is worth a loud
warning and never worth raising - a caller that saw an exception here would
have no way to tell "the backup failed" from "the backup is fine and the
copy of it is not".
"""

from __future__ import annotations

from pathlib import Path

from .. import jobs, rclone
from ..config import Config

# Zero, to `subprocess.run`'s `timeout`, means "wait forever" - the one
# meaning a copy to a remote must never carry, since a wedged link would then
# hold the run open with nobody watching for it.
MINIMUM_TIMEOUT_MINUTES = 1


def _expected(snapshot: Path) -> dict[str, int]:
    """What a complete copy must contain: every file under the snapshot.

    Not the manifest's list of artifacts - a copy missing `SHA256SUMS` or
    the manifest itself is not the same snapshot either, and neither of
    those two names itself in the manifest. Globbing the directory catches
    both along with everything they describe.
    """
    return {
        path.relative_to(snapshot).as_posix(): path.stat().st_size
        for path in sorted(snapshot.rglob("*"))
        if path.is_file()
    }


def _timeout_seconds(cfg: Config) -> int:
    try:
        minutes = int(cfg.get("offsite.timeout_minutes"))
    except (TypeError, ValueError, OverflowError):
        minutes = MINIMUM_TIMEOUT_MINUTES
    if minutes <= 0:
        minutes = MINIMUM_TIMEOUT_MINUTES
    return minutes * 60


def send(snapshot: Path, cfg: Config) -> list[str]:
    """Copies `snapshot` to `offsite.remote`, and never raises.

    Returns complaints fit for printing rather than raising: by the time
    this runs the backup that produced `snapshot` has already finished, and
    a network or remote problem here must not turn that into a failed night.
    An unconfigured remote is not a complaint either - it is most machines,
    and a warning repeated every night for a state nobody is about to fix
    trains everyone to stop reading warnings.

    Every step below catches `Exception`, not only `rclone.RcloneError`: a
    malformed answer from rclone or a vanished file underneath `snapshot`
    would otherwise turn this docstring's promise into a lie, and a caller
    that saw an exception here would have no way to tell "the backup failed"
    from "the backup is fine and the copy of it is not".
    """
    remote = str(cfg.get("offsite.remote") or "")
    if not remote:
        return []

    jobs_dir = Path(str(cfg.get("jobs.dir")))
    host_label = str(cfg.get("host_label") or "")
    timeout = _timeout_seconds(cfg)

    try:
        target = rclone.destination(remote, host_label, snapshot.name)
    except Exception as exc:  # noqa: BLE001
        complaint = str(exc)
        jobs.note(jobs_dir, "offsite", ok=False, error=complaint)
        return [complaint]

    try:
        rclone.copy(snapshot, target, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        complaint = f"sending {snapshot.name} to {target} failed: {exc}"
        jobs.note(jobs_dir, "offsite", ok=False, error=complaint)
        return [complaint]

    try:
        found = rclone.files(target, timeout=timeout)
        problems = rclone.missing(_expected(snapshot), found)
    except Exception as exc:  # noqa: BLE001
        complaint = f"checking the copy of {snapshot.name} at {target} failed: {exc}"
        jobs.note(jobs_dir, "offsite", ok=False, error=complaint)
        return [complaint]

    if problems:
        complaint = f"{target} does not match {snapshot.name}: " + "; ".join(problems)
        jobs.note(jobs_dir, "offsite", ok=False, error=complaint)
        return [complaint]

    jobs.note(jobs_dir, "offsite", ok=True, snapshot=snapshot.name, target=target)
    return []
