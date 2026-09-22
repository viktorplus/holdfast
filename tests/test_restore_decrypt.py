import shlex
from pathlib import Path

import pytest

from holdfast.backup.decrypt import Identity, open_identity, plain_name, tool_for
from holdfast.backup.model import RestoreError

AGE_MARKER = "AGE-" + "SECRET-" + "KEY-1EXAMPLE"


class Runs:
    """Stands in for running a shell line, remembering every one."""

    def __init__(self, code: int = 0):
        self.code = code
        self.lines: list[str] = []

    def __call__(self, line: str) -> int:
        self.lines.append(line)
        return self.code


def a_record(tmp_path: Path, name: str, size: int):
    from holdfast.backup.snapshot import Record

    file = tmp_path / name
    file.write_bytes(b"x" * size)
    return Record(
        path=name,
        component="thing",
        size=size,
        sha256="",
        recipe={"type": "none"},
        check="cat >/dev/null",
        file=file,
    )


def test_the_tool_is_read_from_the_file_and_not_from_the_manifest():
    """A bare machine restores knowing only the artifacts and the key. There
    may be no manifest to consult and no holdfast configured to read one."""
    assert tool_for("db.dump.age") == "age"
    assert tool_for("db.dump.gpg") == "gpg"
    assert tool_for("db.dump") == "none"


def test_the_plain_name_loses_the_encryption_suffix_and_nothing_else():
    assert plain_name("files.tar.zst.age") == "files.tar.zst"
    assert plain_name("files.tar.zst") == "files.tar.zst"


def test_an_unencrypted_artifact_needs_no_key(tmp_path: Path):
    with open_identity(None) as identity:
        assert (
            identity.stream(tmp_path / "a.bin")
            == f"cat -- {shlex.quote(str(tmp_path / 'a.bin'))}"
        )


def test_an_encrypted_artifact_without_a_key_says_which_flag_is_missing(
    tmp_path: Path,
):
    with (
        open_identity(None) as identity,
        pytest.raises(RestoreError, match="--identity"),
    ):
        identity.stream(tmp_path / "a.bin.age")


def test_an_age_key_is_recognised_by_what_is_in_it(tmp_path: Path):
    key = tmp_path / "key.txt"
    key.write_text(f"# created ...\n{AGE_MARKER}\n", encoding="utf-8")

    with open_identity(key) as identity:
        line = identity.stream(tmp_path / "a.bin.age")

    assert identity.kind == "age"
    assert "age --decrypt" in line
    assert shlex.quote(str(key)) in line


def test_a_gpg_key_gets_a_keyring_of_its_own(tmp_path: Path):
    """Never the machine's own keyring: the private key is not supposed to be
    left behind on the machine being restored."""
    key = tmp_path / "secret.asc"
    key.write_text("not an age key\n", encoding="utf-8")
    runs = Runs()

    with open_identity(key, run=runs) as identity:
        home = identity.gnupghome
        line = identity.stream(tmp_path / "a.bin.gpg")

        assert home is not None and home.is_dir()
        assert f"--homedir {shlex.quote(str(home))}" in line
        assert "--pinentry-mode loopback" in line


def test_the_keyring_and_its_agent_are_gone_afterwards(tmp_path: Path):
    key = tmp_path / "secret.asc"
    key.write_text("not an age key\n", encoding="utf-8")
    runs = Runs()

    with open_identity(key, run=runs) as identity:
        home = identity.gnupghome

    assert home is not None and not home.exists()
    # Removing the directory alone leaves a gpg-agent running with the private
    # key in memory, which is the opposite of the promise being made here.
    assert any("gpgconf" in line and "--kill" in line for line in runs.lines)


def test_the_keyring_goes_even_when_the_restore_blew_up(tmp_path: Path):
    """A private key must not outlive a failed run."""
    key = tmp_path / "secret.asc"
    key.write_text("not an age key\n", encoding="utf-8")
    seen = {}

    with pytest.raises(ZeroDivisionError), open_identity(key, run=Runs()) as identity:
        seen["home"] = identity.gnupghome
        raise ZeroDivisionError

    assert not seen["home"].exists()


def test_a_passphrase_protected_key_can_be_opened_without_a_terminal(tmp_path: Path):
    """A key kept offline on a stick is normally passphrase-protected, and a
    rescue box has no pinentry for gpg to reach."""
    key = tmp_path / "secret.asc"
    key.write_text("not an age key\n", encoding="utf-8")
    phrase = tmp_path / "phrase"
    phrase.write_text("x\n", encoding="utf-8")

    with open_identity(key, passphrase_file=phrase, run=Runs()) as identity:
        line = identity.stream(tmp_path / "a.bin.gpg")

    assert f"--passphrase-file {shlex.quote(str(phrase))}" in line


def test_an_unreadable_key_is_refused(tmp_path: Path):
    with (
        pytest.raises(RestoreError, match="gone.txt"),
        open_identity(tmp_path / "gone.txt"),
    ):
        pass


def test_the_probe_opens_the_smallest_artifact_of_each_kind(tmp_path: Path):
    """The smallest, because this runs before anything is touched and a
    multi-gigabyte dump would make that wait for nothing."""
    records = [
        a_record(tmp_path, "big.bin.age", 300),
        a_record(tmp_path, "small.bin.age", 10),
        a_record(tmp_path, "middle.bin.age", 100),
    ]
    key = tmp_path / "key.txt"
    key.write_text(AGE_MARKER, encoding="utf-8")
    runs = Runs()

    with open_identity(key) as identity:
        identity.probe(records, runs)

    assert len(runs.lines) == 1
    assert "small.bin.age" in runs.lines[0]


def test_the_probe_looks_at_every_kind_of_encryption_present(tmp_path: Path):
    """A snapshot may mix .age and .gpg, and one identity file cannot be both.

    So a mixed snapshot with a single key is a snapshot that cannot be fully
    restored - and the point is that this is found out here, before anything
    has been stopped or written, rather than once the .age half is already in.
    """
    records = [
        a_record(tmp_path, "a.bin.age", 10),
        a_record(tmp_path, "b.bin.gpg", 20),
        a_record(tmp_path, "c.bin", 5),
    ]
    key = tmp_path / "key.txt"
    key.write_text(AGE_MARKER, encoding="utf-8")

    with open_identity(key) as identity, pytest.raises(RestoreError, match="gpg"):
        identity.probe(records, Runs())


def test_the_probe_reads_the_artifact_to_the_end(tmp_path: Path):
    """Stopping it early kills the decrypter with SIGPIPE, pipefail reports
    that as failure, and the operator's key gets blamed for every artifact
    bigger than the pipe buffer."""
    records = [a_record(tmp_path, "a.bin.age", 10)]
    key = tmp_path / "key.txt"
    key.write_text(AGE_MARKER, encoding="utf-8")
    runs = Runs()

    with open_identity(key) as identity:
        identity.probe(records, runs)

        assert runs.lines == [identity.stream(records[0].file)]


def test_a_key_that_cannot_open_the_snapshot_says_so_before_anything_moves(
    tmp_path: Path,
):
    records = [a_record(tmp_path, "a.bin.age", 10)]
    key = tmp_path / "key.txt"
    key.write_text(AGE_MARKER, encoding="utf-8")

    with open_identity(key) as identity, pytest.raises(RestoreError) as caught:
        identity.probe(records, Runs(code=1))

    assert "a.bin.age" in str(caught.value)
    assert "age" in str(caught.value)


def test_an_unencrypted_snapshot_needs_no_probe(tmp_path: Path):
    records = [a_record(tmp_path, "a.bin", 10)]
    runs = Runs(code=1)

    with open_identity(None) as identity:
        identity.probe(records, runs)

    assert runs.lines == []


def test_an_identity_is_never_copied_anywhere(tmp_path: Path):
    """It is read where it lies, for the length of one run."""
    key = tmp_path / "key.txt"
    key.write_text(AGE_MARKER, encoding="utf-8")

    with open_identity(key) as identity:
        assert isinstance(identity, Identity)
        assert identity.path == key

    assert sorted(p.name for p in tmp_path.iterdir()) == ["key.txt"]
