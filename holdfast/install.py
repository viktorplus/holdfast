"""The two ways a machine joins the world: alone, or into a collection.

Both write one file. The difference is where the shared values come from -
nowhere, or a profile - and that is the whole of what "adding a machine to the
collection" means. There is no central server to register with.
"""

from __future__ import annotations

import json
import secrets
import socket
import stat
from pathlib import Path
from typing import Any

from .profile import load_profile

CONFIG_NAME = "holdfast.toml"

HEADER = """# holdfast configuration for this machine.
# This file holds one secret: api.token, generated here by `holdfast init`.
# It is therefore worth protecting - that is why it is written 0600 - and it
# does not belong in a backup, a shared repository or a support ticket.
# The other secrets stay out of it, in the environment, as
# HOLDFAST_TELEGRAM_BOT_TOKEN and HOLDFAST_HEARTBEAT_URL.
"""


class InstallError(Exception):
    """The machine cannot be set up as asked."""


def generate_api_token() -> str:
    return secrets.token_hex(32)


def default_host_label() -> str:
    return socket.gethostname() or "unnamed"


def _render(data: dict[str, Any]) -> str:
    def quoted(value: Any) -> str:
        # A TOML basic string is a JSON string, so json.dumps escapes the
        # quotes and backslashes correctly. ensure_ascii=False because this
        # is a file the operator is invited to edit: a Cyrillic host_label
        # belongs there as itself, not as a row of \u escapes.
        return json.dumps(value, ensure_ascii=False)

    def scalar(value: Any) -> str:
        if isinstance(value, list):
            return "[" + ", ".join(quoted(item) for item in value) + "]"
        return quoted(value)

    lines = [HEADER]
    for key, value in data.items():
        if not isinstance(value, dict):
            lines.append(f"{key} = {scalar(value)}")
    for key, value in data.items():
        if isinstance(value, dict):
            lines.append("")
            lines.append(f"[{key}]")
            for inner, inner_value in value.items():
                lines.append(f"{inner} = {scalar(inner_value)}")
    return "\n".join(lines) + "\n"


def _write(config_dir: Path, data: dict[str, Any]) -> Path:
    path = config_dir / CONFIG_NAME
    if path.exists():
        raise InstallError(
            f"{path} already exists; remove it deliberately before running "
            "init again, so that an existing machine is never reset by accident"
        )
    config_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(_render(data), encoding="utf-8")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return path


def _skeleton(host_label: str | None, backup_mode: str = "auto") -> dict[str, Any]:
    return {
        "host_label": host_label or default_host_label(),
        "collection": "",
        "alerts": {"chat_id": ""},
        "offsite": {"remote": ""},
        "encryption": {"recipients": []},
        "backup": {"mode": backup_mode},
        "api": {"token": generate_api_token()},
    }


def init_fresh(
    config_dir: Path, host_label: str | None = None, backup_mode: str = "auto"
) -> Path:
    return _write(config_dir, _skeleton(host_label, backup_mode))


def init_join(
    config_dir: Path,
    profile_path: Path,
    host_label: str | None = None,
    backup_mode: str = "auto",
) -> Path:
    profile = load_profile(profile_path)
    data = _skeleton(host_label, backup_mode)
    data["collection"] = profile.get("collection", "")
    for block in ("alerts", "offsite", "encryption"):
        if block in profile:
            # Merged onto the skeleton, not swapped in for it: a block that
            # grows a machine-only key beside the profile's own one day must
            # not have that key wiped out by a profile that never mentions it.
            data[block] = {**data[block], **profile[block]}
    return _write(config_dir, data)
