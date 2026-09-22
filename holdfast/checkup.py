"""Tell the operator what is missing before silence does it for them.

A watchdog with no delivery channel looks healthy from the inside. This is the
check that says so out loud. A missing offsite copy is not this list's
concern - a fresh machine has no address to give it, and backup_offsite_copy
is what reports one missing once there should be a copy to find.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import Config


@dataclass(frozen=True)
class Complaint:
    key: str
    why: str
    fix: str


def complaints(cfg: Config) -> list[Complaint]:
    found: list[Complaint] = []

    if not cfg.get("host_label"):
        found.append(
            Complaint(
                key="host_label",
                why="alerts would arrive unsigned and shared storage would have "
                "nowhere to put this machine's copies",
                fix="run holdfast init, or set host_label in holdfast.toml",
            )
        )

    if not cfg.get("api.token"):
        found.append(
            Complaint(
                key="api.token",
                why="this token is what authenticates callers of the HTTP API, "
                "which this release does not serve yet",
                fix="run holdfast init, or set HOLDFAST_API_TOKEN",
            )
        )

    chat = cfg.get("alerts.chat_id", "")
    token = cfg.get("alerts.bot_token", "")
    if chat and not token:
        found.append(
            Complaint(
                key="HOLDFAST_TELEGRAM_BOT_TOKEN",
                why=f"alerts.chat_id is set to {chat!r} but there is no token, so "
                "nothing can be delivered",
                fix="set HOLDFAST_TELEGRAM_BOT_TOKEN in the environment",
            )
        )
    if token and not chat:
        found.append(
            Complaint(
                key="alerts.chat_id",
                why="a bot token is present but there is no chat to send to",
                fix="set alerts.chat_id in holdfast.toml",
            )
        )

    return found


def format_complaints(items: list[Complaint]) -> str:
    if not items:
        return "configuration is complete"
    lines = [f"{len(items)} thing(s) missing:", ""]
    for item in items:
        lines.append(f"  {item.key}")
        lines.append(f"    why: {item.why}")
        lines.append(f"    fix: {item.fix}")
        lines.append("")
    return "\n".join(lines)
