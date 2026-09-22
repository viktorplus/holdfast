"""How something arranges to still be here tomorrow.

These checks read times as much as settings. A machine is rarely taken over by
editing a config file in a way a config file can show; what it leaves is
timing - an authorized_keys rewritten seconds after a login, a cron entry that
appeared overnight, a key written into a data file that only ever gets written
by the process that owns it.
"""

from __future__ import annotations

import fnmatch
import os
import re
import stat
from collections.abc import Iterable
from pathlib import Path

from ..context import SKIP_DIRS, Context, days_ago, fmt_time
from ..model import FAIL, GROUP_INTRUSION, PASS, UNKNOWN, WARN
from ..registry import check
from .access import authorized_keys_files

CRON_FILES = ("/etc/crontab",)
CRON_DIRS = (
    "/etc/cron.d",
    "/etc/cron.hourly",
    "/etc/cron.daily",
    "/etc/cron.weekly",
    "/etc/cron.monthly",
    "/var/spool/cron/crontabs",
    "/var/spool/cron",
)
SYSTEMD_DIRS = ("/etc/systemd/system", "/etc/systemd/system/timers.target.wants")

ACCOUNT_FILES = ("/etc/passwd", "/etc/shadow", "/etc/group", "/etc/sudoers")

SUSPICIOUS_CMDLINE = re.compile(
    r"/dev/tcp/|bash -i|sh -i|nc -e|ncat -e|socat .*exec|xmrig|stratum\+tcp"
    r"|nanopool|base64 +-d.*(sh|bash)"
    r"|(curl|wget) [^|]*\| *(ba)?sh",
    re.IGNORECASE,
)

OBFUSCATED_RE = re.compile(
    r"base64\s+(-d|--decode)|base64_decode\s*\(|gzinflate\s*\(|"
    r"str_rot13\s*\(|eval\s*\(\s*(base64_decode|gzinflate|gzuncompress)",
    re.IGNORECASE,
)

# What has no business sitting inside a data store's own file on disk.
DATASTORE_MARKERS = (
    b"authorized_keys",
    b"ssh-rsa",
    b"ssh-ed25519",
    b"/var/spool/cron",
    b"/etc/cron.d",
)
DATASTORE_ROOTS = ("/var/lib/docker/volumes", "/var/lib", "/opt", "/srv", "/home")
DATASTORE_SCAN_BYTES = 32 * 1024 * 1024


@check("ld_so_preload", GROUP_INTRUSION, "Preloaded libraries", "T1574.006", "high")
def _ld_so_preload(ctx: Context):
    manual = "cat /etc/ld.so.preload"
    path = ctx.path("/etc/ld.so.preload")
    if not path.exists():
        return PASS, "the file is absent, which is the ordinary state", manual
    text = (ctx.read_text(path) or "").strip()
    if not text:
        return PASS, "the file exists but is empty", manual
    return (
        FAIL,
        (
            f"ld.so.preload is not empty: {text[:200]} - this is the classic "
            "way to get a library into every process on the machine"
        ),
        manual,
    )


@check("scheduled_jobs", GROUP_INTRUSION, "cron and systemd timers", "T1053", "high")
def _scheduled_jobs(ctx: Context):
    manual = (
        "for u in $(cut -d: -f1 /etc/passwd); do crontab -lu $u; done; "
        "ls -la /etc/cron.d; systemctl list-timers --all"
    )
    entries: list[Path] = []
    for name in CRON_FILES:
        path = ctx.path(name)
        if path.is_file():
            entries.append(path)
    for name in CRON_DIRS + SYSTEMD_DIRS:
        base = ctx.path(name)
        if not base.is_dir():
            continue
        entries.extend(
            path
            for path in sorted(base.iterdir())
            if path.is_file() and not path.is_symlink()
        )
    if not entries:
        return UNKNOWN, "no crontab, no cron.d and no systemd units in sight", manual

    addresses = ctx.indicators().addresses
    recent_days = ctx.recent_days()
    suspicious: list[str] = []
    recent: list[str] = []
    for path in entries:
        text = ctx.read_text(path) or ""
        for number, raw in enumerate(text.splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if (
                SUSPICIOUS_CMDLINE.search(line)
                or OBFUSCATED_RE.search(line)
                or any(address in line for address in addresses)
            ):
                suspicious.append(f"{ctx.show(path)}:{number}: {line[:160]}")
        if days_ago(path.stat().st_mtime) < recent_days:
            recent.append(f"{ctx.show(path)} ({fmt_time(path.stat().st_mtime)})")
    if suspicious:
        return FAIL, "; ".join(suspicious[:10]), manual
    if recent:
        return (
            WARN,
            (
                f"changed in the last {recent_days} days: "
                + ", ".join(recent[:20])
                + ". Nothing matched a signature - confirm these entries are yours."
            ),
            manual,
        )
    return (
        PASS,
        f"{len(entries)} schedule files read, nothing new and nothing matching",
        manual,
    )


@check(
    "authorized_keys_times",
    GROUP_INTRUSION,
    "When authorized_keys was last touched",
    "T1098.004",
    "high",
)
def _authorized_keys_times(ctx: Context):
    manual = (
        "stat -c '%n mtime=%y ctime=%z' /root/.ssh/authorized_keys "
        "/home/*/.ssh/authorized_keys - then line the times up against auth.log"
    )
    files = authorized_keys_files(ctx)
    if not files:
        return UNKNOWN, "no authorized_keys file found", manual
    recent_days = ctx.recent_days()
    forged: list[str] = []
    recent: list[str] = []
    listed: list[str] = []
    for path in files:
        st = path.stat()
        shown = ctx.show(path)
        listed.append(f"{shown}: mtime {fmt_time(st.st_mtime)}")
        # A backdated mtime does not move ctime. A gap between them means the
        # file was touched after it was written.
        if st.st_ctime - st.st_mtime > 86400:
            forged.append(
                f"{shown}: mtime {fmt_time(st.st_mtime)} is "
                f"{(st.st_ctime - st.st_mtime) / 86400:.1f} days older than "
                f"ctime {fmt_time(st.st_ctime)}"
            )
        elif days_ago(st.st_mtime) < recent_days:
            recent.append(f"{shown}: changed {fmt_time(st.st_mtime)}")
    if forged:
        return (
            FAIL,
            (
                "; ".join(forged)
                + " - that is what `touch -t` over a rewritten file looks "
                "like. A chmod and a restore from archive leave the same "
                "trace, so line this up against auth.log rather than taking "
                "it as established."
            ),
            manual,
        )
    if recent:
        return (
            WARN,
            (
                "; ".join(recent)
                + f" - changed within {recent_days} days. Compare with the "
                "login list: a key that appeared right after someone else's "
                "login is a substitution."
            ),
            manual,
        )
    return PASS, "; ".join(listed), manual


@check("account_changes", GROUP_INTRUSION, "Changes to accounts", "T1136", "high")
def _account_changes(ctx: Context):
    manual = (
        "stat -c '%n %y' /etc/passwd /etc/shadow /etc/sudoers; "
        "awk -F: '$3==0 {print $1}' /etc/passwd; ls -la /etc/sudoers.d"
    )
    text = ctx.read_text(ctx.path("/etc/passwd"))
    if text is None:
        return UNKNOWN, "/etc/passwd is not readable", manual
    accounts = ctx.indicators().accounts
    problems: list[str] = []
    for line in text.splitlines():
        parts = line.split(":")
        if len(parts) < 7:
            continue
        name, uid = parts[0], parts[2]
        if uid == "0" and name != "root":
            problems.append(f"{name} has uid 0")
        if name in accounts:
            problems.append(f"account {name} is on the indicator list")

    recent_days = ctx.recent_days()
    recent: list[str] = []
    for name in ACCOUNT_FILES:
        path = ctx.path(name)
        if path.is_file() and days_ago(path.stat().st_mtime) < recent_days:
            recent.append(f"{name} changed {fmt_time(path.stat().st_mtime)}")
    sudoers_d = ctx.path("/etc/sudoers.d")
    if sudoers_d.is_dir():
        for path in sorted(sudoers_d.iterdir()):
            if path.is_file() and days_ago(path.stat().st_mtime) < recent_days:
                recent.append(
                    f"{ctx.show(path)} changed {fmt_time(path.stat().st_mtime)}"
                )
    if problems:
        return FAIL, "; ".join(problems), manual
    if recent:
        return WARN, "; ".join(recent) + " - confirm these changes are yours", manual
    return PASS, "no extra uid 0, and the account files are not fresh", manual


def scan_bytes(path: Path, markers: Iterable[bytes], limit: int) -> set[str]:
    """Chunked substring search with an overlap.

    The overlap matters: a marker straddling a chunk boundary would otherwise
    be missed, and a scanner that misses on a boundary is a scanner that
    reports clean for the wrong reason.
    """
    markers = tuple(markers)
    longest = max(len(marker) for marker in markers)
    found: set[str] = set()
    with open(path, "rb") as handle:
        tail = b""
        read = 0
        while read < limit:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            read += len(chunk)
            window = tail + chunk
            for marker in markers:
                if marker in window:
                    found.add(marker.decode())
            tail = window[-longest:]
    return found


@check(
    "datastore_persistence",
    GROUP_INTRUSION,
    "A data store used to hold a foothold",
    "T1505",
    "high",
)
def _datastore_persistence(ctx: Context):
    """A data store writes its own file; nobody else should be in it.

    A store reachable without a password can be told to write its contents
    wherever the process can reach - an authorized_keys, a cron directory.
    What that leaves behind is a key or a schedule line sitting inside the
    store's own data file, where neither belongs. The check names that class
    of behaviour rather than any one product.
    """
    manual = (
        "strings <data file> | grep -E 'ssh-rsa|authorized_keys|cron' - entries "
        "written through the store's own config end up in its dump"
    )
    patterns = [str(name) for name in ctx.conf_list("audit.datastore.files")]
    if not patterns:
        return UNKNOWN, "no data store file names are configured", manual
    hits: list[str] = []
    # The roots overlap deliberately - /var/lib/docker/volumes is inside
    # /var/lib - so a file reached twice must still be counted once, or the
    # clean result reports more files than the machine has.
    seen: set[str] = set()
    for root in DATASTORE_ROOTS:
        base = ctx.path(root)
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for name in filenames:
                if not any(fnmatch.fnmatch(name, pattern) for pattern in patterns):
                    continue
                path = Path(dirpath) / name
                if str(path) in seen:
                    continue
                seen.add(str(path))
                try:
                    found = scan_bytes(path, DATASTORE_MARKERS, DATASTORE_SCAN_BYTES)
                except OSError:
                    continue
                if found:
                    hits.append(f"{ctx.show(path)}: " + ", ".join(sorted(found)))
    if hits:
        return (
            FAIL,
            (
                "a data store's own file holds strings that do not belong in "
                "one: "
                + "; ".join(hits[:10])
                + " - that is what writing a key or a schedule through the "
                "store looks like afterwards"
            ),
            manual,
        )
    if not seen:
        return UNKNOWN, "no data store files found to read", manual
    return PASS, f"{len(seen)} data store files read, nothing planted in them", manual


def temp_dirs_present(ctx: Context) -> list[str]:
    """Which temporary directories exist on this host."""
    from ..context import TEMP_DIRS

    return [name for name in TEMP_DIRS if ctx.path(name).is_dir()]


def is_script(path: Path, st: os.stat_result) -> bool:
    return bool(stat.S_IMODE(st.st_mode) & 0o111) or path.suffix in (
        ".php",
        ".sh",
        ".pl",
        ".py",
    )
