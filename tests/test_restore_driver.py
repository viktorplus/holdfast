import hashlib
import json
from pathlib import Path

import pytest

from holdfast import jobs
from holdfast.backup.decrypt import open_identity
from holdfast.backup.model import RestoreError
from holdfast.backup.restore import render_list, verify
from holdfast.backup.snapshot import load_snapshot

PATH_RECIPE = {"type": "path", "target": "/"}
BODY = b"a body"


class Runs:
    """Stands in for the shell, remembering the lines and failing on demand."""

    def __init__(self, code: int = 0, fail_on: str | None = None):
        self.code = code
        self.fail_on = fail_on
        self.lines: list[str] = []

    def __call__(self, line: str) -> int:
        self.lines.append(line)
        if self.fail_on and self.fail_on in line:
            return 1
        return self.code


def artifact(name: str, recipe: dict, component: str = "thing") -> dict:
    return {
        "path": name,
        "component": component,
        "size": len(BODY),
        "sha256": hashlib.sha256(BODY).hexdigest(),
        "restore": recipe,
        "check": "cat >/dev/null",
    }


def a_snapshot(tmp_path: Path, *artifacts: dict, **manifest) -> Path:
    directory = tmp_path / "20260920-231500"
    directory.mkdir(parents=True, exist_ok=True)
    for record in artifacts:
        file = directory / record["path"]
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(BODY)
    body = {
        "format": 1,
        "tool": "holdfast",
        "tool_version": "0.1.0",
        "snapshot": "20260920-231500",
        "created_at": "2026-09-20T23:15:00+00:00",
        "host_label": "web-1",
        "compression": {"tool": "zstd", "level": 10},
        "encryption": {
            "enabled": False,
            "tool": "none",
            "suffix": "",
            "recipients": [],
        },
        "components": [],
        "artifacts": list(artifacts),
        **manifest,
    }
    (directory / "manifest.json").write_text(json.dumps(body), encoding="utf-8")
    return directory


def contents(directory: Path) -> list[tuple[str, int]]:
    return sorted(
        (str(p.relative_to(directory)), p.stat().st_size)
        for p in directory.rglob("*")
        if p.is_file()
    )


# --------------------------------------------------------------------------
# list
# --------------------------------------------------------------------------


def test_list_says_what_the_snapshot_is_and_what_is_in_it(tmp_path: Path):
    directory = a_snapshot(
        tmp_path,
        artifact("files.tar.zst", PATH_RECIPE, component="config"),
        artifact(
            "db.dump",
            {"type": "pg_database", "container": "c", "user": "u", "database": "app"},
            component="db",
        ),
    )
    printed = render_list(load_snapshot(directory))

    assert "20260920-231500" in printed
    assert "web-1" in printed
    assert "files.tar.zst" in printed
    assert "config" in printed
    assert "pg_database" in printed


def test_list_needs_no_key_at_all(tmp_path: Path):
    """Looking inside a copy comes before finding the key it was sealed with."""
    directory = a_snapshot(
        tmp_path,
        artifact("files.tar.zst.age", PATH_RECIPE),
        encryption={
            "enabled": True,
            "tool": "age",
            "suffix": ".age",
            "recipients": ["age1example"],
        },
    )
    printed = render_list(load_snapshot(directory))

    assert "encrypted" in printed.lower()
    assert "files.tar.zst.age" in printed


def test_list_names_the_artifacts_nobody_will_put_back(tmp_path: Path):
    directory = a_snapshot(
        tmp_path,
        artifact("files.tar.zst", PATH_RECIPE, component="config"),
        artifact("notes.bin", {"type": "none", "note": "x"}, component="notes"),
    )
    printed = render_list(load_snapshot(directory))

    assert "notes.bin" in printed
    assert "notes" in printed


# --------------------------------------------------------------------------
# verify
# --------------------------------------------------------------------------


def test_a_whole_snapshot_verifies_and_is_written_down(tmp_path: Path):
    directory = a_snapshot(tmp_path, artifact("a.bin", PATH_RECIPE))
    jobs_dir = tmp_path / "jobs"

    with open_identity(None) as identity:
        result = verify(
            load_snapshot(directory), identity, jobs_dir=jobs_dir, run=Runs()
        )

    assert result.ok is True
    assert result.checked == 1
    entry = jobs.last(jobs_dir, "verify")
    assert entry["last_run"]["ok"] is True
    assert entry["last_run"]["snapshot"] == "20260920-231500"


def test_a_journal_that_cannot_be_written_does_not_undo_the_verify(tmp_path: Path):
    """Run by a user who may not write /var/lib/holdfast/jobs, the check has
    still been done: its answer stands, and the lost record is said aloud."""
    directory = a_snapshot(tmp_path, artifact("a.bin", PATH_RECIPE))
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("", encoding="utf-8")

    with open_identity(None) as identity:
        result = verify(
            load_snapshot(directory), identity, jobs_dir=blocked, run=Runs()
        )

    assert result.ok is True
    assert result.checked == 1
    assert len(result.warnings) == 1
    assert result.warnings[0].startswith("could not record this verify in ")


def test_a_changed_byte_is_reported_with_the_file_that_changed(tmp_path: Path):
    directory = a_snapshot(tmp_path, artifact("a.bin", PATH_RECIPE))
    (directory / "a.bin").write_bytes(b"a bodY")
    snapshot = load_snapshot(directory)

    with open_identity(None) as identity:
        result = verify(snapshot, identity, jobs_dir=tmp_path / "jobs", run=Runs())

    assert result.ok is False
    assert any("a.bin" in failure for failure in result.failures)
    assert any("checksum" in failure for failure in result.failures)


def test_an_artifact_that_does_not_read_back_is_reported(tmp_path: Path):
    directory = a_snapshot(tmp_path, artifact("a.bin", PATH_RECIPE))

    with open_identity(None) as identity:
        result = verify(
            load_snapshot(directory),
            identity,
            jobs_dir=tmp_path / "jobs",
            run=Runs(fail_on="a.bin"),
        )

    assert result.ok is False
    assert any("a.bin" in failure for failure in result.failures)


def test_verify_checks_every_artifact_rather_than_stopping_at_the_first(
    tmp_path: Path,
):
    """A second run in the middle of an incident is another half hour."""
    directory = a_snapshot(
        tmp_path,
        artifact("a.bin", PATH_RECIPE, component="one"),
        artifact("b.bin", PATH_RECIPE, component="two"),
    )
    (directory / "a.bin").write_bytes(b"a bodY")
    (directory / "b.bin").write_bytes(b"a bodY")

    with open_identity(None) as identity:
        result = verify(
            load_snapshot(directory), identity, jobs_dir=tmp_path / "jobs", run=Runs()
        )

    assert len(result.failures) == 2


def test_verify_also_looks_at_what_it_could_never_restore(tmp_path: Path):
    """Those bytes are in the snapshot too, and a copy is either whole or not."""
    directory = a_snapshot(
        tmp_path,
        artifact("a.bin", PATH_RECIPE, component="one"),
        artifact("notes.bin", {"type": "none"}, component="notes"),
    )

    with open_identity(None) as identity:
        result = verify(
            load_snapshot(directory), identity, jobs_dir=tmp_path / "jobs", run=Runs()
        )

    assert result.checked == 2


def test_verify_writes_nothing_into_the_snapshot(tmp_path: Path):
    directory = a_snapshot(tmp_path, artifact("a.bin", PATH_RECIPE))
    before = contents(directory)

    with open_identity(None) as identity:
        verify(
            load_snapshot(directory), identity, jobs_dir=tmp_path / "jobs", run=Runs()
        )

    assert contents(directory) == before


def test_a_failure_does_not_erase_the_last_time_it_worked(tmp_path: Path):
    directory = a_snapshot(tmp_path, artifact("a.bin", PATH_RECIPE))
    jobs_dir = tmp_path / "jobs"

    with open_identity(None) as identity:
        verify(load_snapshot(directory), identity, jobs_dir=jobs_dir, run=Runs())
        succeeded_at = jobs.last(jobs_dir, "verify")["last_success"]["at"]

        (directory / "a.bin").write_bytes(b"a bodY")
        verify(load_snapshot(directory), identity, jobs_dir=jobs_dir, run=Runs())

    entry = jobs.last(jobs_dir, "verify")
    assert entry["last_run"]["ok"] is False
    assert entry["last_success"]["at"] == succeeded_at


def test_verify_refuses_a_snapshot_it_cannot_open(tmp_path: Path):
    directory = a_snapshot(
        tmp_path,
        artifact("a.bin.age", PATH_RECIPE),
        encryption={
            "enabled": True,
            "tool": "age",
            "suffix": ".age",
            "recipients": ["age1example"],
        },
    )

    with open_identity(None) as identity, pytest.raises(RestoreError, match="identity"):
        verify(
            load_snapshot(directory), identity, jobs_dir=tmp_path / "jobs", run=Runs()
        )


def test_list_says_so_when_nothing_in_it_can_be_put_back(tmp_path: Path):
    """A gap where the list would be reads as "there is nothing here"."""
    directory = a_snapshot(tmp_path, artifact("notes.bin", {"type": "none"}))
    printed = render_list(load_snapshot(directory))

    assert "nothing in this snapshot has a recipe" in printed
