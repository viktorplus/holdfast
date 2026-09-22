"""atomic.write_text: the same crash-safety as write_json, for plain text.

The pieces auto-discovery adds (components.toml) are TOML, not JSON, so the
atomic write they need has to hand over the exact text rather than serialize
it. write_json becomes a one-line caller of this so there is still only one
place that knows how to swap a file in safely.
"""

from __future__ import annotations

import stat
from pathlib import Path

from support import posix_only

from holdfast.atomic import write_text


def test_write_text_writes_the_file(tmp_path: Path):
    target = tmp_path / "a.toml"
    result = write_text(target, "x = 1\n")
    assert result == target
    assert target.read_text(encoding="utf-8") == "x = 1\n"


def test_write_text_leaves_no_temporary_file(tmp_path: Path):
    write_text(tmp_path / "a.toml", "x = 1\n")
    assert not (tmp_path / ".a.toml.new").exists()


def test_write_text_replaces_existing_content(tmp_path: Path):
    target = tmp_path / "a.toml"
    write_text(target, "x = 1\n")
    write_text(target, "y = 2\n")
    assert target.read_text(encoding="utf-8") == "y = 2\n"


@posix_only
def test_write_text_mode_is_0600(tmp_path: Path):
    target = write_text(tmp_path / "a.toml", "x = 1\n")
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
