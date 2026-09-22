"""The collection profile: what several machines have in common.

A profile is handed around - committed to a private repository, copied to a
new machine, pasted into a chat - so the safe design is not to detect secrets
in it but to make them unrepresentable. Only the keys below are accepted, and
none of them is a secret. Anything else fails loudly rather than travelling.
"""

from __future__ import annotations

import math
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

PROFILE_ALLOWED: dict[str, frozenset[str]] = {
    "alerts": frozenset({"chat_id"}),
    # timeout_minutes is not here: it is a property of this machine's own
    # link, not something a shared profile should hand out to every machine
    # in the collection alike.
    "offsite": frozenset({"remote"}),
    "encryption": frozenset({"recipients"}),
}

TOP_LEVEL_ALLOWED = frozenset({"collection"})

SCALARS = (str, int, float, bool)


class ProfileError(Exception):
    """The profile cannot be used as given."""


def _is_scalar(value: Any) -> bool:
    """A single value that survives the trip into a config file and back.

    nan and inf are floats and TOML accepts them on the way in, but they are
    written back out as ``NaN`` and ``Infinity``, which is JSON's spelling and
    not TOML's: the file would be written, and then unreadable by every
    command after it.
    """
    if isinstance(value, float) and not math.isfinite(value):
        return False
    return isinstance(value, SCALARS)


def _is_writable(value: Any) -> bool:
    """Can this value be written back out as a value in a config file?

    An allowed key name is not enough: a table where a string belongs is
    copied into the machine's config file, where it is not TOML at all, and
    every later command fails on a file the operator never wrote.
    """
    if isinstance(value, list):
        return all(_is_scalar(item) for item in value)
    return _is_scalar(value)


def _unwritable(name: str) -> str:
    return (
        f"{name} must be text, a finite number or a list of them: anything "
        "else cannot be written into a machine's config file"
    )


def profile_complaints(data: Mapping[str, Any]) -> list[str]:
    complaints: list[str] = []
    for key, value in data.items():
        if key in TOP_LEVEL_ALLOWED:
            if not _is_writable(value):
                complaints.append(_unwritable(repr(key)))
            continue
        if key not in PROFILE_ALLOWED:
            complaints.append(
                f"unknown block [{key}]: a profile carries only "
                f"{', '.join(sorted(PROFILE_ALLOWED))}"
            )
            continue
        if not isinstance(value, Mapping):
            complaints.append(f"[{key}] must be a table")
            continue
        for inner, inner_value in value.items():
            if inner not in PROFILE_ALLOWED[key]:
                complaints.append(
                    f"[{key}] does not accept {inner!r}: a profile holds no "
                    f"secrets, only {', '.join(sorted(PROFILE_ALLOWED[key]))}"
                )
            elif not _is_writable(inner_value):
                complaints.append(_unwritable(f"[{key}] {inner!r}"))
    return complaints


def load_profile(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ProfileError(f"no profile at {path}")
    with path.open("rb") as handle:
        try:
            data = tomllib.load(handle)
        except tomllib.TOMLDecodeError as exc:
            raise ProfileError(f"{path} is not valid TOML: {exc}") from exc
    complaints = profile_complaints(data)
    if complaints:
        raise ProfileError("; ".join(complaints))
    return data
