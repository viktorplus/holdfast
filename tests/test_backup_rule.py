import json

import pytest
from support import (
    ORPHAN_VOLUME,
    WP_VOLUME,
    bind_mount,
    inspect_entry,
    volume_mount,
    wordpress_site,
)

from holdfast.backup import component_types
from holdfast.backup.machine import parse_inspect
from holdfast.backup.model import BackupError
from holdfast.backup.rule import (
    Declared,
    Exclusions,
    Skip,
    database_kind,
    parse_exclusions,
    plan,
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


# --------------------------------------------------------------------------
# plan
# --------------------------------------------------------------------------


def planned(
    entries,
    volumes=(),
    *,
    exclude=(),
    declared=(),
    exists=lambda p: True,
    backup_root="/opt/backups",
):
    return plan(
        parse_inspect(json.dumps(entries)),
        list(volumes),
        exclude=parse_exclusions(list(exclude)),
        declared=Declared.of(declared),
        backup_root=backup_root,
        exists=exists,
    )


def mysql_entry(env):
    return inspect_entry(
        "myapp-db-1",
        "mysql:8",
        mounts=[volume_mount("db_data", "/var/lib/mysql")],
        env=env,
    )


def postgres_entry(env):
    return inspect_entry(
        "pg-1",
        "postgres:16",
        mounts=[volume_mount("pg", "/var/lib/postgresql/data")],
        env=env,
    )


def nginx_with(*mounts):
    return inspect_entry("nginx", "nginx:latest", mounts=list(mounts))


def skip_reason(result, what):
    return [s.reason for s in result.skipped if s.what == what]


def test_a_wordpress_site_gives_its_database_its_project_and_its_volume():
    result = planned(wordpress_site(), [WP_VOLUME, ORPHAN_VOLUME])

    assert result.tables == [
        {
            "type": "mysql",
            "name": "myapp-db-1",
            "container": "myapp-db-1",
            "user": "root",
            "databases": ["*"],
            "credentials": "container_env",
            "password_env": "MYSQL_ROOT_PASSWORD",
        },
        {
            "type": "path",
            "name": "project-myapp",
            "path": "/root/myapp",
            "exclude": ["root/myapp/db_data"],
        },
        {
            "type": "docker_volume",
            "name": "volume-myapp-wordpress-1-var-www-html",
            "container": "myapp-wordpress-1",
            "destination": "/var/www/html",
        },
    ]
    assert Skip(f"volume {ORPHAN_VOLUME}", "no container uses it") in result.skipped
    assert (
        Skip("bind mount /root/myapp/db_data", "database data, covered by the dump")
        in result.skipped
    )


def test_no_password_value_reaches_the_plan():
    result = planned(wordpress_site(), [WP_VOLUME, ORPHAN_VOLUME])

    assert "secret-value" not in repr(result)


def test_mysql_without_a_password_variable_is_an_error_that_names_the_way_out():
    with pytest.raises(BackupError) as caught:
        planned([mysql_entry(["MYSQL_DATABASE=wordpress"])])

    assert str(caught.value) == (
        "container 'myapp-db-1': its root password is not in its environment, "
        "so holdfast cannot reach the database; declare a mysql component for it "
        "in holdfast.toml with defaults_file, or exclude it with "
        '"container:myapp-db-1"'
    )


def test_mysql_allowed_an_empty_password_gets_an_empty_password_env():
    result = planned([mysql_entry(["MYSQL_ALLOW_EMPTY_PASSWORD=yes"])])

    assert result.tables[0]["password_env"] == ""


def test_an_empty_allow_empty_password_means_no():
    with pytest.raises(BackupError, match="container:myapp-db-1"):
        planned([mysql_entry(["MYSQL_ALLOW_EMPTY_PASSWORD="])])


def test_a_password_file_variable_is_used_by_name():
    result = planned([mysql_entry(["MYSQL_ROOT_PASSWORD_FILE=/run/secrets/db"])])

    assert result.tables[0]["password_env"] == "MYSQL_ROOT_PASSWORD_FILE"


def test_the_mariadb_password_variable_wins_over_the_mysql_one():
    result = planned(
        [mysql_entry(["MYSQL_ROOT_PASSWORD=x", "MARIADB_ROOT_PASSWORD=y"])]
    )

    assert result.tables[0]["password_env"] == "MARIADB_ROOT_PASSWORD"


def test_postgres_takes_its_user_and_skips_its_pgdata_volume():
    result = planned(
        [
            postgres_entry(
                ["POSTGRES_USER=app", "PGDATA=/var/lib/postgresql/data/pgdata"]
            )
        ],
        ["pg"],
    )

    assert result.tables == [
        {
            "type": "postgres",
            "name": "pg-1",
            "container": "pg-1",
            "user": "app",
            "databases": ["*"],
            "globals": True,
        }
    ]
    assert Skip("volume pg", "database data, covered by the dump") in result.skipped


def test_postgres_without_a_user_variable_uses_postgres():
    result = planned([postgres_entry([])], ["pg"])

    assert result.tables[0]["user"] == "postgres"


def test_a_named_volume_is_taken_by_name():
    result = planned([nginx_with(volume_mount("uploads", "/uploads"))], ["uploads"])

    assert result.tables == [
        {"type": "docker_volume", "name": "volume-uploads", "volume": "uploads"}
    ]


def test_system_bind_mounts_are_skipped():
    result = planned(
        [
            nginx_with(
                bind_mount("/var/run/docker.sock", "/var/run/docker.sock"),
                bind_mount("/etc/localtime", "/etc/localtime"),
                bind_mount("/proc", "/host/proc"),
            )
        ]
    )

    assert result.tables == []
    for source in ("/var/run/docker.sock", "/etc/localtime", "/proc"):
        assert skip_reason(result, f"bind mount {source}") == ["system"]


def test_a_bind_inside_the_backup_root_is_the_snapshots_themselves():
    result = planned(
        [nginx_with(bind_mount("/opt/backups/x", "/backups"))],
        backup_root="/opt/backups",
    )

    assert result.tables == []
    assert skip_reason(result, "bind mount /opt/backups/x") == [
        "the snapshots themselves"
    ]


def test_a_bind_inside_a_taken_bind_is_not_taken_twice():
    result = planned(
        [
            nginx_with(
                bind_mount("/srv/data/sub", "/sub"),
                bind_mount("/srv/data", "/data"),
            )
        ]
    )

    assert result.tables == [
        {"type": "path", "name": "mount-srv-data", "path": "/srv/data", "exclude": []}
    ]
    assert skip_reason(result, "bind mount /srv/data/sub") == [
        "inside another bind mount already taken"
    ]


def test_an_excluded_volume_is_skipped():
    result = planned(
        [nginx_with(volume_mount("uploads", "/uploads"))],
        ["uploads"],
        exclude=["volume:uploads"],
    )

    assert result.tables == []
    assert skip_reason(result, "volume uploads") == ["excluded by you"]


def test_an_excluded_path_drops_the_project():
    result = planned(wordpress_site(), [WP_VOLUME], exclude=["path:/root/myapp"])

    assert all(t["type"] != "path" for t in result.tables)
    assert skip_reason(result, "compose project myapp (/root/myapp)") == [
        "excluded by you"
    ]


def test_an_excluded_database_goes_into_the_table():
    result = planned(wordpress_site(), [WP_VOLUME], exclude=["database:myapp-db-1/tmp"])

    assert result.tables[0]["exclude_databases"] == ["tmp"]


def test_an_excluded_container_drops_its_volume_and_keeps_the_project():
    result = planned(
        wordpress_site(), [WP_VOLUME], exclude=["container:myapp-wordpress-1"]
    )

    assert [t["name"] for t in result.tables] == ["myapp-db-1", "project-myapp"]
    assert skip_reason(result, f"volume {WP_VOLUME}") == ["excluded by you"]


def test_a_project_declared_by_hand_is_not_added_again():
    path = component_types()["path"].from_config(
        {"name": "site", "path": "/root/myapp"}
    )

    result = planned(wordpress_site(), [WP_VOLUME], declared=[path])

    assert all(t["type"] != "path" for t in result.tables)
    assert skip_reason(result, "compose project myapp (/root/myapp)") == [
        "declared by hand"
    ]


def test_a_database_declared_by_hand_is_not_added_again():
    mysql = component_types()["mysql"].from_config(
        {"name": "db", "container": "myapp-db-1"}
    )

    result = planned(wordpress_site(), [WP_VOLUME], declared=[mysql])

    assert all(t["type"] != "mysql" for t in result.tables)
    assert skip_reason(result, "database container myapp-db-1") == ["declared by hand"]


def test_a_project_directory_missing_here_is_skipped():
    result = planned(wordpress_site(), [WP_VOLUME], exists=lambda p: False)

    assert all(t["type"] != "path" for t in result.tables)
    assert skip_reason(result, "compose project myapp (/root/myapp)") == [
        "the project directory is not on this machine"
    ]


def test_a_name_taken_by_hand_gets_a_suffix():
    other = component_types()["path"].from_config(
        {"name": "project-myapp", "path": "/srv/other"}
    )

    result = planned(wordpress_site(), [WP_VOLUME], declared=[other])

    assert [t["name"] for t in result.tables if t["type"] == "path"] == [
        "project-myapp-2"
    ]


def test_measure_lists_what_each_table_will_read():
    result = planned(wordpress_site(), [WP_VOLUME, ORPHAN_VOLUME])

    assert result.measure == [
        ("myapp-db-1", "path", "/root/myapp/db_data"),
        ("project-myapp", "path", "/root/myapp"),
        ("volume-myapp-wordpress-1-var-www-html", "volume", WP_VOLUME),
    ]


def test_a_stopped_container_still_has_its_volume_taken():
    stopped = inspect_entry(
        "nginx",
        "nginx:latest",
        running=False,
        mounts=[volume_mount("uploads", "/uploads")],
    )

    result = planned([stopped], ["uploads"])

    assert result.tables == [
        {"type": "docker_volume", "name": "volume-uploads", "volume": "uploads"}
    ]
