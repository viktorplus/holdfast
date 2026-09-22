"""Who can get in, and how hard it is.

The sshd parser here is worth a word. OpenSSH takes the FIRST value it obtains
for a keyword, and ``Include`` is expanded where it appears, not at the end.
A configuration that says ``PasswordAuthentication no`` near the bottom can
therefore be overridden by an include near the top - which is how a machine
ends up accepting passwords while its main config plainly forbids them. So the
file is walked in order and includes are expanded in place.
"""

from __future__ import annotations

import fnmatch
import os
import re
import stat
import subprocess
from collections.abc import Iterator
from pathlib import Path

from ..context import Context
from ..model import FAIL, GROUP_ACCESS, PASS, UNKNOWN, WARN
from ..registry import check

SSHD_MANUAL = (
    "sshd -T | grep -Ei 'permitrootlogin|passwordauthentication|port' - this "
    "prints the values in force, not the text of the file"
)

MAX_INCLUDE_DEPTH = 5

# sshd takes "Keyword value", "Keyword=value" and "Keyword = value" alike.
SSHD_LINE = re.compile(r"([^\s=]+)(?:\s*=\s*|\s+)(.+)")

SSHD_EFFECTIVE_KEYS = (
    "permitrootlogin",
    "passwordauthentication",
    "permitemptypasswords",
    "port",
)
SSHD_EFFECTIVE_BAD = {
    "permitrootlogin": {"yes", "true"},
    "passwordauthentication": {"yes", "true"},
    "permitemptypasswords": {"yes", "true"},
}

APT_UPDATES_FILE = "/var/lib/update-notifier/updates-available"


def _expand_include(ctx: Context, pattern: str) -> list[Path]:
    if not pattern.startswith("/"):
        pattern = "/etc/ssh/" + pattern
    directory = ctx.path(os.path.dirname(pattern))
    name = os.path.basename(pattern)
    if not directory.is_dir():
        return []
    return sorted(
        p for p in directory.iterdir() if p.is_file() and fnmatch.fnmatch(p.name, name)
    )


def _sshd_lines(
    ctx: Context, path: Path, depth: int
) -> Iterator[tuple[str, str, Path]]:
    if depth > MAX_INCLUDE_DEPTH:
        return
    text = ctx.read_text(path)
    if text is None:
        return
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parsed = SSHD_LINE.fullmatch(line)
        if parsed is None:
            continue
        keyword, value = parsed.group(1), _unquoted(parsed.group(2).strip())
        if keyword.lower() == "include":
            for pattern in value.split():
                for included in _expand_include(ctx, pattern):
                    yield from _sshd_lines(ctx, included, depth + 1)
            continue
        yield keyword, value, path


def _unquoted(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1]
    return value


def _sshd_entries(ctx: Context) -> Iterator[tuple[str, str, Path, bool]]:
    """Every directive in order, and whether it sits inside a Match block.

    A Match block runs to the end of the configuration, and what it sets
    overrides the global value for the connections it matches.
    """
    main = ctx.path("/etc/ssh/sshd_config")
    if not main.is_file():
        return
    in_match = False
    for keyword, value, source in _sshd_lines(ctx, main, depth=0):
        key = keyword.lower()
        in_match = in_match or key == "match"
        yield key, value, source, in_match


def sshd_directives(ctx: Context) -> dict[str, tuple[str, Path]]:
    """Keyword to (value, the file it was read from), first value wins.

    Only the global values: a Match block's are not what every connection
    gets. The first Match line is kept under "match", so that a configuration
    made of nothing but Match blocks still reads as a configuration.
    """
    found: dict[str, tuple[str, Path]] = {}
    for key, value, source, in_match in _sshd_entries(ctx):
        if not in_match or key == "match":
            found.setdefault(key, (value, source))
    return found


def _enabled_in_match(ctx: Context, key: str) -> Path | None:
    """The file of the first Match block that turns ``key`` on, if any."""
    for found, value, source, in_match in _sshd_entries(ctx):
        if in_match and found == key and value.lower() in ("yes", "true"):
            return source
    return None


@check("ssh_root_login", GROUP_ACCESS, "Root login over SSH", "T1078", "high")
def _ssh_root_login(ctx: Context):
    directives = sshd_directives(ctx)
    if not directives:
        return UNKNOWN, "no sshd_config on the mounted host", SSHD_MANUAL
    matched = _enabled_in_match(ctx, "permitrootlogin")
    if matched:
        return (
            FAIL,
            f"PermitRootLogin yes inside a Match block in {ctx.show(matched)}",
            SSHD_MANUAL,
        )
    if "permitrootlogin" not in directives:
        return (
            PASS,
            (
                "PermitRootLogin is not set, so the OpenSSH default applies "
                "(prohibit-password); confirm the value actually in force"
            ),
            SSHD_MANUAL,
        )
    value, source = directives["permitrootlogin"]
    if value.lower() in ("yes", "true"):
        return FAIL, f"PermitRootLogin {value} in {ctx.show(source)}", SSHD_MANUAL
    return PASS, f"PermitRootLogin {value} in {ctx.show(source)}", SSHD_MANUAL


@check(
    "ssh_password_auth",
    GROUP_ACCESS,
    "Password authentication over SSH",
    "T1110",
    "high",
)
def _ssh_password_auth(ctx: Context):
    directives = sshd_directives(ctx)
    if not directives:
        return UNKNOWN, "no sshd_config on the mounted host", SSHD_MANUAL
    matched = _enabled_in_match(ctx, "passwordauthentication")
    if matched:
        return (
            FAIL,
            f"PasswordAuthentication yes inside a Match block in {ctx.show(matched)}",
            SSHD_MANUAL,
        )
    if "passwordauthentication" not in directives:
        return (
            WARN,
            "PasswordAuthentication is not set; the OpenSSH default is yes",
            SSHD_MANUAL,
        )
    value, source = directives["passwordauthentication"]
    if value.lower() in ("yes", "true"):
        return (
            FAIL,
            f"PasswordAuthentication {value} in {ctx.show(source)}",
            SSHD_MANUAL,
        )
    return PASS, f"PasswordAuthentication {value} in {ctx.show(source)}", SSHD_MANUAL


def authorized_keys_files(ctx: Context) -> list[Path]:
    files = []
    root_keys = ctx.path("/root/.ssh/authorized_keys")
    if root_keys.is_file():
        files.append(root_keys)
    home = ctx.path("/home")
    if home.is_dir():
        for user_dir in sorted(home.iterdir()):
            candidate = user_dir / ".ssh" / "authorized_keys"
            if candidate.is_file():
                files.append(candidate)
    return files


@check(
    "authorized_keys",
    GROUP_ACCESS,
    "Keys in authorized_keys",
    "T1098.004",
    "high",
)
def _authorized_keys(ctx: Context):
    manual = (
        "stat -c '%a %U %n' /root/.ssh/authorized_keys /home/*/.ssh/authorized_keys"
    )
    files = authorized_keys_files(ctx)
    if not files:
        return UNKNOWN, "no authorized_keys file found", manual

    problems: list[str] = []
    total = 0
    for path in files:
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            problems.append(f"{ctx.show(path)}: mode {mode:o}, expected 600")
        text = ctx.read_text(path) or ""
        for number, raw in enumerate(text.splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            total += 1
            fields = line.split()
            algo = next(
                (f for f in fields if f.startswith(("ssh-", "ecdsa-", "sk-"))), ""
            )
            if algo == "ssh-dss":
                problems.append(
                    f"{ctx.show(path)}:{number}: ssh-dss (DSA), an obsolete algorithm"
                )
            index = fields.index(algo) if algo in fields else -1
            if index == -1 or len(fields) <= index + 2:
                problems.append(
                    f"{ctx.show(path)}:{number}: key without a comment, "
                    "so its owner cannot be told apart"
                )
    if problems:
        return FAIL, "; ".join(problems), manual
    return PASS, f"{total} keys, modes and algorithms in order", manual


@check("shell_users", GROUP_ACCESS, "Accounts with a shell", "T1078", "low")
def _shell_users(ctx: Context):
    manual = (
        'getent passwd | awk -F: \'$7 !~ /nologin|false|sync/ {print $1":"$3":"$7}\''
    )
    text = ctx.read_text(ctx.path("/etc/passwd"))
    if text is None:
        return UNKNOWN, "/etc/passwd is not readable", manual
    users = []
    for line in text.splitlines():
        parts = line.split(":")
        if len(parts) < 7:
            continue
        name, uid, shell = parts[0], parts[2], parts[6]
        if re.search(r"nologin|/false|/sync", shell):
            continue
        users.append(f"{name}(uid={uid},{shell})")
    if not users:
        return PASS, "no account has an interactive shell", manual
    return WARN, "interactive shell for: " + ", ".join(users), manual


@check(
    "effective_sshd_config",
    GROUP_ACCESS,
    "Effective sshd configuration",
    "T1078",
    "high",
)
def _effective_sshd_config(ctx: Context):
    """``sshd -T`` resolves Include order and Match blocks; parsing does not.

    Running on the host this is answerable. From inside a container it is not:
    sshd is not there, and would answer for the wrong machine. So it stays an
    honest UNKNOWN rather than a guess dressed as a verdict.
    """
    if not ctx.on_host:
        return (
            UNKNOWN,
            (
                "holdfast is running in a container, where sshd -T would answer "
                "for the container and not the host; see ssh_root_login and "
                "ssh_password_auth for what the file itself says"
            ),
            SSHD_MANUAL,
        )
    try:
        result = subprocess.run(
            ["sshd", "-T"], capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return UNKNOWN, f"sshd -T did not run: {exc}", SSHD_MANUAL
    if result.returncode != 0:
        return (
            UNKNOWN,
            f"sshd -T returned {result.returncode}: {result.stderr.strip()[:200]}",
            SSHD_MANUAL,
        )
    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, _, value = line.strip().partition(" ")
        if key.lower() in SSHD_EFFECTIVE_KEYS:
            values.setdefault(key.lower(), value.strip())
    problems = [
        f"{key} {values[key]}"
        for key, bad in SSHD_EFFECTIVE_BAD.items()
        if values.get(key, "").lower() in bad
    ]
    shown = ", ".join(
        f"{key} {values[key]}" for key in SSHD_EFFECTIVE_KEYS if key in values
    )
    if problems:
        return (
            FAIL,
            "in force: " + ", ".join(problems) + f" (all of it: {shown})",
            SSHD_MANUAL,
        )
    return PASS, "in force: " + shown, SSHD_MANUAL


@check(
    "fail2ban_jail",
    GROUP_ACCESS,
    "fail2ban and the port sshd actually listens on",
    "T1110",
    "medium",
)
def _fail2ban_jail(ctx: Context):
    manual = (
        "fail2ban-client status sshd, after a run of failed logins; the jail "
        "port has to match the port sshd is on"
    )
    jail = ctx.path("/etc/fail2ban/jail.local")
    if not jail.is_file():
        jail = ctx.path("/etc/fail2ban/jail.conf")
    if not jail.is_file():
        return FAIL, "fail2ban is not configured: no jail file", manual
    directives = sshd_directives(ctx)
    ssh_port = directives.get("port", ("22", jail))[0].split()[0]
    text = ctx.read_text(jail) or ""
    ports = re.findall(r"^\s*port\s*=\s*(.+)$", text, re.MULTILINE)
    if not ports:
        return UNKNOWN, f"no port is set in {ctx.show(jail)}", manual
    if any(ssh_port in value for value in ports):
        return PASS, f"the jail watches port {ssh_port}, which is where sshd is", manual
    return (
        FAIL,
        (
            f"sshd is on port {ssh_port} but the jail is set to "
            f"{', '.join(ports)}, so bans do nothing"
        ),
        manual,
    )


@check(
    "pending_security_updates",
    GROUP_ACCESS,
    "Packages left unpatched",
    "T1190",
    "medium",
)
def _pending_security_updates(ctx: Context):
    manual = (
        "apt-get -s upgrade | grep -c '^Inst'; "
        "cat /var/lib/update-notifier/updates-available"
    )
    text = ctx.read_text(ctx.path(APT_UPDATES_FILE))
    if text and text.strip():
        security = re.search(r"(\d+)\s+.*security", text, re.IGNORECASE)
        total = re.search(r"(\d+)\s+", text)
        if security and int(security.group(1)) > 0:
            return FAIL, f"security updates waiting: {security.group(1)}", manual
        if total and int(total.group(1)) > 0:
            return WARN, f"updates available: {total.group(1)}", manual
        return PASS, "nothing left unpatched", manual
    if not ctx.on_host:
        return (
            UNKNOWN,
            (
                "no update-notifier file, and apt-get from inside a container "
                "would answer for the image rather than the host"
            ),
            manual,
        )
    try:
        result = subprocess.run(
            ["apt-get", "-s", "upgrade"],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return UNKNOWN, f"apt-get is not available: {exc}", manual
    if result.returncode != 0:
        return UNKNOWN, f"apt-get -s upgrade returned {result.returncode}", manual
    pending = [line for line in result.stdout.splitlines() if line.startswith("Inst ")]
    security = [line for line in pending if "security" in line.lower()]
    if security:
        return FAIL, f"security updates waiting: {len(security)}", manual
    if pending:
        return WARN, f"updates available: {len(pending)}", manual
    return PASS, "nothing left unpatched", manual
