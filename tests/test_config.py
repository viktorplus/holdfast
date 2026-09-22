from pathlib import Path

from holdfast.config import Config, load_config, merge


def test_overlay_wins_even_when_empty():
    """An empty value is how the operator says "stop treating this as normal"."""
    base = {"audit": {"allowed_ports": "80 443"}}
    overlay = {"audit": {"allowed_ports": ""}}
    assert merge(base, overlay) == {"audit": {"allowed_ports": ""}}


def test_absent_key_does_not_erase_the_layer_below():
    base = {"audit": {"allowed_ports": "80 443", "window": "60"}}
    overlay = {"audit": {"window": "30"}}
    assert merge(base, overlay) == {
        "audit": {"allowed_ports": "80 443", "window": "30"}
    }


def test_a_profile_beside_the_config_is_not_a_layer(tmp_path: Path):
    """The allow-list is enforced at install time and nowhere else.

    So this file must not be read here: if it were, the bot_token below would
    become configuration without ever passing that list.
    """
    profile = tmp_path / "profile.toml"
    profile.write_text(
        '[alerts]\nchat_id = "111"\nbot_token = "12345:secret"\n', encoding="utf-8"
    )
    machine = tmp_path / "holdfast.toml"
    machine.write_text('host_label = "web-1"\n', encoding="utf-8")

    cfg = load_config(machine=machine, env={})

    assert cfg.get("alerts.chat_id") == ""
    assert cfg.sources["alerts.chat_id"] == "defaults"
    assert cfg.get("alerts.bot_token") == ""
    assert cfg.sources["host_label"] == "machine"


def test_secrets_come_only_from_the_environment(tmp_path: Path):
    machine = tmp_path / "holdfast.toml"
    machine.write_text('host_label = "web-1"\n', encoding="utf-8")

    cfg = load_config(
        machine=machine, env={"HOLDFAST_TELEGRAM_BOT_TOKEN": "secret-value"}
    )

    assert cfg.get("alerts.bot_token") == "secret-value"
    assert cfg.sources["alerts.bot_token"] == "env"


def test_missing_files_leave_defaults_in_place():
    cfg = load_config(machine=None, env={})
    assert cfg.get("host_label") == ""
    assert cfg.sources["host_label"] == "defaults"


def test_one_config_cannot_write_into_the_next_one():
    """merge used to hand out the module-global DEFAULTS containers themselves.

    Both shapes of the hazard: a write into a nested section, and a write
    into a list inside one.
    """
    first = load_config(machine=None, env={})
    first.values["api"]["token"] = "written-through-one-config"
    first.values["encryption"]["recipients"].append("age1-injected")

    second = load_config(machine=None, env={})

    assert second.get("api.token") == ""
    assert second.get("encryption.recipients") == []


def test_get_returns_default_for_unknown_path():
    cfg = Config(values={}, sources={})
    assert cfg.get("nothing.here", "fallback") == "fallback"


def test_offsite_defaults_replace_the_retired_storage_block():
    """storage.endpoint/bucket has existed since stage 1 with no reader; this
    is the rename that replaces it with what rclone actually needs."""
    cfg = load_config(machine=None, env={})
    assert cfg.get("offsite.remote") == ""
    assert cfg.get("offsite.timeout_minutes") == 120
    assert cfg.get("storage.endpoint") is None
