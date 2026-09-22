"""The application in front, which is usually the way in.

None of this is visible from the network layer and all of it is readable from
the host: a debug endpoint that executes code, a database dump left in the
document root, an administrator account that appeared overnight. Every check
here asks the declared web profile what it is looking at, and answers UNKNOWN
rather than guessing when nothing is declared.
"""

from __future__ import annotations

import calendar
import fnmatch
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from ..context import SKIP_DIRS, Context, fmt_time
from ..model import FAIL, GROUP_APP, PASS, UNKNOWN, WARN
from ..registry import check
from ..webprofile import (
    TRUE_VALUES,
    framework,
    framework_name,
    server,
    server_config_files,
    server_name,
    undeclared,
    web_roots,
)
from .secrets import SECRET_ROOTS

CONTENT_MAX_BYTES = 256 * 1024

WEB_ROOT_FORBIDDEN = (
    "*.sql",
    "*.sql.gz",
    "*.zip",
    "*.tar",
    "*.tar.gz",
    "*.tgz",
    "*.bak",
    "*.old",
    ".env",
    "*.dump",
)

# What a deny rule has to cover, as plain substrings. The config text is
# itself full of regular expressions, so matching a regex against it is a
# trap: `\.(zip|tar)` in the file does not contain the literal ".zip".
REQUIRED_DENIES = {
    "environment files": (".env",),
    "version control": (".git",),
    "database dumps": (".sql",),
    "archives": (".zip", ".tar", ".tgz"),
}

CERT_ROOTS = ("/etc/letsencrypt/live", "/etc/ssl/private", "/etc/nginx/ssl")
CERT_NAMES = ("fullchain.pem", "cert.pem")


def _normalise(block: str) -> str:
    r"""Flatten a location's regex so extensions match literally.

    `\.(zip|tar|gz)$` becomes `..zip.tar.gz)$`, which contains ".zip" and
    ".tar" - the same extensions a reader sees, without parsing PCRE.
    """
    return block.replace("\\", "").replace("(", ".").replace("|", ".")


@check("app_debug_mode", GROUP_APP, "Debug mode in production", "T1592", "high")
def _app_debug_mode(ctx: Context):
    manual = "grep -H -E '^APP_(ENV|DEBUG)=' /var/www/*/.env /opt/*/.env"
    profile = framework(ctx)
    if profile is None:
        return UNKNOWN, undeclared("framework", framework_name(ctx)), manual
    problems: list[str] = []
    checked = 0
    truncated = False
    roots = (*web_roots(ctx), *SECRET_ROOTS)
    for path, st, is_truncated in ctx.walk(roots):
        if is_truncated:
            truncated = True
            break
        if path.name != profile.env_file or st.st_size > CONTENT_MAX_BYTES:
            continue
        text = ctx.read_text(path)
        if text is None:
            continue
        values = {}
        for line in text.splitlines():
            key, sep, value = line.partition("=")
            if sep and key in (profile.debug_key, profile.environment_key):
                values[key] = value.strip().strip("\"'").lower()
        if not values:
            continue
        checked += 1
        if values.get(profile.debug_key) in TRUE_VALUES:
            problems.append(
                f"{ctx.show(path)}: {profile.debug_key}="
                f"{values[profile.debug_key]} - a stack trace carrying the "
                "environment is served to the browser"
            )
        environment = values.get(profile.environment_key)
        if environment and environment != profile.production_value:
            problems.append(
                f"{ctx.show(path)}: {profile.environment_key}={environment}"
            )
    if problems:
        return FAIL, "; ".join(problems[:20]), manual
    if truncated:
        return UNKNOWN, "the scan stopped at its file limit", manual
    if not checked:
        return UNKNOWN, f"no {profile.env_file} carrying those keys was found", manual
    return PASS, f"{checked} environment file(s), production and debug off", manual


@check(
    "dev_packages_installed",
    GROUP_APP,
    "Debug packages in a live deployment",
    "T1592",
    "high",
)
def _dev_packages_installed(ctx: Context):
    profile = framework(ctx)
    manual = "list the installed packages and confirm the debug ones are absent"
    if profile is None:
        return UNKNOWN, undeclared("framework", framework_name(ctx)), manual
    manual = (
        "list the installed packages and look for the debug ones, then request "
        + ", ".join(profile.routes)
        + " - all of them should answer 404"
    )
    found: list[str] = []
    for web_root in [*web_roots(ctx), *SECRET_ROOTS]:
        base = ctx.path(web_root)
        if not base.is_dir():
            continue
        for candidate in profile.package_dirs:
            for suffix in ("", ".."):
                target = base / suffix / candidate
                if target.is_dir():
                    found.append(ctx.show(target.resolve()))
    if found:
        return (
            WARN,
            (
                "installed: "
                + ", ".join(sorted(set(found))[:10])
                + ". A debug package is a code path reachable from outside - "
                "confirm its routes answer 404."
            ),
            manual,
        )
    return PASS, "no debug packages in the directories scanned", manual


@check("dev_routes_reachable", GROUP_APP, "Debug routes from outside", "T1190", "high")
def _dev_routes_reachable(ctx: Context):
    profile = framework(ctx)
    if profile is None:
        return (
            UNKNOWN,
            undeclared("framework", framework_name(ctx)),
            (
                "declare audit.web.framework, then request the debug routes "
                "of that stack from outside and confirm each answers 404"
            ),
        )
    routes = "; ".join(f"curl -si https://<domain>{route}" for route in profile.routes)
    return (
        UNKNOWN,
        "a route is answerable only by requesting it, and this audit sends nothing",
        routes + " - all of them should answer 404",
    )


@check(
    "web_root_extra_files",
    GROUP_APP,
    "Files under the document root that should not be there",
    "T1592",
    "high",
)
def _web_root_extra_files(ctx: Context):
    manual = (
        "find <document root> -maxdepth 2 \\( -name '*.sql*' -o -name '*.zip' "
        "-o -name '*.tar*' -o -name '.env' -o -name '.git' \\)"
    )
    roots = web_roots(ctx)
    if not roots:
        return UNKNOWN, "no document root could be determined", manual
    hits: list[str] = []
    truncated = False
    for path, st, is_truncated in ctx.walk(tuple(roots)):
        if is_truncated:
            truncated = True
            break
        if any(fnmatch.fnmatch(path.name, pattern) for pattern in WEB_ROOT_FORBIDDEN):
            hits.append(f"{ctx.show(path)} ({st.st_size // 1024} KB)")
    for root in roots:
        git_dir = ctx.path(root) / ".git"
        if git_dir.is_dir():
            hits.append(ctx.show(git_dir) + " (a .git directory)")
    if hits:
        return (
            FAIL,
            "sitting under the document root and downloadable: " + ", ".join(hits[:20]),
            manual,
        )
    if truncated:
        return UNKNOWN, "the scan stopped at its file limit", manual
    return PASS, f"nothing stray under {', '.join(roots)}", manual


@check(
    "server_denies_sensitive",
    GROUP_APP,
    "The web server refusing to serve dumps and environment files",
    "T1592",
    "high",
)
def _server_denies_sensitive(ctx: Context):
    manual = (
        "grep for the location blocks covering .env, .git, .sql and .zip in "
        "the server configuration, then request https://<domain>/.env - "
        "expect 403 or 404"
    )
    profile = server(ctx)
    if profile is None:
        return UNKNOWN, undeclared("server", server_name(ctx)), manual
    files = server_config_files(ctx)
    if not files:
        return UNKNOWN, f"no {profile.name} configuration found", manual
    blob = "\n".join(ctx.read_text(path) or "" for path in files)
    # Mentioning .env is not the same as refusing to serve it, so only blocks
    # that actually deny are counted.
    denying = [
        _normalise(block)
        for block in profile.location_block.findall(blob)
        if profile.deny_rule.search(block)
    ]
    missing = [
        name
        for name, needles in REQUIRED_DENIES.items()
        if not any(needle in block for block in denying for needle in needles)
    ]
    if missing:
        return (
            FAIL,
            (
                f"the {profile.name} configuration has no rule covering: "
                + ", ".join(missing)
                + ". A file that lands under the document root will be served as it is."
            ),
            manual,
        )
    return PASS, "rules covering environment files, .git, dumps and archives", manual


def _certificate_expiry(path: Path) -> float | None:
    """notAfter as an epoch, via openssl. None when it cannot be read."""
    try:
        result = subprocess.run(
            ["openssl", "x509", "-enddate", "-noout", "-in", str(path)],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or "notAfter=" not in result.stdout:
        return None
    value = result.stdout.split("notAfter=", 1)[1].strip().replace(" GMT", "")
    try:
        return calendar.timegm(time.strptime(value, "%b %d %H:%M:%S %Y"))
    except ValueError:
        return None


@check(
    "tls_certificate",
    GROUP_APP,
    "How long the certificates last",
    "T1588.004",
    "medium",
)
def _tls_certificate(ctx: Context):
    manual = (
        "openssl x509 -enddate -noout -in <certificate>; then whatever renews "
        "them, in its dry-run mode"
    )
    warn_days = ctx.conf_int("audit.tls.warn_days")
    fail_days = max(1, warn_days // 2)
    certificates: list[Path] = []
    for root in CERT_ROOTS:
        base = ctx.path(root)
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for name in filenames:
                if name in CERT_NAMES or name.endswith(".crt"):
                    certificates.append(Path(dirpath) / name)
    if not certificates:
        return UNKNOWN, "no certificates found", manual
    expiring: list[tuple[float, str]] = []
    unreadable: list[str] = []
    horizon: float | None = None
    for path in sorted(certificates):
        expiry = _certificate_expiry(path)
        if expiry is None:
            unreadable.append(ctx.show(path))
            continue
        days = (expiry - time.time()) / 86400
        horizon = days if horizon is None else min(horizon, days)
        if days < warn_days:
            expiring.append(
                (days, f"{ctx.show(path)}: {days:.0f} days left ({fmt_time(expiry)})")
            )
    if expiring and min(days for days, _ in expiring) < fail_days:
        return FAIL, "; ".join(text for _, text in expiring), manual
    if expiring:
        return (
            WARN,
            "; ".join(text for _, text in expiring) + " - check that renewal works",
            manual,
        )
    if horizon is None:
        return (
            UNKNOWN,
            "openssl is not available or the certificates do not parse: "
            + ", ".join(unreadable[:10]),
            manual,
        )
    return (
        PASS,
        f"{len(certificates)} certificates, the nearest expires in {horizon:.0f} days",
        manual,
    )


@check("admin_accounts", GROUP_APP, "Who administers the application", "T1136", "high")
def _admin_accounts(ctx: Context):
    """Runs the command the operator supplied, and compares the result.

    The audit knows no application's schema, so it does not invent a query. It
    runs the configured command, which must print one line per administrator,
    and reports both indicator matches and any change since the previous run.
    An administrator who appeared overnight is the finding.
    """
    manual = (
        "set audit.admin.command to something that prints one line per "
        "administrator, such as id:username:role; check separately that "
        "second-factor authentication is on"
    )
    command = str(ctx.conf("audit.admin.command") or "").strip()
    if not command:
        return (
            UNKNOWN,
            (
                "audit.admin.command is not set: every application has its own "
                "schema and this audit does not guess one"
            ),
            manual,
            {},
        )
    try:
        result = subprocess.run(
            ["sh", "-c", command],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return UNKNOWN, f"the command did not run: {exc}", manual, {}
    if result.returncode != 0:
        # stderr can carry a connection string, so only the code is reported.
        return UNKNOWN, f"the command returned {result.returncode}", manual, {}
    admins = sorted(line.strip() for line in result.stdout.splitlines() if line.strip())
    data = {"admins": admins}
    accounts = ctx.indicators().accounts
    flagged = [row for row in admins if any(name in row for name in accounts)]
    if flagged:
        return (
            FAIL,
            "an account from the indicator list administers this application: "
            + ", ".join(flagged),
            manual,
            data,
        )
    before = (ctx.previous_data("admin_accounts") or {}).get("admins")
    if before is None:
        return (
            PASS,
            (
                f"{len(admins)} administrators. Baseline recorded; from the "
                "next run onward a change will be named."
            ),
            manual,
            data,
        )
    appeared = [row for row in admins if row not in before]
    gone = [row for row in before if row not in admins]
    if appeared or gone:
        parts = []
        if appeared:
            parts.append("appeared: " + ", ".join(appeared[:10]))
        if gone:
            parts.append("gone: " + ", ".join(gone[:10]))
        return FAIL, "; ".join(parts), manual, data
    return PASS, f"{len(admins)} administrators, unchanged", manual, data


@check("http_endpoint", GROUP_APP, "Whether the site answers", "T1499", "high")
def _http_endpoint(ctx: Context):
    """The one check in holdfast that sends anything.

    Everything else reads. This one makes a single GET, and only when a URL is
    configured - which it is not by default, so an unconfigured installation
    still sends nothing at all.
    """
    manual = "curl -si <url> | head -1; set audit.http.url to enable this check"
    url = str(ctx.conf("audit.http.url") or "").strip()
    if not url:
        return UNKNOWN, "audit.http.url is not set, so there is nothing to ask", manual
    if not url.startswith(("http://", "https://")):
        return UNKNOWN, "audit.http.url must start with http:// or https://", manual
    ok_codes = {str(code) for code in ctx.conf_list("audit.http.ok_codes")} or {"200"}
    request = urllib.request.Request(
        url, method="GET", headers={"User-Agent": "holdfast-audit"}
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            code = response.status
    except urllib.error.HTTPError as exc:
        code = exc.code
    except Exception as exc:  # noqa: BLE001
        return FAIL, f"{url} did not answer: {type(exc).__name__}: {exc}", manual
    if str(code) in ok_codes:
        return PASS, f"{url} -> {code}", manual
    return FAIL, f"{url} -> {code}, expected {', '.join(sorted(ok_codes))}", manual
