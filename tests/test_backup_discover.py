import json
from pathlib import Path

import pytest
from support import ORPHAN_VOLUME, WP_VOLUME, config, inspect_entry, wordpress_site

from holdfast.backup.components_file import read_tables, write
from holdfast.backup.discover import discover
from holdfast.backup.machine import parse_inspect
from holdfast.backup.model import BackupError

AUTO_MESSAGE = (
    'backup.mode is "auto": what is kept is worked out again at every backup, '
    "so there is nothing to write down. `holdfast backup --dry-run` shows what "
    "the next run takes.\n"
)


class Machine:
    """The two questions the rule asks of a machine, answered from fixtures."""

    def __init__(self, entries=(), volumes=()):
        self._entries = list(entries)
        self._volumes = list(volumes)

    def inspect_containers(self):
        return parse_inspect(json.dumps(self._entries))

    def volumes(self):
        return list(self._volumes)


@pytest.fixture(autouse=True)
def project_directories_exist(monkeypatch):
    # wordpress_site()'s /root/myapp does not exist on the machine running the
    # tests; the rule asks os.path.exists through selection.plan_for.
    monkeypatch.setattr("holdfast.backup.selection.os.path.exists", lambda p: True)


def a_wordpress_site() -> Machine:
    return Machine(wordpress_site(), [WP_VOLUME, ORPHAN_VOLUME])


def test_manual_writes_what_the_rule_finds(tmp_path: Path):
    path = tmp_path / "components.toml"

    text = discover(config(component=[]), a_wordpress_site(), path)

    tables = read_tables(path)
    assert [t["name"] for t in tables] == [
        "myapp-db-1",
        "project-myapp",
        "volume-myapp-wordpress-1-var-www-html",
    ]
    assert text.startswith(f"wrote {path} (3 components)\n")
    assert "  + project-myapp (path)" in text
    assert "not taken:" in text
    assert f"  volume {ORPHAN_VOLUME}: no container uses it" in text


def test_a_second_run_on_the_same_machine_changes_nothing(tmp_path: Path):
    path = tmp_path / "components.toml"
    discover(config(component=[]), a_wordpress_site(), path)

    text = discover(config(component=[]), a_wordpress_site(), path)

    assert "no changes" in text
    assert "  + " not in text
    assert (tmp_path / "components.toml.prev").exists()


def test_what_holdfast_toml_declares_is_not_written_again(tmp_path: Path):
    path = tmp_path / "components.toml"
    cfg = config(
        component=[{"type": "path", "name": "mine", "path": "/root/myapp"}],
    )

    discover(cfg, a_wordpress_site(), path)

    assert "project-myapp" not in [t["name"] for t in read_tables(path)]


def test_auto_writes_nothing(tmp_path: Path):
    path = tmp_path / "components.toml"

    text = discover(
        config(**{"backup.mode": "auto"}, component=[]), a_wordpress_site(), path
    )

    assert text == AUTO_MESSAGE
    assert not path.exists()


def test_a_database_it_cannot_reach_fails_it_and_leaves_the_file_alone(
    tmp_path: Path,
):
    path = tmp_path / "components.toml"
    write(path, [{"type": "path", "name": "old", "path": "/old"}])
    before = path.read_bytes()
    no_password = inspect_entry("myapp-db-1", "mysql:8", env=["MYSQL_DATABASE=x"])

    with pytest.raises(BackupError, match="root password is not in its environment"):
        discover(config(component=[]), Machine([no_password]), path)

    assert path.read_bytes() == before
    assert not (tmp_path / "components.toml.prev").exists()


def test_a_0_2_draft_is_replaced_and_kept_as_prev(tmp_path: Path):
    path = tmp_path / "components.toml"
    draft = '[[component]]\ntype = "path"\nname = "opt-app"\npath = "/opt/app"\n'
    path.write_text(draft, encoding="utf-8")

    text = discover(config(component=[]), a_wordpress_site(), path)

    assert (tmp_path / "components.toml.prev").read_text(encoding="utf-8") == draft
    assert "opt-app" not in [t["name"] for t in read_tables(path)]
    assert "  - opt-app (path)" in text
