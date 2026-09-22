from holdfast.checkup import complaints, format_complaints
from holdfast.config import Config


def _config(values: dict) -> Config:
    base = {
        "host_label": "web-1",
        "alerts": {"chat_id": "", "bot_token": ""},
        "offsite": {"remote": "", "timeout_minutes": 120},
        "encryption": {"recipients": []},
        "api": {"token": ""},
        "heartbeat": {"url": ""},
    }
    from holdfast.config import merge

    return Config(values=merge(base, values), sources={})


def test_a_complete_configuration_has_nothing_to_say():
    cfg = _config({"api": {"token": "a" * 64}})
    assert complaints(cfg) == []


def test_host_label_is_required():
    cfg = _config({"host_label": "", "api": {"token": "a" * 64}})
    keys = [item.key for item in complaints(cfg)]
    assert "host_label" in keys


def test_a_channel_without_a_token_cannot_deliver():
    cfg = _config({"alerts": {"chat_id": "-100"}, "api": {"token": "a" * 64}})
    items = complaints(cfg)
    assert [item.key for item in items] == ["HOLDFAST_TELEGRAM_BOT_TOKEN"]
    assert "chat_id" in items[0].why


def test_a_token_without_a_channel_is_also_wrong():
    cfg = _config({"alerts": {"bot_token": "12345:x"}, "api": {"token": "a" * 64}})
    assert [item.key for item in complaints(cfg)] == ["alerts.chat_id"]


def test_a_missing_offsite_remote_is_not_a_complaint():
    """A fresh machine does not know a storage address; that is not this
    list's job to report. backup_offsite_copy is what says a copy is missing."""
    cfg = _config({"api": {"token": "a" * 64}})
    assert complaints(cfg) == []


def test_api_token_is_required_because_the_api_refuses_without_it():
    cfg = _config({})
    assert "api.token" in [item.key for item in complaints(cfg)]


def test_the_report_names_the_fix_not_only_the_problem():
    cfg = _config({"host_label": "", "api": {"token": "a" * 64}})
    text = format_complaints(complaints(cfg))
    assert "host_label" in text
    assert "holdfast init" in text
