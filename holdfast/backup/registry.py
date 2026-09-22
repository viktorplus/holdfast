"""Which component types exist, and turning a configuration into components.

The modules are listed by name rather than discovered by walking the directory.
Discovery hides a typo in a file name as "there are fewer types now", which is
exactly the breakage nobody notices - the same reason the check registry lists
its modules too.
"""

from __future__ import annotations

import importlib
from collections.abc import Mapping
from typing import Any

from ..config import Config
from .model import BackupError, Component

COMPONENT_MODULES: tuple[str, ...] = (
    "path",
    "command",
    "postgres",
    "mysql",
    "docker_volume",
)


def component_types() -> dict[str, type[Component]]:
    """Every known type, keyed by the name used in `type = "..."`."""
    types: dict[str, type[Component]] = {}
    for module_name in COMPONENT_MODULES:
        module = importlib.import_module(f".components.{module_name}", __package__)
        cls = module.COMPONENT
        types[cls.type] = cls
    return types


def load_components(
    cfg: Config, types: Mapping[str, type[Component]] | None = None
) -> list[Component]:
    """The components declared in the configuration, in the declared order.

    ``types`` is a seam for the tests, which exercise this parser without
    depending on which types happen to be implemented.
    """
    known = dict(component_types() if types is None else types)
    tables = cfg.get("component", [])
    if not isinstance(tables, list):
        raise BackupError("[[component]] must be a list of tables")

    components: list[Component] = []
    seen: set[str] = set()
    for index, table in enumerate(tables, start=1):
        if not isinstance(table, Mapping):
            raise BackupError(f"component {index} is not a table")
        name = _type_name(table, index)
        if name not in known:
            raise BackupError(
                f"component {index} has an unknown type {name!r}; "
                f"known types: {', '.join(sorted(known))}"
            )
        component = known[name].from_config(dict(table))
        if component.name in seen:
            # The name is the artifact's file name. A second component with the
            # same one costs a copy and says nothing until the restore.
            raise BackupError(f"two components are both named {component.name!r}")
        seen.add(component.name)
        components.append(component)
    return components


def _type_name(table: Mapping[str, Any], index: int) -> str:
    name = table.get("type")
    if not isinstance(name, str) or not name:
        raise BackupError(f"component {index} has no type")
    return name
