"""Tests for the rclone client.

The module decides nothing about what a failure means, so these tests check
only what it does decide: how a destination is built, what argv it hands to
`rclone`, and how it turns rclone's exit code and stderr into `RcloneError`.
"""

import subprocess

import pytest
from support import needs_rclone

from holdfast.rclone import (
    DIRECTORY_NOT_FOUND,
    RcloneError,
    copy,
    destination,
    directories,
    files,
    missing,
    run,
)


class FakeRunner:
    """Stands in for `run`, remembering how it was called."""

    def __init__(self, stdout: str = "") -> None:
        self.stdout = stdout
        self.calls: list[tuple] = []

    def __call__(self, argv, *, what, timeout=None):
        self.calls.append((argv, what, timeout))
        return self.stdout


# -- destination ------------------------------------------------------------


def test_destination_joins_remote_label_and_snapshot():
    assert (
        destination("shared:backups", "web-1", "20260921-000000")
        == "shared:backups/web-1/20260921-000000"
    )


def test_destination_without_a_snapshot_stops_at_the_label():
    assert destination("shared:backups", "web-1") == "shared:backups/web-1"


def test_destination_strips_a_trailing_slash_on_remote_and_label():
    """No double slash from either side."""
    assert (
        destination("shared:backups/", "/web-1/", "20260921-000000")
        == "shared:backups/web-1/20260921-000000"
    )


def test_destination_refuses_an_empty_remote():
    with pytest.raises(RcloneError, match="offsite.remote"):
        destination("", "web-1")


def test_destination_refuses_an_empty_host_label():
    """An empty label would land the copy on top of another machine's."""
    with pytest.raises(RcloneError, match="another machine"):
        destination("shared:backups", "")


# -- copy ---------------------------------------------------------------


def test_copy_calls_rclone_with_source_and_target_and_no_flag_terminator():
    """Source and target are never flag-shaped here, so `--` is not needed."""
    runner = FakeRunner()

    copy(
        "/tmp/source",
        "shared:backups/web-1/20260921-000000",
        timeout=300,
        runner=runner,
    )

    argv, _, _ = runner.calls[0]
    assert argv == ["copy", "/tmp/source", "shared:backups/web-1/20260921-000000"]


# -- files and directories ------------------------------------------------

# Verbatim shape of the fields a real rclone v1.74.2 returns, confirmed
# against a real run: Path and Size for files, Name for dirs.
FILES_OUTPUT = """[
{"Path":"a.bin","Name":"a.bin","Size":6,"ModTime":"2026-09-21T00:00:00Z","IsDir":false},
{"Path":"sub/b.bin","Name":"b.bin","Size":3,"ModTime":"2026-09-21T00:00:00Z","IsDir":false}
]"""

DIRS_OUTPUT = """[
{"Path":"20260921-000000","Name":"20260921-000000","Size":0,"ModTime":"2026-09-21T00:00:00Z","IsDir":true}
]"""


def test_files_parses_the_verbatim_lsjson_output_into_names_and_sizes():
    """A nested path must survive."""
    runner = FakeRunner(stdout=FILES_OUTPUT)

    found = files("shared:backups/web-1/20260921-000000", timeout=30, runner=runner)

    assert found == {"a.bin": 6, "sub/b.bin": 3}


def test_files_calls_rclone_with_exactly_the_recursive_files_only_listing():
    runner = FakeRunner(stdout="[]")

    files("shared:backups/web-1/20260921-000000", timeout=30, runner=runner)

    argv, _, _ = runner.calls[0]
    assert argv == [
        "lsjson",
        "-R",
        "--files-only",
        "shared:backups/web-1/20260921-000000",
    ]


def test_directories_parses_the_verbatim_lsjson_output_into_names():
    runner = FakeRunner(stdout=DIRS_OUTPUT)

    assert directories("shared:backups/web-1", timeout=30, runner=runner) == [
        "20260921-000000"
    ]


def test_directories_calls_rclone_with_exactly_the_non_recursive_dirs_only_listing():
    """A recursive listing here would enumerate every artifact of every
    snapshot for the sake of one name."""
    runner = FakeRunner(stdout="[]")

    directories("shared:backups/web-1", timeout=30, runner=runner)

    argv, _, _ = runner.calls[0]
    assert argv == ["lsjson", "--dirs-only", "shared:backups/web-1"]


def test_non_json_stdout_is_an_rclone_error_not_a_value_error():
    runner = FakeRunner(stdout="not json")

    with pytest.raises(RcloneError):
        files("shared:backups/web-1", timeout=30, runner=runner)


def test_json_that_is_not_a_list_is_an_rclone_error():
    runner = FakeRunner(stdout='{"Path": "a.bin"}')

    with pytest.raises(RcloneError):
        files("shared:backups/web-1", timeout=30, runner=runner)


def test_a_files_element_missing_path_or_size_is_an_rclone_error_not_a_keyerror():
    """A malformed lsjson element is still a bad answer from rclone; it must
    not surface as a bare KeyError to whoever called `files()`."""
    runner = FakeRunner(stdout='[{"Name": "a.bin", "IsDir": false}]')

    with pytest.raises(RcloneError):
        files("shared:backups/web-1", timeout=30, runner=runner)


def test_a_directories_element_missing_name_is_an_rclone_error_not_a_keyerror():
    runner = FakeRunner(stdout='[{"Path": "sub", "IsDir": true}]')

    with pytest.raises(RcloneError):
        directories("shared:backups/web-1", timeout=30, runner=runner)


# -- missing ----------------------------------------------------------------


def test_missing_is_empty_when_names_and_sizes_match():
    assert missing({"a": 1, "b": 2}, {"a": 1, "b": 2}) == []


def test_missing_reports_a_file_that_never_arrived():
    problems = missing({"a": 1}, {})

    assert len(problems) == 1
    assert "a" in problems[0]


def test_missing_reports_both_sizes_for_a_file_that_arrived_wrong():
    """Neither number alone tells an operator what happened."""
    problems = missing({"a": 10}, {"a": 7})

    assert len(problems) == 1
    assert "10" in problems[0]
    assert "7" in problems[0]


# -- run ----------------------------------------------------------------


def test_run_turns_a_missing_program_into_an_rclone_error_naming_path(monkeypatch):
    def fake_run(argv, **kwargs):
        raise FileNotFoundError("rclone")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RcloneError, match="PATH"):
        run(["version"], what="checking rclone")


def test_run_turns_a_timeout_into_an_rclone_error(monkeypatch):
    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 30)

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RcloneError):
        run(["copy", "a", "b"], what="copying", timeout=30)


def test_run_keeps_the_exit_code_and_uses_the_last_line_of_stderr(monkeypatch):
    """rclone writes what it tried first, and only then why it gave up."""
    stderr = (
        "2026/09/21 02:41:42 ERROR : error listing: directory not found\n"
        "2026/09/21 02:41:42 NOTICE: Failed to lsjson: last error was: gave up\n"
    )

    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 5, "", stderr)

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RcloneError) as caught:
        run(["lsjson", "somewhere"], what="listing")

    assert caught.value.code == 5
    assert "gave up" in str(caught.value)
    assert "error listing" not in str(caught.value)


def test_run_recognises_directory_not_found_by_its_exit_code(monkeypatch):
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv, 3, "[", "error listing: directory not found\n"
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RcloneError) as caught:
        run(["lsjson", "somewhere"], what="listing")

    assert caught.value.code == DIRECTORY_NOT_FOUND


# -- against a real rclone ---------------------------------------------


@needs_rclone
def test_a_real_rclone_copies_lists_and_reports_a_missing_directory(tmp_path):
    """The only test in this module that runs the actual external program.

    It exists to check the four facts the offsite plan is built on, on
    whatever rclone version is installed here.
    """
    source = tmp_path / "source"
    (source / "sub").mkdir(parents=True)
    (source / "a.bin").write_bytes(b"hello!")
    (source / "sub" / "b.bin").write_bytes(b"hi!")
    remote = tmp_path / "remote"

    target = destination(str(remote), "web-1", "20260921-000000")
    # These higher timeouts guard against a wedged process, not slowness:
    # starting rclone on a loaded workstation has taken more than 30 seconds.
    copy(str(source), target, timeout=180)

    found = files(target, timeout=120)
    expected = {"a.bin": 6, "sub/b.bin": 3}
    assert missing(expected, found) == []

    assert directories(destination(str(remote), "web-1"), timeout=120) == [
        "20260921-000000"
    ]

    with pytest.raises(RcloneError) as caught:
        files(str(tmp_path / "nowhere"), timeout=120)
    assert caught.value.code == DIRECTORY_NOT_FOUND
