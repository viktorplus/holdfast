"""Inside the containers, without entering them.

A container's filesystem is reachable from the host through
``/proc/<pid>/root``, so the temporary directory of every running container
can be read without `docker exec`. That matters: a web shell dropped through
the application lands inside the container, not on the host, and a watchdog
that only looks at the host reports a clean machine while it is serving.
"""

from __future__ import annotations

import stat

from ..context import Context, fmt_time
from ..model import FAIL, GROUP_INTRUSION, PASS, UNKNOWN, WARN
from ..registry import check
from .network import DOCKER_OFF
from .persistence import is_script

MAX_ENTRIES_PER_CONTAINER = 200


@check(
    "container_temp",
    GROUP_INTRUSION,
    "The temporary directory inside each container",
    "T1505.003",
    "high",
)
def _container_temp(ctx: Context):
    manual = (
        "for c in $(docker ps -q); do docker exec $c ls -la /tmp; done - or "
        "without exec: ls -la /proc/$(docker inspect -f '{{.State.Pid}}' $c)/root/tmp"
    )
    if not ctx.path("/proc").is_dir():
        return (
            UNKNOWN,
            "the host's /proc is not readable, so neither is any container",
            manual,
        )
    if not ctx.docker_enabled:
        return UNKNOWN, DOCKER_OFF, manual
    containers = ctx.containers()
    if not containers:
        return UNKNOWN, "docker did not answer, or there are no containers", manual
    ignore = ctx.conf_re("audit.temp.ignore")
    indicators = ctx.indicators()
    hits: list[str] = []
    scripts: list[str] = []
    inspected = 0
    for container in containers:
        pid = (container.get("State") or {}).get("Pid")
        if not pid:
            continue
        name = (container.get("Name") or "").lstrip("/")
        base = ctx.path(f"/proc/{pid}/root/tmp")
        if not base.is_dir():
            continue
        inspected += 1
        try:
            children = sorted(base.iterdir())
        except OSError:
            continue
        for path in children[:MAX_ENTRIES_PER_CONTAINER]:
            try:
                st = path.lstat()
            except OSError:
                continue
            if indicators.matches_name(path.name):
                hits.append(f"{name}:/tmp/{path.name}")
            elif ignore and ignore.search(path.name):
                continue
            elif stat.S_ISREG(st.st_mode) and is_script(path, st):
                scripts.append(f"{name}:/tmp/{path.name} ({fmt_time(st.st_mtime)})")
    if hits:
        return (
            FAIL,
            "files from the indicator list in a container's /tmp: " + ", ".join(hits),
            manual,
        )
    if not inspected:
        return UNKNOWN, "no container exposed its /tmp through /proc", manual
    if scripts:
        return (
            WARN,
            (
                "executable files in container temporary directories: "
                + ", ".join(scripts[:20])
                + ". This is where a web shell lands, rather than on the host."
            ),
            manual,
        )
    return PASS, f"read /tmp in {inspected} containers, no scripts in them", manual
