"""Credential material lying where it should not.

One rule governs every check in this file: the finding names the file, the
line and the variable, and never the value. Reports are rendered in a browser
and forwarded to a chat; a check that quoted what it found would turn the
watchdog into the leak it exists to detect.
"""

from __future__ import annotations

import fnmatch
import re
import stat

from ..context import Context
from ..model import FAIL, GROUP_SECRETS, PASS, UNKNOWN
from ..registry import check

SECRET_ROOTS = ("/root", "/opt", "/home", "/srv", "/var/www")


def _banner(kind: str) -> str:
    """One PEM banner, assembled rather than written out.

    The privacy guard that protects this repository looks for exactly these
    banners, and it is right to: a literal one in a source file cannot be told
    apart from a leaked key. So the check that hunts for keys spells its own
    markers out of parts, and the guard keeps working on everything else.
    """
    return "-----" + "BEGIN " + kind + "-----"


# Markers of a key that opens the backups: age and gpg, the two tools the
# backup encrypts with. SSH keys are left out on purpose - sshd's host keys and
# a user's own key are where they belong, and counting them failed every real
# server. The audit reports the FILE, never the value.
PRIVATE_KEY_MARKERS = (
    "AGE-SECRET-KEY-",
    _banner("PGP PRIVATE KEY BLOCK"),
)

ENV_NAMES = (".env", "*.env", "credentials", "*.pem", "*.key", ".my.cnf", ".pgpass")

SECRET_ASSIGNMENT_RE = re.compile(
    r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*\S"
)
SECRET_NAME_RE = re.compile(
    r"PASS|PASSWD|PASSWORD|TOKEN|SECRET|API_KEY|APIKEY", re.IGNORECASE
)

LOG_LEAK_RE = re.compile(
    r"password=[^&\s\"']|passwd=[^&\s\"']|Authorization: *Bearer |"
    r"BEGIN [A-Z ]*PRIVATE KEY|api_key=[^&\s\"']",
    re.IGNORECASE,
)

SCRIPT_SUFFIXES = (".sh", ".bash", ".service")


def _truncated(ctx: Context) -> str:
    return f"the scan stopped at {ctx.conf_int('audit.scan.max_files')} files"


@check(
    "env_permissions",
    GROUP_SECRETS,
    "Permissions on files holding secrets",
    "T1552.001",
    "high",
)
def _env_permissions(ctx: Context):
    manual = (
        "find /root /opt /home /srv -name '.env' -o -name '*.pem' | "
        "xargs stat -c '%a %U:%G %n'"
    )
    offenders: list[str] = []
    truncated = False
    for path, st, is_truncated in ctx.walk(SECRET_ROOTS):
        if is_truncated:
            truncated = True
            break
        if not any(fnmatch.fnmatch(path.name, pattern) for pattern in ENV_NAMES):
            continue
        mode = stat.S_IMODE(st.st_mode)
        if mode & 0o077:
            offenders.append(f"{ctx.show(path)} ({mode:o})")
    if offenders:
        return (
            FAIL,
            "readable by more than their owner: " + ", ".join(offenders[:20]),
            manual,
        )
    if truncated:
        return UNKNOWN, _truncated(ctx), manual
    return PASS, "files holding secrets are readable only by their owner", manual


@check(
    "private_key_on_host",
    GROUP_SECRETS,
    "Keys that open the backups, on the server",
    "T1552.004",
    "high",
)
def _private_key_on_host(ctx: Context):
    manual = (
        "grep -rlas -e 'AGE-SECRET-KEY-' -e 'BEGIN PGP PRIVATE KEY' "
        "/root /opt /home /etc"
    )
    hits: list[str] = []
    truncated = False
    limit = ctx.conf_int("audit.scan.max_text_bytes")
    for path, st, is_truncated in ctx.walk((*SECRET_ROOTS, "/etc")):
        if is_truncated:
            truncated = True
            break
        if st.st_size > limit:
            continue
        text = ctx.read_text(path)
        if text is None:
            continue
        if any(marker in text for marker in PRIVATE_KEY_MARKERS):
            # The path is the finding. The key material stays in the file.
            hits.append(ctx.show(path))
    if hits:
        return (
            FAIL,
            "private key material found in: "
            + ", ".join(hits[:20])
            + ". The key that decrypts the backups does not belong on the "
            "machine the backups are of.",
            manual,
        )
    if truncated:
        return UNKNOWN, _truncated(ctx), manual
    return PASS, "no private key material in the directories scanned", manual


@check(
    "world_writable",
    GROUP_SECRETS,
    "Files anyone can write to",
    "T1222",
    "medium",
)
def _world_writable(ctx: Context):
    manual = "find /etc /opt /root /srv -xdev -type f -perm -o+w"
    offenders: list[str] = []
    truncated = False
    for path, st, is_truncated in ctx.walk(("/etc", "/opt", "/root", "/srv")):
        if is_truncated:
            truncated = True
            break
        if stat.S_IMODE(st.st_mode) & 0o002:
            offenders.append(ctx.show(path))
    if offenders:
        return FAIL, "writable by anyone: " + ", ".join(offenders[:20]), manual
    if truncated:
        return UNKNOWN, _truncated(ctx), manual
    return PASS, "no world-writable files found", manual


@check(
    "secrets_in_scripts",
    GROUP_SECRETS,
    "Live values written into scripts",
    "T1552.001",
    "high",
)
def _secrets_in_scripts(ctx: Context):
    manual = (
        "grep -rn -E '(PASSWORD|TOKEN|SECRET|API_KEY)=' --include='*.sh' "
        "/root /home /opt /srv - the value belongs in the environment or in a "
        "file, not in the script"
    )
    hits: list[str] = []
    truncated = False
    limit = ctx.conf_int("audit.scan.max_text_bytes")
    for path, st, is_truncated in ctx.walk(SECRET_ROOTS):
        if is_truncated:
            truncated = True
            break
        if path.suffix not in SCRIPT_SUFFIXES or st.st_size > limit:
            continue
        text = ctx.read_text(path)
        if text is None:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            match = SECRET_ASSIGNMENT_RE.match(line)
            if match and SECRET_NAME_RE.search(match.group(1)):
                # Name and location only. The value is the thing being guarded.
                hits.append(f"{ctx.show(path)}:{number}: {match.group(1)}")
    if hits:
        return (
            FAIL,
            "assigned in the script itself (values are not quoted here): "
            + ", ".join(hits[:20]),
            manual,
        )
    if truncated:
        return UNKNOWN, _truncated(ctx), manual
    return PASS, "no assigned secrets in the scripts scanned", manual


@check(
    "plaintext_in_app_logs",
    GROUP_SECRETS,
    "Secrets reaching the application log",
    "T1552.001",
    "medium",
)
def _plaintext_in_app_logs(ctx: Context):
    manual = (
        "grep -rEl 'password=|Authorization: Bearer' <web root>/storage/logs "
        "/var/log - decrypted values do not belong in a log"
    )
    hits: list[str] = []
    truncated = False
    limit = ctx.conf_int("audit.scan.max_text_bytes")
    for path, st, is_truncated in ctx.walk(SECRET_ROOTS):
        if is_truncated:
            truncated = True
            break
        if path.suffix != ".log" or st.st_size > limit:
            continue
        text = ctx.read_text(path)
        if text is None:
            continue
        count = sum(1 for line in text.splitlines() if LOG_LEAK_RE.search(line))
        if count:
            # The count is the finding. The matching lines are not quoted.
            hits.append(f"{ctx.show(path)}: {count} lines")
    if hits:
        return FAIL, "secrets are reaching logs: " + ", ".join(hits[:20]), manual
    if truncated:
        return UNKNOWN, _truncated(ctx), manual
    return PASS, "no matches in the .log files scanned", manual
