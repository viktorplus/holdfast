"""Indicators of a campaign, supplied from outside.

Several checks want to know "have you seen any of these": addresses, file
names, checksums, account names. That list belongs to an incident, not to a
tool. It arrives whole, is replaced whole, and is thrown away whole when the
campaign is over.

So it lives in a file the operator points at, and ships empty. Keys in
holdfast.toml would have to be cleaned out by hand, and a forgotten indicator
is a false alarm that outlives whoever added it.
"""

from __future__ import annotations

import fnmatch
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


class IndicatorError(Exception):
    """The indicator file is there but cannot be used."""


@dataclass(frozen=True)
class Indicators:
    addresses: tuple[str, ...] = ()
    filenames: frozenset[str] = frozenset()
    filename_globs: tuple[str, ...] = ()
    md5: dict[str, str] = field(default_factory=dict)
    accounts: tuple[str, ...] = ()
    # Where the list came from, for the finding to name in its clean case.
    source: str = ""

    @property
    def empty(self) -> bool:
        return not (
            self.addresses
            or self.filenames
            or self.filename_globs
            or self.md5
            or self.accounts
        )

    def matches_name(self, name: str) -> bool:
        if name in self.filenames:
            return True
        return any(fnmatch.fnmatch(name, pattern) for pattern in self.filename_globs)

    def describe(self) -> str:
        return (
            f"{len(self.filenames) + len(self.filename_globs)} name(s), "
            f"{len(self.md5)} checksum(s), {len(self.addresses)} address(es), "
            f"{len(self.accounts)} account(s) from {self.source or 'nowhere'}"
        )


def _strings(values, key: str) -> tuple[str, ...]:
    if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
        raise IndicatorError(f"{key} must be a list of strings")
    return tuple(values)


def load_indicators(path: Path | None) -> Indicators:
    """Read the indicator file, or return an empty set when there is none.

    A path that points at nothing raises: the operator asked for a list, and
    silently running without it would turn a deliberate sweep into a check
    that always says "nothing found".
    """
    if path is None or not str(path).strip():
        return Indicators()
    path = Path(path)
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError as exc:
        raise IndicatorError(f"{path} does not exist") from exc
    except OSError as exc:
        raise IndicatorError(f"{path} cannot be read: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise IndicatorError(f"{path} is not valid TOML: {exc}") from exc

    md5 = data.get("md5", {})
    if not isinstance(md5, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in md5.items()
    ):
        raise IndicatorError("md5 must be a table of checksum = where it was seen")

    return Indicators(
        addresses=_strings(data.get("addresses", []), "addresses"),
        filenames=frozenset(_strings(data.get("filenames", []), "filenames")),
        filename_globs=_strings(data.get("filename_globs", []), "filename_globs"),
        md5=dict(md5),
        accounts=_strings(data.get("accounts", []), "accounts"),
        source=str(path),
    )
