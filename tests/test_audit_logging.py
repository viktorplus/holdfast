"""The logging checks."""

from __future__ import annotations

import os
import time

from support import context, write

from holdfast.audit.checks import logging as logging_checks
from holdfast.audit.model import FAIL, PASS, UNKNOWN, WARN


def age(path, days: float) -> None:
    when = time.time() - days * 86400
    os.utime(path, (when, when))


# -- journald_limit ---------------------------------------------------------


def test_no_journald_conf_is_unknown(tmp_path):
    assert logging_checks._journald_limit(context(tmp_path))[0] == UNKNOWN


def test_a_size_limit_passes(tmp_path):
    write(tmp_path, "/etc/systemd/journald.conf", "[Journal]\nSystemMaxUse=500M\n")
    status, detail, _ = logging_checks._journald_limit(context(tmp_path))
    assert status == PASS
    assert "500M" in detail


def test_no_size_limit_fails(tmp_path):
    write(tmp_path, "/etc/systemd/journald.conf", "[Journal]\n#SystemMaxUse=\n")
    assert logging_checks._journald_limit(context(tmp_path))[0] == FAIL


# -- docker_log_rotation ----------------------------------------------------


def test_no_daemon_json_fails(tmp_path):
    status, detail, _ = logging_checks._docker_log_rotation(context(tmp_path))
    assert status == FAIL
    assert "without a limit" in detail


def test_a_max_size_passes(tmp_path):
    write(tmp_path, "/etc/docker/daemon.json", '{"log-opts": {"max-size": "10m"}}')
    assert logging_checks._docker_log_rotation(context(tmp_path))[0] == PASS


def test_daemon_json_that_does_not_parse_is_unknown(tmp_path):
    write(tmp_path, "/etc/docker/daemon.json", "{not json")
    assert logging_checks._docker_log_rotation(context(tmp_path))[0] == UNKNOWN


def test_with_docker_off_the_rotation_check_is_unknown(tmp_path):
    ctx = context(tmp_path)
    ctx.docker_enabled = False
    assert logging_checks._docker_log_rotation(ctx)[0] == UNKNOWN


# -- log_retention_depth ----------------------------------------------------


def test_no_var_log_is_unknown(tmp_path):
    assert logging_checks._log_retention_depth(context(tmp_path))[0] == UNKNOWN


def test_an_empty_var_log_fails(tmp_path):
    (tmp_path / "var" / "log").mkdir(parents=True)
    status, detail, _ = logging_checks._log_retention_depth(context(tmp_path))
    assert status == FAIL
    assert "nothing is recorded" in detail


def test_shallow_logs_warn_with_the_date_history_runs_out(tmp_path):
    write(tmp_path, "/var/log/auth.log", "nothing much\n")
    status, detail, _ = logging_checks._log_retention_depth(context(tmp_path))
    assert status == WARN
    assert "cannot look further back" in detail


def test_deep_enough_logs_pass(tmp_path):
    path = write(tmp_path, "/var/log/auth.log", "nothing much\n")
    age(path, 90)
    status, detail, _ = logging_checks._log_retention_depth(context(tmp_path))
    assert status == PASS
    assert "90 days deep" in detail


# -- remote_log_collector ---------------------------------------------------


def test_no_collector_fails(tmp_path):
    status, detail, _ = logging_checks._remote_log_collector(context(tmp_path))
    assert status == FAIL
    assert "nothing to" in detail


def test_a_shipper_container_passes(tmp_path):
    ctx = context(tmp_path)
    ctx._containers = [{"Name": "/promtail", "Config": {"Image": "grafana/promtail"}}]
    status, detail, _ = logging_checks._remote_log_collector(ctx)
    assert status == PASS
    assert "promtail" in detail


def test_a_remote_syslog_target_passes(tmp_path):
    write(tmp_path, "/etc/rsyslog.d/50-remote.conf", "*.* @@logs.example.com:514\n")
    assert logging_checks._remote_log_collector(context(tmp_path))[0] == PASS
