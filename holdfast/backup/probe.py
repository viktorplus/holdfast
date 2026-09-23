"""Asking the machine what it has, and refusing when it will not say.

Three of the five component types cannot list their artifacts without asking
something: which databases exist, which volumes exist, whether a container is
up. This is where those questions go, and it has one policy - a question the
machine did not answer stops the backup.

That is the opposite of `audit.context.docker_inspect_all`, which returns an
empty list on every failure, and the two are kept apart on purpose. For the
watchdog the empty list is right: a check that cannot answer says UNKNOWN and
the run continues. For a backup it is the defect the old script was patched
for twice - a `docker volume ls` that failed would archive nothing, a `psql`
that failed on authentication would dump nothing, and both nights reported
success. Reusing one helper for both would mean a mode flag selecting between
two opposite meanings of failure, which is worse than having two.

Nothing here is handed to a shell. The shell sees exactly one line per
artifact, built by the engine, and nothing else.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
from collections.abc import Mapping

from .machine import Container, parse_inspect
from .model import BackupError


class Probe:
    """Questions for the machine, each of which may refuse to be answered."""

    def capture(
        self,
        argv: list[str],
        *,
        what: str,
        timeout: int = 60,
        env: Mapping[str, str] | None = None,
    ) -> str:
        """``env`` adds to this process's environment for the child only.

        It carries the path of a credentials file, never a credential: a local
        psql is told where its password file is, and reads it itself.
        """
        try:
            result = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env={**os.environ, **env} if env else None,
            )
        except FileNotFoundError:
            raise BackupError(f"{what}: {argv[0]} is not on PATH") from None
        except subprocess.TimeoutExpired:
            # A wedged docker must not hold the night open forever.
            raise BackupError(
                f"{what}: {argv[0]} did not answer within {timeout} seconds"
            ) from None
        if result.returncode != 0:
            # The first line of stderr, because "the command failed" is not
            # something an operator can act on.
            detail = (result.stderr or "").strip().splitlines()
            reason = detail[0] if detail else f"exit {result.returncode}"
            raise BackupError(f"{what}: {reason}")
        return result.stdout.rstrip("\n")

    def container_running(self, name: str) -> bool:
        """Whether a container by exactly this name is up.

        By its whole name, not a substring: a running `db-replica` must not
        answer for a stopped `db`. The source keeps `grep -Fxq` for this.
        """
        listed = self.capture(
            ["docker", "ps", "--format", "{{.Names}}"],
            what="listing the running containers",
            timeout=30,
        )
        return name in listed.splitlines()

    def containers(self) -> list[tuple[str, str]]:
        """Every running container as (name, image). Used only by --discover."""
        listed = self.capture(
            ["docker", "ps", "--format", "{{.Names}}	{{.Image}}"],
            what="listing the running containers",
            timeout=30,
        )
        found = []
        for line in listed.splitlines():
            name, _, image = line.partition("	")
            if name.strip():
                found.append((name.strip(), image.strip()))
        return found

    def volumes(self) -> list[str]:
        listed = self.capture(
            ["docker", "volume", "ls", "--format", "{{.Name}}"],
            what="listing the docker volumes",
            timeout=30,
        )
        return [line.strip() for line in listed.splitlines() if line.strip()]

    def containers_using_volume(self, name: str) -> list[str]:
        """Who has this volume mounted, running or not.

        There is no declaration for this: a volume says nothing about who uses
        it. The restore has to know, because emptying a volume under a live
        container corrupts whatever it is holding open.
        """
        listed = self.capture(
            [
                "docker",
                "ps",
                "-a",
                "--filter",
                f"volume={name}",
                "--format",
                "{{.Names}}",
            ],
            what=f"finding what uses the volume {name!r}",
            timeout=30,
        )
        return [line.strip() for line in listed.splitlines() if line.strip()]

    def volume_mountpoint(self, name: str) -> str:
        """Where a volume actually lives, asked rather than assumed.

        Building `/var/lib/docker/volumes/<name>/_data` by hand is wrong under
        a non-default data-root and under rootless Docker, and wrong silently.
        """
        path = self.capture(
            ["docker", "volume", "inspect", "--format", "{{.Mountpoint}}", name],
            what=f"finding where the volume {name!r} is stored",
            timeout=30,
        ).strip()
        if not path:
            raise BackupError(f"docker did not say where the volume {name!r} is stored")
        return path

    def directory_size_mb(self, path: str) -> int:
        """Whole megabytes, rounded up.

        Rounding down would report a small volume as zero, and a size limit of
        zero would then archive everything it was meant to exclude.
        """
        total = 0
        for root, _, files in os.walk(path):
            for file in files:
                try:
                    total += os.stat(os.path.join(root, file)).st_size
                except OSError:
                    continue
        return math.ceil(total / (1024 * 1024))

    def inspect_containers(self) -> list[Container]:
        """Every container Docker knows about, running or not, in one picture.

        Two calls because `docker inspect` takes ids, not a filter: first
        `docker ps -aq` to get them, then one `inspect` for all of them
        together, so the machine cannot change its mind between one
        container's picture and the next. An empty listing skips the second
        call rather than asking `docker inspect` for nothing.
        """
        listed = self.capture(
            ["docker", "ps", "-aq"], what="listing the containers", timeout=30
        )
        ids = [line.strip() for line in listed.splitlines() if line.strip()]
        if not ids:
            return []
        text = self.capture(
            ["docker", "inspect", *ids],
            what="inspecting the containers",
            timeout=60,
        )
        return parse_inspect(text)

    def mounted_volume(self, container: str, destination: str) -> str:
        """The name of the volume a container has mounted at `destination`.

        Used where a component names a container and a path inside it rather
        than a volume name directly, so that renaming a volume in a compose
        file does not also require editing the backup configuration.
        """
        text = self.capture(
            ["docker", "inspect", "--format", "{{json .Mounts}}", container],
            what=f"inspecting the mounts of {container!r}",
            timeout=30,
        )
        if text:
            try:
                mounts = json.loads(text)
            except json.JSONDecodeError as exc:
                raise BackupError(
                    f"docker inspect did not return JSON for {container!r}'s mounts: {exc}"
                ) from None
            if not isinstance(mounts, list):
                raise BackupError(
                    f"docker inspect did not return a list of mounts for {container!r}"
                )
        else:
            mounts = []
        for mount in mounts:
            if (
                mount.get("Type") == "volume"
                and mount.get("Destination") == destination
            ):
                return mount.get("Name", "")
        raise BackupError(
            f"the container {container!r} has no volume at {destination}; "
            "bring the application up first (docker compose up -d) and try again"
        )
