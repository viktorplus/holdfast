import json
import stat
from datetime import datetime
from pathlib import Path

import pytest
from support import posix_only

from holdfast import jobs


def test_nothing_recorded_yet_is_not_an_error(tmp_path: Path):
    assert jobs.last(tmp_path / "jobs", "backup") is None


def test_a_successful_run_is_both_the_last_and_the_last_success(tmp_path: Path):
    jobs.record(tmp_path, "backup", ok=True, snapshot="20260920-231500")
    entry = jobs.last(tmp_path, "backup")

    assert entry["last_run"]["ok"] is True
    assert entry["last_success"] == entry["last_run"]


def test_a_failure_does_not_erase_the_last_success(tmp_path: Path):
    """The point of keeping two fields.

    `restore_tested` asks when a snapshot was last verified successfully. With
    one field, tonight's failure answers "never", and a machine that has been
    verifying itself for a year looks like one that never has.
    """
    jobs.record(tmp_path, "verify", ok=True, snapshot="20260918-020000")
    succeeded_at = jobs.last(tmp_path, "verify")["last_success"]["at"]

    jobs.record(tmp_path, "verify", ok=False, error="checksum mismatch")
    entry = jobs.last(tmp_path, "verify")

    assert entry["last_run"]["ok"] is False
    assert entry["last_run"]["error"] == "checksum mismatch"
    assert entry["last_success"]["at"] == succeeded_at
    assert entry["last_success"]["snapshot"] == "20260918-020000"


def test_a_failure_with_no_history_leaves_the_last_success_empty(tmp_path: Path):
    jobs.record(tmp_path, "offsite", ok=False, error="rclone: not found")
    entry = jobs.last(tmp_path, "offsite")

    assert entry["last_run"]["ok"] is False
    assert entry["last_success"] is None


def test_extra_fields_are_carried_through(tmp_path: Path):
    jobs.record(tmp_path, "backup", ok=True, artifacts=4, total_bytes=1234)
    run = jobs.last(tmp_path, "backup")["last_run"]

    assert run["artifacts"] == 4
    assert run["total_bytes"] == 1234


def test_every_entry_is_stamped_with_a_zoned_time(tmp_path: Path):
    jobs.record(tmp_path, "backup", ok=True)
    at = jobs.last(tmp_path, "backup")["last_run"]["at"]

    assert datetime.fromisoformat(at).tzinfo is not None


def test_an_unknown_kind_is_refused(tmp_path: Path):
    """A typo here writes a file nobody will ever read."""
    with pytest.raises(ValueError):
        jobs.record(tmp_path, "backupp", ok=True)


def test_a_corrupt_journal_reads_as_nothing_and_is_replaced(tmp_path: Path):
    """Losing the journal is acceptable; stopping a backup over it is not."""
    path = tmp_path / "backup.json"
    path.write_text("{ this is not json", encoding="utf-8")

    assert jobs.last(tmp_path, "backup") is None

    jobs.record(tmp_path, "backup", ok=True, snapshot="20260920-231500")
    entry = json.loads(path.read_text(encoding="utf-8"))

    assert entry["last_run"]["snapshot"] == "20260920-231500"


def test_the_directory_is_created_on_demand(tmp_path: Path):
    nested = tmp_path / "var" / "lib" / "holdfast" / "jobs"
    jobs.record(nested, "backup", ok=True)

    assert (nested / "backup.json").is_file()


@posix_only
def test_the_journal_is_readable_only_by_its_owner(tmp_path: Path):
    path = jobs.record(tmp_path, "backup", ok=True)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


# --------------------------------------------------------------------------
# note()
# --------------------------------------------------------------------------


def test_note_writes_the_same_thing_as_record(tmp_path: Path):
    jobs.note(tmp_path, "backup", ok=True, snapshot="20260920-231500")
    entry = jobs.last(tmp_path, "backup")

    assert entry["last_run"]["ok"] is True
    assert entry["last_run"]["snapshot"] == "20260920-231500"
    assert entry["last_success"] == entry["last_run"]


def test_note_swallows_an_oserror_from_a_directory_it_cannot_create(tmp_path: Path):
    """The caller has already finished its run - losing the journal must not
    turn that into an exception."""
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")

    jobs.note(blocked, "backup", ok=True)  # must not raise

    assert jobs.last(blocked, "backup") is None
