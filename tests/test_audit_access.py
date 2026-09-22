"""The Access group, run against a synthetic host tree.

Every check gets a pair of fixtures: one machine in the state the check exists
to find, and one machine that is fine. Without the clean half a check that
always fails looks like it works.
"""

from __future__ import annotations

import pytest
from support import context, posix_only, write

from holdfast.audit.checks import access
from holdfast.audit.model import FAIL, PASS, UNKNOWN, WARN

KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEXAMPLEEXAMPLEEXAMPLEEXAMPLEEXAM"


# -- ssh_root_login ---------------------------------------------------------


def test_root_login_yes_fails(tmp_path):
    write(tmp_path, "/etc/ssh/sshd_config", "PermitRootLogin yes\n")
    status, detail, _ = access._ssh_root_login(context(tmp_path))
    assert status == FAIL
    assert "/etc/ssh/sshd_config" in detail


def test_root_login_prohibit_password_passes(tmp_path):
    write(tmp_path, "/etc/ssh/sshd_config", "PermitRootLogin prohibit-password\n")
    assert access._ssh_root_login(context(tmp_path))[0] == PASS


def test_root_login_without_a_config_is_unknown(tmp_path):
    assert access._ssh_root_login(context(tmp_path))[0] == UNKNOWN


def test_an_include_overrides_a_line_written_below_it(tmp_path):
    """This is the whole reason the parser exists.

    OpenSSH takes the first value it obtains, and expands Include where it
    appears. A config that forbids password logins at the bottom still accepts
    them when an include at the top allows them.
    """
    write(
        tmp_path,
        "/etc/ssh/sshd_config",
        "Include /etc/ssh/sshd_config.d/*.conf\nPasswordAuthentication no\n",
    )
    write(
        tmp_path, "/etc/ssh/sshd_config.d/10-cloud.conf", "PasswordAuthentication yes\n"
    )
    status, detail, _ = access._ssh_password_auth(context(tmp_path))
    assert status == FAIL
    assert "10-cloud.conf" in detail


def test_password_auth_unset_warns_because_the_default_is_yes(tmp_path):
    write(tmp_path, "/etc/ssh/sshd_config", "Port 22\n")
    assert access._ssh_password_auth(context(tmp_path))[0] == WARN


def test_password_auth_no_passes(tmp_path):
    write(tmp_path, "/etc/ssh/sshd_config", "PasswordAuthentication no\n")
    assert access._ssh_password_auth(context(tmp_path))[0] == PASS


@pytest.mark.parametrize(
    "line",
    ["PermitRootLogin = yes", "PermitRootLogin=yes", 'PermitRootLogin "yes"'],
)
def test_every_spelling_sshd_accepts_is_read(tmp_path, line):
    """sshd takes an '=' between keyword and value, and quotes around it."""
    write(tmp_path, "/etc/ssh/sshd_config", f"Port 22\n{line}\n")
    assert access._ssh_root_login(context(tmp_path))[0] == FAIL


def test_a_match_block_that_allows_passwords_fails(tmp_path):
    """A Match block overrides the global value for whoever it matches."""
    write(
        tmp_path,
        "/etc/ssh/sshd_config",
        "PasswordAuthentication no\nMatch all\n  PasswordAuthentication yes\n",
    )
    status, detail, _ = access._ssh_password_auth(context(tmp_path))
    assert status == FAIL
    assert "Match" in detail


def test_a_match_block_is_not_the_global_value(tmp_path):
    """Forbidding passwords for one user leaves the default - yes - for the rest."""
    write(
        tmp_path,
        "/etc/ssh/sshd_config",
        "Match User deploy\n  PasswordAuthentication no\n",
    )
    assert access._ssh_password_auth(context(tmp_path))[0] == WARN


# -- authorized_keys --------------------------------------------------------


def test_authorized_keys_without_any_file_is_unknown(tmp_path):
    assert access._authorized_keys(context(tmp_path))[0] == UNKNOWN


@posix_only
def test_a_well_formed_key_file_passes(tmp_path):
    path = write(tmp_path, "/root/.ssh/authorized_keys", f"{KEY} admin@example.com\n")
    path.chmod(0o600)
    status, detail, _ = access._authorized_keys(context(tmp_path))
    assert status == PASS
    assert "1 keys" in detail


def test_a_key_without_a_comment_fails(tmp_path):
    path = write(tmp_path, "/root/.ssh/authorized_keys", f"{KEY}\n")
    path.chmod(0o600)
    status, detail, _ = access._authorized_keys(context(tmp_path))
    assert status == FAIL
    assert "without a comment" in detail


def test_an_obsolete_algorithm_fails(tmp_path):
    path = write(
        tmp_path, "/home/dev/.ssh/authorized_keys", "ssh-dss AAAAB3Nz dev@example.com\n"
    )
    path.chmod(0o600)
    status, detail, _ = access._authorized_keys(context(tmp_path))
    assert status == FAIL
    assert "ssh-dss" in detail


@posix_only
def test_a_key_file_readable_by_others_fails(tmp_path):
    path = write(tmp_path, "/root/.ssh/authorized_keys", f"{KEY} admin@example.com\n")
    path.chmod(0o644)
    status, detail, _ = access._authorized_keys(context(tmp_path))
    assert status == FAIL
    assert "expected 600" in detail


# -- shell_users ------------------------------------------------------------


def test_only_system_accounts_pass(tmp_path):
    write(
        tmp_path,
        "/etc/passwd",
        "root:x:0:0::/root:/usr/sbin/nologin\ndaemon:x:1:1::/:/usr/sbin/nologin\n",
    )
    assert access._shell_users(context(tmp_path))[0] == PASS


def test_an_interactive_account_warns(tmp_path):
    write(tmp_path, "/etc/passwd", "dev:x:1000:1000::/home/dev:/bin/bash\n")
    status, detail, _ = access._shell_users(context(tmp_path))
    assert status == WARN
    assert "dev(uid=1000,/bin/bash)" in detail


def test_missing_passwd_is_unknown(tmp_path):
    assert access._shell_users(context(tmp_path))[0] == UNKNOWN


# -- effective_sshd_config --------------------------------------------------


def test_effective_config_is_unknown_off_the_host(tmp_path, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("subprocess must not run when off the host")

    monkeypatch.setattr(access.subprocess, "run", refuse)
    status, detail, _ = access._effective_sshd_config(context(tmp_path))
    assert status == UNKNOWN
    assert "container" in detail


# -- fail2ban_jail ----------------------------------------------------------


def test_no_jail_file_fails(tmp_path):
    assert access._fail2ban_jail(context(tmp_path))[0] == FAIL


def test_a_jail_on_the_wrong_port_fails(tmp_path):
    write(tmp_path, "/etc/ssh/sshd_config", "Port 2244\n")
    write(tmp_path, "/etc/fail2ban/jail.local", "[sshd]\nenabled = true\nport = 22\n")
    status, detail, _ = access._fail2ban_jail(context(tmp_path))
    assert status == FAIL
    assert "2244" in detail


def test_a_jail_on_the_right_port_passes(tmp_path):
    write(tmp_path, "/etc/ssh/sshd_config", "Port 2244\n")
    write(tmp_path, "/etc/fail2ban/jail.local", "[sshd]\nport = 2244\n")
    assert access._fail2ban_jail(context(tmp_path))[0] == PASS


def test_a_jail_without_a_port_is_unknown(tmp_path):
    write(tmp_path, "/etc/fail2ban/jail.conf", "[sshd]\nenabled = true\n")
    assert access._fail2ban_jail(context(tmp_path))[0] == UNKNOWN


# -- pending_security_updates -----------------------------------------------


def test_security_updates_fail(tmp_path):
    write(
        tmp_path,
        access.APT_UPDATES_FILE,
        "12 updates can be applied immediately.\n3 of these are security updates.\n",
    )
    status, detail, _ = access._pending_security_updates(context(tmp_path))
    assert status == FAIL
    assert "3" in detail


def test_ordinary_updates_warn(tmp_path):
    write(tmp_path, access.APT_UPDATES_FILE, "5 updates can be applied immediately.\n")
    assert access._pending_security_updates(context(tmp_path))[0] == WARN


def test_nothing_pending_passes(tmp_path):
    write(tmp_path, access.APT_UPDATES_FILE, "0 updates can be applied immediately.\n")
    assert access._pending_security_updates(context(tmp_path))[0] == PASS


def test_no_notifier_file_off_the_host_is_unknown(tmp_path):
    assert access._pending_security_updates(context(tmp_path))[0] == UNKNOWN
