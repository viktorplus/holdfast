import hashlib
import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
from support import (
    ORPHAN_VOLUME,
    config,
    inspect_entry,
    needs_sh,
    posix_only,
    volume_mount,
)

from holdfast import jobs
from holdfast.backup import BackupBusy, BackupError, engine
from holdfast.backup.engine import GIGABYTE, rotate, run_backup, run_line
from holdfast.backup.machine import parse_inspect
from holdfast.backup.manifest import build_manifest

WHEN = datetime(2026, 9, 20, 23, 15, 0, tzinfo=UTC)
SNAPSHOT = "20260920-231500"


class Runner:
    """Stands in for the shell, so the engine can be tested without one."""

    def __init__(self, body: bytes = b"a body", code: int = 0, watch=None):
        self.body = body
        self.code = code
        self.lines: list[str] = []
        self.watch = watch
        self.seen: list[list[str]] = []

    def __call__(self, line: str, dest: Path) -> int:
        self.lines.append(line)
        if self.watch is not None:
            self.seen.append(sorted(p.name for p in Path(self.watch).iterdir()))
        dest.write_bytes(self.body)
        return self.code


def setup(tmp_path: Path, *components, **overrides):
    root = tmp_path / "backups"
    values = {
        "backup.root": str(root),
        # Not the default under /run: nobody but root may write there.
        "backup.lock_file": str(tmp_path / "holdfast.lock"),
        "jobs.dir": str(tmp_path / "jobs"),
        "encryption.enabled": False,
        "host_label": "web-1",
        "component": list(components),
    }
    values.update(overrides)
    return root, config(**values)


def nothing_written(root: Path) -> bool:
    return not root.exists() or list(root.iterdir()) == []


def a_command(name="whatever", **extra):
    return {"type": "command", "name": name, "produce": "printf body", **extra}


def test_a_run_that_produces_no_artifact_fails_and_keeps_the_old_ones(
    tmp_path: Path,
):
    """An empty snapshot was journaled as a success and was the newest one, so
    rotation removed every real snapshot around it - and restore refuses an
    empty snapshot anyway. A lost [[component]] list has to be loud."""
    root, cfg = setup(tmp_path)
    old = aged(root, "20260801-000000", days=365)

    with pytest.raises(BackupError, match="nothing"):
        run_backup(cfg, runner=Runner(), now=WHEN)

    assert old.is_dir()
    assert not (root / SNAPSHOT).exists()
    assert jobs.last(tmp_path / "jobs", "backup")["last_run"]["ok"] is False


def test_the_snapshot_is_named_for_the_moment_it_started(tmp_path: Path):
    root, cfg = setup(tmp_path, a_command())
    result = run_backup(cfg, runner=Runner(), now=WHEN)

    assert result.snapshot == SNAPSHOT
    assert result.directory == root / SNAPSHOT


def test_the_final_name_appears_only_when_the_snapshot_is_whole(tmp_path: Path):
    """Half a snapshot must never be mistaken for a snapshot."""
    root, cfg = setup(tmp_path, a_command("one"), a_command("two"))
    root.mkdir(parents=True)
    runner = Runner(watch=root)
    run_backup(cfg, runner=runner, now=WHEN)

    assert runner.seen == [[f".{SNAPSHOT}.tmp"], [f".{SNAPSHOT}.tmp"]]


def test_the_manifest_matches_what_is_on_disk(tmp_path: Path):
    root, cfg = setup(tmp_path, a_command())
    run_backup(cfg, runner=Runner(body=b"exact bytes"), now=WHEN)

    manifest = json.loads((root / SNAPSHOT / "manifest.json").read_text("utf-8"))
    record = manifest["artifacts"][0]
    body = (root / SNAPSHOT / record["path"]).read_bytes()

    assert record["size"] == len(body)
    assert record["sha256"] == hashlib.sha256(body).hexdigest()
    assert manifest["host_label"] == "web-1"
    assert manifest["format"] == 1
    assert manifest["snapshot"] == SNAPSHOT


def test_the_checksums_cover_every_file_but_themselves(tmp_path: Path):
    root, cfg = setup(tmp_path, a_command("one"), a_command("two"))
    run_backup(cfg, runner=Runner(), now=WHEN)

    sums = (root / SNAPSHOT / "SHA256SUMS").read_text("utf-8").splitlines()
    listed = sorted(line.split("  ", 1)[1] for line in sums)

    assert listed == ["manifest.json", "one.bin", "two.bin"]


def test_a_failing_component_leaves_nothing_behind(tmp_path: Path):
    root, cfg = setup(tmp_path, a_command("one"))
    with pytest.raises(BackupError, match="one"):
        run_backup(cfg, runner=Runner(code=3), now=WHEN)

    assert nothing_written(root)
    assert jobs.last(tmp_path / "jobs", "backup")["last_run"]["ok"] is False


def test_an_empty_artifact_is_a_failure_too(tmp_path: Path):
    """Here the pipeline reported success. It produced nothing, which for a
    backup is the same thing as failing and looks like the opposite."""
    root, cfg = setup(tmp_path, a_command("one"))
    with pytest.raises(BackupError, match="empty"):
        run_backup(cfg, runner=Runner(body=b""), now=WHEN)

    assert nothing_written(root)


def test_too_little_space_stops_it_before_anything_is_written(
    tmp_path: Path, monkeypatch
):
    root, cfg = setup(tmp_path, a_command(), **{"backup.min_free_gb": 8})
    monkeypatch.setattr(
        shutil, "disk_usage", lambda _: shutil._ntuple_diskusage(100, 99, 1)
    )
    with pytest.raises(BackupError, match="free"):
        run_backup(cfg, runner=Runner(), now=WHEN)

    assert nothing_written(root)


def test_a_missing_encryption_tool_stops_it_before_anything_is_written(
    tmp_path: Path, monkeypatch
):
    root, cfg = setup(
        tmp_path,
        a_command(),
        **{
            "encryption.enabled": True,
            "encryption.recipients": ["age1" + "qy" * 29],
        },
    )
    monkeypatch.setattr(
        shutil, "which", lambda name: None if name == "age" else "/usr/bin/sh"
    )
    with pytest.raises(BackupError, match="age"):
        run_backup(cfg, runner=Runner(), now=WHEN)

    assert nothing_written(root)


def test_encryption_renames_the_file_but_not_the_recipe(tmp_path: Path, monkeypatch):
    """The recipe and the check describe the body, not the file around it."""
    root, cfg = setup(
        tmp_path,
        {"type": "path", "name": "config", "path": "/opt/example"},
        **{
            "encryption.enabled": True,
            "encryption.recipients": ["age1" + "qy" * 29],
        },
    )
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    run_backup(cfg, runner=Runner(), now=WHEN)

    manifest = json.loads((root / SNAPSHOT / "manifest.json").read_text("utf-8"))
    record = manifest["artifacts"][0]

    assert record["path"] == "config.tar.zst.age"
    assert record["restore"] == {"type": "path", "target": "/"}
    assert record["check"] == "zstd -dc | tar -tf - >/dev/null"
    assert manifest["encryption"]["enabled"] is True


def test_components_run_in_the_order_they_were_declared(tmp_path: Path):
    _root, cfg = setup(
        tmp_path,
        a_command("zebra", produce="printf z"),
        a_command("apple", produce="printf a"),
    )
    runner = Runner()
    run_backup(cfg, runner=runner, now=WHEN)

    assert runner.lines == ["printf z", "printf a"]


def test_an_artifact_name_cannot_leave_the_snapshot(tmp_path: Path):
    _root, cfg = setup(tmp_path, a_command("one", artifact="../escaped.bin"))
    with pytest.raises(BackupError, match="escaped"):
        run_backup(cfg, runner=Runner(), now=WHEN)


def test_the_journal_records_what_the_night_produced(tmp_path: Path):
    _root, cfg = setup(tmp_path, a_command("one"), a_command("two"))
    run_backup(cfg, runner=Runner(body=b"1234"), now=WHEN)
    run = jobs.last(tmp_path / "jobs", "backup")["last_run"]

    assert run["ok"] is True
    assert run["snapshot"] == SNAPSHOT
    assert run["artifacts"] == 2
    assert run["total_bytes"] == 8


# --------------------------------------------------------------------------
# the offsite copy
# --------------------------------------------------------------------------


def test_offsite_complaints_become_warnings_on_the_finished_run(
    tmp_path: Path, monkeypatch
):
    from holdfast.backup import engine

    root, cfg = setup(tmp_path, a_command())
    seen = {}

    def fake_send(directory, sent_cfg):
        seen["directory"] = directory
        seen["cfg"] = sent_cfg
        return ["the second copy did not go through"]

    monkeypatch.setattr(engine, "send", fake_send)

    result = run_backup(cfg, runner=Runner(), now=WHEN)

    assert seen["directory"] == root / SNAPSHOT
    assert seen["cfg"] is cfg
    assert "the second copy did not go through" in result.warnings


@posix_only
def test_the_offsite_copy_runs_after_the_lock_is_released(tmp_path: Path, monkeypatch):
    """The one thing that can catch a later edit pulling the send back inside
    the lock: a fake `rclone.copy` that must be able to take it."""
    import fcntl

    from holdfast import rclone

    lock = tmp_path / "holdfast.lock"
    _root, cfg = setup(
        tmp_path,
        a_command(),
        **{
            "backup.lock_file": str(lock),
            "offsite.remote": str(tmp_path / "remote"),
            "host_label": "web-1",
        },
    )
    acquired = []

    def fake_copy(source, target, *, timeout):
        with open(lock, "r+") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                pytest.fail("the offsite copy ran while the backup lock was held")
        acquired.append(True)

    monkeypatch.setattr(rclone, "copy", fake_copy)
    monkeypatch.setattr(rclone, "files", lambda *a, **k: {})

    run_backup(cfg, runner=Runner(), now=WHEN)

    assert acquired == [True]


@posix_only
def test_a_second_run_does_not_start_while_the_first_is_going(tmp_path: Path):
    import fcntl

    lock = tmp_path / "holdfast.lock"
    root, cfg = setup(tmp_path, a_command(), **{"backup.lock_file": str(lock)})

    with open(lock, "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BackupBusy):
            run_backup(cfg, runner=Runner(), now=WHEN)

    assert nothing_written(root)
    assert jobs.last(tmp_path / "jobs", "backup") is None


@needs_sh
def test_a_real_pipeline_writes_the_bytes_it_said_it_would(tmp_path: Path):
    root, cfg = setup(
        tmp_path, a_command("greeting", produce="printf 'hello holdfast'")
    )
    result = run_backup(cfg, now=WHEN)

    body = (root / SNAPSHOT / "greeting.bin").read_bytes()
    manifest = json.loads((root / SNAPSHOT / "manifest.json").read_text("utf-8"))

    assert body == b"hello holdfast"
    assert manifest["artifacts"][0]["sha256"] == hashlib.sha256(body).hexdigest()
    assert result.total_bytes == len(body)


@needs_sh
def test_a_producer_that_dies_inside_a_pipe_fails_the_run(tmp_path: Path):
    """Only the last command's status counts in a pipe, unless pipefail is on.

    With encryption off nothing else would add it, and zstd compressing an
    empty stdin succeeds - so a dump that failed its login became a stored
    artifact and a successful night.
    """
    root, cfg = setup(
        tmp_path, a_command("dead", produce="(printf partial; exit 1) | cat")
    )

    with pytest.raises(BackupError):
        run_backup(cfg, now=WHEN)

    assert nothing_written(root)


@needs_sh
def test_a_restore_pipeline_fails_when_any_stage_fails():
    assert run_line("false | cat") != 0


# --------------------------------------------------------------------------
# rotation
# --------------------------------------------------------------------------


def aged(root: Path, name: str, days: float) -> Path:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    when = WHEN.timestamp() - days * 86400
    os.utime(directory, (when, when))
    return directory


def test_a_snapshot_past_the_window_goes_and_one_inside_it_stays(tmp_path: Path):
    root = tmp_path / "backups"
    old = aged(root, "20260901-000000", days=7.1)
    young = aged(root, "20260919-000000", days=6.9)
    aged(root, "20260920-231500", days=0)

    removed, warnings = rotate(root, 7, now=WHEN)

    assert removed == [old]
    assert young.is_dir()
    assert warnings == []


def test_the_newest_snapshot_is_never_deleted(tmp_path: Path):
    """A machine whose backup broke months ago would otherwise lose its last
    copy exactly on schedule, and the rotation would look like it worked."""
    root = tmp_path / "backups"
    only_one = aged(root, "20260101-000000", days=300)

    removed, _ = rotate(root, 7, now=WHEN)

    assert removed == []
    assert only_one.is_dir()


def test_rotation_off_removes_nothing(tmp_path: Path):
    root = tmp_path / "backups"
    aged(root, "20260101-000000", days=300)
    aged(root, "20260102-000000", days=299)

    removed, _ = rotate(root, 0, now=WHEN)

    assert removed == []


def test_a_directory_that_is_not_a_snapshot_is_left_alone(tmp_path: Path):
    """The backup root can hold somebody else's things."""
    root = tmp_path / "backups"
    aged(root, "20260920-231500", days=0)
    stranger = aged(root, "notes", days=300)
    dated_but_wrong = aged(root, "2026-09-01", days=300)

    removed, _ = rotate(root, 7, now=WHEN)

    assert removed == []
    assert stranger.is_dir()
    assert dated_but_wrong.is_dir()


def test_a_file_in_the_root_is_left_alone(tmp_path: Path):
    root = tmp_path / "backups"
    aged(root, "20260920-231500", days=0)
    loose = root / "20260101-000000"
    loose.write_text("not a snapshot", encoding="utf-8")
    when = WHEN.timestamp() - 300 * 86400
    os.utime(loose, (when, when))

    removed, _ = rotate(root, 7, now=WHEN)

    assert removed == []
    assert loose.is_file()


@posix_only
def test_rotation_does_not_follow_the_latest_link(tmp_path: Path):
    root = tmp_path / "backups"
    keep = aged(root, "20260920-231500", days=0)
    old = aged(root, "20260101-000000", days=300)
    (root / "latest").symlink_to(keep, target_is_directory=True)

    removed, _ = rotate(root, 7, now=WHEN)

    assert removed == [old]
    assert (root / "latest").is_symlink()
    assert keep.is_dir()


def test_one_directory_that_refuses_to_go_does_not_stop_the_others(
    tmp_path: Path, monkeypatch
):
    """The snapshot is already final by now, so this is a warning, not a
    failure."""
    root = tmp_path / "backups"
    aged(root, "20260920-231500", days=0)
    first = aged(root, "20260101-000000", days=300)
    second = aged(root, "20260102-000000", days=299)
    real = shutil.rmtree

    def stubborn(path, **kwargs):
        if Path(path) == first:
            raise OSError("device or resource busy")
        real(path, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", stubborn)

    removed, warnings = rotate(root, 7, now=WHEN)

    assert removed == [second]
    assert len(warnings) == 1
    assert "20260101-000000" in warnings[0]


def test_a_failed_run_does_not_rotate_anything(tmp_path: Path):
    """A run of failures is exactly when the old copy is the only copy."""
    root, cfg = setup(tmp_path, a_command("one"))
    old = aged(root, "20260101-000000", days=300)
    aged(root, "20260102-000000", days=299)

    with pytest.raises(BackupError):
        run_backup(cfg, runner=Runner(code=1), now=WHEN)

    assert old.is_dir()


def test_a_finished_run_rotates_and_reports_what_it_could_not(tmp_path: Path):
    root, cfg = setup(tmp_path, a_command("one"))
    old = aged(root, "20260101-000000", days=300)

    result = run_backup(cfg, runner=Runner(), now=WHEN)

    assert not old.exists()
    # Not "no warnings at all": on Windows the `latest` link needs a privilege
    # this does not have, and saying so is the right behaviour.
    assert [w for w in result.warnings if "old snapshot" in w] == []


# --------------------------------------------------------------------------
# the manifest's list of what was left out
# --------------------------------------------------------------------------


def a_manifest(**extra):
    return build_manifest(
        snapshot=SNAPSHOT,
        created_at=WHEN.isoformat(),
        host_label="web-1",
        compression={"tool": "zstd", "level": 10},
        encryption={"enabled": False},
        components=[],
        artifacts=[],
        **extra,
    )


def test_a_manifest_with_nothing_skipped_still_says_so():
    """An empty list, not a missing key: a reader should not have to guess
    whether this snapshot predates the list or simply left nothing out."""
    assert a_manifest()["skipped"] == []


def test_a_manifest_keeps_the_skipped_list_it_is_given():
    skipped = [{"what": "volume x", "reason": "no container uses it"}]
    assert a_manifest(skipped=skipped)["skipped"] == skipped


# --------------------------------------------------------------------------
# auto mode: the rule's components, the room they need, what was left out
# --------------------------------------------------------------------------


class Machine:
    """A fake probe with one web container, its named volume and an orphan."""

    def __init__(self, tmp_path: Path, size_mb: int = 1):
        self.mountpoint = tmp_path / "vol"
        self.mountpoint.mkdir(exist_ok=True)
        self.size_mb = size_mb

    def inspect_containers(self):
        entry = inspect_entry(
            "app-web-1", "nginx:1", mounts=[volume_mount("webdata", "/data")]
        )
        return parse_inspect(json.dumps([entry]))

    def volumes(self):
        return ["webdata", ORPHAN_VOLUME]

    def volume_mountpoint(self, name: str) -> str:
        return str(self.mountpoint)

    def directory_size_mb(self, path: str) -> int:
        return self.size_mb


def test_auto_backs_up_what_the_rule_finds_and_names_what_it_left(tmp_path: Path):
    root, cfg = setup(tmp_path, **{"backup.mode": "auto"})
    result = run_backup(cfg, runner=Runner(), now=WHEN, probe=Machine(tmp_path))

    manifest = json.loads((root / SNAPSHOT / "manifest.json").read_text("utf-8"))
    orphan = {"what": f"volume {ORPHAN_VOLUME}", "reason": "no container uses it"}
    assert [a["path"] for a in manifest["artifacts"]] == [
        "docker-volumes/webdata.tar.zst"
    ]
    assert manifest["skipped"] == [orphan]
    assert [s.as_dict() for s in result.skipped] == [orphan]


def test_too_little_room_for_the_estimate_stops_it_before_anything_is_written(
    tmp_path: Path, monkeypatch
):
    """Free space alone is not enough when the rule already knows the snapshot
    will eat most of it."""
    root, cfg = setup(tmp_path, **{"backup.mode": "auto", "backup.min_free_gb": 8})
    monkeypatch.setattr(
        engine.shutil,
        "disk_usage",
        lambda _: shutil._ntuple_diskusage(
            100 * GIGABYTE, 90 * GIGABYTE, 10 * GIGABYTE
        ),
    )
    with pytest.raises(BackupError, match="estimated at up to 3000 MB"):
        run_backup(
            cfg, runner=Runner(), now=WHEN, probe=Machine(tmp_path, size_mb=3000)
        )

    assert nothing_written(root)


def test_no_estimate_asks_for_no_room(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        engine.shutil,
        "disk_usage",
        lambda _: shutil._ntuple_diskusage(
            100 * GIGABYTE, 90 * GIGABYTE, 10 * GIGABYTE
        ),
    )
    engine._require_room(tmp_path, 0, 8)


def test_two_artifacts_with_one_name_stop_it_before_anything_is_written(
    tmp_path: Path,
):
    """Otherwise the second archive quietly replaces the first, and the
    manifest lists a file that is no longer the one it describes."""
    root, cfg = setup(
        tmp_path,
        a_command("one", artifact="same.bin"),
        a_command("two", artifact="same.bin"),
    )
    runner = Runner()
    with pytest.raises(BackupError, match=r"'same\.bin'.*'one'.*'two'"):
        run_backup(cfg, runner=runner, now=WHEN)

    assert runner.lines == []
    assert nothing_written(root)
