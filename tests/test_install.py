import os
import tomllib
from pathlib import Path

import pytest

from holdfast.install import (
    InstallError,
    default_host_label,
    generate_api_token,
    init_fresh,
    init_join,
)
from holdfast.profile import ProfileError


def test_api_token_is_long_and_different_every_time():
    first, second = generate_api_token(), generate_api_token()
    assert len(first) == 64
    assert first != second


def test_default_host_label_is_not_empty():
    assert default_host_label()


def test_fresh_writes_a_usable_config(tmp_path: Path):
    path = init_fresh(tmp_path, host_label="web-1")

    assert path == tmp_path / "holdfast.toml"
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    assert data["host_label"] == "web-1"
    assert len(data["api"]["token"]) == 64


def test_fresh_falls_back_to_the_hostname(tmp_path: Path):
    path = init_fresh(tmp_path)
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    assert data["host_label"] == default_host_label()


def test_fresh_defaults_to_auto_backup_mode(tmp_path: Path):
    path = init_fresh(tmp_path, host_label="web-1")
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    assert data["backup"]["mode"] == "auto"


def test_fresh_can_be_told_manual_backup_mode(tmp_path: Path):
    path = init_fresh(tmp_path, host_label="web-1", backup_mode="manual")
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    assert data["backup"]["mode"] == "manual"


def test_join_defaults_to_auto_backup_mode(tmp_path: Path):
    profile = _profile(tmp_path, 'collection = "prod"\n')
    path = init_join(tmp_path / "etc", profile, host_label="web-2")
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    assert data["backup"]["mode"] == "auto"


def test_join_can_be_told_manual_backup_mode(tmp_path: Path):
    profile = _profile(tmp_path, 'collection = "prod"\n')
    path = init_join(
        tmp_path / "etc", profile, host_label="web-2", backup_mode="manual"
    )
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    assert data["backup"]["mode"] == "manual"


def test_fresh_leaves_the_shared_blocks_empty(tmp_path: Path):
    """A standalone machine shares nothing until someone says otherwise."""
    path = init_fresh(tmp_path, host_label="web-1")
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    assert data["alerts"]["chat_id"] == ""
    assert data["offsite"]["remote"] == ""
    assert data["encryption"]["recipients"] == []


def test_the_header_owns_up_to_the_secret_in_the_file(tmp_path: Path):
    """A header claiming "no secrets here" invites the file into a backup."""
    path = init_fresh(tmp_path, host_label="web-1")
    text = path.read_text(encoding="utf-8")
    header = text.split("\n\n", 1)[0]
    data = tomllib.loads(text)

    assert data["api"]["token"]  # the secret is in the file
    assert "api.token" in header  # and the header says so
    assert "HOLDFAST_API_TOKEN" not in header
    for denial in ("deliberately absent", "never here", "no secrets"):
        assert denial not in header


def test_fresh_refuses_to_overwrite(tmp_path: Path):
    init_fresh(tmp_path, host_label="web-1")
    with pytest.raises(InstallError) as caught:
        init_fresh(tmp_path, host_label="web-2")
    assert "already" in str(caught.value)


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions only")
def test_the_file_is_not_world_readable(tmp_path: Path):
    path = init_fresh(tmp_path, host_label="web-1")
    assert path.stat().st_mode & 0o077 == 0


def test_a_double_quote_in_the_host_label_survives_the_round_trip(tmp_path: Path):
    label = 'web"1'
    path = init_fresh(tmp_path, host_label=label)
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    assert data["host_label"] == label


def test_a_backslash_in_the_host_label_survives_the_round_trip(tmp_path: Path):
    label = "c:" + chr(92) + "backup"
    path = init_fresh(tmp_path, host_label=label)
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    assert data["host_label"] == label


def test_a_non_ascii_host_label_stays_readable(tmp_path: Path):
    """The file invites editing; \\u escapes are not an invitation."""
    label = "веб-1"
    path = init_fresh(tmp_path, host_label=label)
    text = path.read_text(encoding="utf-8")

    assert label in text
    assert "\\u04" not in text
    assert tomllib.loads(text)["host_label"] == label


def _profile(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "profile.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_join_takes_the_blocks_the_profile_carries(tmp_path: Path):
    profile = _profile(tmp_path, 'collection = "prod"\n[alerts]\nchat_id = "-100"\n')
    config_dir = tmp_path / "etc"

    path = init_join(config_dir, profile, host_label="web-2")

    data = tomllib.loads(path.read_text(encoding="utf-8"))
    assert data["collection"] == "prod"
    assert data["alerts"]["chat_id"] == "-100"


def test_join_leaves_absent_blocks_empty(tmp_path: Path):
    """Shared channel, own offsite destination: the profile says so by omission."""
    profile = _profile(tmp_path, '[alerts]\nchat_id = "-100"\n')
    config_dir = tmp_path / "etc"

    data = tomllib.loads(
        init_join(config_dir, profile, "web-2").read_text(encoding="utf-8")
    )

    assert data["alerts"]["chat_id"] == "-100"
    assert data["offsite"]["remote"] == ""
    assert data["encryption"]["recipients"] == []


def test_join_keeps_the_sibling_blocks_the_profile_leaves_out(tmp_path: Path):
    """offsite has one key now, so the pair-of-keys case this test used to
    guard is gone with storage.bucket; what remains worth checking is that
    transferring offsite.remote does not disturb the skeleton's other blocks."""
    profile = _profile(tmp_path, '[offsite]\nremote = "shared:backups"\n')

    data = tomllib.loads(
        init_join(tmp_path / "etc", profile, "web-2").read_text(encoding="utf-8")
    )

    assert data["offsite"]["remote"] == "shared:backups"
    assert data["alerts"]["chat_id"] == ""
    assert data["encryption"]["recipients"] == []


def test_join_gives_this_machine_its_own_api_token(tmp_path: Path):
    profile = _profile(tmp_path, '[alerts]\nchat_id = "-100"\n')

    first = tomllib.loads(
        init_join(tmp_path / "a", profile, "web-2").read_text(encoding="utf-8")
    )
    second = tomllib.loads(
        init_join(tmp_path / "b", profile, "web-3").read_text(encoding="utf-8")
    )

    assert first["api"]["token"] != second["api"]["token"]


def test_join_refuses_a_profile_carrying_a_secret(tmp_path: Path):
    profile = _profile(tmp_path, '[alerts]\nbot_token = "12345:secret"\n')
    with pytest.raises(ProfileError):
        init_join(tmp_path / "etc", profile, "web-2")
    assert not (tmp_path / "etc").exists()


def test_join_refuses_a_missing_profile(tmp_path: Path):
    with pytest.raises(ProfileError):
        init_join(tmp_path / "etc", tmp_path / "absent.toml", "web-2")
    assert not (tmp_path / "etc").exists()


def test_join_never_writes_a_config_it_could_not_read_back(tmp_path: Path):
    """A nan reached the file as `NaN` and broke every command after init."""
    profile = _profile(tmp_path, "[offsite]\nremote = nan\n")
    with pytest.raises(ProfileError):
        init_join(tmp_path / "etc", profile, "web-2")
    assert not (tmp_path / "etc").exists()
