"""Is this the same machine it was last time.

The checks elsewhere ask whether a state is acceptable. These ask a different
question: whether it is the state that was here yesterday. A host that was
fine yesterday and is fine today, but is not the same, is worth a look.

Both carry their evidence forward in the finding's ``data``, which is why the
watchdog keeps a state file at all.
"""

from __future__ import annotations

import gzip
import hashlib
import os
import re
import stat
import subprocess
from collections.abc import Iterable, Iterator
from pathlib import Path

from ..context import Context
from ..model import FAIL, GROUP_INTRUSION, PASS, UNKNOWN, WARN
from ..registry import check
from .access import SSHD_EFFECTIVE_KEYS, sshd_directives
from .network import listening_sockets

SUID_ROOTS = (
    "/home",
    "/opt",
    "/srv",
    "/root",
    "/var/www",
    "/usr/local",
    "/tmp",
    "/var/tmp",
    "/dev/shm",
)

AUTH_LOG_GLOBS = ("auth.log*", "secure*")
ACCEPTED_RE = re.compile(
    r"Accepted (password|publickey|keyboard-interactive\S*) for (\S+) from (\S+)"
)

# Files whose content is not supposed to change between runs. A hash of each
# is carried in the finding so the next run can say what moved; a diff of
# verdicts would not notice sshd_config being edited while still failing for
# the same reason.
WATCHED_FILES = (
    "/etc/ssh/sshd_config",
    "/etc/passwd",
    "/etc/group",
    "/etc/sudoers",
    "/etc/ld.so.preload",
    "/etc/rc.local",
    "/etc/crontab",
    "/etc/hosts",
    "/root/.ssh/authorized_keys",
)
WATCHED_GLOBS = ("/etc/ssh/sshd_config.d/*", "/etc/cron.d/*", "/etc/sudoers.d/*")
MAX_WATCHED = 200


def log_lines(
    ctx: Context, patterns: Iterable[str], max_bytes: int = 8_000_000
) -> Iterator[tuple[str, str]]:
    """Lines from /var/log matching the globs, reading .gz transparently."""
    base = ctx.path("/var/log")
    if not base.is_dir():
        return
    files: list[Path] = []
    for pattern in patterns:
        files.extend(p for p in sorted(base.glob(pattern)) if p.is_file())
    budget = max_bytes
    for path in files:
        opener = gzip.open if path.name.endswith(".gz") else open
        try:
            with opener(path, "rt", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    budget -= len(line)
                    if budget <= 0:
                        return
                    yield path.name, line
        except OSError:
            continue


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


@check(
    "suid_files",
    GROUP_INTRUSION,
    "SUID outside the system paths",
    "T1548.001",
    "high",
)
def _suid_files(ctx: Context):
    manual = (
        "find / -xdev -type f -perm /6000 -printf '%m %u %p\\n' | "
        "grep -v -E '^/(usr/)?(bin|sbin)/'"
    )
    hits: list[str] = []
    truncated = False
    for path, st, is_truncated in ctx.walk(SUID_ROOTS):
        if is_truncated:
            truncated = True
            break
        if st.st_mode & (stat.S_ISUID | stat.S_ISGID):
            hits.append(
                f"{ctx.show(path)} ({stat.S_IMODE(st.st_mode):04o}, uid={st.st_uid})"
            )
    if hits:
        return (
            FAIL,
            (
                "SUID or SGID outside the system directories: "
                + ", ".join(hits[:20])
                + " - this is how a gained privilege is kept"
            ),
            manual,
        )
    if truncated:
        return UNKNOWN, "the scan stopped at its file limit", manual
    return PASS, "no SUID or SGID files outside the system paths", manual


@check("successful_logins", GROUP_INTRUSION, "Successful SSH logins", "T1078", "high")
def _successful_logins(ctx: Context):
    manual = (
        "grep -h 'Accepted ' /var/log/auth.log*; zgrep -h 'Accepted ' "
        "/var/log/auth.log.*.gz; last -F; lastb | wc -l"
    )
    by_password: dict[tuple[str, str], int] = {}
    by_key: dict[tuple[str, str], int] = {}
    saw_log = False
    for _, line in log_lines(ctx, AUTH_LOG_GLOBS):
        saw_log = True
        match = ACCEPTED_RE.search(line)
        if not match:
            continue
        method, user, source = match.group(1), match.group(2), match.group(3)
        bucket = by_password if method == "password" else by_key
        bucket[(user, source)] = bucket.get((user, source), 0) + 1
    if not saw_log:
        return UNKNOWN, "no auth.log on the mounted host", manual

    def render(bucket) -> str:
        return ", ".join(
            f"{user} from {source} x{count}"
            for (user, source), count in sorted(bucket.items(), key=lambda kv: -kv[1])[
                :15
            ]
        )

    if by_password:
        return (
            FAIL,
            (
                "logins accepted BY PASSWORD: "
                + render(by_password)
                + ". A password worked, which means password login was open."
            ),
            manual,
        )
    # A root login is reported however it was made and from wherever.
    root_keys = {key: n for key, n in by_key.items() if key[0] == "root"}
    if root_keys:
        return (
            FAIL,
            (
                "logged in as root: "
                + render(root_keys)
                + ". Nobody logs in as root, key or no key - work from your "
                "own account with sudo."
            ),
            manual,
        )
    # Ordinary key logins from declared addresses are not news. With no list
    # declared they are not reported at all: a page of expected logins every
    # run is how the one unexpected login goes unnoticed.
    allowed = {str(a) for a in ctx.conf_list("audit.ssh.allowed_login_addresses")}
    unexpected = {key: n for key, n in by_key.items() if key[1] not in allowed}
    if allowed and unexpected:
        return (
            WARN,
            "key logins from addresses not on the list: " + render(unexpected),
            manual,
        )
    if by_key:
        return (
            PASS,
            f"{sum(by_key.values())} key logins"
            + (
                ", all from expected addresses"
                if allowed
                else ". audit.ssh.allowed_login_addresses is empty, so a "
                "stranger's address here is indistinguishable from yours - "
                "fill it in."
            ),
            manual,
        )
    return PASS, "no successful logins in the window of log that was read", manual


def watched_files(ctx: Context) -> list[Path]:
    names = list(WATCHED_FILES) + [
        str(name) for name in ctx.conf_list("audit.integrity.watch_files")
    ]
    files = [ctx.path(name) for name in names]
    for pattern in WATCHED_GLOBS:
        directory = ctx.path(os.path.dirname(pattern))
        if directory.is_dir():
            files.extend(sorted(p for p in directory.iterdir() if p.is_file()))
    home = ctx.path("/home")
    if home.is_dir():
        files.extend(sorted(home.glob("*/.ssh/authorized_keys")))
    return [path for path in files if path.is_file()][:MAX_WATCHED]


@check(
    "critical_file_hashes",
    GROUP_INTRUSION,
    "Changes in the files that matter",
    "T1565.001",
    "high",
)
def _critical_file_hashes(ctx: Context):
    manual = (
        "sha256sum /etc/ssh/sshd_config /etc/passwd /etc/sudoers "
        "/root/.ssh/authorized_keys - compare with the previous run"
    )
    files = watched_files(ctx)
    if not files:
        return UNKNOWN, "none of the watched files are present", manual, {}
    current: dict[str, str] = {}
    for path in files:
        try:
            current[ctx.show(path)] = file_sha256(path)
        except OSError:
            continue
    before = ctx.previous_data("critical_file_hashes")
    if not before:
        return (
            PASS,
            (
                f"baseline recorded: {len(current)} files. From the next run "
                "onward any change will be named."
            ),
            manual,
            current,
        )
    changed = [
        name
        for name, digest in current.items()
        if name in before and before[name] != digest
    ]
    added = [name for name in current if name not in before]
    removed = [name for name in before if name not in current]
    parts = []
    if changed:
        parts.append("changed: " + ", ".join(sorted(changed)[:20]))
    if added:
        parts.append("appeared: " + ", ".join(sorted(added)[:20]))
    if removed:
        parts.append("gone: " + ", ".join(sorted(removed)[:20]))
    if parts:
        return (
            FAIL,
            (
                "; ".join(parts) + ". Compare with the logins over the same "
                "period - if this was not you, it was somebody else."
            ),
            manual,
            current,
        )
    return (
        PASS,
        f"{len(current)} files unchanged since the previous run",
        manual,
        current,
    )


def _slice_sshd(ctx: Context) -> str:
    if ctx.on_host:
        try:
            result = subprocess.run(
                ["sshd", "-T"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if result.returncode == 0:
                return ";".join(
                    sorted(
                        line.strip()
                        for line in result.stdout.splitlines()
                        if line.split(" ")[0].lower() in SSHD_EFFECTIVE_KEYS
                    )
                )
        except (OSError, subprocess.SubprocessError):
            pass
    directives = sshd_directives(ctx)
    return ";".join(
        f"{key} {value}"
        for key, (value, _) in sorted(directives.items())
        if key in SSHD_EFFECTIVE_KEYS
    )


def _slice_users(ctx: Context) -> str:
    text = ctx.read_text(ctx.path("/etc/passwd")) or ""
    rows = []
    for line in text.splitlines():
        parts = line.split(":")
        if len(parts) >= 7 and not re.search(r"nologin|/false|/sync", parts[6]):
            rows.append(f"{parts[0]}:{parts[2]}:{parts[6]}")
    return ";".join(sorted(rows))


def _slice_listen(ctx: Context) -> str:
    rows = listening_sockets(ctx)
    if rows is None:
        return ""
    return ";".join(sorted(f"{address}:{port}" for address, port in rows))


def _slice_docker(ctx: Context) -> str:
    rows = []
    for container in ctx.containers():
        name = (container.get("Name") or "").lstrip("/")
        image = (container.get("Config") or {}).get("Image") or ""
        ports = sorted((container.get("NetworkSettings") or {}).get("Ports") or {})
        rows.append(f"{name}|{image}|{','.join(ports)}")
    return ";".join(sorted(rows))


def _watched_cron_files(ctx: Context) -> list[Path]:
    files = []
    crontab = ctx.path("/etc/crontab")
    if crontab.is_file():
        files.append(crontab)
    for name in ("/etc/cron.d", "/var/spool/cron/crontabs"):
        base = ctx.path(name)
        if base.is_dir():
            files.extend(sorted(p for p in base.iterdir() if p.is_file()))
    return files


def _slice_cron(ctx: Context) -> str:
    rows = []
    for path in _watched_cron_files(ctx):
        text = ctx.read_text(path) or ""
        for line in text.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                rows.append(f"{ctx.show(path)}:{stripped}")
    return ";".join(sorted(rows))


CONFIG_SLICES = {
    "sshd": _slice_sshd,
    "users": _slice_users,
    "listen": _slice_listen,
    "docker": _slice_docker,
    "cron": _slice_cron,
}


def _short(value: str, limit: int = 180) -> str:
    return value if len(value) <= limit else value[:limit] + "..."


@check(
    "config_drift",
    GROUP_INTRUSION,
    "Drift in the live configuration",
    "T1565.001",
    "high",
)
def _config_drift(ctx: Context):
    """Snapshots of live configuration, compared with the previous run.

    Which slices are watched is a setting because `docker` and `cron` move on
    every deployment: watching them where deployments are frequent produces a
    finding every day, and a daily finding is not read.
    """
    manual = (
        "sshd -T; getent passwd; ss -Hltn; docker ps --format "
        "'{{.Names}} {{.Image}} {{.Ports}}' - compare with last time"
    )
    wanted = [
        str(name)
        for name in ctx.conf_list("audit.integrity.watch_slices")
        if str(name) in CONFIG_SLICES
    ]
    if not wanted:
        return (
            UNKNOWN,
            "audit.integrity.watch_slices is empty, so drift is not watched",
            manual,
            {},
        )
    current = {name: CONFIG_SLICES[name](ctx) for name in wanted}
    before = ctx.previous_data("config_drift")
    if not before:
        return (
            PASS,
            f"baseline recorded for: {', '.join(wanted)}",
            manual,
            current,
        )
    changed = [
        f"{name}: was {_short(before[name])} -> now {_short(current[name])}"
        for name in wanted
        if name in before and before[name] != current[name]
    ]
    if changed:
        return FAIL, "; ".join(changed), manual, current
    return PASS, f"unchanged slices: {', '.join(wanted)}", manual, current
