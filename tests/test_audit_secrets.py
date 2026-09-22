"""The Secrets group, run against a synthetic host tree.

Four of these tests are not about whether the check finds the problem. They
are about what it says once it has: the report is rendered in a browser and
forwarded to a chat, so a check that quoted its evidence would be a leak
wearing the uniform of a watchdog.
"""

from __future__ import annotations

from support import context, posix_only, write

from holdfast.audit.checks import secrets
from holdfast.audit.model import FAIL, PASS, UNKNOWN

AGE_SECRET = "AGE-SECRET-KEY-1EXAMPLEEXAMPLEEXAMPLEEXAMPLEEXAMPLEEXAMPLEEXAMPL"
TOKEN = "s3cr3t-value-nobody-should-see"


# -- env_permissions --------------------------------------------------------


@posix_only
def test_a_world_readable_env_file_fails(tmp_path):
    write(tmp_path, "/opt/app/.env", "APP_KEY=x\n", mode=0o644)
    status, detail, _ = secrets._env_permissions(context(tmp_path))
    assert status == FAIL
    assert "/opt/app/.env" in detail


@posix_only
def test_an_env_file_only_its_owner_can_read_passes(tmp_path):
    write(tmp_path, "/opt/app/.env", "APP_KEY=x\n", mode=0o600)
    assert secrets._env_permissions(context(tmp_path))[0] == PASS


def test_a_truncated_scan_is_unknown_not_pass(tmp_path):
    for index in range(5):
        write(tmp_path, f"/opt/app/file{index}", "x")
    ctx = context(tmp_path, **{"audit.scan.max_files": 2})
    assert secrets._env_permissions(ctx)[0] == UNKNOWN


# -- private_key_on_host ----------------------------------------------------


def test_private_key_material_is_found(tmp_path):
    write(tmp_path, "/root/backup.key", f"{AGE_SECRET}\n")
    status, detail, _ = secrets._private_key_on_host(context(tmp_path))
    assert status == FAIL
    assert "/root/backup.key" in detail


def test_the_finding_never_carries_the_key_material(tmp_path):
    write(tmp_path, "/root/backup.key", f"{AGE_SECRET}\n")
    _, detail, _ = secrets._private_key_on_host(context(tmp_path))
    assert AGE_SECRET not in detail
    assert "AGE-SECRET-KEY-1" not in detail


def test_a_host_without_key_material_passes(tmp_path):
    write(tmp_path, "/root/notes.txt", "nothing of interest\n")
    assert secrets._private_key_on_host(context(tmp_path))[0] == PASS


def test_the_keys_every_machine_has_are_not_a_finding(tmp_path):
    """sshd's host keys and a user's own SSH key are where they belong. Counted,
    they made this FAIL - and `audit` exit 1 - on every real server."""
    openssh = secrets._banner("OPENSSH PRIVATE KEY")
    write(tmp_path, "/etc/ssh/ssh_host_ed25519_key", f"{openssh}\nAAAA\n")
    write(tmp_path, "/root/.ssh/id_ed25519", f"{openssh}\nAAAA\n")
    assert secrets._private_key_on_host(context(tmp_path))[0] == PASS


def test_a_gpg_key_that_opens_the_backups_is_found(tmp_path):
    pgp = secrets._banner("PGP PRIVATE KEY BLOCK")
    write(tmp_path, "/etc/holdfast/restore.asc", f"{pgp}\nAAAA\n")
    assert secrets._private_key_on_host(context(tmp_path))[0] == FAIL


# -- world_writable ---------------------------------------------------------


@posix_only
def test_a_world_writable_file_fails(tmp_path):
    write(tmp_path, "/etc/app.conf", "x\n", mode=0o666)
    status, detail, _ = secrets._world_writable(context(tmp_path))
    assert status == FAIL
    assert "/etc/app.conf" in detail


@posix_only
def test_ordinary_permissions_pass(tmp_path):
    write(tmp_path, "/etc/app.conf", "x\n", mode=0o644)
    assert secrets._world_writable(context(tmp_path))[0] == PASS


# -- secrets_in_scripts -----------------------------------------------------


def test_a_secret_assigned_in_a_script_fails(tmp_path):
    write(tmp_path, "/opt/app/deploy.sh", f"#!/bin/sh\nAPI_TOKEN={TOKEN}\n")
    status, detail, _ = secrets._secrets_in_scripts(context(tmp_path))
    assert status == FAIL
    assert "/opt/app/deploy.sh:2: API_TOKEN" in detail


def test_the_finding_never_carries_the_assigned_value(tmp_path):
    write(tmp_path, "/opt/app/deploy.sh", f"#!/bin/sh\nAPI_TOKEN={TOKEN}\n")
    _, detail, _ = secrets._secrets_in_scripts(context(tmp_path))
    assert TOKEN not in detail


def test_a_script_reading_its_secret_from_the_environment_passes(tmp_path):
    write(tmp_path, "/opt/app/deploy.sh", '#!/bin/sh\necho "$API_TOKEN"\n')
    assert secrets._secrets_in_scripts(context(tmp_path))[0] == PASS


def test_only_script_suffixes_are_read(tmp_path):
    write(tmp_path, "/opt/app/notes.md", f"API_TOKEN={TOKEN}\n")
    assert secrets._secrets_in_scripts(context(tmp_path))[0] == PASS


# -- plaintext_in_app_logs --------------------------------------------------


def test_a_secret_in_a_log_fails_and_is_counted_not_quoted(tmp_path):
    write(
        tmp_path,
        "/var/www/app/storage/logs/laravel.log",
        f"POST /login password={TOKEN}\nPOST /login password={TOKEN}\n",
    )
    status, detail, _ = secrets._plaintext_in_app_logs(context(tmp_path))
    assert status == FAIL
    assert "2 lines" in detail
    assert TOKEN not in detail


def test_a_clean_log_passes(tmp_path):
    write(tmp_path, "/var/www/app/storage/logs/laravel.log", "GET / 200\n")
    assert secrets._plaintext_in_app_logs(context(tmp_path))[0] == PASS
