import json

import pytest
from support import bind_mount, inspect_entry, volume_mount, wordpress_site

from holdfast.backup.machine import Container, Mount, parse_inspect
from holdfast.backup.model import BackupError
from holdfast.backup.probe import Probe

# --------------------------------------------------------------------------
# parse_inspect
# --------------------------------------------------------------------------


def test_containers_come_back_sorted_by_name_without_the_leading_slash():
    containers = parse_inspect(json.dumps(wordpress_site()))

    assert [c.name for c in containers] == ["myapp-db-1", "myapp-wordpress-1"]


def test_a_container_carries_its_compose_project_and_working_directory():
    containers = parse_inspect(json.dumps(wordpress_site()))
    db = containers[0]

    assert db.project == "myapp"
    assert db.working_dir == "/root/myapp"


def test_a_bind_mount_has_no_volume_name():
    containers = parse_inspect(json.dumps(wordpress_site()))
    db = containers[0]

    assert db.mounts == (
        Mount(
            kind="bind",
            name="",
            source="/root/myapp/db_data",
            destination="/var/lib/mysql",
        ),
    )


def test_env_names_are_kept_but_values_are_not():
    containers = parse_inspect(json.dumps(wordpress_site()))
    db = next(c for c in containers if c.name == "myapp-db-1")

    assert "MYSQL_ROOT_PASSWORD" in db.env_names
    assert "secret-value" not in repr(containers)


def test_pgdata_postgres_user_and_allow_empty_are_kept_with_their_values():
    entry = inspect_entry(
        "db",
        "postgres:16",
        env=[
            "PGDATA=/var/lib/postgresql/data/pgdata",
            "POSTGRES_USER=app",
            "MYSQL_ALLOW_EMPTY_PASSWORD=yes",
        ],
    )
    container = parse_inspect(json.dumps([entry]))[0]

    assert container.env == {
        "PGDATA": "/var/lib/postgresql/data/pgdata",
        "POSTGRES_USER": "app",
        "MYSQL_ALLOW_EMPTY_PASSWORD": "yes",
    }


def test_a_stopped_container_is_not_running():
    entry = inspect_entry("db", "postgres:16", running=False)

    container = parse_inspect(json.dumps([entry]))[0]

    assert container.running is False


def test_missing_labels_env_and_mounts_do_not_crash():
    entry = {
        "Name": "/db",
        "Config": {"Image": "postgres:16", "Labels": None, "Env": None},
        "State": {"Running": True},
        "Mounts": None,
    }

    container = parse_inspect(json.dumps([entry]))[0]

    assert container == Container(name="db", image="postgres:16", running=True)


def test_non_json_is_a_backup_error():
    with pytest.raises(BackupError, match="docker inspect"):
        parse_inspect("not json")


def test_a_json_value_that_is_not_a_list_is_a_backup_error():
    with pytest.raises(BackupError, match="docker inspect"):
        parse_inspect(json.dumps({"not": "a list"}))


def test_an_entry_that_is_not_an_object_is_a_backup_error():
    with pytest.raises(BackupError, match="docker inspect"):
        parse_inspect(json.dumps([1]))


# --------------------------------------------------------------------------
# Probe.inspect_containers
# --------------------------------------------------------------------------


class FakeProbe(Probe):
    def __init__(self, answers: dict[tuple, str]):
        self.answers = answers
        self.calls: list[list[str]] = []

    def capture(self, argv, *, what, timeout=60, env=None):
        self.calls.append(list(argv))
        return self.answers[tuple(argv)]


def test_no_containers_means_no_second_call():
    probe = FakeProbe({("docker", "ps", "-aq"): ""})

    assert probe.inspect_containers() == []
    assert probe.calls == [["docker", "ps", "-aq"]]


def test_two_ids_are_inspected_together():
    entries = json.dumps([inspect_entry("a", "img"), inspect_entry("b", "img")])
    probe = FakeProbe(
        {
            ("docker", "ps", "-aq"): "id1\nid2\n",
            ("docker", "inspect", "id1", "id2"): entries,
        }
    )

    containers = probe.inspect_containers()

    assert [c.name for c in containers] == ["a", "b"]
    assert probe.calls[1] == ["docker", "inspect", "id1", "id2"]


# --------------------------------------------------------------------------
# Probe.mounted_volume
# --------------------------------------------------------------------------


def test_mounted_volume_finds_the_volume_at_the_destination():
    mounts = json.dumps([volume_mount("wp_data", "/var/www/html")])
    probe = FakeProbe(
        {("docker", "inspect", "--format", "{{json .Mounts}}", "web-1"): mounts}
    )

    assert probe.mounted_volume("web-1", "/var/www/html") == "wp_data"


def test_a_bind_at_the_same_destination_does_not_count():
    mounts = json.dumps([bind_mount("/root/myapp/data", "/var/www/html")])
    probe = FakeProbe(
        {("docker", "inspect", "--format", "{{json .Mounts}}", "web-1"): mounts}
    )

    with pytest.raises(BackupError, match="docker compose up -d"):
        probe.mounted_volume("web-1", "/var/www/html")


def test_no_matching_mount_names_the_fix():
    probe = FakeProbe(
        {("docker", "inspect", "--format", "{{json .Mounts}}", "web-1"): "[]"}
    )

    with pytest.raises(BackupError, match="docker compose up -d"):
        probe.mounted_volume("web-1", "/var/www/html")


def test_a_non_json_mounts_answer_is_a_backup_error():
    probe = FakeProbe(
        {("docker", "inspect", "--format", "{{json .Mounts}}", "web-1"): "not json"}
    )

    with pytest.raises(BackupError, match="docker inspect"):
        probe.mounted_volume("web-1", "/var/www/html")


def test_a_mounts_answer_that_is_not_a_list_is_a_backup_error():
    mounts = json.dumps({"not": "a list"})
    probe = FakeProbe(
        {("docker", "inspect", "--format", "{{json .Mounts}}", "web-1"): mounts}
    )

    with pytest.raises(BackupError, match="docker inspect"):
        probe.mounted_volume("web-1", "/var/www/html")
