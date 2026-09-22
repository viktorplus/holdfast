from pathlib import Path

import pytest
from support import PATH_RECIPE, Runs, SnapshotProbe, artifact, snapshot_dir

from holdfast import jobs
from holdfast.backup.decrypt import open_identity
from holdfast.backup.model import RestoreError
from holdfast.backup.restore import restore
from holdfast.backup.snapshot import load_snapshot

PG_DATABASE = {
    "type": "pg_database",
    "container": "db",
    "user": "u",
    "database": "app",
}
PG_GLOBALS = {"type": "pg_globals", "container": "db", "user": "u"}


def restoring(directory: Path, tmp_path: Path, **over):
    settings = {
        "root": "/",
        "component": None,
        "dry_run": False,
        "assume_yes": True,
        "stop": True,
        "from_other_host": False,
        "this_label": "web-1",
        "jobs_dir": tmp_path / "jobs",
        "probe": SnapshotProbe(),
        "run": Runs(),
        "say": lambda line: None,
        "ask": None,
    }
    settings.update(over)
    with open_identity(None) as identity:
        return restore(load_snapshot(directory), identity, **settings)


# -- before anything is touched --------------------------------------------


def test_a_snapshot_from_another_machine_is_refused_by_name(tmp_path: Path):
    """The expensive mistake, and the one a typed word nobody reads does
    nothing about."""
    directory = snapshot_dir(tmp_path, artifact("a.bin", PATH_RECIPE))
    runs = Runs()

    with pytest.raises(RestoreError) as caught:
        restoring(directory, tmp_path, this_label="db-2", run=runs)

    assert "web-1" in str(caught.value)
    assert "db-2" in str(caught.value)
    assert "--from-other-host" in str(caught.value)
    assert runs.lines == []


def test_a_machine_with_no_label_cannot_confirm_where_it_is(tmp_path: Path):
    directory = snapshot_dir(tmp_path, artifact("a.bin", PATH_RECIPE))

    with pytest.raises(RestoreError, match="--from-other-host"):
        restoring(directory, tmp_path, this_label="")


def test_saying_so_allows_it(tmp_path: Path):
    directory = snapshot_dir(tmp_path, artifact("a.bin", PATH_RECIPE))
    result = restoring(directory, tmp_path, this_label="db-2", from_other_host=True)

    assert result.done


def test_the_confirmation_asks_for_the_label_and_refuses_anything_else(
    tmp_path: Path,
):
    directory = snapshot_dir(tmp_path, artifact("a.bin", PATH_RECIPE))
    runs = Runs()
    asked: list[str] = []

    with pytest.raises(RestoreError, match="nothing was restored"):
        restoring(
            directory,
            tmp_path,
            assume_yes=False,
            ask=lambda prompt: (asked.append(prompt), "yes")[1],
            run=runs,
        )

    assert "web-1" in asked[0]
    assert runs.lines == []


def test_the_right_word_lets_it_through(tmp_path: Path):
    directory = snapshot_dir(tmp_path, artifact("a.bin", PATH_RECIPE))
    result = restoring(
        directory, tmp_path, assume_yes=False, ask=lambda prompt: "web-1"
    )

    assert result.done


def test_the_plan_is_printed_before_the_question_is_asked(tmp_path: Path):
    directory = snapshot_dir(tmp_path, artifact("a.bin", PATH_RECIPE))
    order: list[str] = []

    restoring(
        directory,
        tmp_path,
        assume_yes=False,
        say=lambda line: order.append("said"),
        ask=lambda prompt: (order.append("asked"), "web-1")[1],
    )

    assert order.index("said") < order.index("asked")


def test_a_dry_run_changes_nothing_and_asks_nothing(tmp_path: Path):
    directory = snapshot_dir(tmp_path, artifact("a.bin", PATH_RECIPE))
    runs, probe = Runs(), SnapshotProbe()

    result = restoring(
        directory, tmp_path, dry_run=True, assume_yes=False, run=runs, probe=probe
    )

    assert runs.lines == []
    assert probe.ran == []
    assert result.done


def test_a_dry_run_does_not_write_the_journal(tmp_path: Path):
    """A rehearsal restored nothing, so a journal entry recording it as a
    success would be a lie - and it is exactly the lie ``restore_tested``
    would be fooled by."""
    directory = snapshot_dir(tmp_path, artifact("a.bin", PATH_RECIPE))
    restoring(directory, tmp_path, dry_run=True)

    assert jobs.last(tmp_path / "jobs", "restore") is None


def test_a_dry_run_does_not_erase_the_memory_of_a_real_restore(tmp_path: Path):
    directory = snapshot_dir(tmp_path, artifact("a.bin", PATH_RECIPE))
    restoring(directory, tmp_path)
    real_success = jobs.last(tmp_path / "jobs", "restore")["last_success"]

    restoring(directory, tmp_path, dry_run=True)

    assert jobs.last(tmp_path / "jobs", "restore")["last_success"] == real_success


def test_a_corrupt_artifact_stops_it_before_anything_is_stopped(tmp_path: Path):
    directory = snapshot_dir(tmp_path, artifact("a.bin", PATH_RECIPE))
    (directory / "a.bin").write_bytes(b"a bodY")
    probe = SnapshotProbe()

    with pytest.raises(RestoreError, match="checksum"):
        restoring(directory, tmp_path, probe=probe)

    assert probe.ran == []


def test_artifacts_nobody_can_put_back_are_named(tmp_path: Path):
    directory = snapshot_dir(
        tmp_path,
        artifact("a.bin", PATH_RECIPE, component="config"),
        artifact("notes.bin", {"type": "none"}, component="notes"),
    )
    said: list[str] = []
    result = restoring(directory, tmp_path, say=said.append)

    assert [r.path for r in result.skipped] == ["notes.bin"]
    assert any("notes.bin" in line for line in said)


def test_narrowing_to_one_component_still_checks_every_recipe(tmp_path: Path):
    directory = snapshot_dir(
        tmp_path,
        artifact("a.bin", PATH_RECIPE, component="config"),
        artifact("b.bin", {"type": "quantum"}, component="odd"),
    )

    with pytest.raises(RestoreError, match="quantum"):
        restoring(directory, tmp_path, component="config")


def test_narrowing_to_one_component_restores_only_it(tmp_path: Path):
    directory = snapshot_dir(
        tmp_path,
        artifact("a.bin", PATH_RECIPE, component="config"),
        artifact("b.bin", PATH_RECIPE, component="uploads"),
    )
    runs = Runs()
    restoring(directory, tmp_path, component="config", run=runs)

    assert len(runs.lines) == 1
    assert "a.bin" in runs.lines[0]


# -- the order that cannot be rearranged -----------------------------------


def test_the_phases_run_in_the_order_that_cannot_be_rearranged(tmp_path: Path):
    """Files and volumes while the services are down, then the services, then
    the dumps that need them running."""
    directory = snapshot_dir(
        tmp_path,
        artifact("db.dump", PG_DATABASE, component="db"),
        artifact("files.tar.zst", PATH_RECIPE, component="config"),
        artifact("g.sql.zst", PG_GLOBALS, component="db"),
    )
    probe, runs = SnapshotProbe(), Runs()
    restoring(directory, tmp_path, probe=probe, run=runs)

    files = next(i for i, line in enumerate(runs.lines) if "files.tar.zst" in line)
    globals_at = next(i for i, line in enumerate(runs.lines) if "g.sql.zst" in line)
    database = next(i for i, line in enumerate(runs.lines) if "db.dump" in line)
    assert files < globals_at < database

    stops = [i for i, argv in enumerate(probe.ran) if argv[:2] == ["docker", "stop"]]
    starts = [i for i, argv in enumerate(probe.ran) if argv[:2] == ["docker", "start"]]
    assert stops and starts and stops[0] < starts[0]


def test_globals_go_in_before_the_databases_their_roles_own(tmp_path: Path):
    """Even when the manifest lists them the other way round: a database
    restored before its roles exist belongs to nobody."""
    directory = snapshot_dir(
        tmp_path,
        artifact("db.dump", PG_DATABASE, component="db"),
        artifact("g.sql.zst", PG_GLOBALS, component="db"),
    )
    runs = Runs()
    restoring(directory, tmp_path, run=runs)

    globals_at = next(i for i, line in enumerate(runs.lines) if "g.sql.zst" in line)
    database = next(i for i, line in enumerate(runs.lines) if "db.dump" in line)
    assert globals_at < database


def test_a_failure_puts_the_services_back_and_says_what_state_this_is(
    tmp_path: Path,
):
    directory = snapshot_dir(
        tmp_path,
        artifact("files.tar.zst", PATH_RECIPE, component="config"),
        artifact("g.sql.zst", PG_GLOBALS, component="db"),
    )
    probe = SnapshotProbe()

    with pytest.raises(RestoreError) as caught:
        restoring(directory, tmp_path, probe=probe, run=Runs(fail_on="files.tar.zst"))

    assert "PARTIALLY RESTORED" in str(caught.value)
    assert "again" in str(caught.value)
    assert ["docker", "start", "db"] in probe.ran


def test_an_error_from_the_machine_still_puts_the_services_back(tmp_path: Path):
    """Not every failure is a RestoreError: docker refusing to create a volume
    raised past the handler and left the containers stopped."""
    directory = snapshot_dir(
        tmp_path,
        artifact("v.tar.zst", {"type": "docker_volume", "volume": "uploads"}),
    )
    probe = SnapshotProbe(users={"uploads": ["app"]}, refuse={"uploads"})

    with pytest.raises(RestoreError, match="PARTIALLY RESTORED"):
        restoring(directory, tmp_path, probe=probe)

    assert ["docker", "start", "app"] in probe.ran
    assert jobs.last(tmp_path / "jobs", "restore")["last_run"]["ok"] is False


def test_an_interrupt_still_puts_the_services_back(tmp_path: Path):
    directory = snapshot_dir(
        tmp_path,
        artifact("files.tar.zst", PATH_RECIPE, component="config"),
        artifact("g.sql.zst", PG_GLOBALS, component="db"),
    )
    probe = SnapshotProbe()

    def interrupted(line: str) -> int:
        raise KeyboardInterrupt

    with pytest.raises(RestoreError, match="interrupted"):
        restoring(directory, tmp_path, probe=probe, run=interrupted)

    assert ["docker", "start", "db"] in probe.ran


def test_a_failure_is_written_into_the_journal(tmp_path: Path):
    directory = snapshot_dir(tmp_path, artifact("a.bin", PATH_RECIPE))

    with pytest.raises(RestoreError):
        restoring(directory, tmp_path, run=Runs(fail_on="a.bin"))

    assert jobs.last(tmp_path / "jobs", "restore")["last_run"]["ok"] is False


def test_a_finished_restore_is_written_into_the_journal(tmp_path: Path):
    directory = snapshot_dir(tmp_path, artifact("a.bin", PATH_RECIPE))
    restoring(directory, tmp_path)
    entry = jobs.last(tmp_path / "jobs", "restore")["last_run"]

    assert entry["ok"] is True
    assert entry["snapshot"] == "20260920-231500"
    assert entry["artifacts"] == 1


# -- the recipes -----------------------------------------------------------


def test_a_rehearsal_root_rebases_every_path(tmp_path: Path):
    """Which is what makes a restore into a scratch directory possible."""
    directory = snapshot_dir(
        tmp_path, artifact("a.bin", {"type": "path", "target": "/opt/example"})
    )
    runs = Runs()
    restoring(directory, tmp_path, root="/tmp/drill", run=runs)

    assert "/tmp/drill/opt/example" in runs.lines[0]


def test_a_rehearsal_root_refuses_what_it_cannot_rebase(tmp_path: Path):
    """--root moves files. Volumes and databases have no scratch copy to go
    into, so a rehearsal went on to overwrite the live ones."""
    directory = snapshot_dir(
        tmp_path,
        artifact("files.tar.zst", PATH_RECIPE, component="config"),
        artifact("g.sql.zst", PG_GLOBALS, component="db"),
    )
    probe = SnapshotProbe()
    runs = Runs()

    with pytest.raises(RestoreError, match="--root"):
        restoring(directory, tmp_path, root="/tmp/drill", probe=probe, run=runs)

    assert runs.lines == []
    assert probe.ran == []


def test_a_rehearsal_root_is_allowed_for_the_files_alone(tmp_path: Path):
    directory = snapshot_dir(
        tmp_path,
        artifact("files.tar.zst", PATH_RECIPE, component="config"),
        artifact("g.sql.zst", PG_GLOBALS, component="db"),
    )
    runs = Runs()
    restoring(directory, tmp_path, root="/tmp/drill", component="config", run=runs)

    assert len(runs.lines) == 1
    assert "/tmp/drill" in runs.lines[0]


def test_a_target_of_root_rebases_too(tmp_path: Path):
    directory = snapshot_dir(tmp_path, artifact("a.bin", PATH_RECIPE))
    runs = Runs()
    restoring(directory, tmp_path, root="/tmp/drill", run=runs)

    assert "/tmp/drill" in runs.lines[0]
    assert " -C / " not in runs.lines[0]


def test_a_volume_is_emptied_and_never_merged_into(tmp_path: Path):
    """Unpacking on top of leftovers is a merge, not a restore."""
    mount = tmp_path / "mount"
    mount.mkdir()
    directory = snapshot_dir(
        tmp_path,
        artifact("v.tar.zst", {"type": "docker_volume", "volume": "uploads"}),
    )
    probe = SnapshotProbe(mountpoints={"uploads": str(mount)})
    runs = Runs()
    restoring(directory, tmp_path, probe=probe, run=runs)

    assert ["docker", "volume", "create", "uploads"] in probe.ran
    together = " ".join(runs.lines)
    assert "rm -rf" in together
    assert "refusing" in together
    assert "tar -xf -" in together


def test_a_volume_whose_storage_cannot_be_read_is_refused(tmp_path: Path):
    directory = snapshot_dir(
        tmp_path,
        artifact("v.tar.zst", {"type": "docker_volume", "volume": "uploads"}),
    )
    probe = SnapshotProbe(mountpoints={"uploads": str(tmp_path / "gone")})

    with pytest.raises(RestoreError, match="uploads"):
        restoring(directory, tmp_path, probe=probe)


def test_a_database_is_waited_for_and_created_before_the_dump_goes_in(
    tmp_path: Path,
):
    directory = snapshot_dir(tmp_path, artifact("db.dump", PG_DATABASE))
    runs = Runs()
    restoring(directory, tmp_path, run=runs)
    together = " ".join(runs.lines)

    assert "pg_isready" in together
    assert "create database" in together
    assert "--clean --if-exists --no-owner" in together


def test_a_custom_format_dump_is_not_decompressed_on_the_way_back(tmp_path: Path):
    """pg_dump -Fc carries its own compression, so nothing unwraps it twice."""
    directory = snapshot_dir(tmp_path, artifact("db.dump", PG_DATABASE))
    runs = Runs()
    restoring(directory, tmp_path, run=runs)
    line = next(line for line in runs.lines if "pg_restore" in line)

    assert "zstd -dc" not in line


def test_the_globals_are_replayed_without_stopping_on_a_role_that_exists(
    tmp_path: Path,
):
    """Roles that are already there make psql noisy, not wrong."""
    directory = snapshot_dir(tmp_path, artifact("g.sql.zst", PG_GLOBALS))
    runs = Runs()
    restoring(directory, tmp_path, run=runs)
    line = next(line for line in runs.lines if "g.sql.zst" in line)

    assert "ON_ERROR_STOP" not in line
    assert "psql" in line


def test_a_mysql_dump_goes_back_through_the_client(tmp_path: Path):
    directory = snapshot_dir(
        tmp_path,
        artifact(
            "shop.sql.zst",
            {"type": "mysql_database", "container": "shop", "database": "s"},
        ),
    )
    runs = Runs()
    restoring(directory, tmp_path, run=runs)
    together = " ".join(runs.lines)

    assert "zstd -dc" in together
    assert "mysql" in together


def test_without_a_container_the_tools_are_run_directly(tmp_path: Path):
    directory = snapshot_dir(
        tmp_path,
        artifact(
            "db.dump",
            {"type": "pg_database", "container": "", "user": "u", "database": "app"},
        ),
    )
    runs = Runs()
    restoring(directory, tmp_path, run=runs)

    assert not any("docker exec" in line for line in runs.lines)
