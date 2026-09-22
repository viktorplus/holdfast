import subprocess
from pathlib import Path

import pytest

from holdfast.backup import BackupError
from holdfast.backup.probe import Probe


class Ran:
    """Stands in for subprocess.run, remembering how it was called."""

    def __init__(self, stdout="", stderr="", code=0, raises=None):
        self.stdout = stdout
        self.stderr = stderr
        self.code = code
        self.raises = raises
        self.calls: list[tuple] = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if self.raises is not None:
            raise self.raises
        return subprocess.CompletedProcess(argv, self.code, self.stdout, self.stderr)


def probe_with(monkeypatch, ran: Ran) -> Probe:
    monkeypatch.setattr(subprocess, "run", ran)
    return Probe()


def test_output_comes_back_without_its_trailing_newlines(monkeypatch):
    probe = probe_with(monkeypatch, Ran(stdout="one\ntwo\n\n"))

    assert probe.capture(["echo"], what="a thing") == "one\ntwo"


def test_a_failure_carries_both_the_question_and_the_answer(monkeypatch):
    """ "The command failed" tells an operator nothing they can act on."""
    probe = probe_with(
        monkeypatch, Ran(code=2, stderr="FATAL: password authentication failed\n")
    )

    with pytest.raises(BackupError) as caught:
        probe.capture(["psql"], what="listing the databases")

    assert "listing the databases" in str(caught.value)
    assert "password authentication failed" in str(caught.value)


def test_a_missing_program_names_the_program(monkeypatch):
    probe = probe_with(monkeypatch, Ran(raises=FileNotFoundError("docker")))

    with pytest.raises(BackupError, match="docker"):
        probe.capture(["docker", "ps"], what="listing containers")


def test_a_hung_command_names_the_program_and_the_limit(monkeypatch):
    """A wedged docker must not hold the night open forever."""
    probe = probe_with(
        monkeypatch, Ran(raises=subprocess.TimeoutExpired(["docker"], 30))
    )

    with pytest.raises(BackupError) as caught:
        probe.capture(["docker", "ps"], what="listing containers", timeout=30)

    assert "docker" in str(caught.value)
    assert "30" in str(caught.value)


def test_nothing_is_ever_handed_to_a_shell(monkeypatch):
    """The probe passes an argument list. The shell only ever sees the one
    line the engine builds for an artifact."""
    ran = Ran(stdout="x")
    probe = probe_with(monkeypatch, ran)
    probe.capture(["docker", "ps", "--format", "{{.Names}}"], what="listing")

    argv, kwargs = ran.calls[0]
    assert argv == ["docker", "ps", "--format", "{{.Names}}"]
    assert kwargs.get("shell") in (None, False)


def test_a_container_is_recognised_by_its_whole_name(monkeypatch):
    """Not by a substring. The source keeps grep -Fxq for this reason: a
    running db-replica must not answer for a stopped db."""
    probe = probe_with(monkeypatch, Ran(stdout="db-replica\nweb\n"))

    assert probe.container_running("db-replica") is True
    assert probe.container_running("db") is False


def test_volumes_are_split_and_the_blank_lines_dropped(monkeypatch):
    probe = probe_with(monkeypatch, Ran(stdout="one\ntwo\n\n"))

    assert probe.volumes() == ["one", "two"]


def test_a_failed_listing_is_a_refusal_and_never_an_empty_list(monkeypatch):
    """The defect this whole object exists to prevent.

    An empty list here would archive nothing and call the backup a success.
    """
    probe = probe_with(monkeypatch, Ran(code=1, stderr="Cannot connect to daemon"))

    with pytest.raises(BackupError):
        probe.volumes()


def test_a_mountpoint_comes_back_as_a_path(monkeypatch):
    probe = probe_with(monkeypatch, Ran(stdout="/var/lib/docker/volumes/x/_data\n"))

    assert probe.volume_mountpoint("x") == "/var/lib/docker/volumes/x/_data"


def test_a_volume_with_no_mountpoint_is_a_refusal(monkeypatch):
    probe = probe_with(monkeypatch, Ran(stdout="\n"))

    with pytest.raises(BackupError, match="x"):
        probe.volume_mountpoint("x")


def test_a_directory_is_measured_in_whole_megabytes(tmp_path: Path):
    (tmp_path / "deep").mkdir()
    (tmp_path / "deep" / "a").write_bytes(b"x" * 2_000_000)
    (tmp_path / "b").write_bytes(b"y" * 100)

    assert Probe().directory_size_mb(str(tmp_path)) == 2


def test_a_directory_with_one_byte_in_it_is_not_zero(tmp_path: Path):
    """Rounding down would let a size limit of zero archive everything."""
    (tmp_path / "a").write_bytes(b"x")

    assert Probe().directory_size_mb(str(tmp_path)) == 1


def test_an_empty_directory_is_zero(tmp_path: Path):
    assert Probe().directory_size_mb(str(tmp_path)) == 0


def test_containers_come_back_as_names_with_their_images(monkeypatch):
    probe = probe_with(monkeypatch, Ran(stdout="db\tpostgres:16\nweb\tnginx\n\n"))

    assert probe.containers() == [("db", "postgres:16"), ("web", "nginx")]


def test_an_environment_carries_a_path_and_not_a_credential(monkeypatch):
    """A local psql is told where its password file is, and reads it itself."""
    ran = Ran(stdout="x")
    probe = probe_with(monkeypatch, ran)
    probe.capture(["psql"], what="listing", env={"PGPASSFILE": "/etc/pgpass"})

    _, kwargs = ran.calls[0]
    assert kwargs["env"]["PGPASSFILE"] == "/etc/pgpass"


def test_the_users_of_a_volume_are_asked_for_including_stopped_ones(monkeypatch):
    """A stopped container will be started again, and it must not come back to
    a volume that was replaced under it."""
    ran = Ran(stdout="web\nworker\n")
    probe = probe_with(monkeypatch, ran)

    assert probe.containers_using_volume("uploads") == ["web", "worker"]
    assert "-a" in ran.calls[0][0]
    assert "volume=uploads" in ran.calls[0][0]
