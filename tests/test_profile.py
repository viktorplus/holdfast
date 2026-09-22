from pathlib import Path

import pytest

from holdfast.profile import ProfileError, load_profile, profile_complaints


def test_accepts_the_three_shared_blocks():
    data = {
        "collection": "prod",
        "alerts": {"chat_id": "-1001234567890"},
        "offsite": {"remote": "shared:backups"},
        "encryption": {"recipients": ["age1abc"]},
    }
    assert profile_complaints(data) == []


def test_a_block_may_be_left_out_entirely():
    """Shared channel, own offsite destination, own key - expressed by omission."""
    assert profile_complaints({"alerts": {"chat_id": "-100"}}) == []


def test_rejects_the_retired_storage_block():
    """rclone knows its own endpoint and bucket; the pair never had a reader."""
    complaints = profile_complaints({"storage": {"endpoint": "s3.example.com"}})
    assert len(complaints) == 1
    assert "storage" in complaints[0]


def test_rejects_a_key_offsite_no_longer_carries():
    """The directory inside the remote comes from host_label, not a profile key."""
    complaints = profile_complaints({"offsite": {"endpoint": "s3.example.com"}})
    assert len(complaints) == 1
    assert "endpoint" in complaints[0]


def test_rejects_a_secret_smuggled_into_a_known_block():
    data = {"alerts": {"chat_id": "-100", "bot_token": "12345:secret"}}
    complaints = profile_complaints(data)
    assert len(complaints) == 1
    assert "bot_token" in complaints[0]


def test_rejects_an_unknown_block():
    complaints = profile_complaints({"database": {"password": "hunter2"}})
    assert len(complaints) == 1
    assert "database" in complaints[0]


def test_rejects_a_table_where_a_value_belongs():
    """`[offsite.remote]` names an allowed key, but not a writable value."""
    complaints = profile_complaints({"offsite": {"remote": {"host": "x"}}})
    assert len(complaints) == 1
    assert "remote" in complaints[0]


def test_rejects_a_list_holding_a_table():
    complaints = profile_complaints({"encryption": {"recipients": [{"key": "age1"}]}})
    assert len(complaints) == 1
    assert "recipients" in complaints[0]


def test_rejects_a_table_at_the_top_level():
    complaints = profile_complaints({"collection": {"name": "prod"}})
    assert len(complaints) == 1
    assert "collection" in complaints[0]


def test_rejects_a_number_toml_cannot_write_back():
    """`remote = nan` is valid TOML going in and unreadable coming out."""
    assert len(profile_complaints({"offsite": {"remote": float("nan")}})) == 1
    assert len(profile_complaints({"offsite": {"remote": float("inf")}})) == 1
    assert len(profile_complaints({"encryption": {"recipients": [float("nan")]}})) == 1
    assert profile_complaints({"offsite": {"remote": 1.5}}) == []


def test_a_table_valued_key_leaves_nothing_behind(tmp_path: Path):
    """The old failure mode: init succeeded and wrote a file that is not TOML."""
    from holdfast.install import init_join

    path = tmp_path / "profile.toml"
    path.write_text('[offsite.remote]\nhost = "x"\n', encoding="utf-8")

    with pytest.raises(ProfileError):
        init_join(tmp_path / "etc", path, "web-2")
    assert not (tmp_path / "etc").exists()


def test_load_refuses_a_profile_with_complaints(tmp_path: Path):
    path = tmp_path / "profile.toml"
    path.write_text('[alerts]\nbot_token = "12345:secret"\n', encoding="utf-8")
    with pytest.raises(ProfileError) as caught:
        load_profile(path)
    assert "bot_token" in str(caught.value)


def test_load_returns_a_clean_profile(tmp_path: Path):
    path = tmp_path / "profile.toml"
    path.write_text(
        'collection = "prod"\n[alerts]\nchat_id = "-100"\n', encoding="utf-8"
    )
    assert load_profile(path)["alerts"]["chat_id"] == "-100"


def test_a_broken_file_is_an_error_and_not_a_traceback(tmp_path: Path):
    path = tmp_path / "profile.toml"
    path.write_text("collection = \n", encoding="utf-8")
    with pytest.raises(ProfileError) as caught:
        load_profile(path)
    assert "profile.toml" in str(caught.value)


def test_missing_file_is_an_error(tmp_path: Path):
    with pytest.raises(ProfileError):
        load_profile(tmp_path / "absent.toml")


def test_the_shipped_profile_example_has_a_recipient_that_actually_works():
    """Someone will copy this file into a profile. The first version of its
    recipient was five characters short of a real one, the same defect that
    was fixed in holdfast.example.toml at an earlier stage - and a machine
    joined with a profile like that would be refused on its first backup."""
    from support import config

    from holdfast.backup.encrypt import Encryption

    example = Path(__file__).resolve().parent.parent / "examples/profile.example.toml"
    profile = load_profile(example)

    cfg = config(**{"encryption.recipients": profile["encryption"]["recipients"]})

    assert Encryption.from_config(cfg).enabled is True
