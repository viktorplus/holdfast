"""The register of checks.

Check modules are named here one by one rather than discovered by walking the
package. Discovery hides a typo in a filename as "there are fewer checks
today", which is exactly the kind of breakage nobody notices: the report still
renders, still says PASS, and quietly stopped looking at something.
"""

from __future__ import annotations

import importlib

from .model import GROUPS, SEVERITIES, Check

CHECK_MODULES = (
    "access",
    "secrets",
    "persistence",
    "execution",
    "network",
    "integrity",
    "containers",
    "application",
    "logging",
    "backups",
)

_REGISTRY: dict[str, Check] = {}
_LOADED = False


def check(
    id: str,
    group: str,
    title: str,
    technique: str,
    severity: str = "medium",
):
    """Register one check.

    The arguments are validated at import time on purpose. A check with a
    group nobody renders, or a severity nobody sorts by, would otherwise be
    discovered when the report is already in front of an operator.
    """
    if group not in GROUPS:
        raise RuntimeError(f"check {id!r}: unknown group {group!r}")
    if severity not in SEVERITIES:
        raise RuntimeError(f"check {id!r}: unknown severity {severity!r}")

    def wrap(fn):
        if id in _REGISTRY:
            raise RuntimeError(f"check {id!r} is registered twice")
        _REGISTRY[id] = Check(
            id=id,
            group=group,
            title=title,
            technique=technique,
            severity=severity,
            run=fn,
        )
        return fn

    return wrap


def _load() -> None:
    global _LOADED
    if _LOADED:
        return
    for name in CHECK_MODULES:
        importlib.import_module(f"{__package__}.checks.{name}")
    _LOADED = True


def all_checks() -> list[Check]:
    """Every registered check, in report order."""
    _load()
    return sorted(_REGISTRY.values(), key=lambda c: (GROUPS.index(c.group), c.id))
