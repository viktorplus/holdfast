from pathlib import Path

import pytest

from holdfast.backup.model import BackupError, RestoreError
from holdfast.backup.services import Services, owners
from holdfast.backup.snapshot import Record


class FakeProbe:
    def __init__(self, users=None, refuse=()):
        self.users = users or {}
        self.refuse = set(refuse)
        self.ran: list[list[str]] = []

    def capture(self, argv, *, what: str, timeout: int = 60, env=None) -> str:
        self.ran.append(list(argv))
        if argv[-1] in self.refuse:
            raise BackupError(f"{what}: it would not")
        return ""

    def containers_using_volume(self, name: str) -> list[str]:
        return list(self.users.get(name, []))


def record(kind: str, **recipe) -> Record:
    return Record(
        path=f"{kind}.bin",
        component="thing",
        size=1,
        sha256="",
        recipe={"type": kind, **recipe},
        check="cat >/dev/null",
        file=Path("nowhere"),
    )


def test_the_containers_a_recipe_names_are_collected_once_and_in_order():
    records = [
        record("pg_globals", container="db", user="u"),
        record("pg_database", container="db", user="u", database="app"),
        record("mysql_database", container="shop", database="s"),
    ]

    assert owners(records, FakeProbe()) == ["db", "shop"]


def test_a_volume_has_no_declaration_so_docker_is_asked():
    """Emptying a volume under a live container corrupts what it holds open."""
    probe = FakeProbe(users={"uploads": ["web", "worker"]})
    records = [record("docker_volume", volume="uploads")]

    assert owners(records, probe) == ["web", "worker"]


def test_a_path_recipe_owns_nothing():
    assert owners([record("path", target="/")], FakeProbe()) == []


def test_an_override_replaces_the_container_the_recipe_names():
    """Restoring onto a machine where the container has another name."""
    records = [record("pg_database", container="db", user="u", database="app")]

    assert owners(records, FakeProbe(), pg_container="other-db") == ["other-db"]


def test_stopping_is_refused_rather_than_carried_on_from():
    """Carrying on means writing into a live process's files."""
    probe = FakeProbe(refuse={"stubborn"})
    services = Services(["stubborn"], probe)

    with pytest.raises(RestoreError, match="stubborn"):
        services.stop()


def test_no_stop_leaves_everything_alone():
    probe = FakeProbe()
    services = Services(["db"], probe, enabled=False)
    services.stop()
    services.start()

    assert probe.ran == []


def test_a_dry_run_asks_docker_for_nothing():
    probe = FakeProbe()
    services = Services(["db"], probe, dry_run=True)
    services.stop()
    services.start()

    assert probe.ran == []


def test_what_was_stopped_is_what_comes_back_up():
    probe = FakeProbe()
    services = Services(["db", "shop"], probe)
    services.stop()
    probe.ran.clear()
    services.start()

    assert [argv[-1] for argv in probe.ran] == ["db", "shop"]


def test_only_what_this_run_stopped_is_brought_back():
    """A list kept as it goes, not recomputed: bring_up runs when something has
    already gone wrong, and starting a container this run never touched is a
    change nobody asked for."""
    probe = FakeProbe(refuse={"shop"})
    services = Services(["db", "shop"], probe)

    with pytest.raises(RestoreError):
        services.stop()

    probe.ran.clear()
    complaints = services.bring_up()

    assert [argv[-1] for argv in probe.ran] == ["db"]
    assert complaints == []


def test_bringing_up_never_raises_because_it_runs_after_something_broke():
    probe = FakeProbe(refuse={"db"})
    services = Services(["db"], probe)
    services.stopped.append("db")

    complaints = services.bring_up()

    assert len(complaints) == 1
    assert "db" in complaints[0]
