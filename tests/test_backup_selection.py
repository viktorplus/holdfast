import json
from pathlib import Path

import pytest
from support import ORPHAN_VOLUME, WP_VOLUME, config, wordpress_site

from holdfast.backup.components_file import HEADER, write
from holdfast.backup.machine import parse_inspect
from holdfast.backup.model import BackupError
from holdfast.backup.registry import component_types
from holdfast.backup.rule import Plan, Skip
from holdfast.backup.selection import backup_mode, select


class Machine:
    """A fake probe: the same four methods `plan_for` calls, plus a counter.

    The counter is how "manual with no components.toml is never asked
    anything" gets proven - a bug that quietly shells out to `docker` in
    manual mode would otherwise pass every other assertion.
    """

    def __init__(self, entries=(), volumes=(), sizes=None):
        self._entries = list(entries)
        self._volumes = list(volumes)
        self.sizes = sizes or {}
        self.calls = 0

    def inspect_containers(self):
        self.calls += 1
        return parse_inspect(json.dumps(self._entries))

    def volumes(self):
        self.calls += 1
        return list(self._volumes)

    def volume_mountpoint(self, name: str) -> str:
        self.calls += 1
        return f"/var/lib/docker/volumes/{name}/_data"

    def directory_size_mb(self, path: str) -> int:
        self.calls += 1
        return self.sizes.get(path, 1)


def a_path_component(name: str, path: str) -> dict:
    return {"type": "path", "name": name, "path": path}


def write_components_toml(tmp_path: Path, *tables: dict) -> Path:
    path = tmp_path / "components.toml"
    write(path, list(tables))
    return path


# --------------------------------------------------------------------------
# backup_mode
# --------------------------------------------------------------------------


def test_no_mode_is_manual():
    assert backup_mode(config()) == "manual"


def test_an_unknown_mode_names_the_two_that_exist():
    cfg = config(**{"backup.mode": "sometimes"})
    with pytest.raises(
        BackupError,
        match=r'backup\.mode is \'sometimes\'; it is either "auto" or "manual"',
    ):
        backup_mode(cfg)


# --------------------------------------------------------------------------
# select: manual
# --------------------------------------------------------------------------


def test_manual_with_no_components_file_backs_up_only_what_was_declared():
    cfg = config(component=[a_path_component("a", "/etc/a")])
    probe = Machine()

    selection = select(cfg, probe)

    assert selection.mode == "manual"
    assert [c.name for c in selection.components] == ["a"]
    assert selection.origins == {"a": "holdfast.toml"}
    assert selection.skipped == []
    assert selection.sizes_mb == {}
    assert probe.calls == 0  # manual never asks the machine anything


def test_manual_with_a_components_file_adds_both_sources(tmp_path: Path):
    cfg = config(component=[a_path_component("a", "/etc/a")])
    components_file = write_components_toml(tmp_path, a_path_component("b", "/etc/b"))

    selection = select(cfg, Machine(), components_file=components_file)

    assert [c.name for c in selection.components] == ["a", "b"]
    assert selection.origins == {"a": "holdfast.toml", "b": "components.toml"}


def test_manual_ignores_a_components_file_that_does_not_exist(tmp_path: Path):
    cfg = config(component=[a_path_component("a", "/etc/a")])

    selection = select(cfg, Machine(), components_file=tmp_path / "gone.toml")

    assert [c.name for c in selection.components] == ["a"]


def test_manual_ignores_a_0_2_draft_and_says_why(tmp_path: Path):
    """holdfast 0.2's --discover told operators to redirect its draft into
    components.toml; 0.3 must not start backing that draft up unannounced."""
    cfg = config(component=[a_path_component("a", "/etc/a")])
    draft = tmp_path / "components.toml"
    draft.write_text(
        '[[component]]\ntype = "path"\nname = "b"\npath = "/etc/b"\n',
        encoding="utf-8",
    )

    selection = select(cfg, Machine(), components_file=draft)

    assert [c.name for c in selection.components] == ["a"]
    assert selection.warnings == [
        (
            f"{draft} was not written by holdfast backup --discover, so it is "
            "ignored; merge what you need into holdfast.toml and delete it, or run "
            "holdfast backup --discover to replace it"
        )
    ]


def test_manual_with_a_discover_file_has_no_warnings(tmp_path: Path):
    components_file = write_components_toml(tmp_path, a_path_component("b", "/etc/b"))

    assert select(config(), Machine(), components_file=components_file).warnings == []


def test_a_broken_discover_file_is_an_error_naming_it(tmp_path: Path):
    broken = tmp_path / "components.toml"
    broken.write_text(HEADER + "[[component]\n", encoding="utf-8")

    with pytest.raises(BackupError, match=r"components\.toml"):
        select(config(), Machine(), components_file=broken)


def test_an_unknown_mode_is_refused_by_select_too():
    cfg = config(**{"backup.mode": "sometimes"})
    with pytest.raises(BackupError, match="sometimes"):
        select(cfg, Machine())


def test_a_bad_exclude_item_is_refused_even_in_manual_mode():
    cfg = config(**{"backup.exclude": ["x"]})
    with pytest.raises(BackupError, match="backup.exclude"):
        select(cfg, Machine())


# --------------------------------------------------------------------------
# select: auto
# --------------------------------------------------------------------------


def test_auto_on_a_wordpress_site_finds_three_components(monkeypatch):
    # wordpress_site()'s project directory, /root/myapp, does not exist on
    # this machine; plan_for asks os.path.exists, so it is patched to say yes.
    monkeypatch.setattr("holdfast.backup.selection.os.path.exists", lambda p: True)
    cfg = config(**{"backup.mode": "auto"}, component=[])
    probe = Machine(entries=wordpress_site(), volumes=[WP_VOLUME, ORPHAN_VOLUME])

    selection = select(cfg, probe)

    assert selection.mode == "auto"
    names = [c.name for c in selection.components]
    assert names == [
        "myapp-db-1",
        "project-myapp",
        "volume-myapp-wordpress-1-var-www-html",
    ]
    assert selection.origins == {n: "rule" for n in names}
    assert selection.skipped
    assert Skip(f"volume {ORPHAN_VOLUME}", "no container uses it") in selection.skipped
    assert set(selection.sizes_mb) == set(names)
    assert selection.estimate_mb == sum(selection.sizes_mb.values())
    assert probe.calls > 0


def test_auto_plus_a_hand_declared_component_adds_a_fourth(monkeypatch):
    monkeypatch.setattr("holdfast.backup.selection.os.path.exists", lambda p: True)
    cfg = config(
        **{"backup.mode": "auto"},
        component=[a_path_component("nginx", "/etc/nginx")],
    )
    probe = Machine(entries=wordpress_site(), volumes=[WP_VOLUME])

    selection = select(cfg, probe)

    names = [c.name for c in selection.components]
    assert names == [
        "nginx",
        "myapp-db-1",
        "project-myapp",
        "volume-myapp-wordpress-1-var-www-html",
    ]
    assert selection.origins["nginx"] == "holdfast.toml"


# --------------------------------------------------------------------------
# select: name collisions
# --------------------------------------------------------------------------


def test_the_same_name_from_both_files_is_an_error_naming_both_sources(tmp_path: Path):
    cfg = config(component=[a_path_component("a", "/etc/a")])
    components_file = write_components_toml(tmp_path, a_path_component("a", "/etc/b"))

    with pytest.raises(
        BackupError,
        match=r"two components are both named 'a': one in holdfast\.toml, "
        r"one from components\.toml",
    ):
        select(cfg, Machine(), components_file=components_file)


def test_the_rule_never_actually_collides_but_the_registry_still_refuses_it(
    monkeypatch,
):
    """The rule already avoids doubling a declared name (see rule.Declared);
    this proves `select` would still catch it if it somehow did not."""
    monkeypatch.setattr(
        "holdfast.backup.selection.plan_for",
        lambda cfg, probe, declared: Plan(
            tables=[{"type": "path", "name": "a", "path": "/etc/b"}],
            skipped=[],
            measure=[],
        ),
    )
    cfg = config(**{"backup.mode": "auto"}, component=[a_path_component("a", "/etc/a")])

    with pytest.raises(
        BackupError,
        match=r"two components are both named 'a': one in holdfast\.toml, one from rule",
    ):
        select(cfg, Machine())


def test_load_components_still_works_through_the_split(monkeypatch):
    """components_from_tables took over the body; load_components is now a
    one-line wrapper, and this proves the wrapper still does its job."""
    from holdfast.backup import load_components

    cfg = config(component=[a_path_component("a", "/etc/a")])
    assert [c.name for c in load_components(cfg, types=component_types())] == ["a"]
