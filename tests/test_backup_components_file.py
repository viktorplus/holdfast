import pytest
from support import posix_only

from holdfast.backup.components_file import (
    HEADER,
    difference,
    read_tables,
    render,
    write,
    written_by_discover,
)
from holdfast.backup.model import BackupError

# The tables the rule gives a WordPress site (see test_backup_rule.py), plus
# one that carries every escape and value type render has to get right.
TABLES = [
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
    {
        "type": "path",
        "name": "odd",
        "path": 'C:\\a "quoted" путь\x7f',
        "exclude": [],
        "globals": True,
        "max_mb": 512,
    },
]


def test_render_then_read_gives_back_exactly_the_tables(tmp_path):
    path = tmp_path / "components.toml"
    path.write_text(render(TABLES), encoding="utf-8")

    assert read_tables(path) == TABLES


def test_render_starts_with_the_header_and_writes_booleans_as_toml():
    text = render(TABLES)

    assert text.startswith(HEADER)
    assert "globals = true\n" in text
    assert "max_mb = 512\n" in text


@pytest.mark.parametrize("value", [1.5, None, {"a": 1}, [[1]]])
def test_render_refuses_a_value_toml_would_not_give_back(value):
    with pytest.raises(BackupError, match="bad"):
        render([{"type": "path", "name": "x", "bad": value}])


def test_read_tables_of_a_missing_file_is_empty(tmp_path):
    assert read_tables(tmp_path / "components.toml") == []


def test_read_tables_of_broken_toml_names_the_file(tmp_path):
    path = tmp_path / "components.toml"
    path.write_text(HEADER + "[[component]\nname = \n", encoding="utf-8")

    with pytest.raises(BackupError, match="components.toml"):
        read_tables(path)


def test_write_keeps_the_previous_file_and_returns_its_tables(tmp_path):
    path = tmp_path / "components.toml"

    assert write(path, TABLES[:1]) == []
    assert not (tmp_path / "components.toml.prev").exists()
    first = path.read_text(encoding="utf-8")

    assert write(path, TABLES[1:2]) == TABLES[:1]
    assert (tmp_path / "components.toml.prev").read_text(encoding="utf-8") == first
    assert read_tables(path) == TABLES[1:2]


def test_write_over_a_file_that_is_not_toml_treats_it_as_empty(tmp_path):
    path = tmp_path / "components.toml"
    path.write_text("not = = toml\n", encoding="utf-8")

    assert write(path, TABLES[:1]) == []
    assert (tmp_path / "components.toml.prev").read_text(encoding="utf-8") == (
        "not = = toml\n"
    )


@posix_only
def test_write_leaves_the_file_readable_by_its_owner_only(tmp_path):
    path = tmp_path / "components.toml"
    write(path, TABLES)

    assert path.stat().st_mode & 0o777 == 0o600


@posix_only
def test_the_previous_file_is_kept_private_too(tmp_path):
    """A 0.2 draft the operator wrote at 0644 is still this file's content."""
    path = tmp_path / "components.toml"
    path.write_text('[[component]]\ntype = "path"\n', encoding="utf-8")
    path.chmod(0o644)

    write(path, TABLES)

    assert (tmp_path / "components.toml.prev").stat().st_mode & 0o777 == 0o600


def test_written_by_discover_looks_at_the_first_line_only(tmp_path):
    ours = tmp_path / "ours.toml"
    ours.write_text(render(TABLES), encoding="utf-8")
    draft = tmp_path / "draft.toml"
    draft.write_text('[[component]]\ntype = "path"\n', encoding="utf-8")
    edited = tmp_path / "edited.toml"
    edited.write_text("# my notes\n" + render(TABLES), encoding="utf-8")

    assert written_by_discover(ours)
    assert not written_by_discover(draft)
    assert not written_by_discover(edited)
    assert not written_by_discover(tmp_path / "missing.toml")


def test_difference_names_added_removed_and_changed_in_that_order():
    before = [
        {"type": "path", "name": "b", "path": "/b"},
        {"type": "path", "name": "gone", "path": "/g"},
        {"type": "path", "name": "same", "path": "/s"},
    ]
    after = [
        {"type": "path", "name": "same", "path": "/s"},
        {"type": "path", "name": "b", "path": "/b2"},
        {"type": "mysql", "name": "new-z"},
        {"type": "path", "name": "new-a", "path": "/a"},
    ]

    assert difference(before, after) == [
        "  + new-a (path)",
        "  + new-z (mysql)",
        "  - gone (path)",
        "  ~ b (path)",
    ]


def test_difference_of_nothing_is_nothing():
    assert difference([], []) == []
    assert difference(TABLES, TABLES) == []
