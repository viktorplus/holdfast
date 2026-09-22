"""The integrity and container checks.

The two drift checks are the only ones whose answer depends on a previous run,
so they are exercised the way they actually work: run once, save the report
through the real state file, run again against what was saved. Handing them a
hand-written `previous` would test the assertion rather than the mechanism.
"""

from __future__ import annotations

import gzip

from support import context, posix_only, write

from holdfast.audit.checks import containers, integrity
from holdfast.audit.model import FAIL, PASS, UNKNOWN, WARN
from holdfast.audit.state import load_last, save_last


def basic_host(tmp_path):
    write(tmp_path, "/etc/ssh/sshd_config", "Port 22\nPermitRootLogin no\n")
    write(tmp_path, "/etc/passwd", "root:x:0:0::/root:/usr/sbin/nologin\n")
    return tmp_path


# -- suid_files -------------------------------------------------------------


def test_no_suid_anywhere_passes(tmp_path):
    write(tmp_path, "/opt/app/run", "#!/bin/sh\n")
    assert integrity._suid_files(context(tmp_path))[0] == PASS


@posix_only
def test_a_suid_binary_outside_the_system_paths_fails(tmp_path):
    path = write(tmp_path, "/tmp/rootme", "x")
    path.chmod(0o4755)
    status, detail, _ = integrity._suid_files(context(tmp_path))
    assert status == FAIL
    assert "/tmp/rootme" in detail


def test_a_truncated_suid_scan_is_unknown(tmp_path):
    for index in range(5):
        write(tmp_path, f"/opt/f{index}", "x")
    ctx = context(tmp_path, **{"audit.scan.max_files": 2})
    assert integrity._suid_files(ctx)[0] == UNKNOWN


# -- successful_logins ------------------------------------------------------


def test_no_auth_log_is_unknown(tmp_path):
    assert integrity._successful_logins(context(tmp_path))[0] == UNKNOWN


def test_a_password_login_fails(tmp_path):
    write(
        tmp_path,
        "/var/log/auth.log",
        "Sep 20 10:00:00 host sshd[1]: Accepted password for deploy from "
        "203.0.113.10 port 51000 ssh2\n",
    )
    status, detail, _ = integrity._successful_logins(context(tmp_path))
    assert status == FAIL
    assert "BY PASSWORD" in detail


def test_a_root_key_login_fails(tmp_path):
    write(
        tmp_path,
        "/var/log/auth.log",
        "Sep 20 10:00:00 host sshd[1]: Accepted publickey for root from "
        "203.0.113.10 port 51000 ssh2\n",
    )
    status, detail, _ = integrity._successful_logins(context(tmp_path))
    assert status == FAIL
    assert "as root" in detail


def test_ordinary_key_logins_pass_and_say_the_list_is_empty(tmp_path):
    write(
        tmp_path,
        "/var/log/auth.log",
        "Sep 20 10:00:00 host sshd[1]: Accepted publickey for deploy from "
        "203.0.113.10 port 51000 ssh2\n",
    )
    status, detail, _ = integrity._successful_logins(context(tmp_path))
    assert status == PASS
    assert "fill it in" in detail


def test_a_key_login_from_outside_a_declared_list_warns(tmp_path):
    write(
        tmp_path,
        "/var/log/auth.log",
        "Sep 20 10:00:00 host sshd[1]: Accepted publickey for deploy from "
        "203.0.113.10 port 51000 ssh2\n",
    )
    ctx = context(tmp_path, **{"audit.ssh.allowed_login_addresses": ["192.0.2.5"]})
    status, detail, _ = integrity._successful_logins(ctx)
    assert status == WARN
    assert "203.0.113.10" in detail


def test_rotated_gzip_logs_are_read_too(tmp_path):
    base = tmp_path / "var" / "log"
    base.mkdir(parents=True)
    with gzip.open(base / "auth.log.1.gz", "wt", encoding="utf-8") as handle:
        handle.write(
            "Sep 01 10:00:00 host sshd[1]: Accepted password for deploy from "
            "203.0.113.10 port 51000 ssh2\n"
        )
    assert integrity._successful_logins(context(tmp_path))[0] == FAIL


# -- critical_file_hashes ---------------------------------------------------


def test_no_watched_file_is_unknown(tmp_path):
    assert integrity._critical_file_hashes(context(tmp_path))[0] == UNKNOWN


def test_the_first_run_records_a_baseline_without_accusing(tmp_path):
    basic_host(tmp_path)
    status, detail, _, data = integrity._critical_file_hashes(context(tmp_path))
    assert status == PASS
    assert "baseline recorded" in detail
    assert data


def test_a_changed_file_is_named_on_the_next_run(tmp_path):
    """Through the real state file, not a hand-written previous."""
    host = basic_host(tmp_path / "host")
    state_dir = tmp_path / "state"

    first = context(host)
    _, _, _, data = integrity._critical_file_hashes(first)
    save_last(
        state_dir,
        {"findings": [{"id": "critical_file_hashes", "data": data}]},
    )

    write(host, "/etc/ssh/sshd_config", "Port 22\nPermitRootLogin yes\n")

    second = context(host)
    second.previous = load_last(state_dir)
    status, detail, _, _ = integrity._critical_file_hashes(second)
    assert status == FAIL
    assert "/etc/ssh/sshd_config" in detail
    assert "changed:" in detail


def test_an_unchanged_host_passes_on_the_second_run(tmp_path):
    host = basic_host(tmp_path / "host")
    first = context(host)
    _, _, _, data = integrity._critical_file_hashes(first)
    second = context(host)
    second.previous = {"findings": [{"id": "critical_file_hashes", "data": data}]}
    status, detail, _, _ = integrity._critical_file_hashes(second)
    assert status == PASS
    assert "unchanged" in detail


# -- config_drift -----------------------------------------------------------


def test_no_slices_watched_is_unknown(tmp_path):
    ctx = context(tmp_path, **{"audit.integrity.watch_slices": []})
    assert integrity._config_drift(ctx)[0] == UNKNOWN


def test_the_first_drift_run_records_a_baseline(tmp_path):
    basic_host(tmp_path)
    status, detail, _, data = integrity._config_drift(context(tmp_path))
    assert status == PASS
    assert "baseline recorded" in detail
    assert set(data) == {"sshd", "users", "listen"}


def test_a_changed_slice_fails_on_the_next_run(tmp_path):
    host = basic_host(tmp_path / "host")
    _, _, _, data = integrity._config_drift(context(host))

    write(host, "/etc/passwd", "root:x:0:0::/root:/bin/bash\n")

    second = context(host)
    second.previous = {"findings": [{"id": "config_drift", "data": data}]}
    status, detail, _, _ = integrity._config_drift(second)
    assert status == FAIL
    assert "users:" in detail


def test_an_unknown_slice_name_is_ignored_rather_than_fatal(tmp_path):
    basic_host(tmp_path)
    ctx = context(tmp_path, **{"audit.integrity.watch_slices": ["sshd", "nonsense"]})
    status, _, _, data = integrity._config_drift(ctx)
    assert status == PASS
    assert set(data) == {"sshd"}


# -- container_temp ---------------------------------------------------------


def test_no_proc_means_no_view_into_containers(tmp_path):
    assert containers._container_temp(context(tmp_path))[0] == UNKNOWN


def test_no_containers_is_unknown(tmp_path):
    (tmp_path / "proc").mkdir()
    ctx = context(tmp_path)
    ctx._containers = []
    assert containers._container_temp(ctx)[0] == UNKNOWN


def test_a_clean_container_temp_passes(tmp_path):
    write(tmp_path, "/proc/42/root/tmp/notes.txt", "nothing\n")
    ctx = context(tmp_path)
    ctx._containers = [{"Name": "/app", "State": {"Pid": 42}}]
    status, detail, _ = containers._container_temp(ctx)
    assert status == PASS
    assert "1 containers" in detail


def test_a_script_in_a_container_temp_warns(tmp_path):
    write(tmp_path, "/proc/42/root/tmp/shell.php", "<?php\n")
    ctx = context(tmp_path)
    ctx._containers = [{"Name": "/app", "State": {"Pid": 42}}]
    status, detail, _ = containers._container_temp(ctx)
    assert status == WARN
    assert "app:/tmp/shell.php" in detail


def test_an_indicator_in_a_container_temp_fails(tmp_path):
    write(tmp_path, "/proc/42/root/tmp/planted.php", "<?php\n")
    iocs = tmp_path / "iocs.toml"
    iocs.write_text('filenames = ["planted.php"]\n', encoding="utf-8")
    ctx = context(tmp_path, **{"audit.indicators": str(iocs)})
    ctx._containers = [{"Name": "/app", "State": {"Pid": 42}}]
    status, detail, _ = containers._container_temp(ctx)
    assert status == FAIL
    assert "app:/tmp/planted.php" in detail


def test_container_temp_with_docker_off_is_unknown(tmp_path):
    (tmp_path / "proc").mkdir()
    ctx = context(tmp_path)
    ctx.docker_enabled = False
    status, detail, _ = containers._container_temp(ctx)
    assert status == UNKNOWN
    assert "not consulted" in detail
