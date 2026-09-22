"""Everything a check is allowed to look at.

A check never opens a host path directly. It asks the context, which maps the
path onto whatever is mounted as the host - the real root when holdfast runs
on the machine, a mount point when it runs in a container, a temporary
directory when it runs under test. That indirection is what makes the whole
watchdog testable without a broken server to point it at.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Config
from .indicators import Indicators, load_indicators

# Walking a real /opt or /home can mean millions of files. Every scan is
# bounded, and a scan that hits its limit says so instead of reporting a clean
# result it never established.
MAX_SCAN_FILES = 40000
MAX_TEXT_BYTES = 1_000_000

SKIP_DIRS = frozenset(
    {
        ".git",
        "node_modules",
        "__pycache__",
        "vendor",
        "site-packages",
        "proc",
        "sys",
        "dev",
        "run",
        "snap",
    }
)

# Temporary directories: writable by anyone, cleared on reboot, and therefore
# where a payload lands first.
TEMP_DIRS = ("/tmp", "/var/tmp", "/dev/shm")


def fmt_time(epoch: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(epoch)) + " UTC"


def days_ago(epoch: float) -> float:
    return (time.time() - epoch) / 86400


def docker_inspect_all() -> list[dict[str, Any]]:
    """`docker inspect` of every container, or an empty list.

    Docker is optional (see the threat model): a machine without it is served
    in full, and the checks that need it say UNKNOWN rather than failing.
    """
    try:
        ids = subprocess.run(
            ["docker", "ps", "-aq"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if ids.returncode != 0 or not ids.stdout.strip():
            return []
        result = subprocess.run(
            ["docker", "inspect", *ids.stdout.split()],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if result.returncode != 0:
            return []
        parsed = json.loads(result.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return []
    return parsed if isinstance(parsed, list) else []


@dataclass
class Context:
    host: Path
    config: Config
    # The report from the previous run, when there is one. Checks that keep a
    # baseline compare against it; everything else ignores it.
    previous: dict[str, Any] | None = None
    # Whether the checks may talk to a Docker daemon at all. `host` remaps the
    # filesystem, and nothing else: with a tree mounted somewhere, the local
    # daemon still answers for the machine holdfast is running on, which is
    # right in production and wrong when auditing a copy of another machine.
    docker_enabled: bool = True
    # Same reasoning, for the network rather than Docker: `host` remaps the
    # filesystem and nothing else, so a watchdog run against a mounted copy
    # of another machine would otherwise reach out to the real shared
    # storage. With the network refused, `backup_offsite_copy` still answers
    # from the journal, and says out loud that the storage was not asked.
    offsite_enabled: bool = True
    _containers: list[dict[str, Any]] | None = field(default=None, repr=False)
    _indicators: Indicators | None = field(default=None, repr=False)

    # -- the host filesystem ------------------------------------------------

    def path(self, absolute: str) -> Path:
        """Maps a host-absolute path onto the mounted host filesystem."""
        return self.host / absolute.lstrip("/")

    def show(self, path: Path) -> str:
        """Renders a path the way it looks on the host, not where it is read."""
        try:
            return "/" + str(path.relative_to(self.host)).replace(os.sep, "/")
        except ValueError:
            return str(path)

    def read_text(self, path: Path) -> str | None:
        try:
            if path.stat().st_size > self.conf_int("audit.scan.max_text_bytes"):
                return None
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    @property
    def on_host(self) -> bool:
        """True when this process runs on the machine being audited.

        Then ``sshd -T`` and friends answer for the real host, and a criterion
        that would otherwise be an honest UNKNOWN can actually be checked.
        """
        return str(self.host) in (os.sep, "/")

    def walk(self, roots: Iterable[str]) -> Iterator[tuple[Path, os.stat_result, bool]]:
        """Bounded walk over the host tree. The third value is the truncation flag.

        A file is yielded once however many of the given roots contain it. The
        roots do overlap - a document root under /var/www is also inside the
        secrets roots, and /var/lib/docker/volumes is inside /var/lib - and a
        check that saw the same file twice would count it twice and report the
        same problem twice.
        """
        seen = 0
        visited: set[str] = set()
        limit = self.conf_int("audit.scan.max_files")
        for root in roots:
            base = self.path(root)
            if not base.is_dir():
                continue
            for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
                dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
                for name in filenames:
                    path = Path(dirpath) / name
                    key = str(path)
                    if key in visited:
                        continue
                    visited.add(key)
                    try:
                        st = path.lstat()
                    except OSError:
                        continue
                    if not stat.S_ISREG(st.st_mode):
                        continue
                    seen += 1
                    if seen > limit:
                        yield path, st, True
                        return
                    yield path, st, False

    # -- settings -----------------------------------------------------------

    def conf(self, key: str) -> Any:
        return self.config.get(key)

    def conf_list(self, key: str) -> list[Any]:
        """A configured array.

        An empty array is a value, not an absence: it is how an operator says
        "stop treating anything as normal here".
        """
        value = self.config.get(key)
        if value is None:
            return []
        if isinstance(value, list):
            return list(value)
        return [value]

    def conf_int(self, key: str) -> int:
        value = self.config.get(key)
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    def conf_re(self, key: str) -> re.Pattern | None:
        """A configured regex, or None when it is empty or malformed.

        A broken pattern must not take the check down: it means the operator
        mistyped an exclusion, and the right answer is to exclude nothing and
        report everything, not to report nothing.
        """
        raw = str(self.config.get(key) or "").strip()
        if not raw:
            return None
        try:
            return re.compile(raw, re.IGNORECASE)
        except re.error:
            return None

    # -- the previous run ---------------------------------------------------

    def previous_data(self, check_id: str) -> dict[str, Any]:
        for finding in (self.previous or {}).get("findings", []):
            if finding.get("id") == check_id:
                return finding.get("data") or {}
        return {}

    # -- the world outside the filesystem -----------------------------------

    def containers(self) -> list[dict[str, Any]]:
        """Every container, inspected once per run rather than per check."""
        if not self.docker_enabled:
            return []
        if self._containers is None:
            self._containers = docker_inspect_all()
        return self._containers

    def indicators(self) -> Indicators:
        """The campaign indicator list, read once per run.

        A path that points at nothing is a configuration error, and it is
        raised: the operator asked for a sweep, and a sweep with no list that
        reports "nothing found" is worse than no sweep at all.
        """
        if self._indicators is None:
            path = str(self.config.get("audit.indicators") or "").strip()
            self._indicators = load_indicators(Path(path) if path else None)
        return self._indicators

    def recent_days(self) -> int:
        """No `or` fallback here, deliberately.

        The default lives in config.DEFAULTS, so a configured value always
        arrives. Falling back on a falsy one would quietly override a zero,
        and a zero is how an operator says "stop treating recency as news".
        """
        return self.conf_int("audit.recent_days")
