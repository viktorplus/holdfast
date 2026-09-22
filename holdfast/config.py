"""Configuration in three layers.

Lowest to highest: built-in defaults, this machine's file, then the
environment. The environment carries secrets and nothing else, so a config
file can be read, copied and reviewed without leaking anything.

A collection profile is not one of them. It is install-time material: `init
--join` checks it against the allow-list in `profile.py` and copies its values
into the machine's file once. A `profile.toml` left in the configuration
directory afterwards is not read here, which is what keeps the allow-list from
being a formality - there is no second path by which an unchecked key, or a
secret, becomes configuration.

One rule is worth stating because it is easy to get backwards: a key PRESENT
in a higher layer wins even when its value is empty. An empty string is how an
operator says "stop treating this as normal", and silently falling back to the
layer below would ignore that instruction.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "host_label": "",
    "collection": "",
    "alerts": {"chat_id": "", "bot_token": ""},
    "offsite": {
        # One whole rclone destination, "<remote>:<path>". The machine's own
        # host_label and the snapshot name are appended to it, so two
        # machines in one collection cannot be handed the same directory by
        # a shared profile. Empty means every copy stays on this machine,
        # and backup_offsite_copy says so in the report.
        "remote": "",
        # A copy that has not finished in this long is given up on. It is a
        # setting because the right value is this machine's data over its
        # slowest link, which nothing here can guess - and because an upload
        # with no cap holds nothing but somebody's attention until morning.
        "timeout_minutes": 120,
    },
    # What this machine keeps, declared rather than built in. Every entry is a
    # [[component]] table; the types are listed in holdfast/backup/registry.py.
    "component": [],
    "backup": {
        # Where snapshots land. One directory per run, named by timestamp.
        "root": "/opt/backups",
        # A snapshot older than this many days is deleted, and this means what
        # it says - unlike backup_s1, where it kept them a day longer. Zero
        # turns rotation off. The newest snapshot is never deleted, whatever
        # this says: a machine whose backup broke would otherwise lose its last
        # copy exactly on schedule.
        "retention_days": 7,
        # Refuse to start with less than this free, before anything is written.
        "min_free_gb": 8,
        # Compression. 10 is a fair balance for a VPS; 0 threads means "as many
        # as there are cores".
        "zstd_level": 10,
        "zstd_threads": 0,
        # Held for the length of a run, so the nightly timer and a manual run
        # cannot produce a snapshot each at the same time.
        "lock_file": "/run/holdfast-backup.lock",
    },
    "encryption": {
        # On by default. A typo that empties the recipients must not quietly
        # start producing copies readable by whoever ends up holding the disk,
        # so turning this off is something the operator writes down on purpose.
        "enabled": True,
        # "age" or "gpg". Public keys only: a passphrase would have to live on
        # the same machine as the copies it protects.
        "tool": "age",
        # Public keys, and/or a file holding one per line. The two add up.
        # List several so that losing one key does not make the backups
        # unreadable, and so a new key can be introduced before the old one is
        # retired.
        "recipients": [],
        "recipients_file": "",
    },
    "jobs": {
        # What each job last did, and when it last did it successfully. Not
        # under [backup]: the restore and the offsite copy write here too.
        "dir": "/var/lib/holdfast/jobs",
    },
    "api": {"token": ""},
    "heartbeat": {"url": ""},
    "audit": {
        # The only thing the watchdog writes: the previous report, so that the
        # checks asking "is this the same state as last time" have a baseline.
        "state_dir": "/var/lib/holdfast/audit",
        # A file of campaign indicators, supplied from outside and empty by
        # default. Such a list belongs to an incident, not to a tool.
        "indicators": "",
        # How far back a change to a sensitive file is still worth mentioning.
        "recent_days": 14,
        # A real /opt or /home can hold millions of files. Every scan is
        # bounded, and a scan that hits its limit reports UNKNOWN rather than a
        # clean result it never established.
        "scan": {"max_files": 40000, "max_text_bytes": 1000000},
        "network": {
            # Outbound traffic to these ports is ordinary. Anything else is
            # worth a look, which is the point: a reverse shell or a miner
            # rarely picks a port on this list.
            "allowed_out_ports": [80, 443, 53, 123, 25, 465, 587, 993, 995, 22],
            # Individual peers this machine legitimately reaches on an odd port.
            "allowed_out_addresses": [],
            # Ports whose publication by Docker to the world is expected.
            # Docker publishes past the host firewall, so "ufw is on" does not
            # mean "this port is closed".
            "allowed_published_ports": [80, 443],
        },
        "ssh": {
            # Addresses expected to log in by key. Empty means ordinary key
            # logins are not reported at all - a page of expected logins every
            # run is how the one unexpected login goes unnoticed. A root login
            # and a password login are reported whatever this says.
            "allowed_login_addresses": [],
        },
        "temp": {
            # A file in a temporary directory is interesting while it is new.
            "window_minutes": 60,
            # Names that are ordinary for this machine. Reporting the same
            # twenty session helpers every day trains everyone to skip the
            # section they appear in.
            "ignore": r"sess_|systemd-private|snap\.|pear|php[A-Za-z0-9]{6}$",
        },
        "cpu": {
            # Sustained load that looks like a miner: percent of one core,
            # averaged over the process's whole life, and how long it must
            # have been running before it counts.
            "limit_percent": 90,
            "min_age_seconds": 1800,
            "ignore": (
                "mysqldump|mariadb-dump|tar |gzip|xz|composer|npm|node |"
                "rsync|restic|borg|apt|dpkg|yum|unattended"
            ),
        },
        "integrity": {
            # Extra files under integrity control. sshd_config.d, cron.d,
            # sudoers.d and every authorized_keys are added automatically.
            "watch_files": [],
            # Slices of live configuration watched for drift. docker and cron
            # move on every deployment and are off by default; add them where
            # deployments are rare.
            "watch_slices": ["sshd", "users", "listen"],
        },
        "datastore": {
            # Files a data store writes to disk. A key or a cron line inside
            # one of these got there by someone else's hand.
            "files": ["dump.rdb", "appendonly.aof", "appendonly.aof.*"],
        },
        "web": {
            # What is being served, declared rather than guessed. A stack that
            # is not declared is reported UNKNOWN rather than checked against
            # assumptions that may not hold. This release knows "laravel" and
            # "nginx".
            "framework": "",
            "server": "",
            # Document roots, for when the server is not one holdfast can read
            # the configuration of.
            "roots": [],
        },
        "tls": {
            # Warn this many days before a certificate expires; fail at half.
            "warn_days": 30,
        },
        "logs": {
            # Below this depth an investigation has nothing older to look at.
            "min_days": 30,
        },
        "backup": {
            # Where snapshots land. Read as a directory, so it answers whoever
            # wrote them.
            "root": "",
            # Older than this is a delay; more than twice this is a failure.
            "max_age_hours": 26,
        },
        "http": {
            # The one check that sends a request. Empty means it sends nothing.
            "url": "",
            "ok_codes": [200, 301, 302],
        },
        "admin": {
            # A command printing one line per administrator. Every application
            # has its own schema and this audit does not guess one.
            "command": "",
        },
        "origin": {
            # The CDN or WAF in front of this machine, named only so that the
            # manual instruction can say which one to test against.
            "service": "",
        },
    },
}

ENV_KEYS: dict[str, tuple[str, str]] = {
    "HOLDFAST_TELEGRAM_BOT_TOKEN": ("alerts", "bot_token"),
    "HOLDFAST_API_TOKEN": ("api", "token"),
    "HOLDFAST_HEARTBEAT_URL": ("heartbeat", "url"),
}


class ConfigError(Exception):
    """A configuration file is there but cannot be read."""


def _owned(value: Any) -> Any:
    """A copy of every container, so that nobody else holds one of them.

    DEFAULTS is a module global. Handing one of its sections - or one of its
    lists - out inside a Config means a caller writing through that Config
    edits the defaults, and every Config loaded afterwards in the process
    inherits the write. The scalars inside are immutable; the containers
    around them are what must not be shared.
    """
    if isinstance(value, Mapping):
        return {key: _owned(inner) for key, inner in value.items()}
    if isinstance(value, list):
        return [_owned(item) for item in value]
    return value


def merge(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    """Overlay wins for every key it declares, including empty ones."""
    result: dict[str, Any] = dict(base)
    for key, value in overlay.items():
        below = result.get(key)
        if isinstance(value, Mapping) and isinstance(below, Mapping):
            result[key] = merge(below, value)
        else:
            result[key] = _owned(value)
    return result


def _flatten(values: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    flat: dict[str, Any] = {}
    for key, value in values.items():
        path = f"{prefix}{key}"
        if isinstance(value, Mapping):
            flat.update(_flatten(value, f"{path}."))
        else:
            flat[path] = value
    return flat


def _read_toml(path: Path | None) -> dict[str, Any]:
    if path is None or not path.is_file():
        return {}
    with path.open("rb") as handle:
        try:
            return tomllib.load(handle)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{path} is not valid TOML: {exc}") from exc


def _from_env(env: Mapping[str, str]) -> dict[str, Any]:
    layer: dict[str, Any] = {}
    for name, (section, key) in ENV_KEYS.items():
        if name in env:
            layer.setdefault(section, {})[key] = env[name]
    return layer


@dataclass(frozen=True)
class Config:
    """Merged configuration plus the layer each value came from.

    ``sources`` exists so that ``holdfast config check`` can tell an operator
    not only what a value is but where to change it.
    """

    values: dict[str, Any]
    sources: dict[str, str]

    def get(self, path: str, default: Any = None) -> Any:
        node: Any = self.values
        for part in path.split("."):
            if not isinstance(node, Mapping) or part not in node:
                return default
            node = node[part]
        return node


def load_config(machine: Path | None, env: Mapping[str, str]) -> Config:
    layers = [
        ("defaults", DEFAULTS),
        ("machine", _read_toml(machine)),
        ("env", _from_env(env)),
    ]

    values: dict[str, Any] = {}
    sources: dict[str, str] = {}
    for name, layer in layers:
        values = merge(values, layer)
        for path in _flatten(layer):
            sources[path] = name
    return Config(values=values, sources=sources)
