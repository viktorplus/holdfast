"""Helpers shared by the tests.

A synthetic host tree in a temporary directory is the only way to exercise the
watchdog without a compromised server to point it at, so building one is a
one-liner here rather than a ritual in every test. The same goes for a
snapshot: the restore tests need one on disk, and there is no reason for two
test files to know how to lay one out.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from holdfast.audit.context import Context
from holdfast.config import load_config

# Windows reports 0o666 for every file it owns, so a check that judges POSIX
# modes cannot be tested here. On Linux, in CI, these run for real.
posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX file modes only")

# Not the same condition: a Windows machine with Git installed has a real bash,
# and a test that runs the actual pipeline should run there too rather than be
# skipped along with the file-mode ones. bash, because that is what the
# pipelines run under.
needs_sh = pytest.mark.skipif(shutil.which("bash") is None, reason="no bash")

needs_rclone = pytest.mark.skipif(shutil.which("rclone") is None, reason="no rclone")


def config(**overrides):
    cfg = load_config(machine=None, env={})
    for path, value in overrides.items():
        node = cfg.values
        parts = path.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    return cfg


def context(host: Path, **overrides) -> Context:
    return Context(host=host, config=config(**overrides))


def write(root: Path, absolute: str, text: str, mode: int | None = None) -> Path:
    path = root / absolute.lstrip("/")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if mode is not None:
        path.chmod(mode)
    return path


# --------------------------------------------------------------------------
# a snapshot on disk, for the restore tests
# --------------------------------------------------------------------------

BODY = b"a body"
PATH_RECIPE = {"type": "path", "target": "/"}


class Runs:
    """Stands in for the shell: remembers the lines, fails where told to."""

    def __init__(self, code: int = 0, fail_on: str | None = None):
        self.code = code
        self.fail_on = fail_on
        self.lines: list[str] = []

    def __call__(self, line: str) -> int:
        self.lines.append(line)
        if self.fail_on and self.fail_on in line:
            return 1
        return self.code


def artifact(name: str, recipe: dict, component: str = "thing") -> dict:
    import hashlib

    return {
        "path": name,
        "component": component,
        "size": len(BODY),
        "sha256": hashlib.sha256(BODY).hexdigest(),
        "restore": recipe,
        "check": "cat >/dev/null",
    }


def snapshot_dir(tmp_path: Path, *artifacts: dict, **manifest) -> Path:
    import json

    directory = tmp_path / "20260920-231500"
    directory.mkdir(parents=True, exist_ok=True)
    for record in artifacts:
        file = directory / record["path"]
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(BODY)
    body = {
        "format": 1,
        "tool": "holdfast",
        "tool_version": "0.1.0",
        "snapshot": "20260920-231500",
        "created_at": "2026-09-20T23:15:00+00:00",
        "host_label": "web-1",
        "compression": {"tool": "zstd", "level": 10},
        "encryption": {
            "enabled": False,
            "tool": "none",
            "suffix": "",
            "recipients": [],
        },
        "components": [],
        "artifacts": list(artifacts),
        **manifest,
    }
    (directory / "manifest.json").write_text(json.dumps(body), encoding="utf-8")
    return directory


class SnapshotProbe:
    """A machine the restore can talk to, without one being there."""

    def __init__(self, users=None, mountpoints=None, refuse=(), mounted=None):
        self.users = users or {}
        self.mountpoints = mountpoints or {}
        self.mounted = mounted or {}
        self.refuse = set(refuse)
        self.ran: list[list[str]] = []

    def capture(self, argv, *, what: str, timeout: int = 60, env=None) -> str:
        from holdfast.backup.model import BackupError

        self.ran.append(list(argv))
        if argv[-1] in self.refuse:
            raise BackupError(f"{what}: it would not")
        return ""

    def containers_using_volume(self, name: str) -> list[str]:
        return list(self.users.get(name, []))

    def volume_mountpoint(self, name: str) -> str:
        return self.mountpoints[name]

    def mounted_volume(self, container: str, destination: str) -> str:
        from holdfast.backup.model import BackupError

        if (container, destination) not in self.mounted:
            raise BackupError(
                f"the container {container!r} has no volume at {destination}; "
                "bring the application up first (docker compose up -d) and try again"
            )
        return self.mounted[(container, destination)]


# --------------------------------------------------------------------------
# fixtures shaped like `docker inspect`, for the auto-discovery tests
# --------------------------------------------------------------------------

# 64 hex characters, the way Docker names an unnamed volume.
WP_VOLUME = "a1" * 32
ORPHAN_VOLUME = "b2" * 32


def inspect_entry(
    name: str,
    image: str,
    *,
    running: bool = True,
    project: str = "",
    working_dir: str = "",
    mounts=(),
    env=(),
) -> dict:
    """One element of the list `docker inspect` prints."""
    labels = {}
    if project:
        labels["com.docker.compose.project"] = project
    if working_dir:
        labels["com.docker.compose.project.working_dir"] = working_dir
    return {
        "Name": f"/{name}",
        "Config": {"Image": image, "Labels": labels, "Env": list(env)},
        "State": {"Running": running},
        "Mounts": list(mounts),
    }


def volume_mount(name: str, destination: str) -> dict:
    return {
        "Type": "volume",
        "Name": name,
        "Source": f"/var/lib/docker/volumes/{name}/_data",
        "Destination": destination,
    }


def bind_mount(source: str, destination: str) -> dict:
    return {"Type": "bind", "Source": source, "Destination": destination}


def wordpress_site() -> list[dict]:
    """Two containers of one compose project, shaped like a real WordPress site."""
    return [
        inspect_entry(
            "myapp-db-1",
            "mysql:5.7",
            project="myapp",
            working_dir="/root/myapp",
            mounts=[bind_mount("/root/myapp/db_data", "/var/lib/mysql")],
            env=["MYSQL_ROOT_PASSWORD=secret-value", "MYSQL_DATABASE=wordpress"],
        ),
        inspect_entry(
            "myapp-wordpress-1",
            "wordpress:latest",
            project="myapp",
            working_dir="/root/myapp",
            mounts=[volume_mount(WP_VOLUME, "/var/www/html")],
            env=["WORDPRESS_DB_PASSWORD=secret-value"],
        ),
    ]


def myapp_exists(monkeypatch) -> None:
    """Say /root/myapp exists, and leave every other path to the real check.

    The rule asks `os.path.exists` whether a compose project directory is on
    this machine. That function is shared by the whole process - pathlib and
    shutil use it too on newer Pythons - so answering yes to everything
    breaks unrelated file handling; only the fixture's directory is faked.
    """
    real = os.path.exists

    def exists(path) -> bool:
        where = os.fspath(path).replace("\\", "/")
        return where == "/root/myapp" or where.startswith("/root/myapp/") or real(path)

    monkeypatch.setattr(os.path, "exists", exists)
