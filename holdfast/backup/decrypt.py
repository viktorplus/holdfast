"""Opening a snapshot, without leaving the key on the machine that opened it.

Which tool encrypted an artifact is read from its file name, never from the
manifest. A snapshot is opened on the worst day a machine has, often on a
different machine with nothing configured, and the promise is that the
artifacts and the key are enough.

The key is read where it lies, for the length of one run. A gpg secret key
needs a keyring, so it gets a throwaway one that is destroyed afterwards -
together with the agent gpg starts behind it, because removing the directory
alone leaves that agent running with the private key in memory, which is the
opposite of the promise being made.
"""

from __future__ import annotations

import contextlib
import shlex
import shutil
import tempfile
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from .engine import run_line
from .model import RestoreError

SUFFIXES = {".age": "age", ".gpg": "gpg"}
NONE = "none"

# An age identity file says what it is. A gpg key does not, so it is tried.
AGE_MARKER = "AGE-" + "SECRET-" + "KEY-"

GPG_COMMON = "gpg --batch --yes --no-tty --quiet --no-options"

Run = Callable[[str], int]


def tool_for(name: str) -> str:
    """Which tool encrypted this file, judged by its name alone."""
    return SUFFIXES.get(Path(name).suffix, NONE)


def plain_name(name: str) -> str:
    return name[: -len(Path(name).suffix)] if tool_for(name) != NONE else name


@dataclass(frozen=True)
class Identity:
    path: Path | None
    kind: str
    gnupghome: Path | None = None
    passphrase_file: Path | None = None

    def stream(self, file: Path) -> str:
        """A shell line writing this artifact's plaintext to stdout.

        Never to a file: the plaintext of a snapshot has no more business on
        the restoring machine's disk than it had on the backing-up one's.
        """
        tool = tool_for(file.name)
        quoted = shlex.quote(str(file))
        if tool == NONE:
            return f"cat -- {quoted}"
        if self.path is None:
            raise RestoreError(
                f"{file.name} is encrypted and no key was given; pass --identity"
            )
        if tool == "age":
            return f"age --decrypt -i {shlex.quote(str(self.path))} -- {quoted}"
        if self.gnupghome is None:
            raise RestoreError(
                f"{self.path} is not a gpg secret key, and {file.name} needs one"
            )
        phrase = (
            f" --passphrase-file {shlex.quote(str(self.passphrase_file))}"
            if self.passphrase_file
            else ""
        )
        # --pinentry-mode loopback is what makes a passphrase-protected key
        # usable without a terminal. Without it gpg reaches for a pinentry that
        # a rescue box does not have, and hangs - and a key kept offline on a
        # stick is normally protected, so that is the common case.
        return (
            f"{GPG_COMMON} --homedir {shlex.quote(str(self.gnupghome))} "
            f"--pinentry-mode loopback{phrase} --decrypt -- {quoted}"
        )

    def probe(self, records: Sequence, run: Run) -> None:
        """Open one artifact of each kind, before anything has been touched.

        The smallest of each kind, because this is pure waiting and a run that
        is going to fail on the key should fail in a second rather than after a
        multi-gigabyte dump. And read to the end rather than stopped early:
        stopping it kills the decrypter with SIGPIPE, pipefail calls that a
        failure, and the key gets blamed for every artifact larger than the
        pipe buffer.
        """
        smallest: dict[str, object] = {}
        for record in records:
            tool = tool_for(record.path)
            if tool == NONE:
                continue
            best = smallest.get(tool)
            if best is None or record.size < best.size:  # type: ignore[union-attr]
                smallest[tool] = record
        for tool, record in smallest.items():
            if run(self.stream(record.file)) != 0:  # type: ignore[union-attr]
                raise RestoreError(
                    f"the key given cannot open {record.path} ({tool});"  # type: ignore[union-attr]
                    " nothing has been changed"
                )


@contextlib.contextmanager
def open_identity(
    path: Path | None,
    *,
    passphrase_file: Path | None = None,
    run: Run = run_line,
) -> Iterator[Identity]:
    if path is None:
        yield Identity(path=None, kind=NONE)
        return

    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise RestoreError(f"the identity file cannot be read: {path}: {exc}") from None

    if AGE_MARKER in text:
        yield Identity(path=path, kind="age", passphrase_file=passphrase_file)
        return

    home = Path(tempfile.mkdtemp(prefix="holdfast-gnupg-"))
    try:
        home.chmod(0o700)
        imported = run(
            f"{GPG_COMMON} --homedir {shlex.quote(str(home))} "
            f"--import {shlex.quote(str(path))}"
        )
        yield Identity(
            path=path,
            kind="gpg" if imported == 0 else "age",
            gnupghome=home if imported == 0 else None,
            passphrase_file=passphrase_file,
        )
    finally:
        # The agent first. Removing the directory on its own leaves gpg-agent
        # alive holding the private key in memory.
        run(f"gpgconf --homedir {shlex.quote(str(home))} --kill all")
        shutil.rmtree(home, ignore_errors=True)
