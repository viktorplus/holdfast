"""What this machine listens on, and what it talks to.

Four of these checks never return anything but UNKNOWN, and that is the point.
Asking a machine whether its own port is reachable from the internet tells you
nothing about the firewall in front of it, and a vhost bound to one address
answers differently over loopback. Rather than guess, the report names the
command and the vantage point it has to be run from.
"""

from __future__ import annotations

import ipaddress
import subprocess

from ..context import Context
from ..model import FAIL, GROUP_NETWORK, PASS, UNKNOWN, WARN
from ..registry import check

LOOPBACK_BINDINGS = ("127.0.0.1", "::1", "localhost")

# Not "nothing found": nothing was looked at.
DOCKER_OFF = "docker was not consulted, because this run was told not to"


def hex_address(raw: str) -> str:
    """/proc/net/tcp stores addresses as little-endian hex words."""
    try:
        if len(raw) == 8:
            return str(ipaddress.ip_address(bytes.fromhex(raw)[::-1]))
        if len(raw) == 32:
            words = [raw[i : i + 8] for i in range(0, 32, 8)]
            return str(
                ipaddress.ip_address(
                    b"".join(bytes.fromhex(word)[::-1] for word in words)
                )
            )
    except ValueError:
        pass
    return raw


def _sockets(ctx: Context, state: str) -> list[tuple[str, int]] | None:
    """Remote (address, port) pairs in the given /proc/net/tcp state."""
    rows: list[tuple[str, int]] = []
    saw_file = False
    for name in ("/proc/net/tcp", "/proc/net/tcp6"):
        text = ctx.read_text(ctx.path(name))
        if text is None:
            continue
        saw_file = True
        for line in text.splitlines()[1:]:
            fields = line.split()
            if len(fields) < 4 or fields[3] != state:
                continue
            column = fields[2] if state == "01" else fields[1]
            if ":" not in column:
                continue
            address, port = column.rsplit(":", 1)
            try:
                rows.append((hex_address(address), int(port, 16)))
            except ValueError:
                continue
    return rows if saw_file else None


def listening_sockets(ctx: Context) -> list[tuple[str, int]] | None:
    return _sockets(ctx, "0A")


@check(
    "docker_published_ports",
    GROUP_NETWORK,
    "Ports Docker published to the world",
    "T1133",
    "high",
)
def _docker_published_ports(ctx: Context):
    manual = (
        "docker ps --format '{{.Names}} {{.Ports}}'; ss -Hltn - Docker "
        "publishes past the host firewall, so an enabled firewall does not "
        "mean a closed port"
    )
    if not ctx.docker_enabled:
        return UNKNOWN, DOCKER_OFF, manual
    containers = ctx.containers()
    if not containers:
        return UNKNOWN, "docker did not answer, or there are no containers", manual
    allowed = {
        str(port) for port in ctx.conf_list("audit.network.allowed_published_ports")
    }
    exposed: list[str] = []
    for container in containers:
        name = (container.get("Name") or "").lstrip("/")
        ports = (container.get("NetworkSettings") or {}).get("Ports") or {}
        for internal, bindings in ports.items():
            for binding in bindings or []:
                host_ip = binding.get("HostIp", "")
                host_port = binding.get("HostPort", "")
                if host_ip in LOOPBACK_BINDINGS:
                    continue
                if host_port in allowed:
                    continue
                exposed.append(
                    f"{name}: {host_ip or '0.0.0.0'}:{host_port} -> {internal}"
                )
    if exposed:
        return FAIL, "published to the world: " + "; ".join(exposed[:20]), manual
    return PASS, "only the expected ports are published outward", manual


@check(
    "redis_without_password",
    GROUP_NETWORK,
    "A key-value store with no password",
    "T1190",
    "high",
)
def _redis_without_password(ctx: Context):
    manual = "docker exec <container> redis-cli config get requirepass"
    if not ctx.docker_enabled:
        return UNKNOWN, DOCKER_OFF, manual
    containers = ctx.containers()
    if not containers:
        return UNKNOWN, "docker did not answer, or there are no containers", manual
    stores = [
        c
        for c in containers
        if "redis" in ((c.get("Config") or {}).get("Image") or "").lower()
    ]
    if not stores:
        return PASS, "no such container on this machine", manual
    unprotected: list[str] = []
    unanswered: list[str] = []
    for container in stores:
        name = (container.get("Name") or "").lstrip("/")
        if (container.get("State") or {}).get("Running") is not True:
            continue
        try:
            result = subprocess.run(
                ["docker", "exec", name, "redis-cli", "config", "get", "requirepass"],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            unanswered.append(name)
            continue
        # Without a password: the key, then an empty value. With one: redis
        # refuses to answer at all, and redis-cli still exits 0.
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        if lines == ["requirepass"]:
            unprotected.append(name)
        elif not (lines and lines[0].startswith("NOAUTH")):
            unanswered.append(name)
    if unprotected:
        return FAIL, "requirepass is empty in: " + ", ".join(unprotected), manual
    if unanswered:
        return UNKNOWN, "no readable answer from: " + ", ".join(unanswered), manual
    return PASS, "every store found asks for a password", manual


@check(
    "established_connections",
    GROUP_NETWORK,
    "Outbound connections in progress",
    "T1071",
    "medium",
)
def _established_connections(ctx: Context):
    manual = (
        "ss -tnp state established; cat /proc/net/nf_conntrack | "
        "awk '{print $7, $9}' | sort | uniq -c | sort -rn | head"
    )
    rows = _sockets(ctx, "01")
    if rows is None:
        return UNKNOWN, "the host's /proc/net/tcp is not readable", manual
    allowed_ports = {
        str(port) for port in ctx.conf_list("audit.network.allowed_out_ports")
    }
    allowed_addresses = {
        str(address) for address in ctx.conf_list("audit.network.allowed_out_addresses")
    }
    indicators = ctx.indicators().addresses
    hits: list[str] = []
    unusual: set[str] = set()
    external = 0
    for address, port in rows:
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:
            continue
        if address in indicators:
            hits.append(f"{address}:{port}")
            continue
        # Loopback and the container networks carry constant internal traffic;
        # what matters here is where this machine reaches on the internet.
        if not parsed.is_global:
            continue
        external += 1
        if address in allowed_addresses:
            continue
        if str(port) not in allowed_ports:
            unusual.add(f"{address}:{port}")
    if hits:
        return (
            FAIL,
            "connected to an address on the indicator list: " + ", ".join(hits),
            manual,
        )
    if unusual:
        return (
            WARN,
            "outbound on unusual ports: " + ", ".join(sorted(unusual)[:20]),
            manual,
        )
    return PASS, f"{external} external connections, all on expected ports", manual


@check(
    "external_open_ports",
    GROUP_NETWORK,
    "Ports visible from the internet",
    "T1133",
    "high",
)
def _external_open_ports(ctx: Context):
    return (
        UNKNOWN,
        (
            "answerable only from outside: the firewall is not visible from "
            "the machine behind it"
        ),
        (
            "nmap -Pn -p- <external address> - expect 80, 443 and SSH from "
            "admin addresses only"
        ),
    )


@check(
    "external_db_reachable",
    GROUP_NETWORK,
    "Databases reachable from outside",
    "T1133",
    "high",
)
def _external_db_reachable(ctx: Context):
    return (
        UNKNOWN,
        "answerable only from outside",
        (
            "redis-cli -h <address> ping; mysql -h <address>; psql -h <address>"
            " - all three should be refused"
        ),
    )


@check(
    "external_egress",
    GROUP_NETWORK,
    "What the application container can reach",
    "T1041",
    "high",
)
def _external_egress(ctx: Context):
    return (
        UNKNOWN,
        "requires attempting a connection, and this audit sends nothing",
        (
            "docker exec <app> sh -c 'timeout 5 bash -c \"exec "
            "3<>/dev/tcp/192.0.2.1/9999\"' - the connection should not establish"
        ),
    )


@check(
    "origin_reachable_directly",
    GROUP_NETWORK,
    "The origin answering past the service in front of it",
    "T1133",
    "high",
)
def _origin_reachable_directly(ctx: Context):
    """A CDN or WAF in front only helps while nobody can go around it.

    Which service that is varies by installation, so it is a setting rather
    than a name written into the tool. With none configured the instruction
    still stands on its own.
    """
    service = str(ctx.conf("audit.origin.service") or "").strip()
    named = f" past {service}" if service else " past whatever sits in front of it"
    return (
        UNKNOWN,
        f"answerable only from outside, and only by overriding the Host header{named}",
        (
            "curl -si --resolve <domain>:443:<origin address> https://<domain>/ "
            "and curl -si http://<origin address>/ - a request straight to the "
            "address should not serve the site"
        ),
    )
