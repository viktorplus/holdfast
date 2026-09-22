"""PostgreSQL, as logical dumps rather than files on disk.

A dump can be loaded into a different minor version, a different machine and a
different filesystem; a copy of the data directory can be loaded into almost
nothing. That is the whole reason this type exists instead of pointing a `path`
component at /var/lib/postgresql.

One artifact per database, plus one for the globals - roles and tablespaces
live outside any single database, and a restore without them produces a
database nobody has permission to use.

holdfast never sees a password. libpq reads its own password file, and this
only ever passes the path to it, which is not a secret.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from typing import Any

from ..model import Artifact, BackupError, BuildContext, Component

# A running PostgreSQL is asked which databases it has. Templates are excluded
# because restoring one is not meaningful; the order makes the snapshot's
# artifact list stable between nights.
LIST_DATABASES = (
    "select datname from pg_database where datistemplate = false order by datname;"
)

# The source validates against this same set, and the reason is worth keeping:
# a name with a slash writes the artifact outside its directory, and a control
# character breaks the manifest outright. Better to find out now, with the
# database named, than during a recovery.
DATABASE_NAME = re.compile(r"^[A-Za-z0-9_.][A-Za-z0-9_.-]*$")

# What psql says when the server wants a password it was not given: none at
# all, or a wrong one from somewhere other than a password file.
PASSWORD_REFUSALS = ("no password supplied", "password authentication failed")

# pg_dump -Fc writes its own compressed format, so nothing is stacked on top.
DUMP_SIGNATURE = 'magic=$(head -c 5); cat >/dev/null; [ "$magic" = PGDMP ]'


def _join(prefix: str, rest: str) -> str:
    """Without a container there is no prefix, and no leading space either."""
    return f"{prefix} {rest}" if prefix else rest


@dataclass(frozen=True)
class PostgresComponent(Component):
    user: str = ""
    container: str = ""
    databases: tuple[str, ...] = ("*",)
    globals: bool = True
    defaults_file: str = ""

    type = "postgres"

    @classmethod
    def from_config(cls, table: dict[str, Any]) -> PostgresComponent:
        name = cls._name(table)
        listed = table.get("databases", ["*"])
        if not isinstance(listed, list):
            raise BackupError(f"component {name!r}: databases has to be a list")
        return cls(
            name=name,
            user=cls._required(table, "user", name),
            container=str(table.get("container") or ""),
            databases=tuple(str(item) for item in listed) or ("*",),
            globals=bool(table.get("globals", True)),
            defaults_file=str(table.get("defaults_file") or ""),
        )

    def artifacts(self, ctx: BuildContext) -> list[Artifact]:
        self._require_container(ctx)
        zstd = f"zstd -T{ctx.zstd_threads} -{ctx.zstd_level} -q"

        found: list[Artifact] = []
        if self.globals:
            found.append(
                Artifact(
                    name=f"postgres/{self.name}-globals.sql.zst",
                    produce=_join(
                        self._line(),
                        f"pg_dumpall {self._auth()} --globals-only | {zstd}",
                    ),
                    recipe={
                        "type": "pg_globals",
                        "container": self.container,
                        "user": self.user,
                    },
                    check="zstd -dc >/dev/null",
                )
            )
        for database in self._databases(ctx):
            found.append(
                Artifact(
                    name=f"postgres/{self.name}-{database}.dump",
                    produce=_join(
                        self._line(),
                        f"pg_dump {self._auth()} -d {shlex.quote(database)} -Fc -Z6",
                    ),
                    recipe={
                        "type": "pg_database",
                        "container": self.container,
                        "user": self.user,
                        "database": database,
                    },
                    # Reading the signature and then draining the rest. Without
                    # the drain the decrypter upstream dies of SIGPIPE, and a
                    # healthy snapshot is reported as corrupt.
                    check=DUMP_SIGNATURE,
                )
            )
        return found

    def describe(self) -> dict[str, Any]:
        return {
            **super().describe(),
            "container": self.container,
            "user": self.user,
            "databases": list(self.databases),
            "globals": self.globals,
            "defaults_file": self.defaults_file,
        }

    def containers(self) -> list[str]:
        return [self.container] if self.container else []

    # -- the parts of a command line ---------------------------------------

    def _line(self) -> str:
        """What goes in front of pg_dump in the shell line."""
        if self.container:
            passfile = (
                f"-e PGPASSFILE={shlex.quote(self.defaults_file)} "
                if self.defaults_file
                else ""
            )
            return f"docker exec {passfile}{shlex.quote(self.container)}".strip()
        if self.defaults_file:
            return f"PGPASSFILE={shlex.quote(self.defaults_file)}"
        return ""

    def _auth(self) -> str:
        return f"-U {shlex.quote(self.user)}"

    def _argv(self, *rest: str) -> tuple[list[str], dict[str, str]]:
        """The same command as an argument list, for asking rather than piping."""
        env: dict[str, str] = {}
        if self.container:
            prefix = ["docker", "exec"]
            if self.defaults_file:
                prefix += ["-e", f"PGPASSFILE={self.defaults_file}"]
            prefix.append(self.container)
        else:
            prefix = []
            if self.defaults_file:
                env["PGPASSFILE"] = self.defaults_file
        return prefix + list(rest), env

    # -- asking the machine -------------------------------------------------

    def _require_container(self, ctx: BuildContext) -> None:
        if not self.container:
            return
        if not ctx.probe.container_running(self.container):
            raise BackupError(
                f"component {self.name!r}: the container "
                f"{self.container!r} is not running, so there is nothing to dump"
            )

    def _databases(self, ctx: BuildContext) -> list[str]:
        if list(self.databases) != ["*"]:
            found = list(self.databases)
        else:
            argv, env = self._argv(
                "psql", "-U", self.user, "-d", "postgres", "-At", "-c", LIST_DATABASES
            )
            try:
                answer = ctx.probe.capture(
                    argv,
                    what=f"listing the databases of {self.name!r}",
                    env=env or None,
                )
            except BackupError as error:
                # The --discover draft leaves defaults_file commented out, and
                # psql's refusal does not say which line of holdfast.toml to
                # change.
                if self.defaults_file or not any(
                    refusal in str(error) for refusal in PASSWORD_REFUSALS
                ):
                    raise
                raise BackupError(
                    f"{error} - this component has no defaults_file, so psql "
                    "had no password file to read; set defaults_file in its "
                    "[[component]] table"
                ) from None
            found = [line.strip() for line in answer.splitlines() if line.strip()]
            if not found:
                # Not "nothing to do". A psql that failed on authentication used
                # to produce an empty list, zero dumps and a night that reported
                # success.
                raise BackupError(
                    f"component {self.name!r}: postgres answered with no databases "
                    "at all, which a working server does not do"
                )
        for database in found:
            if not DATABASE_NAME.match(database):
                raise BackupError(
                    f"component {self.name!r}: the database name {database!r} "
                    "cannot be stored safely - a slash would write outside the "
                    "snapshot and a control character would break the manifest"
                )
        return found


COMPONENT = PostgresComponent
