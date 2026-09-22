"""Tests for sending a finished snapshot to shared storage.

`rclone` is a real external program, exercised for real only by
test_rclone.py's `needs_rclone` test. Everywhere here it is faked, the same
way test_audit_application.py fakes the one check that sends an HTTP
request: nothing in this file reaches a network or a remote.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from support import config

from holdfast import jobs, rclone
from holdfast.backup import offsite

SNAPSHOT_NAME = "20260921-020000"


def snapshot(tmp_path: Path) -> Path:
    directory = tmp_path / SNAPSHOT_NAME
    directory.mkdir()
    (directory / "manifest.json").write_text("{}", encoding="utf-8")
    (directory / "SHA256SUMS").write_text("", encoding="utf-8")
    (directory / "one.bin").write_bytes(b"12345")
    return directory


def cfg(tmp_path: Path, **overrides):
    values = {
        "offsite.remote": "shared:backups",
        "host_label": "web-1",
        "jobs.dir": str(tmp_path / "jobs"),
    }
    values.update(overrides)
    return config(**values)


def refuse(*args, **kwargs):
    raise AssertionError("this must not be called")


# -- an unconfigured remote --------------------------------------------------


def test_an_unconfigured_remote_sends_nothing_and_writes_no_journal(
    tmp_path, monkeypatch
):
    """A machine with no remote is most of them, not a nightly complaint."""
    monkeypatch.setattr(rclone, "copy", refuse)

    complaints = offsite.send(
        snapshot(tmp_path), cfg(tmp_path, **{"offsite.remote": ""})
    )

    assert complaints == []
    assert jobs.last(tmp_path / "jobs", "offsite") is None


# -- a successful send --------------------------------------------------


def test_a_successful_send_copies_verifies_and_records_success(tmp_path, monkeypatch):
    directory = snapshot(tmp_path)
    calls = []

    def fake_copy(source, target, *, timeout):
        calls.append(("copy", source, target, timeout))

    def fake_files(target, *, timeout):
        calls.append(("files", target, timeout))
        return offsite._expected(directory)

    monkeypatch.setattr(rclone, "copy", fake_copy)
    monkeypatch.setattr(rclone, "files", fake_files)

    complaints = offsite.send(directory, cfg(tmp_path))

    target = f"shared:backups/web-1/{SNAPSHOT_NAME}"
    assert complaints == []
    assert calls[0] == ("copy", directory, target, 7200)
    assert calls[1] == ("files", target, 7200)

    entry = jobs.last(tmp_path / "jobs", "offsite")
    assert entry["last_success"]["snapshot"] == SNAPSHOT_NAME
    assert entry["last_success"]["target"] == target


# -- an empty host label --------------------------------------------------


def test_an_empty_host_label_is_refused_before_any_copy(tmp_path, monkeypatch):
    """An empty label would land the copy on top of another machine's."""
    monkeypatch.setattr(rclone, "copy", refuse)

    complaints = offsite.send(snapshot(tmp_path), cfg(tmp_path, host_label=""))

    assert len(complaints) == 1
    assert "another machine" in complaints[0]
    entry = jobs.last(tmp_path / "jobs", "offsite")
    assert entry["last_run"]["ok"] is False


# -- a failed copy --------------------------------------------------


def test_a_failed_copy_is_reported_and_files_is_never_called(tmp_path, monkeypatch):
    def failing_copy(source, target, *, timeout):
        raise rclone.RcloneError("connection refused")

    monkeypatch.setattr(rclone, "copy", failing_copy)
    monkeypatch.setattr(rclone, "files", refuse)

    complaints = offsite.send(snapshot(tmp_path), cfg(tmp_path))

    assert len(complaints) == 1
    assert "connection refused" in complaints[0]
    entry = jobs.last(tmp_path / "jobs", "offsite")
    assert entry["last_run"]["ok"] is False


# -- send() never raises, whatever the exception --------------------------
#
# `send()`'s own docstring promises it never raises. Each step used to catch
# only `rclone.RcloneError`; these pin that the contract holds for anything
# else too - a malformed answer from rclone or an `OSError` from `_expected`
# reading a file that vanished after the copy, none of which are
# `RcloneError`.


def test_an_unexpected_error_naming_the_destination_is_reported_not_raised(
    tmp_path, monkeypatch
):
    def broken_destination(remote, label, snapshot=""):
        raise TypeError("boom")

    monkeypatch.setattr(rclone, "destination", broken_destination)

    complaints = offsite.send(snapshot(tmp_path), cfg(tmp_path))

    assert len(complaints) == 1
    entry = jobs.last(tmp_path / "jobs", "offsite")
    assert entry["last_run"]["ok"] is False


def test_an_unexpected_error_from_copy_is_reported_not_raised(tmp_path, monkeypatch):
    def broken_copy(source, target, *, timeout):
        raise KeyError("boom")

    monkeypatch.setattr(rclone, "copy", broken_copy)

    complaints = offsite.send(snapshot(tmp_path), cfg(tmp_path))

    assert len(complaints) == 1
    entry = jobs.last(tmp_path / "jobs", "offsite")
    assert entry["last_run"]["ok"] is False


def test_an_unexpected_error_from_files_is_reported_not_raised(tmp_path, monkeypatch):
    """The exact shape of a malformed lsjson element before `rclone.py` was
    also fixed to turn it into `RcloneError`: `send()` must not depend on
    that fix to keep its own promise."""
    directory = snapshot(tmp_path)
    monkeypatch.setattr(rclone, "copy", lambda *a, **k: None)

    def broken_files(target, *, timeout):
        raise KeyError("Path")

    monkeypatch.setattr(rclone, "files", broken_files)

    complaints = offsite.send(directory, cfg(tmp_path))

    assert len(complaints) == 1
    entry = jobs.last(tmp_path / "jobs", "offsite")
    assert entry["last_run"]["ok"] is False


def test_a_vanished_snapshot_file_does_not_escape_send(tmp_path, monkeypatch):
    """`_expected()`'s `path.stat()` can raise `OSError` if a file
    disappears between the copy and this post-copy check."""
    directory = snapshot(tmp_path)
    monkeypatch.setattr(rclone, "copy", lambda *a, **k: None)
    monkeypatch.setattr(rclone, "files", lambda *a, **k: {})

    def vanished(snap):
        raise OSError("file vanished")

    monkeypatch.setattr(offsite, "_expected", vanished)

    complaints = offsite.send(directory, cfg(tmp_path))

    assert len(complaints) == 1
    entry = jobs.last(tmp_path / "jobs", "offsite")
    assert entry["last_run"]["ok"] is False


# -- a mismatch between what was sent and what is there --------------------


def test_a_missing_file_on_the_remote_is_reported_by_name(tmp_path, monkeypatch):
    directory = snapshot(tmp_path)
    monkeypatch.setattr(rclone, "copy", lambda *a, **k: None)
    monkeypatch.setattr(rclone, "files", lambda *a, **k: {})

    complaints = offsite.send(directory, cfg(tmp_path))

    assert len(complaints) == 1
    assert "one.bin" in complaints[0]
    assert jobs.last(tmp_path / "jobs", "offsite")["last_run"]["ok"] is False


def test_a_missing_file_reads_differently_from_a_failed_copy(tmp_path, monkeypatch):
    """An operator reading this has to know whether to chase the network or
    the storage side - the two complaints must not look alike."""
    directory = snapshot(tmp_path)
    monkeypatch.setattr(rclone, "copy", lambda *a, **k: None)
    monkeypatch.setattr(rclone, "files", lambda *a, **k: {})
    [missing_complaint] = offsite.send(directory, cfg(tmp_path))

    def failing_copy(source, target, *, timeout):
        raise rclone.RcloneError("connection refused")

    monkeypatch.setattr(rclone, "copy", failing_copy)
    [copy_complaint] = offsite.send(directory, cfg(tmp_path))

    assert missing_complaint != copy_complaint


def test_a_size_mismatch_names_both_sizes(tmp_path, monkeypatch):
    directory = snapshot(tmp_path)
    expected = offsite._expected(directory)
    wrong = {**expected, "one.bin": 1}
    monkeypatch.setattr(rclone, "copy", lambda *a, **k: None)
    monkeypatch.setattr(rclone, "files", lambda *a, **k: wrong)

    [complaint] = offsite.send(directory, cfg(tmp_path))

    assert "5 bytes expected" in complaint
    assert "1 found" in complaint


# -- the journal itself is unreachable --------------------------------------


def test_an_unwritable_journal_does_not_fail_the_send(tmp_path, monkeypatch):
    """The copy already left; that matters more than the record of it."""
    directory = snapshot(tmp_path)
    jobs_file = tmp_path / "jobs"
    jobs_file.write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr(rclone, "copy", lambda *a, **k: None)
    monkeypatch.setattr(rclone, "files", lambda *a, **k: offsite._expected(directory))

    complaints = offsite.send(directory, cfg(tmp_path, **{"jobs.dir": str(jobs_file)}))

    assert complaints == []


# -- the timeout --------------------------------------------------


def test_the_timeout_minutes_become_seconds(tmp_path, monkeypatch):
    directory = snapshot(tmp_path)
    seen = {}

    def fake_copy(source, target, *, timeout):
        seen["timeout"] = timeout

    monkeypatch.setattr(rclone, "copy", fake_copy)
    monkeypatch.setattr(rclone, "files", lambda *a, **k: offsite._expected(directory))

    offsite.send(directory, cfg(tmp_path, **{"offsite.timeout_minutes": 120}))

    assert seen["timeout"] == 7200


@pytest.mark.parametrize("garbage", [0, -5, "not a number", float("inf"), float("nan")])
def test_a_bad_timeout_becomes_the_minimum_not_unlimited(
    tmp_path, monkeypatch, garbage
):
    directory = snapshot(tmp_path)
    seen = {}

    def fake_copy(source, target, *, timeout):
        seen["timeout"] = timeout

    monkeypatch.setattr(rclone, "copy", fake_copy)
    monkeypatch.setattr(rclone, "files", lambda *a, **k: offsite._expected(directory))

    offsite.send(directory, cfg(tmp_path, **{"offsite.timeout_minutes": garbage}))

    assert seen["timeout"] == offsite.MINIMUM_TIMEOUT_MINUTES * 60


# -- _expected --------------------------------------------------


def test_expected_includes_the_manifest_and_checksums_not_just_artifacts(tmp_path):
    directory = snapshot(tmp_path)

    expected = offsite._expected(directory)

    assert set(expected) == {"manifest.json", "SHA256SUMS", "one.bin"}
    assert expected["one.bin"] == 5
