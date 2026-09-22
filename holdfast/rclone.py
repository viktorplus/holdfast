"""A thin client over the external `rclone` program.

Both directions of the offsite copy - the backup sending a snapshot out, and
the watchdog asking later whether it is still there - go through the same
handful of rclone invocations, so they belong in one module rather than two
that would drift apart on argv shapes.

What a failure means is not decided here. `RcloneError` carries rclone's exit
code and nothing else; a missing directory is the ordinary, expected state of
a nightly send that has not run yet, and it is the whole point of a watchdog
check that expects one to be there. Same split as between `backup/probe.py`
and `audit/context.py`: the object that runs the command does not get to know
which meaning applies to whoever is calling it.

No credential passes through here. A remote is a name configured in
`rclone.conf` on the machine; this module never reads that file or touches
what it points to.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable

# Verified against a real run of rclone v1.74.2: the code rclone uses for
# "the target directory does not exist", and the only exit code worth telling
# apart from an ordinary failure - it means "nothing has been sent here yet",
# not "asking failed".
DIRECTORY_NOT_FOUND = 3


class RcloneError(Exception):
    """Something went wrong calling rclone. Its meaning is for the caller."""

    def __init__(self, message: str, *, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


def destination(remote: str, host_label: str, snapshot: str = "") -> str:
    """Where a snapshot for this machine lives on the remote.

    `rclone copy SRC DST` puts the contents of SRC inside DST, so the
    snapshot name has to be part of the destination - there is no directory
    rclone creates on its own that would hold it otherwise.
    """
    remote = remote.strip().rstrip("/")
    label = host_label.strip().strip("/")
    if not remote:
        raise RcloneError("offsite.remote is empty")
    if not label:
        raise RcloneError(
            "the host label is empty: the copy would land on top of another machine's"
        )
    target = f"{remote}/{label}"
    return f"{target}/{snapshot}" if snapshot else target


def run(argv: list[str], *, what: str, timeout: int | None = None) -> str:
    """Runs `rclone` with `argv` and returns its stdout.

    Stdout is returned as-is, unparsed: a failed `lsjson` has already
    written `[` before it gives up, so a caller that trusts stdout without
    checking the exit code first would be parsing that half-written array.
    """
    try:
        result = subprocess.run(
            ["rclone", *argv],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        raise RcloneError(f"{what}: rclone is not on PATH") from None
    except subprocess.TimeoutExpired:
        raise RcloneError(
            f"{what}: rclone did not answer within {timeout} seconds"
        ) from None
    if result.returncode != 0:
        # rclone logs what it tried first and only at the end why it gave
        # up, so the first line of stderr is not the answer - the last one
        # is, the same way probe.py's docker failures use the first because
        # docker orders it the other way round.
        lines = [
            line.strip() for line in (result.stderr or "").splitlines() if line.strip()
        ]
        reason = lines[-1] if lines else f"exit {result.returncode}"
        raise RcloneError(f"{what}: {reason}", code=result.returncode)
    return result.stdout


def copy(
    source: str | os.PathLike[str],
    target: str,
    *,
    timeout: int | None,
    runner: Callable[..., str] = run,
) -> None:
    """Sends the contents of `source` into `target`.

    No `--` before the paths: rclone's end-of-flags marker was never
    exercised here, and a source or target that looks like a flag does not
    happen - both come from this module's own `destination()` or from a
    local snapshot directory.
    """
    runner(["copy", str(source), target], what=f"copying to {target}", timeout=timeout)


def _listing(output: str, *, what: str, required: tuple[str, ...]) -> list[dict]:
    """Parses an `lsjson` answer and checks each element has `required`.

    A malformed element is still a bad answer from rclone, the same as
    non-JSON output or JSON that is not a list - it must become `RcloneError`
    here rather than a bare `KeyError` in whichever caller indexes into it.
    """
    try:
        parsed = json.loads(output)
    except ValueError:
        raise RcloneError(f"{what}: rclone did not return JSON") from None
    if not isinstance(parsed, list):
        raise RcloneError(f"{what}: rclone returned JSON that is not a list") from None
    for entry in parsed:
        if not isinstance(entry, dict) or any(key not in entry for key in required):
            raise RcloneError(
                f"{what}: rclone returned a list entry missing {'/'.join(required)}"
            ) from None
    return parsed


def files(
    target: str,
    *,
    timeout: int | None,
    runner: Callable[..., str] = run,
) -> dict[str, int]:
    """Every file under `target`, by path relative to it, with its size.

    Recursive on purpose: this is the delivery's own post-copy check, and a
    non-recursive listing would only ever see the top-level names inside
    `target`, never the files a component actually wrote.
    """
    what = f"listing the files in {target}"
    output = runner(
        ["lsjson", "-R", "--files-only", target], what=what, timeout=timeout
    )
    parsed = _listing(output, what=what, required=("Path", "Size"))
    return {entry["Path"]: entry["Size"] for entry in parsed}


def directories(
    target: str,
    *,
    timeout: int | None,
    runner: Callable[..., str] = run,
) -> list[str]:
    """The top-level directory names under `target` - the snapshots sent so far.

    Non-recursive on purpose: recursing here would walk into every snapshot
    and list every artifact in it, for the sake of names this call never
    reads.
    """
    what = f"listing the directories in {target}"
    output = runner(["lsjson", "--dirs-only", target], what=what, timeout=timeout)
    parsed = _listing(output, what=what, required=("Name",))
    return [entry["Name"] for entry in parsed]


def missing(expected: dict[str, int], found: dict[str, int]) -> list[str]:
    """Names and sizes present in `expected` but not matching in `found`.

    Names and sizes, never checksums: the bytes on the remote are already
    encrypted, so nothing there hashes back to the plaintext the backup
    started from, and a backend asked for someone else's checksum answers
    "I don't know" - an answer that reads like a refusal without being one.
    """
    problems = []
    for name, size in expected.items():
        if name not in found:
            problems.append(f"{name}: missing")
        elif found[name] != size:
            problems.append(f"{name}: {size} bytes expected, {found[name]} found")
    return problems
