"""Turning a producer into a pipeline whose output nobody else can read.

Two tools, `age` and `gpg`, and one mode: public keys. A passphrase would have
to live on the same machine as the copies it protects, which hands them to
whoever ends up with that machine - so it is refused, by name, because a
configuration carried over from an older tool is likely to ask for it.

Nothing here runs anything. It builds one shell line, the engine runs it, and
the private key never appears in either: it is needed to read a snapshot, never
to write one.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import Config
from .model import BackupError

AGE_RECIPIENT = re.compile(r"^age1[0-9a-z]{58}$")
GPG_FINGERPRINT = re.compile(r"^(0x)?[0-9A-Fa-f]{40}$")

SUFFIX = {"age": ".age", "gpg": ".gpg"}

# --no-options ignores whatever is in the calling user's gpg.conf, so the
# backup does not change meaning with someone's home directory.
# --compress-algo none leaves compression to zstd, one stage earlier.
# --trust-model always encrypts to the backup key without demanding that the
# machine's keyring vouch for it; a server has nobody to sign anything.
GPG_FLAGS = (
    "gpg --batch --yes --no-tty --quiet --no-options "
    "--compress-algo none --no-armor --no-encrypt-to "
    "--trust-model always --encrypt --output -"
)


@dataclass(frozen=True)
class Encryption:
    enabled: bool
    tool: str
    recipients: tuple[str, ...]
    suffix: str

    @classmethod
    def from_config(cls, cfg: Config) -> Encryption:
        if not cfg.get("encryption.enabled"):
            return cls(enabled=False, tool="none", recipients=(), suffix="")

        tool = str(cfg.get("encryption.tool") or "").strip()
        if tool not in SUFFIX:
            raise BackupError(
                f"encryption.tool is {tool!r}; it has to be age or gpg",
            )

        mode = str(cfg.get("encryption.mode") or "recipients").strip()
        if mode != "recipients":
            raise BackupError(
                f"encryption.mode is {mode!r}; holdfast encrypts to public keys "
                "only. A passphrase would have to be stored on the same machine "
                "as the copies it protects, which is no protection from whoever "
                "ends up holding that machine."
            )

        recipients = _recipients(cfg)
        if not recipients:
            raise BackupError(
                "encryption is on and no recipient is configured; set "
                "encryption.recipients, or encryption.recipients_file, or turn "
                "encryption off on purpose. Nothing here writes a plaintext "
                "archive instead."
            )
        for recipient in recipients:
            _validate(tool, recipient)

        return cls(enabled=True, tool=tool, recipients=recipients, suffix=SUFFIX[tool])

    def wrap(self, produce: str) -> str:
        """``produce``, with its output encrypted, still written to stdout."""
        if not self.enabled:
            return produce
        quoted = " ".join(f"-r {shlex.quote(r)}" for r in self.recipients)
        filter_ = (
            f"age --encrypt {quoted}" if self.tool == "age" else f"{GPG_FLAGS} {quoted}"
        )
        return f"{produce} | {filter_}"

    def describe(self) -> dict[str, Any]:
        """For the manifest, which is plaintext inside the snapshot."""
        return {
            "enabled": self.enabled,
            "tool": self.tool,
            "suffix": self.suffix,
            "recipients": list(self.recipients),
        }


def _recipients(cfg: Config) -> tuple[str, ...]:
    """The configured list and the configured file, added together."""
    found: list[str] = []
    listed = cfg.get("encryption.recipients") or []
    if not isinstance(listed, list):
        raise BackupError("encryption.recipients must be a list")
    found.extend(str(item).strip() for item in listed)

    path = str(cfg.get("encryption.recipients_file") or "").strip()
    if path:
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            # Reading nothing here means encrypting to nobody.
            raise BackupError(f"encryption.recipients_file cannot be read: {exc}")
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                found.append(line)

    return tuple(item for item in found if item)


def _validate(tool: str, recipient: str) -> None:
    if tool == "age":
        if not AGE_RECIPIENT.match(recipient):
            raise BackupError(
                f"{recipient!r} is not an age recipient; one looks like "
                "age1 followed by 58 characters"
            )
        return
    if not GPG_FINGERPRINT.match(recipient):
        raise BackupError(
            f"{recipient!r} is not a gpg fingerprint; give the full 40 "
            "characters. Short key ids collide by construction, and a "
            "collision here encrypts the backup to someone else."
        )
