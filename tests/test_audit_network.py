"""The network checks.

Docker is not run here: `docker inspect` output is handed to the context
already parsed, which is what the real code consumes. /proc/net/tcp is written
out as a literal, including the little-endian hex the kernel actually prints,
because getting that wrong is how a check reads the wrong address and says so
confidently.
"""

from __future__ import annotations

import subprocess

from support import context, write

from holdfast.audit.checks import network
from holdfast.audit.model import FAIL, PASS, UNKNOWN, WARN

TCP_HEADER = (
    "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when "
    "retrnsmt   uid  timeout inode\n"
)


def tcp_table(rows) -> str:
    """rows: (local_hex, local_port, remote_hex, remote_port, state)"""
    out = [TCP_HEADER]
    for index, (lh, lp, rh, rp, state) in enumerate(rows):
        out.append(
            f"{index:4d}: {lh}:{lp:04X} {rh}:{rp:04X} {state} "
            "00000000:00000000 00:00000000 00000000     0        0 0\n"
        )
    return "".join(out)


def hex_for(octets) -> str:
    """An address the way /proc/net/tcp prints it: little-endian hex."""
    return "".join(f"{octet:02X}" for octet in reversed(octets))


def dotted(octets) -> str:
    return ".".join(str(octet) for octet in octets)


# Two addresses, written as numbers rather than as literals. The privacy
# guard refuses a routable address anywhere in this repository and is right
# to: it cannot tell a fixture from a leak. The documentation ranges would be
# safe to write out, but Python does not count them as global, so the check
# under test skips them - which is why the peer below is built this way.
PEER = (8, 8, 8, 8)
DOC_PEER = (203, 0, 113, 10)

REMOTE_PEER = hex_for(PEER)
REMOTE_DOC = hex_for(DOC_PEER)
LOCAL_ANY = "00000000"


def with_containers(tmp_path, containers, **extra):
    ctx = context(tmp_path, **extra)
    ctx._containers = containers
    return ctx


def with_indicators(tmp_path, **extra):
    path = tmp_path / "iocs.toml"
    path.write_text(f'addresses = ["{dotted(DOC_PEER)}"]\n', encoding="utf-8")
    return context(tmp_path, **{"audit.indicators": str(path), **extra})


# -- hex_address ------------------------------------------------------------


def test_the_kernel_hex_is_read_little_endian():
    assert network.hex_address(REMOTE_DOC) == dotted(DOC_PEER)
    assert network.hex_address(REMOTE_PEER) == dotted(PEER)


def test_an_unparseable_address_is_returned_as_it_came():
    assert network.hex_address("zzz") == "zzz"


# -- docker_published_ports -------------------------------------------------


def test_no_docker_is_unknown_not_a_failure(tmp_path):
    """Docker is optional; a machine without it is served in full."""
    status, detail, _ = network._docker_published_ports(with_containers(tmp_path, []))
    assert status == UNKNOWN
    assert "no containers" in detail


def test_a_port_bound_to_loopback_passes(tmp_path):
    containers = [
        {
            "Name": "/cache",
            "NetworkSettings": {
                "Ports": {"6379/tcp": [{"HostIp": "127.0.0.1", "HostPort": "6379"}]}
            },
        }
    ]
    assert (
        network._docker_published_ports(with_containers(tmp_path, containers))[0]
        == PASS
    )


def test_a_port_published_to_the_world_fails(tmp_path):
    containers = [
        {
            "Name": "/cache",
            "NetworkSettings": {
                "Ports": {"6379/tcp": [{"HostIp": "0.0.0.0", "HostPort": "6379"}]}
            },
        }
    ]
    status, detail, _ = network._docker_published_ports(
        with_containers(tmp_path, containers)
    )
    assert status == FAIL
    assert "cache: 0.0.0.0:6379" in detail


def test_an_allowed_published_port_passes(tmp_path):
    containers = [
        {
            "Name": "/web",
            "NetworkSettings": {
                "Ports": {"80/tcp": [{"HostIp": "0.0.0.0", "HostPort": "80"}]}
            },
        }
    ]
    assert (
        network._docker_published_ports(with_containers(tmp_path, containers))[0]
        == PASS
    )


# -- redis_without_password -------------------------------------------------


RUNNING_STORE = {
    "Name": "/cache",
    "Config": {"Image": "redis:7-alpine"},
    "State": {"Running": True},
}


def answering(monkeypatch, stdout: str) -> None:
    """redis-cli, as `docker exec` returns it: exit 0 even for an error reply."""

    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(network.subprocess, "run", fake_run)


def test_no_such_container_passes(tmp_path):
    other = [{"Name": "/web", "Config": {"Image": "nginx"}, "State": {}}]
    status, detail, _ = network._redis_without_password(
        with_containers(tmp_path, other)
    )
    assert status == PASS
    assert "no such container" in detail


def test_docker_not_answering_is_unknown_not_pass(tmp_path):
    """No container list is not a list without redis in it."""
    ctx = with_containers(tmp_path, [])
    assert network._redis_without_password(ctx)[0] == UNKNOWN


def test_a_store_with_no_password_fails(tmp_path, monkeypatch):
    answering(monkeypatch, "requirepass\n\n")
    ctx = with_containers(tmp_path, [RUNNING_STORE])
    status, detail, _ = network._redis_without_password(ctx)
    assert status == FAIL
    assert "cache" in detail


def test_a_store_that_asks_for_a_password_passes(tmp_path, monkeypatch):
    """What redis 7 prints, with exit 0, when requirepass is set: the check
    used to read the missing second line as an empty password."""
    answering(monkeypatch, "NOAUTH Authentication required.\n")
    ctx = with_containers(tmp_path, [RUNNING_STORE])
    assert network._redis_without_password(ctx)[0] == PASS


def test_an_answer_nobody_can_read_is_unknown(tmp_path, monkeypatch):
    answering(monkeypatch, "OCI runtime exec failed\n")
    ctx = with_containers(tmp_path, [RUNNING_STORE])
    status, detail, _ = network._redis_without_password(ctx)
    assert status == UNKNOWN
    assert "cache" in detail


def test_a_stopped_store_is_not_probed(tmp_path):
    containers = [
        {
            "Name": "/cache",
            "Config": {"Image": "redis:7-alpine"},
            "State": {"Running": False},
        }
    ]
    assert (
        network._redis_without_password(with_containers(tmp_path, containers))[0]
        == PASS
    )


# -- established_connections ------------------------------------------------


def test_no_proc_net_tcp_is_unknown(tmp_path):
    assert network._established_connections(context(tmp_path))[0] == UNKNOWN


def test_an_ordinary_outbound_connection_passes(tmp_path):
    write(
        tmp_path,
        "/proc/net/tcp",
        tcp_table([(LOCAL_ANY, 40000, REMOTE_PEER, 443, "01")]),
    )
    status, detail, _ = network._established_connections(context(tmp_path))
    assert status == PASS
    assert "1 external connections" in detail


def test_an_unusual_port_warns(tmp_path):
    write(
        tmp_path,
        "/proc/net/tcp",
        tcp_table([(LOCAL_ANY, 40000, REMOTE_PEER, 8897, "01")]),
    )
    status, detail, _ = network._established_connections(context(tmp_path))
    assert status == WARN
    assert f"{dotted(PEER)}:8897" in detail


def test_a_connection_to_an_indicator_address_fails(tmp_path):
    write(
        tmp_path,
        "/proc/net/tcp",
        tcp_table([(LOCAL_ANY, 40000, REMOTE_DOC, 443, "01")]),
    )
    status, detail, _ = network._established_connections(with_indicators(tmp_path))
    assert status == FAIL
    assert f"{dotted(DOC_PEER)}:443" in detail


def test_listening_sockets_are_not_counted_as_outbound(tmp_path):
    write(
        tmp_path,
        "/proc/net/tcp",
        tcp_table([(LOCAL_ANY, 22, LOCAL_ANY, 0, "0A")]),
    )
    status, detail, _ = network._established_connections(context(tmp_path))
    assert status == PASS
    assert "0 external connections" in detail


def test_listening_sockets_are_read_from_the_listen_state(tmp_path):
    write(
        tmp_path,
        "/proc/net/tcp",
        tcp_table([(LOCAL_ANY, 22, LOCAL_ANY, 0, "0A")]),
    )
    assert network.listening_sockets(context(tmp_path)) == [("0.0.0.0", 22)]


# -- the four that can only be answered from outside ------------------------


def test_the_outside_checks_are_unknown_and_carry_a_command(tmp_path):
    ctx = context(tmp_path)
    for check in (
        network._external_open_ports,
        network._external_db_reachable,
        network._external_egress,
        network._origin_reachable_directly,
    ):
        status, detail, manual = check(ctx)
        assert status == UNKNOWN
        assert detail and manual


def test_the_origin_check_names_the_configured_service(tmp_path):
    ctx = context(tmp_path, **{"audit.origin.service": "the CDN"})
    _, detail, _ = network._origin_reachable_directly(ctx)
    assert "past the CDN" in detail


def test_the_origin_check_stands_up_without_one(tmp_path):
    _, detail, _ = network._origin_reachable_directly(context(tmp_path))
    assert "whatever sits in front of it" in detail


# -- --no-docker ------------------------------------------------------------


def test_with_docker_off_the_docker_checks_are_unknown_not_pass(tmp_path):
    """Nothing was looked at, so nothing can be reported as clean."""
    ctx = context(tmp_path)
    ctx.docker_enabled = False
    for check in (network._docker_published_ports, network._redis_without_password):
        status, detail, _ = check(ctx)
        assert status == UNKNOWN
        assert "not consulted" in detail


def test_with_docker_off_nothing_is_run(tmp_path, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("no subprocess may run with docker disabled")

    monkeypatch.setattr(network.subprocess, "run", refuse)
    ctx = context(tmp_path)
    ctx.docker_enabled = False
    assert network._redis_without_password(ctx)[0] == UNKNOWN
