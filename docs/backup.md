# Backup

What holdfast keeps is a list of components, and the engine turns that list
into one snapshot per run. The list comes from two places: the
`[[component]]` tables the operator writes in `holdfast.toml`, and - on a
Docker host - a rule that works out what the machine runs and what of it is
worth keeping.

```sh
holdfast backup --dry-run    # print what would run, and what was left out
holdfast backup              # produce a snapshot
```

## Two modes

```toml
[backup]
mode = "auto"      # or "manual"
exclude = []
```

| Mode | Where the list comes from | When the rule runs |
|---|---|---|
| `auto` | the rule, plus `holdfast.toml` | at every `backup` and `backup --dry-run` |
| `manual` | `holdfast.toml`, plus `components.toml` | only when `holdfast backup --discover` is run |

`holdfast init --backup-mode auto|manual` writes the mode into a new
`holdfast.toml`, `auto` by default; changing the line later changes the mode.
A configuration with no `backup.mode` - every installation made before 0.3.0 -
is `manual`, and behaves as it did: only the components it declares, and the
machine is not looked at.

`auto` needs Docker. On a machine without it every run stops on the first
question the rule asks, and the error ends with `backup.mode is "auto"; on a
machine without Docker set backup.mode = "manual"`: set the line to
`"manual"`, or pass `--backup-mode manual` to `holdfast init`.

`auto` is right for most machines: a container or a volume added this
afternoon is in tonight's snapshot without anybody editing anything. `manual`
is for a machine whose snapshot should change only after a person has read
the change.

In both modes a hand-written component wins. Whatever it covers - the same
path, the same volume, the database container it names - the rule leaves
alone, and says so.

## The rule

The rule's picture of the machine is one `docker inspect` of every container,
running or stopped, and the list of volumes Docker knows. It keeps the names
of the containers' environment variables and none of their values, except
three that are not secrets: `PGDATA`, `POSTGRES_USER`, and the
`*_ALLOW_EMPTY_*PASSWORD` switches. A Docker that does not answer fails the
run: "no containers" is never inferred from an error, because that is how an
empty snapshot reports success.

It takes four things:

1. **databases**, as dumps. A container is a database by its image name -
   `mysql`, `mariadb`, `percona`, `percona-server` for MySQL, `postgres`,
   `postgis` for PostgreSQL - with any registry or tag. Every database is
   dumped except the server's own: `information_schema`,
   `performance_schema`, `sys` and `mysql` for MySQL, the template
   databases for PostgreSQL, which also gets its roles;
2. **compose project directories**, whole, one per project, found from the
   labels `docker compose` puts on its containers. A database's data
   directory bound inside the project is left out of that archive;
3. **volumes**, named or anonymous, mounted by any container, running or
   stopped - one artifact per volume however many containers share it;
4. **bind mounts outside the project directories**, files or directories.

A folder the rule keeps - a project directory or a bind mount - leaves out of
its archive everything below it that is reported as not taken or is taken on
its own: a database's data directory (dumped, declared by hand or excluded), a
`path:` exclusion, another project directory, `backup.root`, another kept bind
mount. A bind of `/root` therefore does not archive the project `/root/myapp`
a second time with its raw database files, and a bind of `/opt` does not
archive the snapshots being written to `/opt/backups`.

Hosts outside Docker are not looked at. What the machine keeps outside Docker
is declared by hand, as a `path` or `command` component.

Everything the rule considers and does not take is reported, in the dry run,
in the backup's output and in the manifest's `skipped` list:

| Reason | What it means |
|---|---|
| `no container uses it` | an orphaned volume. holdfast never removes it |
| `database data, covered by the dump` | the directory a database container keeps its files in |
| `excluded by you` | named in `backup.exclude`, directly or through its container |
| `declared by hand` | a component in `holdfast.toml` already covers it |
| `system` | a bind mount of `/`, `/proc`, `/sys`, `/dev`, `/run`, `/var/run`, `/var/lib/docker`, `/etc/localtime`, `/etc/timezone`, or any path ending in `.sock` |
| `the snapshots themselves` | inside `backup.root` |
| `inside a compose project directory` | already in that project's archive |
| `inside another bind mount already taken` | already in that mount's archive |
| `not on this machine` | a bind mount whose source is not on disk |
| `the project directory is not on this machine` | a compose project whose directory is not on disk |

There is no size limit in the rule: everything is taken, and what is not
wanted is excluded by name. Before the first byte is written, the rule adds
up the size of everything it takes - database data directories standing in
for their dumps - and a run that would leave less than `backup.min_free_gb`
free is refused with the estimate and the free space in the message. The
estimate is before compression and counts a database both as its dump and
inside its project, so it errs towards refusing.

What stops the run rather than being reported: a MySQL container with no root
password in its environment, a database container that is not running, a
Docker that does not answer, an estimate that does not fit, a malformed
`backup.exclude`, and two artifacts that would be written to the same file.

### Excluding

```toml
[backup]
exclude = [
  "volume:myapp_cache",
  "path:/srv/old-site",
  "database:myapp-db-1/scratch",
  "container:myapp-adminer-1",
]
```

| Kind | Leaves out |
|---|---|
| `volume:<name>` | one volume, named or anonymous |
| `path:<absolute path>` | a project directory or a bind mount source at or under that path; a subdirectory inside a kept project directory or bind mount is cut out of that archive |
| `database:<container>/<database>` | one database; the container's others are still dumped |
| `container:<name>` | its dumps, its bind mounts, and its volumes unless another container uses them |

`path:/root/myapp/logs`, inside the project directory `/root/myapp`, keeps
the project and leaves `logs` out of its archive: the project's command gains
`--exclude=root/myapp/logs`, and the skipped list says
`path /root/myapp/logs: excluded by you`. `path:/root/myapp` leaves the whole
project out. `container:` does not leave out the container's compose project,
which belongs to every service in it.

A path is normalised before it is used: `path:/root/myapp/logs/` and
`path:/root/myapp//logs` are `path:/root/myapp/logs`. Left as written, a
trailing slash would make tar archive the folder after all.

An unknown kind, an empty value or a relative path is a configuration error,
in both modes, not a silent skip. An entry that matches nothing on this
machine - no such volume or container, a `database:` whose container is not a
database, a path under nothing the rule looked at - is a warning in `auto`
mode, in the dry run and in the backup's output: it is most likely a typo,
and a typo here keeps what it meant to leave out.

### What the components are called

| What | Component | File in the snapshot |
|---|---|---|
| project directory of project `P` | `project-P` | `project-P.tar.zst` |
| bind mount outside a project | `mount-<slug of the source>` | `mount-<slug>.tar.zst` |
| named volume `V` | `volume-<slug of V>` | `docker-volumes/V.tar.zst` |
| anonymous volume at `D` in container `C` | `volume-C-<slug of D>` | `docker-volumes/C--<slug of D>.tar.zst` |
| MySQL container `C` | `<slug of C>` | `mysql/<slug of C>-<database>.sql.zst` |
| PostgreSQL container `C` | `<slug of C>` | as any `postgres` component |

A slug is the name lower-cased, with anything outside `a-z 0-9 _ -` folded to
a dash. The component name is what `holdfast restore --component` takes. A
name already taken - by `holdfast.toml` or by an earlier find - gets `-2`,
`-3`.

An anonymous volume is named after its container and mount point because its
own name is random, and different on every machine compose brings the
application up on. For the same reason its restore recipe keeps the
container and the path, not the name - see `docs/restore.md`.

### components.toml

In `manual` mode, `holdfast backup --discover` runs the rule once and writes
what it found to `components.toml` beside `holdfast.toml`
(`/etc/holdfast/components.toml` by default):

```
wrote /etc/holdfast/components.toml (3 components)

changes:
  + myapp-db-1 (mysql)
  + project-myapp (path)
  + volume-myapp-wordpress-1-var-www-html (docker_volume)

not taken:
  bind mount /root/myapp/db_data: database data, covered by the dump
```

The file is written whole every time, `0600`, and the previous one is kept as
`components.toml.prev`. `changes:` lists what was added (`+`), removed (`-`)
or changed (`~`) against the previous file, or says `no changes`. The file is
holdfast's, not the operator's: `holdfast.toml` keeps its comments only as
long as nothing rewrites it, so discovery writes here instead, and the
operator's own components and exclusions stay in `holdfast.toml`, where
`--discover` also reads them.

Its components are frozen: a path, a volume by name or an anonymous volume by
container and mount point, a database container with `databases = ["*"]`.
New databases in a known container are therefore picked up at every backup;
a new container, project, named volume or bind mount needs `--discover` again.

A backup reads `components.toml` only if its first line is the one
`--discover` writes. A file without it - the draft holdfast 0.2's
`--discover` printed, saved to that path - is ignored with a warning rather
than backed up as it stands. In `auto` mode the file is not read at all, and
its presence is a warning too. `--discover` in `auto` mode writes nothing and
points at `--dry-run`.

### Upgrading from 0.2

A 0.2 configuration keeps working unchanged: it has no `backup.mode`, which
is `manual`. Two things change at once all the same: MySQL's own `mysql`
schema is no longer dumped, and a MySQL restore now uses the same
`defaults_file` the dump did. To move to the rule, set `mode = "auto"` under
`[backup]`, remove the `[[component]]` tables it now covers - typically the
database container, the every-volume `docker_volume` table and the project
`path` - keep the ones for data outside Docker, delete a 0.2 draft left at
`components.toml`, and read `holdfast backup --dry-run`. An every-volume
`docker_volume` table left in place in `auto` mode is a warning at every run:
the rule leaves every volume to it, anonymous ones included, and that form
archives an anonymous volume by a name no new container will ever have, so
it cannot be restored into one.

Also different for a 0.2 configuration, whatever its mode:

- a database `user` with anything outside `A-Z a-z 0-9 _ . -` is refused
  when the configuration is loaded;
- a MySQL component with both `container` and `defaults_file` runs through
  `sh -c` in the container, which chooses `mariadb-dump` or `mysqldump`
  there, so the container needs a `sh`;
- `holdfast backup --discover` needs a configuration that loads, and writes
  `components.toml` instead of printing a draft; a 0.2 draft saved at that
  path is ignored with a warning;
- `holdfast backup --dry-run` prints the mode, each component with where it
  came from, and what was not taken - scripts reading the old output need
  looking at;
- the manifest carries a `skipped` list, empty unless the rule ran.

A MySQL credentials file that 0.2 had mounted into the database container is
not needed by the rule. If it sits inside the project directory it is
archived with the project, encrypted like everything else.

## A component

A component declares one part of this machine and owes the engine three
answers:

| It says | Which becomes |
|---|---|
| how to describe itself | its entry in the manifest |
| how to produce its bytes | one shell line, writing to stdout |
| how those bytes are checked | the command `holdfast restore verify` runs |

Everything else - the order, encryption, checksums, the manifest, rotation,
free space, what happens when something fails - stays in the engine. Adding a
type means answering three questions, not understanding a pipeline.

Five types, and a rule that runs through all of them: a component which
cannot produce its artifacts stops the backup. It does not skip itself with a
warning, because a night that produced no database dump and reported success
is the failure this tool exists to make impossible.

| Type | What it takes |
|---|---|
| `path` | a directory or file, as a tar stream through zstd |
| `postgres` | logical dumps, one per database, plus the globals |
| `mysql` | logical dumps, one per database |
| `docker_volume` | one volume, by name or by container and mount point; or every volume below a size limit, minus a skip list |
| `command` | whatever the operator's own command produces |

`command` is the declared seam, and the honest answer to "it has to back up any
software": the listed types are supported properly, and for anything else there
is a place to stand. The command runs exactly as written - holdfast wraps it in
no compression of its own, so the manifest describes what was actually stored.
What holdfast cannot know is the format, so it claims nothing about it: with no
`restore` table declared, the manifest says so and `holdfast restore` will leave
that artifact alone rather than guess.

Order is the order of the list. Nothing sorts it, because the order is the
operator's statement about what depends on what. In `auto` mode the
hand-written components come first, then the rule's: databases, projects,
volumes, bind mounts.

## Databases, and the password holdfast does not hold

`postgres` and `mysql` take logical dumps, one artifact per database, and the
PostgreSQL type takes one more for the globals - roles and tablespaces live
outside any single database, and a restore without them produces a database
nobody has permission to use.

A dump rather than a copy of the data directory, because a dump loads into a
different minor version, a different machine and a different filesystem, and a
copy of the files loads into almost nothing. For the same reason the rule
leaves a database container's data directory out of everything else it takes,
and a database's own Docker volume belongs in a hand-written every-volume
`docker_volume` component's `exclude`: taking it as well stores a second,
worse copy of the same data.

**holdfast never sees a password.** A database the rule finds is reached with
the password its own container was started with. The component carries the
*name* of the variable, and the dump runs in the container's shell, which
expands it and hands it to the client as `MYSQL_PWD` - not in argv, where a
process list shows it, and never through holdfast or onto a disk:

```toml
[[component]]
type = "mysql"
name = "myapp-db-1"
container = "myapp-db-1"
user = "root"
credentials = "container_env"
password_env = "MYSQL_ROOT_PASSWORD"
```

The rule looks for `MARIADB_ROOT_PASSWORD`, `MYSQL_ROOT_PASSWORD`, then their
`_FILE` forms, whose file is read inside the container; an
`*_ALLOW_EMPTY_*PASSWORD` switch means root without a password. A container
with none of them - one started with `MYSQL_RANDOM_ROOT_PASSWORD`, say - stops
the run and names the two ways out: declare the component by hand with
`defaults_file`, or exclude the container. The client is whichever the
container has, `mariadb-dump` or `mysqldump`: MariaDB 11 images no longer carry
the `mysql` names. PostgreSQL needs no password at all: `psql -U` runs inside
the container over its local socket, as the role from `POSTGRES_USER`.

The container's variable goes stale in one way: the image reads it once, when
the data directory is first created, so a root password changed afterwards is
not in it. It can also be different from the start: the official `mysql`
images put `MYSQL_ROOT_PASSWORD` into SQL at first start, where a backslash
is read as an escape, so a root password containing `\` is not the one the
server keeps (seen on `mysql:5.7` and `mysql:8.4`; `mariadb:11` keeps it as
written). Either way the run stops on `Access denied` with the hint that the
password in the container's environment was not accepted, and the answer is
`defaults_file`. With it,
the server reads its own credentials file, and the configuration carries the
path to that file, which is not a secret. In a container the client is again
chosen there, `mariadb-dump` or `mysqldump` (`mariadb` or `mysql` to list and
to restore), with `--defaults-file` as its first argument, where these tools
accept it:

```toml
[[component]]
type = "mysql"
name = "shop"
container = "shop-mysql"
defaults_file = "/etc/holdfast/mysql.cnf"
```

For PostgreSQL the same key names a libpq password file, which is passed as
`PGPASSFILE`. Keep either file `0600`. With `container` set, the path is the
one inside the container. `credentials` and `defaults_file` together are a
configuration error: they are two sources of one password.

A component declared by hand for a container wins over the rule, which then
reports that container as `declared by hand`.

Both types work without Docker: leave `container` out and the tools are run
directly. Docker is optional throughout holdfast, and a machine without it is
served in full.

Four refusals are worth knowing before the first night:

| What happens | Why it is not a warning |
|---|---|
| a database container is not running | the old script returned success here, having dumped nothing at all |
| a running server lists no databases | that is what a `psql` failing on authentication looks like |
| a database name is not a plain word | a slash writes the artifact outside the snapshot, a control character breaks the manifest |
| a volume's storage cannot be read | under rootless Docker it never can be; say so in `exclude` if it is not wanted |

A volume larger than `max_mb` is the one thing that is skipped quietly, because
the limit is a rule the operator wrote. The rule is in the manifest, which is
how a missing volume gets explained later; the list of what it skipped is not.

## Seeing what a run will take

```sh
holdfast backup --dry-run
```

It makes the same selection the backup makes and prints it, writing nothing:

```
mode: auto
myapp-db-1 (mysql, from rule, ~210 MB)
  mysql/myapp-db-1-wordpress.sql.zst.age
    docker exec myapp-db-1 sh -c '...; export MYSQL_PWD="$MYSQL_ROOT_PASSWORD"; exec "$c" -uroot ... --databases wordpress' | zstd -T0 -10 -q | age --encrypt -r age1...
project-myapp (path, from rule, ~210 MB)
  project-myapp.tar.zst.age
    ls -d -- /root/myapp >/dev/null && tar ... --exclude=root/myapp/db_data -cf - -C / root/myapp | zstd -T0 -10 -q | age --encrypt -r age1...
volume-myapp-wordpress-1-var-www-html (docker_volume, from rule, ~105 MB)
  docker-volumes/myapp-wordpress-1--var-www-html.tar.zst.age
    tar ... -cf - -C /var/lib/docker/volumes/<volume>/_data . | zstd -T0 -10 -q | age --encrypt -r age1...

not taken:
  volume <64 hex characters>: no container uses it
  bind mount /root/myapp/db_data: database data, covered by the dump

estimated size before compression: 525 MB
```

Per component: its name, type, where it came from (`rule`, `holdfast.toml` or
`components.toml`) and, for the rule's, the estimated size; then each artifact
and the exact line that will produce it. After them, what was not taken and
why, the estimate, and any warnings. A dry run refuses what the backup would
refuse on the configuration - an invalid component or exclusion, a Docker
that does not answer, a database that turns its login away, two artifacts
for one file. What the backup checks on the machine just before it writes, it
reports as warnings starting `the backup would refuse:` and carries on: a
missing `bash` or encryption tool, and - only if `backup.root` already
exists, since a dry run creates nothing - too little free space or too little
room for the estimate. A clean dry run is still not a promise that every
container will answer tonight.

## A snapshot

One directory per run, named for the moment the run started:

```
/srv/backups/
  20260920-231500/
    mail-config.tar.zst.age
    inventory.json.age
    manifest.json
    SHA256SUMS
  latest -> 20260920-231500
```

`manifest.json` is plaintext on purpose. A snapshot is read on the worst day
this machine has, often on a different machine with no holdfast installed and
no configuration to consult, so everything needed to understand the directory
is inside it, in a form a person can read. It holds the tool version, the
timestamp, this machine's `host_label`, the compression and encryption settings
with the list of recipients, a description of each component, and per artifact:
its path, size, `sha256`, the recipe for putting it back, and the command that
checks it. In `auto` mode it also holds `skipped`: everything the rule left
out, each with its reason, so a volume missing from the snapshot is explained
by the snapshot itself.

Nothing in it is a secret. Every description fed into it gives the shape of a
thing and not its contents - a `command` component's own command line stays
out, because it can hold a path and the name of an internal tool.

A snapshot whose recipe this version cannot carry out is not considered
verified, however cleanly its bytes decrypt. Reporting it healthy would send an
operator into a restore that silently does nothing.

`SHA256SUMS` covers every file including the manifest, in the format
`sha256sum -c` reads, on a machine that has never heard of holdfast.

## The plaintext never reaches the disk

Production and encryption are one pipeline, and its output goes straight into
the destination file:

```
pg_dump ... | zstd -10 -q | age -r age1... > /srv/backups/.20260920-231500.tmp/db.sql.zst.age
```

There is no step at which an unencrypted copy exists, so the local copy is
protected as well as the offsite one. It also means restoring, even here, needs
the private key.

The shell gets exactly that one line per artifact and no more. The destination
file is opened by holdfast itself, which is why the line contains no redirect:
there is nothing to quote, and nothing for a path with a space in it to break.

## Encryption

Two tools, `age` and `gpg`, and one mode: public keys.

```toml
[encryption]
enabled = true
tool = "age"
recipients = ["age1...", "age1..."]
```

List more than one - a primary key and a spare - so that losing one key does
not make every backup unreadable, and so a new key can be introduced before the
old one is retired.

**The private key never lives on this machine.** It is generated elsewhere and
kept elsewhere; holdfast needs it to read a snapshot, never to write one:

```sh
age-keygen -o key.txt        # keep key.txt off this machine, offline
grep 'public key' key.txt    # the age1... line goes into recipients
```

Encryption is on by default, and with it on a missing tool or a missing
recipient **stops the run**, before anything is written. There is no branch
that falls back to a plaintext archive. The failure this guards against is
silent: a typo empties the recipients list, and every night afterwards produces
copies readable by whoever ends up holding the disk.

A passphrase is refused, by name and with the reason. It would have to be
stored on the same machine as the copies it protects, which is no protection
from whoever ends up with that machine. A short gpg key id is refused too:
short ids collide by construction, and a collision here encrypts the backup to
somebody else.

## What happens when something fails

The order of a run is chosen so that everything refusable is refused before a
single byte is written: a bad encryption setting, a missing tool, a bad
component, another backup already running, too little disk.

Past that point the rule reverses. The work happens under a temporary name
`.20260920-231500.tmp` and is renamed last, so an interrupted run leaves nothing
that could be mistaken for a backup. A component that fails takes the whole
directory with it, and the journal records the failure.

An empty artifact counts as a failure even though the pipeline reported
success. For a backup those are the same event wearing each other's face.

So does an empty snapshot: a run in which no component produced anything -
the `[[component]]` list lost from `holdfast.toml`, say - fails instead of
finishing. Finished, it would be the newest snapshot, and rotation would remove
the real ones around it.

A backup that is already running is not an error: the command says so and exits
zero. The nightly timer overlapping a manual run happens, and a non-zero code
every such night is how real failures stop being read.

## Rotation

```toml
[backup]
retention_days = 7
```

Snapshots older than `retention_days * 24` hours are deleted. Zero turns
rotation off.

**The number means what it says.** Scripts that rotate with
`find -mtime +N` keep a snapshot a day longer, because that test is true only
at N+1 full days: `7` there keeps a snapshot for eight. A key-rotation runbook
that counts the destruction date of an old key from this number has to use
exactly `retention_days` days.

Two things rotation will not do. It never deletes the newest snapshot, whatever
the window says - a machine whose backup broke months ago would otherwise lose
its last copy exactly on schedule, and the rotation would look like it had
worked. And it runs only after a snapshot has already been completed and
renamed: a run of failures is exactly when the old copy is the only copy.

A directory it cannot remove becomes a warning on the finished run, not a
failure. By then the snapshot is final, and a stuck directory is not a reason to
call the night a loss.

## The second copy

A snapshot that never leaves the machine that made it is no copy at all once
that machine is lost. When `offsite.remote` is set, the finished snapshot is
sent there with [rclone](https://rclone.org), one remote configured outside
holdfast and never read or touched by it beyond the copy:

```toml
[offsite]
remote = "shared:backups"
timeout_minutes = 120
```

It lands under the machine's own `host_label`, not directly in `remote`:

```
shared:backups/
  web-1/20260920-231500/
  web-2/20260920-231500/
```

An empty label is refused rather than guessed, because guessing wrong would
land one machine's copies inside another's directory, silently overwriting
whichever arrived second. This is also why a profile shared across a
collection can carry `offsite.remote` but never `host_label` - see
`profile.py`.

Sending happens after the snapshot directory has been renamed to its final
name, and outside the run lock. Both halves are load-bearing. After the
rename, because there is no earlier point at which "the snapshot" and "what
would be sent" mean the same thing - a run interrupted before that point
leaves nothing behind to send. Outside the lock, because an upload can sit
on a slow or dead link for hours with nobody watching, and a lock held that
long would turn one bad night into `BackupBusy` on every night after it, for
a backup that had already finished and succeeded. `offsite.timeout_minutes`
bounds the wait; it is read in minutes and handed to rclone in seconds, and a
value of zero or garbage becomes the minimum rather than "no limit" - an
unbounded copy is exactly the hang this setting exists to prevent.

A failed send is not a failed backup. The snapshot already on this machine is
whole; what is missing is the second copy, and that is reported as a warning
- the same ones `holdfast backup` already prints to stderr - never as a
failure of the night's run. The journal below still records the attempt as
failed, so the watchdog keeps saying so every day until an operator fixes it,
rather than once and then silently.

holdfast never deletes on the remote. Rotation happens locally only, so a
remote holds every snapshot ever sent until something else - a lifecycle
rule on the remote, or an operator - removes it. This is also why the
account behind `remote` needs so little: it writes a new snapshot and lists
just enough to confirm what it wrote arrived intact. It never needs to
delete anything, and it never needs to read the contents of a snapshot back
- that is the restore's job, done later, from a different machine.

The credentials for `remote` live in `rclone.conf` on this machine, set up
once outside holdfast and outside its configuration file - the same reason
database passwords are never in `holdfast.toml` either: a config file that
can be read, copied and reviewed safely is worth more than one field of
convenience.

## The journal

Successful backups already leave a trace: every snapshot is a directory with a
manifest, which is what `backup_freshness` reads. Three things leave none - a
run that failed, a snapshot that was verified, and a copy that was sent to
shared storage - so each job kind keeps a small file:

```
/var/lib/holdfast/jobs/
  backup.json  verify.json  restore.json  offsite.json
```

Each holds the last run and, separately, the last successful one. Two fields
rather than one, because with a single field tonight's failure erases the memory
of last night's success, and the check that asks "when was a restore last proven
to work" would answer "never" for a machine that has proven it every week for a
year.

`offsite.json` is written the same way as the other three: every call to
`send` records the attempt, successful or not, so `backup_offsite_copy` has
something to read the first time a remote is configured, not only after the
first success.

A rehearsal - `restore --dry-run` - writes nothing to `restore.json`. It
reads a snapshot and reports what it would do, and a record next to real
restores would let a rehearsal stand in for the proof `restore_tested` is
asking for.

There is no history here. If the web interface or the digests need one, that is
something added beside this rather than a change to it.
