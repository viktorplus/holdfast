"""What is being served, declared rather than guessed.

Four checks in the Application group only make sense against a particular
stack: which file holds the debug switch, which directories a debug package
installs into, which routes ought to answer 404, which server config declares
the document root. Guessing that from what happens to be on disk is how a
watchdog reports a clean bill of health for an application it never
recognised.

So the stack is a setting. A declared profile is checked properly; an
undeclared one reports UNKNOWN and says so; a profile this release does not
know reports UNKNOWN and says that instead. The three are different facts and
an operator needs to be able to tell them apart.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .context import SKIP_DIRS, Context

DEFAULT_WEB_ROOTS = ("/var/www",)


@dataclass(frozen=True)
class Framework:
    name: str
    env_file: str
    debug_key: str
    environment_key: str
    production_value: str
    # Directories a debug or profiling package installs into, relative to the
    # application root. Their presence in a live deployment is the finding.
    package_dirs: tuple[str, ...]
    # Routes that must answer 404 from outside. Only ever printed, never
    # requested: this tool does not send.
    routes: tuple[str, ...]


@dataclass(frozen=True)
class Server:
    name: str
    config_roots: tuple[str, ...]
    config_suffixes: tuple[str, ...]
    root_directive: re.Pattern = field(compare=False, default=None)  # type: ignore[assignment]
    location_block: re.Pattern = field(compare=False, default=None)  # type: ignore[assignment]
    deny_rule: re.Pattern = field(compare=False, default=None)  # type: ignore[assignment]


FRAMEWORKS: dict[str, Framework] = {
    "laravel": Framework(
        name="laravel",
        env_file=".env",
        debug_key="APP_DEBUG",
        environment_key="APP_ENV",
        production_value="production",
        package_dirs=(
            "vendor/facade/ignition",
            "vendor/spatie/laravel-ignition",
            "vendor/laravel/telescope",
            "vendor/barryvdh/laravel-debugbar",
        ),
        routes=(
            "/_ignition/health-check",
            "/telescope",
        ),
    ),
}

SERVERS: dict[str, Server] = {
    "nginx": Server(
        name="nginx",
        config_roots=("/etc/nginx",),
        config_suffixes=(".conf", ".vhost"),
        root_directive=re.compile(r"^\s*root\s+([^;]+);", re.MULTILINE),
        location_block=re.compile(r"location[^{}]*\{[^{}]*\}", re.IGNORECASE),
        deny_rule=re.compile(r"\bdeny\s+all\b|\breturn\s+40[0-9]\b", re.IGNORECASE),
    ),
}

TRUE_VALUES = frozenset({"1", "true", "on", "yes"})


def framework_name(ctx: Context) -> str:
    return str(ctx.conf("audit.web.framework") or "").strip().lower()


def server_name(ctx: Context) -> str:
    return str(ctx.conf("audit.web.server") or "").strip().lower()


def framework(ctx: Context) -> Framework | None:
    return FRAMEWORKS.get(framework_name(ctx))


def server(ctx: Context) -> Server | None:
    return SERVERS.get(server_name(ctx))


def undeclared(kind: str, name: str) -> str:
    """The two ways a profile can be missing, told apart.

    "Nothing declared" and "declared something this release cannot read" lead
    to different actions, so they must not share a sentence.
    """
    if not name:
        return (
            f"no {kind} is declared, so this cannot be answered; "
            f"set audit.web.{kind} in holdfast.toml"
        )
    return (
        f"{kind} {name!r} is not a profile this release knows, so this is "
        "not being checked rather than being checked and passing"
    )


def server_config_files(ctx: Context) -> list[Path]:
    profile = server(ctx)
    if profile is None:
        return []
    files: list[Path] = []
    for root in profile.config_roots:
        base = ctx.path(root)
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for name in filenames:
                if name.endswith(profile.config_suffixes) or "sites-" in dirpath:
                    files.append(Path(dirpath) / name)
    return sorted(files)


def web_roots(ctx: Context) -> list[str]:
    """Document roots: from the server's own config, then settings, then /var/www.

    Reading them out of the config is the whole point. A guessed root is a
    check that scans the wrong directory and reports it clean.
    """
    profile = server(ctx)
    roots: list[str] = []
    if profile is not None and profile.root_directive is not None:
        for path in server_config_files(ctx):
            text = ctx.read_text(path)
            if text is None:
                continue
            for match in profile.root_directive.finditer(text):
                value = match.group(1).strip().strip('"')
                if value.startswith("/") and value not in roots:
                    roots.append(value)
    if roots:
        return roots
    declared = [str(root) for root in ctx.conf_list("audit.web.roots") if str(root)]
    if declared:
        return declared
    return [root for root in DEFAULT_WEB_ROOTS if ctx.path(root).is_dir()]
