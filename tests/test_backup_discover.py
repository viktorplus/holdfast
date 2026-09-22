import tomllib
from pathlib import Path

from holdfast.backup import BackupError, load_components
from holdfast.backup.discover import discover
from holdfast.config import load_config


class Machine:
    """A machine that answers, or one that has no Docker at all."""

    def __init__(self, containers=(), volumes=(), sizes=None, docker=True):
        self._containers = list(containers)
        self._volumes = list(volumes)
        self.sizes = sizes or {}
        self.docker = docker

    def _refuse(self):
        raise BackupError("listing the running containers: docker is not on PATH")

    def containers(self):
        if not self.docker:
            self._refuse()
        return list(self._containers)

    def volumes(self):
        if not self.docker:
            self._refuse()
        return list(self._volumes)

    def volume_mountpoint(self, name: str) -> str:
        return f"/var/lib/docker/volumes/{name}/_data"

    def directory_size_mb(self, path: str) -> int:
        return self.sizes.get(path.rsplit("/", 2)[-2], 1)


def loads(text: str) -> dict:
    return tomllib.loads(text)


def a_host(tmp_path: Path, *directories: str) -> Path:
    for directory in directories:
        (tmp_path / directory.lstrip("/")).mkdir(parents=True, exist_ok=True)
    return tmp_path


def test_a_machine_without_docker_still_gets_a_draft(tmp_path: Path):
    """Discovery is advice, not a backup run. It has nothing to refuse over."""
    host = a_host(tmp_path, "opt/stalwart")
    text = discover(Machine(docker=False), host=host)

    assert "docker is not on PATH" in text
    assert [c["type"] for c in loads(text)["component"]] == ["path"]


def test_a_postgres_container_becomes_a_postgres_component(tmp_path: Path):
    machine = Machine(containers=[("signal-postgres", "postgres:16")])
    text = discover(machine, host=a_host(tmp_path))
    component = loads(text)["component"][0]

    assert component["type"] == "postgres"
    assert component["container"] == "signal-postgres"


def test_mysql_and_mariadb_are_both_recognised(tmp_path: Path):
    machine = Machine(containers=[("shop-db", "mysql:8"), ("wiki-db", "mariadb:11")])
    text = discover(machine, host=a_host(tmp_path))

    assert [c["type"] for c in loads(text)["component"]] == ["mysql", "mysql"]


def test_a_container_that_is_not_a_database_is_not_guessed_at(tmp_path: Path):
    machine = Machine(containers=[("web", "nginx:1.27")])

    assert loads(discover(machine, host=a_host(tmp_path))).get("component", []) == []


def test_each_directory_under_opt_becomes_a_path_component(tmp_path: Path):
    host = a_host(tmp_path, "opt/Stalwart Mail", "opt/example")
    text = discover(Machine(docker=False), host=host)
    names = [c["name"] for c in loads(text)["component"]]

    assert names == ["stalwart-mail", "example"]


def test_a_volume_over_the_limit_is_written_into_exclude(tmp_path: Path):
    """Rather than vanishing from the draft, where nobody would ask why."""
    machine = Machine(volumes=["small", "huge"], sizes={"huge": 4000})
    text = discover(machine, host=a_host(tmp_path))
    volumes = next(c for c in loads(text)["component"] if c["type"] == "docker_volume")

    assert volumes["exclude"] == ["huge"]


def test_a_database_volume_is_excluded_because_the_dump_covers_it(tmp_path: Path):
    """A copy of a running data directory is not a backup of the database."""
    machine = Machine(
        containers=[("signal-postgres", "postgres:16")],
        volumes=["signal-engine_pg_data", "uploads"],
    )
    text = discover(machine, host=a_host(tmp_path))
    volumes = next(c for c in loads(text)["component"] if c["type"] == "docker_volume")

    assert "signal-engine_pg_data" in volumes["exclude"]


def test_the_draft_is_a_configuration_holdfast_can_actually_read(tmp_path: Path):
    """The point of the whole file. A draft that does not parse is worse than
    no draft, because it is found out one edit later."""
    machine = Machine(
        containers=[("signal-postgres", "postgres:16"), ("shop-db", "mariadb:11")],
        volumes=["uploads", "huge"],
        sizes={"huge": 9000},
    )
    host = a_host(tmp_path, "opt/example")
    draft = tmp_path / "holdfast.toml"
    draft.write_text(discover(machine, host=host), encoding="utf-8")

    components = load_components(load_config(machine=draft, env={}))

    assert {c.type for c in components} == {
        "postgres",
        "mysql",
        "docker_volume",
        "path",
    }


def test_discovery_writes_nothing(tmp_path: Path):
    host = a_host(tmp_path, "opt/example")
    before = sorted(p.name for p in host.iterdir())

    discover(Machine(docker=False), host=host)

    assert sorted(p.name for p in host.iterdir()) == before
