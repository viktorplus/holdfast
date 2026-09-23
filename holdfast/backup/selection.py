"""Choosing which components a backup actually runs, by mode.

`manual` is what every installation did before 0.3.0: only what the operator
wrote in `[[component]]`. `auto` adds whatever `rule.plan` finds running on
the machine, on top of the same hand-written components - the rule already
refuses to double anything `Declared.of` reports, so the two sources never
collide on what they cover, only on what they choose to call it.

`Selection` is the one thing the engine (task 8) and `--discover` (task 9)
read from here: the components to back up, where each one came from, what
auto-discovery decided not to take, and how much room the auto ones need
before the run starts.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Config
from .components_file import read_tables, written_by_discover
from .model import BackupError, Component
from .registry import components_from_tables
from .rule import Declared, Plan, Skip, parse_exclusions
from .rule import plan as rule_plan

MODES = ("auto", "manual")


@dataclass(frozen=True)
class Selection:
    """What a backup run backs up, and the bookkeeping behind that choice."""

    mode: str
    components: list[Component]
    origins: dict[
        str, str
    ]  # component name -> "holdfast.toml" | "components.toml" | "rule"
    skipped: list[Skip] = field(default_factory=list)
    sizes_mb: dict[str, int] = field(default_factory=dict)  # component name -> MB
    warnings: list[str] = field(default_factory=list)

    @property
    def estimate_mb(self) -> int:
        return sum(self.sizes_mb.values())


def backup_mode(cfg: Config) -> str:
    """ "" means "manual", the way every installation before 0.3.0 behaved."""
    mode = cfg.get("backup.mode", "")
    if mode == "":
        return "manual"
    if mode not in MODES:
        raise BackupError(f'backup.mode is {mode!r}; it is either "auto" or "manual"')
    return mode


def plan_for(cfg: Config, probe: Any, declared: list[Component]) -> Plan:
    """What the rule finds on this machine, given what is already declared."""
    exclude = parse_exclusions(cfg.get("backup.exclude", []))
    try:
        containers = probe.inspect_containers()
        volumes = probe.volumes()
    except BackupError as exc:
        # `holdfast init` defaults to auto, so on a host without Docker this
        # is the first thing the operator sees; say where the switch is.
        if backup_mode(cfg) != "auto":
            raise
        raise BackupError(
            f'{exc} - backup.mode is "auto"; on a machine without Docker set '
            'backup.mode = "manual"'
        ) from None
    return rule_plan(
        containers,
        volumes,
        exclude=exclude,
        declared=Declared.of(declared),
        backup_root=str(cfg.get("backup.root")),
        exists=os.path.exists,
    )


def _size_of(probe: Any, kind: str, where: str) -> int:
    if kind == "volume":
        where = probe.volume_mountpoint(where)
    return probe.directory_size_mb(where)


def _check_collision(name: str, declared_names: set[str], origin: str) -> None:
    if name in declared_names:
        raise BackupError(
            f"two components are both named {name!r}: "
            f"one in holdfast.toml, one from {origin}"
        )


def select(cfg: Config, probe: Any, components_file: Path | None = None) -> Selection:
    """The components a backup run should back up, and why."""
    mode = backup_mode(cfg)
    # Parsed in every mode, right away, so a typo in the list is caught
    # before anything else runs - not only once the mode becomes auto.
    parse_exclusions(cfg.get("backup.exclude", []))

    declared = components_from_tables(cfg.get("component", []))
    declared_names = {c.name for c in declared}
    origins: dict[str, str] = {c.name: "holdfast.toml" for c in declared}

    if mode == "auto":
        found = plan_for(cfg, probe, declared)
        added = components_from_tables(found.tables)
        for component in added:
            _check_collision(component.name, declared_names, "rule")
            origins[component.name] = "rule"
        sizes_mb: dict[str, int] = {}
        for name, kind, where in found.measure:
            sizes_mb[name] = sizes_mb.get(name, 0) + _size_of(probe, kind, where)
        # Left there by manual mode, most likely; an operator who switched
        # should hear that the list stopped counting, not find out later.
        auto_warnings = list(found.warnings)
        # The rule leaves every volume to such a component, anonymous ones
        # included, and the old form archives those by a name no new
        # container will ever carry.
        if Declared.of(declared).every_volume:
            auto_warnings.append(
                "a docker_volume component without volume or container takes "
                "every volume by name, and anonymous volumes archived that way "
                "cannot be restored into a new container; remove it and let "
                'backup.mode = "auto" take the volumes'
            )
        if components_file is not None and components_file.exists():
            auto_warnings.append(
                f"{components_file} is not read in auto mode; delete it, or set "
                'backup.mode = "manual" to use it'
            )
        return Selection(
            mode=mode,
            components=[*declared, *added],
            origins=origins,
            skipped=found.skipped,
            sizes_mb=sizes_mb,
            warnings=auto_warnings,
        )

    added = []
    warnings = []
    if components_file is not None and components_file.exists():
        if written_by_discover(components_file):
            added = components_from_tables(read_tables(components_file))
            for component in added:
                _check_collision(component.name, declared_names, "components.toml")
                origins[component.name] = "components.toml"
        else:
            # Most likely the draft holdfast 0.2's --discover said to redirect
            # here and paste into holdfast.toml. Backing it up as it stands
            # would change what an upgraded machine keeps without anyone
            # deciding to, so it is left out, loudly.
            warnings.append(
                f"{components_file} was not written by holdfast backup "
                "--discover, so it is ignored; merge what you need into "
                "holdfast.toml and delete it, or run holdfast backup "
                "--discover to replace it"
            )
    return Selection(
        mode=mode,
        components=[*declared, *added],
        origins=origins,
        skipped=[],
        sizes_mb={},
        warnings=warnings,
    )
