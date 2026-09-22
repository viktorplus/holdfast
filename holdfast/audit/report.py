"""Running the register, and saying what came back.

The report is grouped the way a person reads it, not the way the checks are
filed. Two rules decide the shape of the text:

UNKNOWN is printed as loudly as FAIL, together with the command to settle it
by hand. A report whose UNKNOWNs are invisible is worse than no report,
because it reads as a clean bill of health for things nobody looked at.

``data`` never reaches the text. It is material for the next run, not for a
reader, and some of it is long.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

import holdfast

from .context import Context
from .model import FAIL, GROUPS, PASS, STATUSES, UNKNOWN, WARN, Finding
from .registry import all_checks


@dataclass(frozen=True)
class Report:
    started_at: str
    host_label: str
    version: str
    findings: list[Finding]

    def as_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "host_label": self.host_label,
            "version": self.version,
            "findings": [finding.as_dict() for finding in self.findings],
        }

    def counts(self) -> dict[str, int]:
        return {
            status: sum(1 for f in self.findings if f.status == status)
            for status in STATUSES
        }


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def run_audit(ctx: Context, only: list[str] | None = None) -> Report:
    findings: list[Finding] = []
    for entry in all_checks():
        if only and entry.id not in only:
            continue
        try:
            result = entry.run(ctx)
            status, detail, manual = result[0], result[1], result[2]
            data = result[3] if len(result) > 3 else {}
        except Exception as exc:  # noqa: BLE001
            # One broken check must not take the other forty-eight with it,
            # and it must not be reported as PASS either.
            status = UNKNOWN
            detail = f"the check raised {type(exc).__name__}: {exc}"
            manual = "this is a defect in holdfast, not a finding about the host"
            data = {}
        findings.append(
            Finding(
                id=entry.id,
                group=entry.group,
                title=entry.title,
                technique=entry.technique,
                severity=entry.severity,
                status=status,
                detail=detail,
                manual=manual,
                data=data,
            )
        )
    return Report(
        started_at=_utc_now(),
        host_label=str(ctx.conf("host_label") or ""),
        version=holdfast.__version__,
        findings=findings,
    )


def render_text(report: Report) -> str:
    counts = report.counts()
    summary = (
        f"{counts[FAIL]} failed, {counts[WARN]} warned, "
        f"{counts[UNKNOWN]} unknown, {counts[PASS]} passed"
    )
    lines = [summary]
    for group in GROUPS:
        in_group = [f for f in report.findings if f.group == group]
        if not in_group:
            continue
        lines.append("")
        lines.append(group)
        for finding in sorted(in_group, key=lambda f: STATUSES.index(f.status)):
            lines.append(f"  [{finding.status}] {finding.title} ({finding.id})")
            if finding.detail:
                lines.append(f"      {finding.detail}")
            if finding.status != PASS and finding.manual:
                lines.append(f"      check by hand: {finding.manual}")
    return "\n".join(lines)


def render_json(report: Report) -> str:
    return json.dumps(report.as_dict(), indent=2, sort_keys=True)


def exit_code(report: Report) -> int:
    return 1 if report.counts()[FAIL] else 0


def render_coverage(checks=None) -> str:
    """The coverage table, and the source of docs/checks.md.

    Documentation generated from the register cannot drift away from the
    register, which is the only reason it is generated at all.
    """
    entries = all_checks() if checks is None else checks
    rows = [(c.id, c.group, c.technique, c.severity) for c in entries]
    headers = ("id", "group", "technique", "severity")
    widths = [
        max(len(headers[i]), max((len(r[i]) for r in rows), default=0))
        for i in range(4)
    ]
    out = [
        "  ".join(headers[i].ljust(widths[i]) for i in range(4)).rstrip(),
        "  ".join("-" * widths[i] for i in range(4)),
    ]
    for row in rows:
        out.append("  ".join(row[i].ljust(widths[i]) for i in range(4)).rstrip())
    out.append("")
    out.append(f"{len(rows)} checks")
    return "\n".join(out)


def render_checks_markdown(checks=None) -> str:
    """docs/checks.md, generated from the register.

    A test compares the file in the repository with this output, so the
    documented coverage cannot quietly diverge from the coverage that exists.
    """
    entries = all_checks() if checks is None else checks
    lines = [
        "# Checks",
        "",
        "Generated from the register by `holdfast audit --coverage --markdown`.",
        "Editing this file by hand achieves nothing: a test compares it with",
        "what the code actually registers.",
        "",
        "| id | group | technique | severity | what it answers |",
        "| --- | --- | --- | --- | --- |",
    ]
    for entry in entries:
        lines.append(
            f"| `{entry.id}` | {entry.group} | {entry.technique} | "
            f"{entry.severity} | {entry.title} |"
        )
    lines.append("")
    lines.append(f"{len(entries)} checks.")
    # No trailing blank line: print() adds the final newline, and the file is
    # produced by redirecting print().
    return "\n".join(lines)


def render_baseline(ctx: Context) -> str:
    """What this machine would call normal, for the operator to cross out.

    The first run on a real machine buries an operator in findings against
    empty allow-lists, which is a known way to make a watchdog unwelcome. This
    turns that into a step of the installation: look at what is here now, and
    declare the part of it that is meant to be here.

    It prints rather than rewriting holdfast.toml. That is a decision, not a
    shortcut: writing TOML back out with the standard library drops every
    comment in the file, including the ones explaining what each setting does.
    Crossing lines out before pasting is the point of the step anyway.
    """
    from .checks.integrity import ACCEPTED_RE, AUTH_LOG_GLOBS, log_lines
    from .checks.network import LOOPBACK_BINDINGS, _sockets

    published: set[str] = set()
    for container in ctx.containers():
        ports = (container.get("NetworkSettings") or {}).get("Ports") or {}
        for bindings in ports.values():
            for binding in bindings or []:
                if binding.get("HostIp") in LOOPBACK_BINDINGS:
                    continue
                if binding.get("HostPort"):
                    published.add(str(binding["HostPort"]))

    outbound: set[str] = set()
    for _, port in _sockets(ctx, "01") or []:
        outbound.add(str(port))

    logins: set[str] = set()
    for _, line in log_lines(ctx, AUTH_LOG_GLOBS):
        match = ACCEPTED_RE.search(line)
        if match and match.group(1) != "password":
            logins.add(match.group(3))

    def numbers(values: set[str]) -> str:
        return ", ".join(sorted(values, key=lambda v: (len(v), v)))

    def strings(values: set[str]) -> str:
        return ", ".join(f'"{value}"' for value in sorted(values))

    if not (published or outbound or logins):
        return (
            "Nothing observed that would need declaring: no published ports, "
            "no outbound connections and no key logins in the log that was "
            "read. Leave the allow-lists as they are."
        )

    lines = [
        "# Observed on this machine. Cross out whatever is not meant to be",
        "# here, then paste what remains into holdfast.toml. Nothing below",
        "# has been written anywhere: that is deliberate, because rewriting",
        "# the file would drop the comments that explain each setting.",
        "",
        "[audit.network]",
        f"allowed_published_ports = [{numbers(published)}]",
        f"allowed_out_ports = [{numbers(outbound)}]",
        "",
        "[audit.ssh]",
        f"allowed_login_addresses = [{strings(logins)}]",
    ]
    return "\n".join(lines)
