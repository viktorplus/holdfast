"""What is running, and what is lying around waiting to be run."""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

from ..context import TEMP_DIRS, Context, fmt_time
from ..model import FAIL, GROUP_INTRUSION, PASS, UNKNOWN, WARN
from ..registry import check
from .persistence import OBFUSCATED_RE, SUSPICIOUS_CMDLINE, is_script
from .secrets import SECRET_ROOTS

CONTENT_MAX_BYTES = 256 * 1024
SWEEP_ROOTS = (*TEMP_DIRS, *SECRET_ROOTS)

# USER_HZ is 100 on every Linux this runs on.
CLOCK_TICKS = 100.0


def proc_entries(ctx: Context) -> list[tuple[str, str, str]]:
    """(pid, exe, cmdline) for every visible process, from the host's /proc."""
    proc = ctx.path("/proc")
    if not proc.is_dir():
        return []
    out = []
    for entry in sorted(proc.iterdir()):
        if not entry.name.isdigit():
            continue
        try:
            exe = os.readlink(entry / "exe")
        except OSError:
            exe = ""
        try:
            cmdline = (
                (entry / "cmdline")
                .read_bytes()
                .replace(b"\0", b" ")
                .decode("utf-8", "replace")
                .strip()
            )
        except OSError:
            cmdline = ""
        out.append((entry.name, exe, cmdline))
    return out


def file_md5(path: Path, limit: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.md5()
    read = 0
    with open(path, "rb") as handle:
        while read < limit:
            chunk = handle.read(65536)
            if not chunk:
                break
            read += len(chunk)
            digest.update(chunk)
    return digest.hexdigest()


@check(
    "process_from_temp",
    GROUP_INTRUSION,
    "Processes running out of a temporary directory",
    "T1059",
    "high",
)
def _process_from_temp(ctx: Context):
    manual = (
        "ps -eo pid,user,lstart,args; readlink /proc/<PID>/exe - and the same "
        "inside containers through /proc/<PID>/root/tmp"
    )
    entries = proc_entries(ctx)
    if not entries:
        return UNKNOWN, "the host's /proc is not readable", manual
    hits = [
        f"pid={pid} exe={exe}" for pid, exe, _ in entries if exe.startswith(TEMP_DIRS)
    ]
    if hits:
        return FAIL, "; ".join(hits[:20]), manual
    return PASS, f"{len(entries)} processes, none from a temporary directory", manual


@check(
    "suspicious_process",
    GROUP_INTRUSION,
    "Command lines that match a known shape",
    "T1059",
    "high",
)
def _suspicious_process(ctx: Context):
    manual = "ps -eo pid,user,etimes,args | grep -Ei '/dev/tcp/|stratum'"
    entries = proc_entries(ctx)
    if not entries:
        return UNKNOWN, "the host's /proc is not readable", manual
    hits = [
        f"pid={pid}: {cmdline[:160]}"
        for pid, _, cmdline in entries
        if cmdline and SUSPICIOUS_CMDLINE.search(cmdline)
    ]
    if hits:
        return FAIL, "; ".join(hits[:10]), manual
    return PASS, "nothing matching a reverse shell or a miner", manual


@check(
    "executables_in_temp",
    GROUP_INTRUSION,
    "New executables in a temporary directory",
    "T1036",
    "medium",
)
def _executables_in_temp(ctx: Context):
    """Only what is NEW, and only what the host has not declared ordinary.

    Reporting every executable in /tmp on every run means reporting the same
    twenty session helpers forever, and a report that says the same thing
    every day is not read on the day it changes. Hence a time window and an
    exclusion pattern the operator owns.
    """
    window = ctx.conf_int("audit.temp.window_minutes")
    ignore = ctx.conf_re("audit.temp.ignore")
    manual = (
        "find /tmp /var/tmp /dev/shm -type f "
        "\\( -perm -u+x -o -name '*.php' -o -name '*.sh' \\) "
        f"-mmin -{window}"
    )
    cutoff = time.time() - window * 60
    hits: list[str] = []
    older = 0
    for directory in TEMP_DIRS:
        if not ctx.path(directory).is_dir():
            continue
        for path, st, truncated in ctx.walk((directory,)):
            if truncated:
                break
            if not is_script(path, st):
                continue
            if ignore and ignore.search(path.name):
                continue
            if st.st_mtime < cutoff:
                older += 1
                continue
            hits.append(f"{ctx.show(path)} ({fmt_time(st.st_mtime)})")
    tail = f"; {older} older one(s) not listed" if older else ""
    if hits:
        return (
            WARN,
            (
                f"new within {window} minutes: "
                + ", ".join(hits[:20])
                + ". Some of this may be ordinary - check the owner and the "
                "time." + tail
            ),
            manual,
        )
    return PASS, f"no new executables within {window} minutes{tail}", manual


@check(
    "obfuscated_payload",
    GROUP_INTRUSION,
    "Payloads written to be unreadable",
    "T1027",
    "high",
)
def _obfuscated_payload(ctx: Context):
    """A class of shape, not one command.

    The original of this check looked for `base64 -d` alone, which is a single
    spelling of a general idea: put the payload through an encoding so that
    reading the file tells you nothing. Several encodings are recognised here,
    and the verdict stays WARN, because every one of these appears in
    legitimate code. The right instruction is "decode it and look", not "you
    are compromised".
    """
    manual = (
        "grep -rlas -e 'base64 -d' -e 'base64_decode' -e 'gzinflate' /tmp /opt /var/www"
    )
    hits: list[str] = []
    truncated = False
    for path, st, is_truncated in ctx.walk(SWEEP_ROOTS):
        if is_truncated:
            truncated = True
            break
        if st.st_size > CONTENT_MAX_BYTES:
            continue
        text = ctx.read_text(path)
        if text is None or not OBFUSCATED_RE.search(text):
            continue
        hits.append(f"{ctx.show(path)} ({fmt_time(st.st_mtime)})")
    if hits:
        return (
            WARN,
            (
                "an encoded payload shape appears in: "
                + ", ".join(hits[:20])
                + ". The shape is legitimate on its own - decode the payload "
                "and check the owner and the time."
            ),
            manual,
        )
    if truncated:
        return UNKNOWN, "the scan stopped at its file limit", manual
    return PASS, "no encoded payload shapes in the directories scanned", manual


@check(
    "known_indicators",
    GROUP_INTRUSION,
    "Indicators from a supplied list",
    "T1204.002",
    "high",
)
def _known_indicators(ctx: Context):
    """Sweep for a list that arrives from outside.

    holdfast ships no indicators. A list of them belongs to one incident and
    goes stale the moment the campaign ends, and a stale indicator nobody owns
    is worse than none at all.
    """
    manual = (
        "point audit.indicators at a file of addresses, file names, checksums "
        "and account names; see docs/configuration.md for its shape"
    )
    indicators = ctx.indicators()
    if indicators.empty:
        return (
            UNKNOWN,
            "no indicator list is configured, so there is nothing to match against",
            manual,
        )
    by_name: list[str] = []
    by_hash: list[str] = []
    by_address: list[str] = []
    truncated = False
    for path, st, is_truncated in ctx.walk(SWEEP_ROOTS):
        if is_truncated:
            truncated = True
            break
        if indicators.matches_name(path.name):
            by_name.append(f"{ctx.show(path)} ({fmt_time(st.st_mtime)})")
            continue
        if st.st_size > CONTENT_MAX_BYTES:
            continue
        if indicators.md5:
            try:
                digest = file_md5(path)
            except OSError:
                digest = ""
            if digest in indicators.md5:
                by_hash.append(
                    f"{ctx.show(path)} = md5 {digest} ({indicators.md5[digest]})"
                )
                continue
        if not indicators.addresses:
            continue
        text = ctx.read_text(path)
        if text is None:
            continue
        found = [address for address in indicators.addresses if address in text]
        if found:
            by_address.append(f"{ctx.show(path)}: {', '.join(found)}")
    if by_name or by_hash or by_address:
        parts = []
        if by_name:
            parts.append("by name: " + ", ".join(by_name[:10]))
        if by_hash:
            parts.append("by checksum: " + ", ".join(by_hash[:10]))
        if by_address:
            parts.append("by address: " + ", ".join(by_address[:10]))
        return FAIL, "; ".join(parts) + ". Delete nothing - this is evidence.", manual
    if truncated:
        return UNKNOWN, "the scan stopped at its file limit", manual
    # "Clean" without saying what was looked for is worth nothing.
    return PASS, "no matches. Checked: " + indicators.describe(), manual


@check(
    "cpu_anomaly",
    GROUP_INTRUSION,
    "Load that has been sustained for hours",
    "T1496",
    "medium",
)
def _cpu_anomaly(ctx: Context):
    """Average CPU over the process's whole life, not an instantaneous reading.

    A miner pegs a core for hours; a backup pegs one for minutes. Averaging
    over lifetime separates them without taking a second sample, and the
    minimum age keeps ordinary bursts out.
    """
    limit = ctx.conf_int("audit.cpu.limit_percent")
    min_age = ctx.conf_int("audit.cpu.min_age_seconds")
    ignore = ctx.conf_re("audit.cpu.ignore")
    manual = (
        "ps -eo pid,etimes,pcpu,user,args --sort=-pcpu | head - sustained load "
        f"above {limit}% for longer than {min_age}s"
    )
    uptime_text = ctx.read_text(ctx.path("/proc/uptime"))
    if uptime_text is None:
        return UNKNOWN, "/proc/uptime is not readable", manual
    try:
        uptime = float(uptime_text.split()[0])
    except (ValueError, IndexError):
        return UNKNOWN, "/proc/uptime does not parse", manual
    hits: list[str] = []
    for pid, _, cmdline in proc_entries(ctx):
        stat_text = ctx.read_text(ctx.path(f"/proc/{pid}/stat"))
        if not stat_text:
            continue
        # The comm field can contain spaces and parentheses, so split after it.
        _, _, rest = stat_text.partition(") ")
        fields = rest.split()
        if len(fields) < 20:
            continue
        try:
            cpu_seconds = (int(fields[11]) + int(fields[12])) / CLOCK_TICKS
            age = uptime - int(fields[19]) / CLOCK_TICKS
        except ValueError:
            continue
        if age < min_age or age <= 0:
            continue
        percent = cpu_seconds / age * 100
        if percent < limit:
            continue
        if ignore and cmdline and ignore.search(cmdline):
            continue
        hits.append(
            f"pid={pid} {percent:.0f}% over {age / 3600:.1f}h: {(cmdline or '?')[:120]}"
        )
    if not hits:
        return PASS, f"nothing above {limit}% for longer than {min_age}s", manual
    return (
        WARN,
        "; ".join(hits[:5]) + ". This may be a scheduled job - check audit.cpu.ignore.",
        manual,
    )
