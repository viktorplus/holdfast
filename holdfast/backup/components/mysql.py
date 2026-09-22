"""MySQL and MariaDB, as logical dumps.

Same shape as the PostgreSQL type and for the same reason: a dump loads into a
different version and a different machine, a copy of the data directory does
not.

The server's own schemas are left out. `information_schema` and
`performance_schema` are views onto the server that is running, not data, and
`sys` is built from them; replaying any of them over a live server is a way to
break it rather than to restore it.

holdfast never sees a password. mysql and mysqldump read their own defaults
file, and this only passes the path.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from typing import Any

from ..model import Artifact, BackupError, BuildContext, Component

LIST_DATABASES = "show databases"

# Belongs to the running server, not to the data.
SERVER_SCHEMAS = frozenset({"information_schema", "performance_schema", "sys"})

# Same reasoning as the PostgreSQL type: this name becomes a file name inside
# the snapshot.
DATABASE_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]*$")

# --single-transaction takes a consistent InnoDB snapshot instead of locking
# the tables a live site is reading. --quick streams rows out instead of
# building a whole table in memory first, which is what kills the backup on the
# one table that matters. --routines and --events keep the parts of a schema
# that are not tables, and whose absence is noticed only after a restore.
DUMP_FLAGS = "--single-transaction --quick --routines --events"


@dataclass(frozen=True)
class MysqlComponent(Component):
    container: str = ""
    user: str = ""
    databases: tuple[str, ...] = ("*",)
    defaults_file: str = ""

    type = "mysql"

    @classmethod
    def from_config(cls, table: dict[str, Any]) -> MysqlComponent:
        name = cls._name(table)
        listed = table.get("databases", ["*"])
        if not isinstance(listed, list):
            raise BackupError(f"component {name!r}: databases has to be a list")
        return cls(
            name=name,
            container=str(table.get("container") or ""),
            user=str(table.get("user") or ""),
            databases=tuple(str(item) for item in listed) or ("*",),
            defaults_file=str(table.get("defaults_file") or ""),
        )

    def artifacts(self, ctx: BuildContext) -> list[Artifact]:
        self._require_container(ctx)
        zstd = f"zstd -T{ctx.zstd_threads} -{ctx.zstd_level} -q"
        return [
            Artifact(
                name=f"mysql/{self.name}-{database}.sql.zst",
                produce=self._produce(database, zstd),
                recipe={
                    "type": "mysql_database",
                    "container": self.container,
                    "database": database,
                    "defaults_file": self.defaults_file,
                },
                check="zstd -dc >/dev/null",
            )
            for database in self._databases(ctx)
        ]

    def describe(self) -> dict[str, Any]:
        return {
            **super().describe(),
            "container": self.container,
            "user": self.user,
            "databases": list(self.databases),
            "defaults_file": self.defaults_file,
        }

    def containers(self) -> list[str]:
        return [self.container] if self.container else []

    # -- command lines ------------------------------------------------------

    def _first(self) -> str:
        """--defaults-file, which these tools accept only as the first flag.

        Anywhere else it is ignored, and the failure that follows reads as a
        wrong password rather than as a misplaced argument.
        """
        return (
            f"--defaults-file={shlex.quote(self.defaults_file)} "
            if self.defaults_file
            else ""
        )

    def _credentials(self) -> str:
        return f"-u {shlex.quote(self.user)} " if self.user else ""

    def _produce(self, database: str, zstd: str) -> str:
        line = (
            f"mysqldump {self._first()}{self._credentials()}{DUMP_FLAGS} "
            f"--databases {shlex.quote(database)} | {zstd}"
        )
        return (
            f"docker exec {shlex.quote(self.container)} {line}"
            if self.container
            else line
        )

    def _argv(self, *rest: str) -> list[str]:
        prefix = ["docker", "exec", self.container] if self.container else []
        first = [f"--defaults-file={self.defaults_file}"] if self.defaults_file else []
        user = ["-u", self.user] if self.user else []
        return prefix + [rest[0]] + first + user + list(rest[1:])

    # -- asking the machine -------------------------------------------------

    def _require_container(self, ctx: BuildContext) -> None:
        if self.container and not ctx.probe.container_running(self.container):
            raise BackupError(
                f"component {self.name!r}: the container "
                f"{self.container!r} is not running, so there is nothing to dump"
            )

    def _databases(self, ctx: BuildContext) -> list[str]:
        if list(self.databases) != ["*"]:
            found = list(self.databases)
        else:
            answer = ctx.probe.capture(
                self._argv("mysql", "-N", "-B", "-e", LIST_DATABASES),
                what=f"listing the databases of {self.name!r}",
            )
            found = [
                line.strip()
                for line in answer.splitlines()
                if line.strip() and line.strip() not in SERVER_SCHEMAS
            ]
            if not found:
                raise BackupError(
                    f"component {self.name!r}: mysql answered with no databases of "
                    "its own, which a server worth backing up does not do"
                )
        for database in found:
            if not DATABASE_NAME.match(database):
                raise BackupError(
                    f"component {self.name!r}: the database name {database!r} "
                    "cannot be stored safely - it becomes a file name inside "
                    "the snapshot"
                )
        return found


COMPONENT = MysqlComponent
