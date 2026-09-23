"""Stopping what is holding the data, and putting it back afterwards.

Loading a dump into a running database, or emptying a volume under a container
that has it open, produces data that is neither the old set nor the new one.
So the containers a restore touches go down first and come back up after.

Which containers those are comes from two places. A database recipe names its
container, because the component declared one, and so does the recipe of an
unnamed volume. A named volume names nothing - a volume says nothing about who
mounts it - so Docker is asked, and asked about
stopped containers too: a stopped one will be started again, and it must not
come back to a volume that was replaced underneath it.
"""

from __future__ import annotations

from collections.abc import Sequence

from .model import BackupError, RestoreError
from .snapshot import Record

NAMED_BY_RECIPE = ("pg_globals", "pg_database", "mysql_database")


def owners(
    records: Sequence[Record],
    probe,
    *,
    pg_container: str | None = None,
) -> list[str]:
    """Every container that has to be down, once each, in a stable order."""
    found: list[str] = []
    for record in records:
        for name in _for(record, probe, pg_container):
            if name and name not in found:
                found.append(name)
    return found


def _for(record: Record, probe, pg_container: str | None) -> list[str]:
    if record.kind in NAMED_BY_RECIPE:
        declared = str(record.recipe.get("container") or "")
        if record.kind.startswith("pg_") and pg_container:
            return [pg_container]
        return [declared] if declared else []
    if record.kind == "docker_volume":
        # An unnamed volume's recipe names its container; its old name would
        # be a question about a volume this machine may never have had.
        container = str(record.recipe.get("container") or "")
        if container:
            return [container]
        return probe.containers_using_volume(str(record.recipe.get("volume") or ""))
    return []


class Services:
    """The containers of one restore, and what has actually been done to them."""

    def __init__(
        self,
        names: Sequence[str],
        probe,
        *,
        dry_run: bool = False,
        enabled: bool = True,
    ):
        self.names = list(names)
        self.probe = probe
        self.dry_run = dry_run
        self.enabled = enabled
        # Kept as it goes rather than recomputed, so that what comes back up is
        # exactly what this run took down.
        self.stopped: list[str] = []

    def stop(self) -> list[str]:
        if not self._acting():
            return []
        for name in self.names:
            try:
                self.probe.capture(
                    ["docker", "stop", name],
                    what=f"stopping {name}",
                    timeout=120,
                )
            except BackupError as exc:
                # Not "carrying on": the next step writes into files this
                # container has open.
                raise RestoreError(
                    f"{name} would not stop, so its data is not going to be "
                    f"rewritten underneath it ({exc})"
                ) from None
            self.stopped.append(name)
        return list(self.stopped)

    def start(self) -> None:
        if not self._acting():
            return
        for name in self.stopped:
            try:
                self.probe.capture(
                    ["docker", "start", name], what=f"starting {name}", timeout=120
                )
            except BackupError as exc:
                raise RestoreError(f"{name} would not start again ({exc})") from None

    def bring_up(self) -> list[str]:
        """Put back what this run took down, after something else went wrong.

        Never raises. It is called when the restore has already failed, and a
        machine left both half-restored and switched off is worse than either.
        """
        complaints: list[str] = []
        if not self._acting():
            return complaints
        for name in self.stopped:
            try:
                self.probe.capture(
                    ["docker", "start", name], what=f"starting {name}", timeout=120
                )
            except BackupError:
                complaints.append(f"{name} did not start again; start it by hand")
        return complaints

    def _acting(self) -> bool:
        return bool(self.names) and self.enabled and not self.dry_run
