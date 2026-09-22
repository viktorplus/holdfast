"""What the snapshot says about itself.

Plaintext on purpose. A snapshot is read on the worst day this machine has, and
often on a different machine entirely - one with no holdfast installed and no
configuration to consult. Everything needed to understand the directory has to
be inside it, in a form a person can read without a tool.

That is also why nothing here holds a secret, and why every describe() feeding
this file is written to give the shape of a thing and not its contents: whoever
holds the disk can read this.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import holdfast

FORMAT = 1
SUMS_FILE = "SHA256SUMS"
CHUNK = 1024 * 1024


def digest(path: Path) -> tuple[str, int]:
    """The sha256 and the size of a file, read once."""
    sha = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK):
            sha.update(chunk)
            size += len(chunk)
    return sha.hexdigest(), size


def build_manifest(
    *,
    snapshot: str,
    created_at: str,
    host_label: str,
    compression: dict[str, Any],
    encryption: dict[str, Any],
    components: list[dict[str, Any]],
    artifacts: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "format": FORMAT,
        "tool": "holdfast",
        "tool_version": holdfast.__version__,
        "snapshot": snapshot,
        "created_at": created_at,
        "host_label": host_label,
        "compression": compression,
        "encryption": encryption,
        "components": components,
        "artifacts": artifacts,
    }


def write_sha256sums(directory: Path) -> Path:
    """Checksums for every file in the snapshot, in `sha256sum` format.

    The manifest already carries a checksum per artifact. This file covers the
    manifest too, and is readable by `sha256sum -c` on a machine that has never
    heard of holdfast.
    """
    path = directory / SUMS_FILE
    lines = []
    for file in sorted(p for p in directory.rglob("*") if p.is_file()):
        if file == path:
            continue
        sha, _ = digest(file)
        relative = file.relative_to(directory).as_posix()
        lines.append(f"{sha}  {relative}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
