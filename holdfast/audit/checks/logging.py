"""Whether there will be anything left to read afterwards.

Whoever gets root clears the local journals. These checks ask two questions
about that: how much history is on this disk right now, and whether any of it
has been copied somewhere the same person cannot reach.
"""

from __future__ import annotations

import json
import re

from ..context import Context, days_ago, fmt_time
from ..model import FAIL, GROUP_LOGS, PASS, UNKNOWN, WARN
from ..registry import check
from .network import DOCKER_OFF

EVIDENCE_LOG_GLOBS = ("auth.log*", "syslog*", "secure*")

LOG_SHIPPER_IMAGES = (
    "promtail",
    "vector",
    "filebeat",
    "fluent",
    "grafana/agent",
    "alloy",
)
RSYSLOG_REMOTE_RE = re.compile(r"^\s*[^#\s].*@@?[\w.\-]+:\d+", re.MULTILINE)


@check("journald_limit", GROUP_LOGS, "A size limit on the journal", "T1565.001", "low")
def _journald_limit(ctx: Context):
    manual = (
        "journalctl --disk-usage; grep -E '^SystemMaxUse' /etc/systemd/journald.conf"
    )
    text = ctx.read_text(ctx.path("/etc/systemd/journald.conf"))
    if text is None:
        return UNKNOWN, "journald.conf was not found", manual
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("SystemMaxUse") and "=" in stripped:
            value = stripped.split("=", 1)[1].strip()
            if value:
                return PASS, f"SystemMaxUse={value}", manual
    return (
        FAIL,
        "SystemMaxUse is not set, so the journal may take up to a tenth of the volume",
        manual,
    )


@check(
    "docker_log_rotation",
    GROUP_LOGS,
    "A size limit on container logs",
    "T1565.001",
    "low",
)
def _docker_log_rotation(ctx: Context):
    manual = "cat /etc/docker/daemon.json"
    if not ctx.docker_enabled:
        return UNKNOWN, DOCKER_OFF, manual
    text = ctx.read_text(ctx.path("/etc/docker/daemon.json"))
    if text is None:
        return FAIL, "no daemon.json: container logs grow without a limit", manual
    try:
        data = json.loads(text)
    except ValueError:
        return UNKNOWN, "daemon.json does not parse as JSON", manual
    options = data.get("log-opts") or {}
    if options.get("max-size"):
        return PASS, f"log-opts: {json.dumps(options)}", manual
    return FAIL, "daemon.json has no log-opts.max-size", manual


@check(
    "log_retention_depth",
    GROUP_LOGS,
    "How far back the logs go",
    "T1070.002",
    "medium",
)
def _log_retention_depth(ctx: Context):
    manual = (
        "ls -la --time-style=long-iso /var/log/auth.log* /var/log/syslog*; "
        "then the rotation rules for those files"
    )
    base = ctx.path("/var/log")
    if not base.is_dir():
        return UNKNOWN, "/var/log is not readable", manual
    files = []
    for pattern in EVIDENCE_LOG_GLOBS:
        files.extend(p for p in base.glob(pattern) if p.is_file())
    if not files:
        return (
            FAIL,
            "no auth.log and no syslog in /var/log: nothing is recorded",
            manual,
        )
    minimum = ctx.conf_int("audit.logs.min_days")
    oldest = min(files, key=lambda p: p.stat().st_mtime)
    depth = days_ago(oldest.stat().st_mtime)
    compressed = [p.name for p in files if p.name.endswith(".gz")]
    detail = (
        f"{depth:.0f} days deep, oldest is {oldest.name} "
        f"({fmt_time(oldest.stat().st_mtime)}); {len(compressed)} compressed"
    )
    if depth < minimum:
        return (
            WARN,
            (
                detail + f". Nothing before {fmt_time(oldest.stat().st_mtime)} is on "
                "this disk any more, so an investigation cannot look further "
                "back than that. Copy the compressed files off before the next "
                "rotation."
            ),
            manual,
        )
    return PASS, detail, manual


@check(
    "remote_log_collector",
    GROUP_LOGS,
    "Logs leaving this machine",
    "T1070.002",
    "high",
)
def _remote_log_collector(ctx: Context):
    manual = (
        "look for a log shipper among the running containers, and for a remote "
        "target in the syslog configuration"
    )
    shippers: list[str] = []
    for container in ctx.containers():
        image = ((container.get("Config") or {}).get("Image") or "").lower()
        if any(name in image for name in LOG_SHIPPER_IMAGES):
            shippers.append((container.get("Name") or "").lstrip("/") or image)
    configs = [ctx.path("/etc/rsyslog.conf")]
    rsyslog_d = ctx.path("/etc/rsyslog.d")
    if rsyslog_d.is_dir():
        configs.extend(sorted(p for p in rsyslog_d.iterdir() if p.is_file()))
    for path in configs:
        text = ctx.read_text(path)
        if text and RSYSLOG_REMOTE_RE.search(text):
            shippers.append(ctx.show(path))
    if shippers:
        return PASS, "logs are sent off the machine: " + ", ".join(shippers), manual
    return (
        FAIL,
        (
            "nothing collects these logs anywhere else. Whoever gets root "
            "clears the local journals, and then there is nothing to "
            "reconstruct the incident from."
        ),
        manual,
    )
