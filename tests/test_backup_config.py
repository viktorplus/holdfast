from dataclasses import dataclass
from pathlib import Path

import pytest
from support import config

from holdfast.backup import BackupError, component_types, load_components
from holdfast.backup.model import Artifact, BuildContext, Component


@dataclass(frozen=True)
class Fake(Component):
    """A type registered only by these tests, so the parser can be exercised.

    The real types arrive with the code that understands them; this one exists
    to test the machinery around them and nothing else.
    """

    type = "fake"

    def artifacts(self, ctx: BuildContext) -> list[Artifact]:
        return [Artifact(f"{self.name}.bin", "printf x", {"type": "none"}, "cat")]


TYPES = {"fake": Fake}


def machine(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "holdfast.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_no_components_is_an_empty_list_not_an_error():
    assert load_components(config(), types=TYPES) == []


def test_a_component_without_a_type_says_which_key_is_missing():
    cfg = config(component=[{"name": "a"}])
    with pytest.raises(BackupError, match="type"):
        load_components(cfg, types=TYPES)


def test_an_unknown_type_lists_the_known_ones():
    cfg = config(component=[{"type": "postgres", "name": "a"}])
    with pytest.raises(BackupError, match="fake"):
        load_components(cfg, types=TYPES)


def test_a_component_without_a_name_is_refused():
    cfg = config(component=[{"type": "fake"}])
    with pytest.raises(BackupError, match="name"):
        load_components(cfg, types=TYPES)


def test_two_components_cannot_share_a_name():
    """The name becomes the artifact's file name inside the snapshot.

    Letting the second quietly overwrite the first costs exactly one copy, and
    nothing says so until the restore.
    """
    cfg = config(
        component=[
            {"type": "fake", "name": "data"},
            {"type": "fake", "name": "data"},
        ]
    )
    with pytest.raises(BackupError, match="data"):
        load_components(cfg, types=TYPES)


@pytest.mark.parametrize("name", ["../etc", "Data", "a b", "-lead", "", "a/b"])
def test_a_name_that_could_leave_the_snapshot_is_refused(name: str):
    cfg = config(component=[{"type": "fake", "name": name}])
    with pytest.raises(BackupError):
        load_components(cfg, types=TYPES)


def test_components_keep_the_order_they_were_declared_in():
    """Order is the operator's statement about what depends on what."""
    cfg = config(
        component=[
            {"type": "fake", "name": "zebra"},
            {"type": "fake", "name": "apple"},
        ]
    )
    assert [c.name for c in load_components(cfg, types=TYPES)] == ["zebra", "apple"]


def test_a_component_is_not_a_table():
    cfg = config(component=["not a table"])
    with pytest.raises(BackupError):
        load_components(cfg, types=TYPES)


def test_the_registry_only_claims_types_that_exist():
    for name, cls in component_types().items():
        assert cls.type == name


def test_array_of_tables_survives_the_config_layers(tmp_path: Path):
    """The parser reads what TOML actually produces, not what it looks like."""
    path = machine(
        tmp_path,
        '[[component]]\ntype = "fake"\nname = "one"\n\n'
        '[[component]]\ntype = "fake"\nname = "two"\n',
    )
    from holdfast.config import load_config

    cfg = load_config(machine=path, env={})
    assert [c.name for c in load_components(cfg, types=TYPES)] == ["one", "two"]


def test_the_defaults_carry_a_backup_section():
    cfg = config()
    assert cfg.get("backup.root")
    assert cfg.get("backup.retention_days") == 7
    assert cfg.get("backup.min_free_gb") == 8
    assert cfg.get("backup.zstd_level") == 10
    assert cfg.get("encryption.enabled") is True
    assert cfg.get("encryption.tool") == "age"
    assert cfg.get("component") == []
    assert cfg.get("backup.mode") == ""
    assert cfg.get("backup.exclude") == []


def test_the_shipped_example_is_a_configuration_that_actually_works():
    """Someone will copy this file. It has to parse, and its recipient has to
    be one age would accept - the first version of it was four characters short
    and nothing said so."""
    from holdfast.backup.encrypt import Encryption
    from holdfast.config import load_config

    example = Path(__file__).resolve().parent.parent / "examples/holdfast.example.toml"
    cfg = load_config(machine=example, env={})

    components = load_components(cfg)

    assert {c.type for c in components} == {
        "postgres",
        "mysql",
        "docker_volume",
        "path",
        "command",
    }
    assert Encryption.from_config(cfg).enabled is True
