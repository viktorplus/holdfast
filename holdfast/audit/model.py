"""What a check is, and what it is allowed to say.

Two vocabularies meet here and are deliberately kept apart. ``group`` is how a
finding is presented to whoever is on duty - "Access", "Network", "Secrets" -
because at three in the morning the question is what is on fire, not which
adversary tactic it belongs to. ``technique`` is the ATT&CK identifier, and it
exists so that coverage can be counted and argued about. A check declares both
and never has to choose.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

PASS = "PASS"
FAIL = "FAIL"
WARN = "WARN"
UNKNOWN = "UNKNOWN"

STATUSES = (FAIL, WARN, UNKNOWN, PASS)

GROUP_ACCESS = "Access"
GROUP_NETWORK = "Network"
GROUP_SECRETS = "Secrets and permissions"
GROUP_APP = "Application"
GROUP_INTRUSION = "Traces of intrusion"
GROUP_LOGS = "Logs and monitoring"
GROUP_BACKUPS = "Backups"

# Report order. Access and network come first because they answer "is the door
# open"; backups come last because they answer "how bad is it if it was".
GROUPS = (
    GROUP_ACCESS,
    GROUP_NETWORK,
    GROUP_SECRETS,
    GROUP_APP,
    GROUP_INTRUSION,
    GROUP_LOGS,
    GROUP_BACKUPS,
)

SEVERITIES = ("low", "medium", "high")


@dataclass(frozen=True)
class Finding:
    """One answer, ready to be rendered or sent.

    ``detail`` is read by people and may be forwarded off the machine, so it
    carries names, paths, counts - never the value of anything secret.
    ``data`` is the opposite: machine-readable material the NEXT run compares
    against, such as file hashes. It is never rendered and never sent.
    """

    id: str
    group: str
    title: str
    technique: str
    severity: str
    status: str
    detail: str = ""
    manual: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "group": self.group,
            "title": self.title,
            "technique": self.technique,
            "severity": self.severity,
            "status": self.status,
            "detail": self.detail,
            "manual": self.manual,
            "data": self.data,
        }


@dataclass(frozen=True)
class Check:
    id: str
    group: str
    title: str
    technique: str
    severity: str
    run: Callable[[Any], tuple]
