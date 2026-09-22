import shlex

import pytest
from support import needs_sh

from holdfast.backup import BackupError, component_types
from holdfast.backup.engine import run_line
from holdfast.backup.model import BuildContext

CTX = BuildContext(zstd_level=10, zstd_threads=0)


def build(type_name: str, **table):
    return component_types()[type_name].from_config({"type": type_name, **table})


def only(component):
    artifacts = component.artifacts(CTX)
    assert len(artifacts) == 1
    return artifacts[0]


def test_every_implemented_type_is_registered():
    assert set(component_types()) == {
        "path",
        "command",
        "postgres",
        "mysql",
        "docker_volume",
    }


# --------------------------------------------------------------------------
# path
# --------------------------------------------------------------------------


def test_a_path_component_needs_a_path():
    with pytest.raises(BackupError, match="path"):
        build("path", name="config")


def test_a_relative_path_is_refused():
    """What it is relative to would depend on who started the backup."""
    with pytest.raises(BackupError, match="absolute"):
        build("path", name="config", path="opt/example")


def test_the_artifact_is_named_after_the_component():
    artifact = only(build("path", name="mail-config", path="/opt/example"))

    assert artifact.name == "mail-config.tar.zst"


def test_the_producer_archives_from_the_root_and_compresses():
    artifact = only(build("path", name="config", path="/opt/example"))

    assert "tar" in artifact.produce
    assert "-C /" in artifact.produce
    assert "opt/example" in artifact.produce
    assert "zstd -T0 -10 -q" in artifact.produce


def test_a_live_server_changing_files_does_not_fail_the_archive():
    """Both flags are carried over on purpose: without them any busy log file
    ends the backup."""
    artifact = only(build("path", name="config", path="/opt/example"))

    assert "--warning=no-file-changed" in artifact.produce
    assert "--ignore-failed-read" in artifact.produce


@needs_sh
def test_a_path_that_is_not_there_fails_instead_of_archiving_nothing():
    """--ignore-failed-read also forgives the path itself being missing: tar
    exits 0 with an empty archive, and a renamed or unmounted directory became
    an empty artifact every night. zstd is swapped for cat so that this runs
    wherever tar does."""
    artifact = only(build("path", name="config", path="/opt/holdfast-not-here"))

    assert run_line(artifact.produce.replace("zstd -T0 -10 -q", "cat")) != 0


def test_every_exclusion_reaches_tar_as_its_own_flag():
    """Quoted, so that the shell hands the pattern to tar instead of expanding
    it against the directory the backup happens to start in."""
    artifact = only(
        build(
            "path",
            name="config",
            path="/opt/example",
            exclude=["*/logs/*", "*.sock"],
        )
    )

    assert f"--exclude={shlex.quote('*/logs/*')}" in artifact.produce
    assert f"--exclude={shlex.quote('*.sock')}" in artifact.produce


def test_a_path_with_a_quote_in_it_cannot_break_the_line():
    artifact = only(build("path", name="config", path="/opt/it's here"))

    assert shlex.quote("opt/it's here") in artifact.produce
    assert artifact.produce.count("|") == 1


def test_the_recipe_says_where_it_goes_back():
    artifact = only(build("path", name="config", path="/opt/example"))

    assert artifact.recipe == {"type": "path", "target": "/"}


def test_the_check_reads_the_archive_rather_than_trusting_the_checksum():
    artifact = only(build("path", name="config", path="/opt/example"))

    assert "zstd -dc" in artifact.check
    assert "tar -tf -" in artifact.check


def test_the_manifest_records_what_was_taken():
    component = build("path", name="config", path="/opt/example", exclude=["*.sock"])

    assert component.describe() == {
        "type": "path",
        "name": "config",
        "path": "/opt/example",
        "exclude": ["*.sock"],
    }


# --------------------------------------------------------------------------
# command
# --------------------------------------------------------------------------


def test_a_command_component_needs_a_producer():
    with pytest.raises(BackupError, match="produce"):
        build("command", name="whatever")


def test_the_operators_command_is_used_exactly_as_written():
    """The seam for everything the five types do not cover.

    Wrapping it in holdfast's own compression would make the manifest describe
    something other than what was stored.
    """
    artifact = only(build("command", name="whatever", produce="dump-something --all"))

    assert artifact.produce == "dump-something --all"


def test_the_artifact_name_defaults_and_can_be_given():
    default = only(build("command", name="whatever", produce="x"))
    named = only(
        build("command", name="whatever", produce="x", artifact="whatever.sql.gz")
    )

    assert default.name == "whatever.bin"
    assert named.name == "whatever.sql.gz"


def test_without_a_declared_recipe_nothing_claims_to_know_how_to_restore_it():
    artifact = only(build("command", name="whatever", produce="x"))

    assert artifact.recipe["type"] == "none"


def test_a_declared_recipe_is_carried_through_untouched():
    """Stage 3C reads this. Stage 3A does not interpret it."""
    artifact = only(
        build(
            "command",
            name="whatever",
            produce="x",
            restore={"type": "path", "target": "/srv"},
        )
    )

    assert artifact.recipe == {"type": "path", "target": "/srv"}


def test_the_default_check_only_claims_the_bytes_were_readable():
    artifact = only(build("command", name="whatever", produce="x"))

    assert artifact.check == "cat >/dev/null"


def test_a_declared_check_replaces_it():
    artifact = only(build("command", name="whatever", produce="x", check="gzip -t"))

    assert artifact.check == "gzip -t"


def test_the_manifest_does_not_repeat_the_operators_command():
    """It can hold a path and the name of an internal tool, and the manifest
    sits in the snapshot in plaintext."""
    component = build(
        "command", name="whatever", produce="/usr/local/bin/dump-something --all"
    )

    assert "produce" not in component.describe()
    assert component.describe() == {
        "type": "command",
        "name": "whatever",
        "artifact": "whatever.bin",
    }


# --------------------------------------------------------------------------
# a stand-in for the machine
# --------------------------------------------------------------------------


class FakeProbe:
    """Answers the questions a component asks, without a machine to ask."""

    def __init__(
        self,
        output: str = "",
        running=(),
        volumes=(),
        mountpoints=None,
        sizes=None,
        fails: str | None = None,
    ):
        self.output = output
        self.running = set(running)
        self._volumes = list(volumes)
        self.mountpoints = mountpoints or {}
        self.sizes = sizes or {}
        self.fails = fails
        self.asked: list[list[str]] = []

    def capture(self, argv, *, what: str, timeout: int = 60, env=None) -> str:
        self.asked.append(list(argv))
        if self.fails:
            raise BackupError(f"{what}: {self.fails}")
        return self.output

    def container_running(self, name: str) -> bool:
        self.asked.append(["container_running", name])
        return name in self.running

    def volumes(self) -> list[str]:
        return list(self._volumes)

    def volume_mountpoint(self, name: str) -> str:
        return self.mountpoints[name]

    def directory_size_mb(self, path: str) -> int:
        return self.sizes.get(path, 1)


def ctx_with(probe: FakeProbe) -> BuildContext:
    return BuildContext(zstd_level=10, zstd_threads=0, probe=probe)


# --------------------------------------------------------------------------
# postgres
# --------------------------------------------------------------------------


def a_postgres(**table):
    return build("postgres", name="db", user="signal", **table)


def test_a_postgres_component_needs_a_user():
    with pytest.raises(BackupError, match="user"):
        build("postgres", name="db")


def test_postgres_without_a_container_runs_the_tools_directly():
    """Docker is optional; a machine without it is served in full."""
    probe = FakeProbe(output="app")
    artifacts = a_postgres(globals=False).artifacts(ctx_with(probe))

    assert "docker" not in artifacts[0].produce
    assert artifacts[0].produce.startswith("pg_dump")
    assert ["container_running", "db"] not in probe.asked


def test_a_declared_container_that_is_not_running_stops_the_backup():
    """The place the old script went quiet: it returned success with no dump."""
    probe = FakeProbe(output="app", running=())
    with pytest.raises(BackupError, match="signal-postgres"):
        a_postgres(container="signal-postgres").artifacts(ctx_with(probe))


def test_the_database_list_is_asked_for_with_the_query_that_skips_templates():
    probe = FakeProbe(output="app\nmetrics", running={"c"})
    a_postgres(container="c").artifacts(ctx_with(probe))
    asked = " ".join(probe.asked[-1])

    assert "datistemplate = false" in asked
    assert "psql" in asked


def test_an_explicit_list_of_databases_asks_the_machine_nothing():
    probe = FakeProbe(output="should not be used")
    artifacts = a_postgres(databases=["app"], globals=False).artifacts(ctx_with(probe))

    assert probe.asked == []
    assert len(artifacts) == 1


def test_a_running_postgres_with_no_databases_stops_the_backup():
    """Not "nothing to do". A psql that failed on authentication used to give
    an empty list, zero dumps, and a night that reported success - which is the
    comment the source carries above its own fix."""
    probe = FakeProbe(output="", running={"c"})
    with pytest.raises(BackupError, match="no databases"):
        a_postgres(container="c").artifacts(ctx_with(probe))


@pytest.mark.parametrize("name", ["a/b", "a b", "a;b", "-"])
def test_a_database_name_that_would_not_survive_the_manifest_is_refused(name: str):
    """A slash writes the artifact outside its directory; a control character
    breaks the manifest outright. Better to find out with the database named
    than during a recovery."""
    probe = FakeProbe(output=name, running={"c"})
    with pytest.raises(BackupError):
        a_postgres(container="c").artifacts(ctx_with(probe))


def test_a_name_declared_by_hand_is_checked_too():
    """psql -At separates names by newline, so one cannot arrive that way. The
    explicit list is the channel where an impossible name is possible."""
    probe = FakeProbe()
    with pytest.raises(BackupError):
        a_postgres(databases=["a" + chr(10) + "b"], globals=False).artifacts(
            ctx_with(probe)
        )


def test_globals_come_first_and_go_through_zstd():
    probe = FakeProbe(output="app", running={"c"})
    artifacts = a_postgres(container="c").artifacts(ctx_with(probe))

    assert artifacts[0].name == "postgres/db-globals.sql.zst"
    assert "pg_dumpall" in artifacts[0].produce
    assert "--globals-only" in artifacts[0].produce
    assert "zstd -T0 -10 -q" in artifacts[0].produce
    assert artifacts[0].recipe["type"] == "pg_globals"


def test_globals_can_be_turned_off():
    probe = FakeProbe(output="app", running={"c"})
    artifacts = a_postgres(container="c", globals=False).artifacts(ctx_with(probe))

    assert [a.name for a in artifacts] == ["postgres/db-app.dump"]


def test_each_database_becomes_its_own_dump():
    probe = FakeProbe(output="app\nmetrics", running={"c"})
    artifacts = a_postgres(container="c", globals=False).artifacts(ctx_with(probe))

    assert [a.name for a in artifacts] == [
        "postgres/db-app.dump",
        "postgres/db-metrics.dump",
    ]
    assert artifacts[0].recipe == {
        "type": "pg_database",
        "container": "c",
        "user": "signal",
        "database": "app",
    }


def test_a_custom_format_dump_is_not_compressed_twice():
    """pg_dump -Fc is already compressed; stacking zstd on it buys nothing."""
    probe = FakeProbe(output="app", running={"c"})
    artifacts = a_postgres(container="c", globals=False).artifacts(ctx_with(probe))

    assert "-Fc" in artifacts[0].produce
    assert "zstd" not in artifacts[0].produce


def test_the_dump_check_drains_the_stream_after_reading_the_signature():
    """Without the drain the decrypter upstream dies of SIGPIPE, and a whole
    healthy snapshot is reported as corrupt."""
    probe = FakeProbe(output="app", running={"c"})
    artifacts = a_postgres(container="c", globals=False).artifacts(ctx_with(probe))

    assert "PGDMP" in artifacts[0].check
    assert "cat >/dev/null" in artifacts[0].check


def test_the_password_file_is_named_but_never_read_by_holdfast():
    """libpq reads it. holdfast only ever holds the path, which is not a
    secret, so it can live in the TOML without breaking the rule in section 6.
    """
    probe = FakeProbe(output="app", running={"c"})
    component = a_postgres(
        container="c", globals=False, defaults_file="/etc/holdfast/pgpass"
    )
    artifacts = component.artifacts(ctx_with(probe))

    assert "PGPASSFILE=/etc/holdfast/pgpass" in artifacts[0].produce
    assert component.describe()["defaults_file"] == "/etc/holdfast/pgpass"


def test_a_postgres_component_owns_its_container():
    """Read by the restore, which stops it before loading a dump into it."""
    assert a_postgres(container="c").containers() == ["c"]
    assert a_postgres().containers() == []


def test_the_manifest_describes_the_postgres_declaration():
    component = a_postgres(container="c", databases=["app"], globals=False)

    assert component.describe() == {
        "type": "postgres",
        "name": "db",
        "container": "c",
        "user": "signal",
        "databases": ["app"],
        "globals": False,
        "defaults_file": "",
    }


# --------------------------------------------------------------------------
# mysql
# --------------------------------------------------------------------------


def a_mysql(**table):
    return build("mysql", name="shop", **table)


def test_mysql_without_a_container_runs_the_tools_directly():
    probe = FakeProbe(output="shopdb")
    artifacts = a_mysql().artifacts(ctx_with(probe))

    assert artifacts[0].produce.startswith("mysqldump")


def test_a_declared_mysql_container_that_is_down_stops_the_backup():
    probe = FakeProbe(output="shopdb", running=())
    with pytest.raises(BackupError, match="shop-mysql"):
        a_mysql(container="shop-mysql").artifacts(ctx_with(probe))


def test_the_database_list_is_asked_for_in_a_parseable_form():
    probe = FakeProbe(output="shopdb", running={"c"})
    a_mysql(container="c").artifacts(ctx_with(probe))
    asked = probe.asked[-1]

    assert "show databases" in " ".join(asked)
    assert "-N" in asked and "-B" in asked


def test_the_server_own_schemas_are_not_dumped():
    """Their contents belong to the server that is being restored into, and
    replaying them over a running one is how a restore breaks the server."""
    probe = FakeProbe(
        output="information_schema\nperformance_schema\nsys\nshopdb", running={"c"}
    )
    artifacts = a_mysql(container="c").artifacts(ctx_with(probe))

    assert [a.name for a in artifacts] == ["mysql/shop-shopdb.sql.zst"]


def test_a_server_with_only_its_own_schemas_stops_the_backup():
    probe = FakeProbe(output="information_schema\nsys", running={"c"})
    with pytest.raises(BackupError, match="no databases"):
        a_mysql(container="c").artifacts(ctx_with(probe))


@pytest.mark.parametrize("name", ["a/b", "a b", "a;b"])
def test_a_mysql_database_name_that_would_not_survive_is_refused(name: str):
    probe = FakeProbe(output=name, running={"c"})
    with pytest.raises(BackupError):
        a_mysql(container="c").artifacts(ctx_with(probe))


def test_a_consistent_snapshot_is_taken_without_locking_the_site():
    """--single-transaction takes an InnoDB snapshot instead of locking the
    tables a live site is reading. --quick streams rows instead of collecting a
    whole table in memory first, which is what kills the backup on the one
    table that matters."""
    probe = FakeProbe(output="shopdb", running={"c"})
    produce = a_mysql(container="c").artifacts(ctx_with(probe))[0].produce

    assert "--single-transaction" in produce
    assert "--quick" in produce
    assert "--routines" in produce
    assert "--events" in produce
    assert "zstd -T0 -10 -q" in produce


def test_the_credentials_file_comes_first_on_the_command_line():
    """mysql and mysqldump accept --defaults-file only as the first argument.
    Anywhere else it is ignored, and the failure reads as a wrong password."""
    probe = FakeProbe(output="shopdb")
    component = a_mysql(defaults_file="/etc/holdfast/mysql.cnf")
    produce = component.artifacts(ctx_with(probe))[0].produce

    assert produce.startswith("mysqldump --defaults-file=/etc/holdfast/mysql.cnf ")
    assert probe.asked[-1][1] == "--defaults-file=/etc/holdfast/mysql.cnf"


def test_the_mysql_recipe_and_check_say_what_they_know():
    probe = FakeProbe(output="shopdb", running={"c"})
    artifact = a_mysql(container="c").artifacts(ctx_with(probe))[0]

    assert artifact.recipe == {
        "type": "mysql_database",
        "container": "c",
        "database": "shopdb",
        "defaults_file": "",
    }
    assert artifact.check == "zstd -dc >/dev/null"


def test_a_mysql_component_owns_its_container():
    assert a_mysql(container="c").containers() == ["c"]


def test_the_manifest_describes_the_mysql_declaration_and_no_password():
    component = a_mysql(container="c", user="backup", databases=["shopdb"])

    assert component.describe() == {
        "type": "mysql",
        "name": "shop",
        "container": "c",
        "user": "backup",
        "databases": ["shopdb"],
        "defaults_file": "",
    }


# --------------------------------------------------------------------------
# docker_volume
# --------------------------------------------------------------------------


def volumes_on(tmp_path, *names, sizes=None):
    """A probe whose volumes are real directories, because the type checks."""
    mountpoints = {}
    for name in names:
        directory = tmp_path / name
        directory.mkdir(parents=True, exist_ok=True)
        mountpoints[name] = str(directory)
    return FakeProbe(
        volumes=names,
        mountpoints=mountpoints,
        sizes={mountpoints[n]: (sizes or {}).get(n, 1) for n in names},
    )


def test_one_volume_component_produces_one_artifact_per_volume(tmp_path):
    probe = volumes_on(tmp_path, "app_data", "cache")
    artifacts = build("docker_volume", name="volumes").artifacts(ctx_with(probe))

    assert [a.name for a in artifacts] == [
        "docker-volumes/app_data.tar.zst",
        "docker-volumes/cache.tar.zst",
    ]


def test_an_excluded_volume_is_not_taken(tmp_path):
    probe = volumes_on(tmp_path, "app_data", "pg_data")
    component = build("docker_volume", name="volumes", exclude=["pg_data"])

    assert [a.name for a in component.artifacts(ctx_with(probe))] == [
        "docker-volumes/app_data.tar.zst"
    ]


def test_a_volume_over_the_limit_is_left_and_is_not_a_failure(tmp_path):
    """The limit is the operator's own rule, not something going wrong."""
    probe = volumes_on(tmp_path, "small", "huge", sizes={"small": 10, "huge": 900})
    component = build("docker_volume", name="volumes", max_mb=512)

    assert [a.name for a in component.artifacts(ctx_with(probe))] == [
        "docker-volumes/small.tar.zst"
    ]


def test_a_volume_exactly_at_the_limit_is_taken(tmp_path):
    probe = volumes_on(tmp_path, "edge", sizes={"edge": 512})
    component = build("docker_volume", name="volumes", max_mb=512)

    assert len(component.artifacts(ctx_with(probe))) == 1


def test_every_volume_being_too_big_is_still_not_a_failure(tmp_path):
    """The one case where taking nothing is legitimate: the list was answered,
    and everything on it was ruled out by a rule the operator wrote."""
    probe = volumes_on(tmp_path, "huge", sizes={"huge": 9000})
    component = build("docker_volume", name="volumes", max_mb=512)

    assert component.artifacts(ctx_with(probe)) == []


def test_a_volume_whose_storage_cannot_be_read_stops_the_backup(tmp_path):
    probe = FakeProbe(
        volumes=["app_data"], mountpoints={"app_data": str(tmp_path / "gone")}
    )
    with pytest.raises(BackupError) as caught:
        build("docker_volume", name="volumes").artifacts(ctx_with(probe))

    assert "app_data" in str(caught.value)
    assert "rootless" in str(caught.value)
    assert "exclude" in str(caught.value)


def test_a_volume_name_that_would_not_survive_is_refused(tmp_path):
    probe = FakeProbe(volumes=["../escape"], mountpoints={"../escape": str(tmp_path)})
    with pytest.raises(BackupError):
        build("docker_volume", name="volumes").artifacts(ctx_with(probe))


def test_the_volume_archive_is_taken_from_where_docker_says_it_is(tmp_path):
    probe = volumes_on(tmp_path, "app_data")
    artifact = build("docker_volume", name="volumes").artifacts(ctx_with(probe))[0]
    where = shlex.quote(str(tmp_path / "app_data"))

    assert f"-C {where} ." in artifact.produce
    assert "zstd -T0 -10 -q" in artifact.produce
    assert artifact.recipe == {"type": "docker_volume", "volume": "app_data"}
    assert artifact.check == "zstd -dc | tar -tf - >/dev/null"


def test_the_manifest_records_the_rule_that_decided_what_was_taken(tmp_path):
    component = build("docker_volume", name="volumes", exclude=["pg_data"], max_mb=256)

    assert component.describe() == {
        "type": "docker_volume",
        "name": "volumes",
        "exclude": ["pg_data"],
        "max_mb": 256,
    }
