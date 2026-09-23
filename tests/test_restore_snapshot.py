import hashlib
import json
from pathlib import Path

import pytest

from holdfast.backup.model import RestoreError
from holdfast.backup.snapshot import load_snapshot

PATH_RECIPE = {"type": "path", "target": "/"}


def artifact(name: str, recipe: dict, body: bytes = b"a body", **extra) -> dict:
    return {
        "path": name,
        "component": extra.get("component", "thing"),
        "size": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
        "restore": recipe,
        "check": "cat >/dev/null",
        **{k: v for k, v in extra.items() if k != "component"},
    }


def a_snapshot(tmp_path: Path, *artifacts: dict, **manifest) -> Path:
    directory = tmp_path / "20260920-231500"
    directory.mkdir(parents=True, exist_ok=True)
    for record in artifacts:
        file = directory / record["path"]
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(b"a body")
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


def test_a_directory_with_no_manifest_says_which_directory(tmp_path: Path):
    (tmp_path / "empty").mkdir()
    with pytest.raises(RestoreError, match="empty"):
        load_snapshot(tmp_path / "empty")


def test_a_manifest_that_does_not_parse_is_refused(tmp_path: Path):
    directory = a_snapshot(tmp_path, artifact("a.bin", PATH_RECIPE))
    (directory / "manifest.json").write_text("{ not json", encoding="utf-8")

    with pytest.raises(RestoreError, match="manifest"):
        load_snapshot(directory)


def test_a_newer_format_is_refused_rather_than_read_in_part(tmp_path: Path):
    """Reading what this version happens to understand would restore some of a
    snapshot and silently leave the rest."""
    directory = a_snapshot(tmp_path, artifact("a.bin", PATH_RECIPE), format=2)

    with pytest.raises(RestoreError, match="newer"):
        load_snapshot(directory)


def test_a_manifest_with_no_artifacts_is_refused(tmp_path: Path):
    with pytest.raises(RestoreError, match="artifact"):
        load_snapshot(a_snapshot(tmp_path))


def test_an_artifact_with_no_checksum_is_refused(tmp_path: Path):
    """Restoring data nothing can vouch for is worse than not restoring."""
    record = artifact("a.bin", PATH_RECIPE)
    del record["sha256"]

    with pytest.raises(RestoreError, match="sha256"):
        load_snapshot(a_snapshot(tmp_path, record))


def test_a_file_listed_but_absent_is_refused(tmp_path: Path):
    directory = a_snapshot(tmp_path, artifact("a.bin", PATH_RECIPE))
    (directory / "a.bin").unlink()

    with pytest.raises(RestoreError, match="a.bin"):
        load_snapshot(directory)


def test_every_recipe_is_checked_including_the_ones_not_selected(tmp_path: Path):
    """A snapshot carrying a recipe this version cannot carry out is not one it
    can honestly restore part of."""
    directory = a_snapshot(
        tmp_path,
        artifact("good.bin", PATH_RECIPE, component="good"),
        artifact("odd.bin", {"type": "quantum_entanglement"}, component="odd"),
    )

    with pytest.raises(RestoreError, match="quantum_entanglement"):
        load_snapshot(directory)


@pytest.mark.parametrize(
    "recipe",
    [
        {"type": "path"},
        {"type": "path", "target": ""},
        {"type": "path", "target": "srv"},
        {"type": "path", "target": "/srv/../../etc"},
    ],
)
def test_a_path_recipe_that_could_land_anywhere_is_refused(tmp_path, recipe):
    with pytest.raises(RestoreError):
        load_snapshot(a_snapshot(tmp_path, artifact("a.bin", recipe)))


@pytest.mark.parametrize("volume", ["", "../escape", "a b", "a/b"])
def test_a_volume_recipe_with_an_impossible_name_is_refused(tmp_path, volume):
    recipe = {"type": "docker_volume", "volume": volume}
    with pytest.raises(RestoreError):
        load_snapshot(a_snapshot(tmp_path, artifact("a.bin", recipe)))


@pytest.mark.parametrize(
    "recipe",
    [
        {"type": "docker_volume", "container": "c", "destination": "data"},
        {"type": "docker_volume", "container": "c", "destination": "/a/../b"},
        {"type": "docker_volume", "container": "a b", "destination": "/data"},
        {"type": "docker_volume", "container": "c"},
    ],
)
def test_an_unnamed_volume_recipe_that_could_land_anywhere_is_refused(tmp_path, recipe):
    with pytest.raises(RestoreError):
        load_snapshot(a_snapshot(tmp_path, artifact("a.bin", recipe)))


def test_an_unnamed_volume_recipe_is_accepted(tmp_path):
    recipe = {"type": "docker_volume", "container": "app-web-1", "destination": "/data"}
    snapshot = load_snapshot(a_snapshot(tmp_path, artifact("a.bin", recipe)))

    assert snapshot.records[0].recipe == recipe


@pytest.mark.parametrize(
    "recipe",
    [
        {"type": "pg_database", "container": "c", "user": "u"},
        {"type": "pg_database", "container": "c", "user": "u", "database": "a/b"},
        {"type": "pg_database", "container": "a b", "user": "u", "database": "d"},
        {"type": "pg_globals", "container": "c", "user": "u;drop"},
        {"type": "mysql_database", "container": "c", "database": "a b"},
        {
            "type": "mysql_database",
            "container": "c",
            "database": "d",
            "credentials": "other",
        },
        {
            "type": "mysql_database",
            "container": "c",
            "database": "d",
            "credentials": "container_env",
            "password_env": "bad name",
        },
        {
            "type": "mysql_database",
            "container": "",
            "database": "d",
            "credentials": "container_env",
            "password_env": "PW",
        },
        {"type": "mysql_database", "container": "c", "database": "d", "user": "a;b"},
    ],
)
def test_a_database_recipe_with_an_impossible_name_is_refused(tmp_path, recipe):
    with pytest.raises(RestoreError):
        load_snapshot(a_snapshot(tmp_path, artifact("a.bin", recipe)))


def test_a_good_snapshot_reads_back_what_it_holds(tmp_path: Path):
    directory = a_snapshot(
        tmp_path,
        artifact("files.tar.zst", PATH_RECIPE, component="config"),
        artifact(
            "db.dump",
            {"type": "pg_database", "container": "c", "user": "u", "database": "app"},
            component="db",
        ),
    )
    snapshot = load_snapshot(directory)

    assert snapshot.snapshot == "20260920-231500"
    assert snapshot.host_label == "web-1"
    assert [r.component for r in snapshot.records] == ["config", "db"]
    assert snapshot.records[0].file == directory / "files.tar.zst"


def test_an_artifact_nobody_can_restore_is_listed_apart(tmp_path: Path):
    """So that a restore cannot report "done" about data it never touched."""
    directory = a_snapshot(
        tmp_path,
        artifact("files.tar.zst", PATH_RECIPE, component="config"),
        artifact("thing.bin", {"type": "none", "note": "x"}, component="thing"),
    )
    snapshot = load_snapshot(directory)

    assert [r.component for r in snapshot.records] == ["config"]
    assert [r.component for r in snapshot.unrestorable] == ["thing"]


def test_selecting_one_component_narrows_the_work(tmp_path: Path):
    directory = a_snapshot(
        tmp_path,
        artifact("a.bin", PATH_RECIPE, component="config"),
        artifact("b.bin", PATH_RECIPE, component="uploads"),
    )
    snapshot = load_snapshot(directory)

    assert [r.component for r in snapshot.select("uploads")] == ["uploads"]
    assert len(snapshot.select(None)) == 2


def test_selecting_a_component_that_is_not_there_lists_the_ones_that_are(
    tmp_path: Path,
):
    directory = a_snapshot(tmp_path, artifact("a.bin", PATH_RECIPE, component="config"))
    snapshot = load_snapshot(directory)

    with pytest.raises(RestoreError) as caught:
        snapshot.select("uploads")

    assert "config" in str(caught.value)


def test_a_changed_byte_is_noticed(tmp_path: Path):
    directory = a_snapshot(tmp_path, artifact("a.bin", PATH_RECIPE))
    snapshot = load_snapshot(directory)

    assert snapshot.records[0].checksum_ok() is True

    (directory / "a.bin").write_bytes(b"a bodY")
    assert load_snapshot(directory).records[0].checksum_ok() is False
