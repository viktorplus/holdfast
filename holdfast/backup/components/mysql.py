"""MySQL and MariaDB, as logical dumps.

Same shape as the PostgreSQL type and for the same reason: a dump loads into a
different version and a different machine, a copy of the data directory does
not.

The server's own schemas are left out. `information_schema` and
`performance_schema` are views onto the server that is running, not data, and
`sys` is built from them; replaying any of them over a live server is a way to
break it rather than to restore it. `mysql` is left out too: it holds the old
server's users and grants, and loaded into a new container it only gets in the
way of the root password and the users that container was started with.

holdfast never sees a password. Either mysql and mysqldump read their own
defaults file, and this only passes the path, or - with credentials set to
container_env - the password stays where the database container keeps it. The
command then runs in the container's own shell, which expands the variable
named by password_env and hands it to the client as MYSQL_PWD: not in argv,
where any process list shows it, and never through holdfast or onto a disk.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from typing import Any

from ..model import USER_NAME, Artifact, BackupError, BuildContext, Component

LIST_DATABASES = "show databases"

# Belongs to the running server, not to the data.
SERVER_SCHEMAS = frozenset({"information_schema", "performance_schema", "sys", "mysql"})

# A variable name, because it is pasted into a shell script unquoted.
ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")

# MariaDB 11 images no longer carry the mysql names, and MySQL images never
# carried the mariadb ones, so the container's shell picks whichever it has.
CLIENTS = {
    "dump": ("mariadb-dump", "mysqldump"),
    "client": ("mariadb", "mysql"),
    "admin": ("mariadb-admin", "mysqladmin"),
}

CONTAINER_ENV = "container_env"

# The one way the container's own password goes stale: the image reads it once,
# when the data directory is first created.
STALE_PASSWORD = (
    " - the password in the container's environment was not accepted; if the "
    "root password was changed after the container was first started, declare "
    "defaults_file for this component instead"
)

# Same reasoning as the PostgreSQL type: this name becomes a file name inside
# the snapshot.
DATABASE_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]*$")

# --single-transaction takes a consistent InnoDB snapshot instead of locking
# the tables a live site is reading. --quick streams rows out instead of
# building a whole table in memory first, which is what kills the backup on the
# one table that matters. --routines and --events keep the parts of a schema
# that are not tables, and whose absence is noticed only after a restore.
DUMP_FLAGS = "--single-transaction --quick --routines --events"


def in_container(tool: str, password_env: str, args: str) -> str:
    """A script for `sh -c` inside the database container.

    export rather than `MYSQL_PWD=... exec`: whether an assignment in front of
    exec reaches the program is up to the shell, and the container's shell is
    not ours to choose. A `_FILE` variable names a file inside the container,
    so the file is read there too.
    """
    first, second = CLIENTS[tool]
    script = (
        f"c=$(command -v {first} || command -v {second}) || "
        f"{{ echo 'neither {first} nor {second} is in this container' >&2; "
        "exit 127; }; "
    )
    if password_env.endswith("_FILE"):
        script += f'export MYSQL_PWD="$(cat "${password_env}")"; '
    elif password_env:
        script += f'export MYSQL_PWD="${password_env}"; '
    return script + f'exec "$c" {args}'


@dataclass(frozen=True)
class MysqlComponent(Component):
    container: str = ""
    user: str = ""
    databases: tuple[str, ...] = ("*",)
    defaults_file: str = ""
    credentials: str = ""
    password_env: str = ""
    exclude_databases: tuple[str, ...] = ()

    type = "mysql"

    @classmethod
    def from_config(cls, table: dict[str, Any]) -> MysqlComponent:
        name = cls._name(table)
        listed = table.get("databases", ["*"])
        if not isinstance(listed, list):
            raise BackupError(f"component {name!r}: databases has to be a list")
        container = str(table.get("container") or "")
        defaults_file = str(table.get("defaults_file") or "")
        user = str(table.get("user") or "")
        credentials = str(table.get("credentials") or "")
        password_env = str(table.get("password_env") or "")
        if not USER_NAME.match(user):
            raise BackupError(
                f"component {name!r}: the user {user!r} is not a name a restore "
                "would accept"
            )
        if credentials not in ("", CONTAINER_ENV):
            raise BackupError(
                f"component {name!r}: credentials can only be {CONTAINER_ENV!r}, "
                f"not {credentials!r}"
            )
        if password_env and not ENV_NAME.match(password_env):
            raise BackupError(
                f"component {name!r}: password_env {password_env!r} is not the "
                "name of an environment variable"
            )
        if password_env and not credentials:
            raise BackupError(
                f"component {name!r}: password_env is read only with "
                f"credentials = {CONTAINER_ENV!r}, and without it would be ignored"
            )
        if credentials and not container:
            raise BackupError(
                f"component {name!r}: credentials = {CONTAINER_ENV!r} reads the "
                "password from a container, and no container is declared"
            )
        if credentials and defaults_file:
            raise BackupError(
                f"component {name!r}: credentials and defaults_file are two "
                "sources of one password; keep one of them"
            )
        excluded = table.get("exclude_databases", [])
        if not isinstance(excluded, list):
            raise BackupError(f"component {name!r}: exclude_databases has to be a list")
        return cls(
            name=name,
            container=container,
            user=user,
            databases=tuple(str(item) for item in listed) or ("*",),
            defaults_file=defaults_file,
            credentials=credentials,
            password_env=password_env,
            exclude_databases=tuple(str(item) for item in excluded),
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
                    "user": self.user,
                    "credentials": self.credentials,
                    "password_env": self.password_env,
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
            "credentials": self.credentials,
            "password_env": self.password_env,
            "exclude_databases": list(self.exclude_databases),
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

    def _login(self) -> str:
        return f"-u{shlex.quote(self.user or 'root')}"

    def _in_container(self) -> bool:
        """Whether the client is picked inside the container.

        Always with container_env; with a defaults file too, whenever there is
        a container, because MariaDB 11 images carry only the mariadb names
        and a hard-coded mysqldump would not be there to read the file.
        """
        return bool(self.credentials or (self.container and self.defaults_file))

    def _script_login(self) -> str:
        # The defaults file stays the first argument to the client.
        if self.credentials:
            return self._login()
        return f"{self._first()}{self._credentials()}".rstrip()

    def _produce(self, database: str, zstd: str) -> str:
        if self._in_container():
            script = in_container(
                "dump",
                self.password_env,
                f"{self._script_login()} {DUMP_FLAGS} "
                f"--databases {shlex.quote(database)}",
            )
            return (
                f"docker exec {shlex.quote(self.container)} sh -c "
                f"{shlex.quote(script)} | {zstd}"
            )
        line = (
            f"mysqldump {self._first()}{self._credentials()}{DUMP_FLAGS} "
            f"--databases {shlex.quote(database)} | {zstd}"
        )
        return (
            f"docker exec {shlex.quote(self.container)} {line}"
            if self.container
            else line
        )

    def _listing(self) -> list[str]:
        if self._in_container():
            args = f"{self._script_login()} -N -B -e {shlex.quote(LIST_DATABASES)}"
            script = in_container("client", self.password_env, args)
            return ["docker", "exec", self.container, "sh", "-c", script]
        return self._argv("mysql", "-N", "-B", "-e", LIST_DATABASES)

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
            try:
                answer = ctx.probe.capture(
                    self._listing(),
                    what=f"listing the databases of {self.name!r}",
                )
            except BackupError as error:
                # mysql's refusal does not say which line of holdfast.toml to
                # change. A hand-written component may have no defaults_file;
                # one the rule found reads the password from its container,
                # which stops working once the root password is changed.
                if self.defaults_file or "Access denied" not in str(error):
                    raise
                if self.credentials:
                    raise BackupError(f"{error}{STALE_PASSWORD}") from None
                raise BackupError(
                    f"{error} - this component has no defaults_file, so mysql "
                    "ran without a password; set defaults_file in its "
                    "[[component]] table"
                ) from None
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
        # After the emptiness check: a server that answered nothing is a
        # failure, a server whose every database was excluded is a choice.
        found = [db for db in found if db not in self.exclude_databases]
        for database in found:
            if not DATABASE_NAME.match(database):
                raise BackupError(
                    f"component {self.name!r}: the database name {database!r} "
                    "cannot be stored safely - it becomes a file name inside "
                    "the snapshot"
                )
        return found


COMPONENT = MysqlComponent
