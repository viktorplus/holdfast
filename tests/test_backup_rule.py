import pytest

from holdfast.backup import component_types
from holdfast.backup.model import BackupError
from holdfast.backup.rule import (
    Declared,
    Exclusions,
    database_kind,
    parse_exclusions,
    under,
)

# --------------------------------------------------------------------------
# parse_exclusions
# --------------------------------------------------------------------------


def test_an_empty_list_parses_to_empty_exclusions():
    assert parse_exclusions([]) == Exclusions()


def test_each_of_the_four_kinds_lands_in_its_own_field():
    exclusions = parse_exclusions(
        [
            "volume:orphan_data",
            "path:/root/myapp/logs",
            "database:myapp-db-1/tmp",
            "container:myapp-redis-1",
        ]
    )

    assert exclusions.volumes == frozenset({"orphan_data"})
    assert exclusions.paths == ("/root/myapp/logs",)
    assert exclusions.databases == frozenset({("myapp-db-1", "tmp")})
    assert exclusions.containers == frozenset({"myapp-redis-1"})


def test_a_database_exclusion_splits_on_the_first_slash():
    exclusions = parse_exclusions(["database:myapp-db-1/tmp"])

    assert exclusions.databases == frozenset({("myapp-db-1", "tmp")})


@pytest.mark.parametrize(
    "item",
    ["nonsense", "volume:", "unknown:thing", ""],
)
def test_an_unknown_kind_or_empty_value_names_the_four_kinds(item):
    with pytest.raises(
        BackupError,
        match=r"is not one of volume:<\.\.\.>, path:<\.\.\.>, database:<\.\.\.>, container:<\.\.\.>",
    ):
        parse_exclusions([item])


def test_a_relative_path_is_rejected():
    with pytest.raises(BackupError, match="has to name an absolute path"):
        parse_exclusions(["path:root/myapp/logs"])


def test_a_database_without_a_slash_is_rejected():
    with pytest.raises(BackupError, match=r"has to be database:<container>/<database>"):
        parse_exclusions(["database:myapp-db-1"])


def test_not_a_list_is_rejected():
    with pytest.raises(BackupError, match="backup.exclude has to be a list"):
        parse_exclusions("path:/root")


# --------------------------------------------------------------------------
# Exclusions.path
# --------------------------------------------------------------------------


def test_exclusions_path_matches_something_under_an_excluded_path():
    exclusions = Exclusions(paths=("/root/myapp/logs",))

    assert exclusions.path("/root/myapp/logs/a") is True


def test_exclusions_path_does_not_match_a_sibling_with_a_shared_prefix():
    exclusions = Exclusions(paths=("/root/myapp/logs",))

    assert exclusions.path("/root/myapp/logs2") is False


# --------------------------------------------------------------------------
# under
# --------------------------------------------------------------------------


def test_under_a_child_directory():
    assert under("/a/b", "/a") is True


def test_under_itself():
    assert under("/a", "/a") is True


def test_not_under_a_sibling_with_a_shared_prefix():
    assert under("/ab", "/a") is False


def test_everything_is_under_the_root():
    assert under("/x", "/") is True


# --------------------------------------------------------------------------
# database_kind
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "image,kind",
    [
        ("mysql:5.7", "mysql"),
        ("docker.io/library/mariadb:11", "mysql"),
        ("registry:5000/percona:8", "mysql"),
        ("postgres@sha256:" + "a" * 64, "postgres"),
        ("postgis/postgis:16", "postgres"),
        ("wordpress:latest", ""),
        ("mysqld-exporter", ""),
    ],
)
def test_database_kind(image, kind):
    assert database_kind(image) == kind


# --------------------------------------------------------------------------
# Declared.of
# --------------------------------------------------------------------------


def test_declared_of_collects_a_path_component():
    types = component_types()
    path = types["path"].from_config({"name": "logs", "path": "/root/myapp/logs"})

    declared = Declared.of([path])

    assert declared.names == frozenset({"logs"})
    assert declared.paths == ("/root/myapp/logs",)


def test_declared_of_collects_a_mysql_container():
    types = component_types()
    mysql = types["mysql"].from_config({"name": "db", "container": "myapp-db-1"})

    declared = Declared.of([mysql])

    assert declared.databases == frozenset({"myapp-db-1"})


def test_declared_of_the_old_form_docker_volume_means_every_volume():
    types = component_types()
    volume = types["docker_volume"].from_config({"name": "volumes"})

    declared = Declared.of([volume])

    assert declared.every_volume is True
    assert declared.volumes == frozenset()
    assert declared.mounts == frozenset()
