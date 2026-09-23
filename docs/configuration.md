# Configuration

## Layers

Three layers are merged in this order, lowest first. A layer higher in the
list wins whenever it declares a key at all.

| Layer | Source | Carries |
|---|---|---|
| defaults | built into holdfast (`DEFAULTS` in `config.py`) | every key, each at an empty or falsy default |
| machine | `<config-dir>/holdfast.toml` | this machine's own values, written by `holdfast init` and editable afterward |
| env | `HOLDFAST_*` environment variables | secrets, and only secrets |

`<config-dir>` defaults to `/etc/holdfast`; `--config-dir` overrides it for
any holdfast command, and it goes before the subcommand, never after:

```sh
holdfast --config-dir /srv/holdfast config check
```

The collection profile is deliberately not a layer. It is material for
`holdfast init --join`, which checks it against the allow-list below and
copies its values into that machine's `holdfast.toml`, once; a `profile.toml`
left in the configuration directory afterwards is not read at all.

That is a decision, not an oversight, and it buys two things. The allow-list
becomes binding rather than advisory - there is no second path by which a key
it does not permit, or a secret, becomes configuration. And the answer to
"where does this value come from" is one file on this machine, which is the
file an operator already has open.

The cost is that a value shared by several machines is changed on each of
them. See `docs/install.md` for what that looks like in practice.

## The empty value rule

A key **present** in a higher layer wins even when its value is empty. An
empty string or an empty list is how an operator says "stop treating this as
normal" - falling back to the layer below would silently undo that.

For example: `holdfast init` wrote a fresh `api.token` into this machine's
`holdfast.toml`, and the environment sets `HOLDFAST_API_TOKEN=""` - an empty
value, deliberately. The environment is the higher layer and it declares the
key, so the token is empty and `config check` reports it missing, rather than
quietly falling back to the file the operator was overriding.

## Keys

| Key | Default | Normally set in | Purpose |
|---|---|---|---|
| `host_label` | `""` | machine file; written by `holdfast init` from the hostname, or `--host-label` | how this machine signs itself in alerts, and the directory it owns in shared storage |
| `collection` | `""` | machine file; `init --join` copies it from the profile's top level | which collection this machine belongs to; empty means standalone |
| `alerts.chat_id` | `""` | machine file; `init --join` copies it from the profile | where alerts are delivered |
| `alerts.bot_token` | `""` | environment only, `HOLDFAST_TELEGRAM_BOT_TOKEN` | the credential that authenticates alert delivery |
| `offsite.remote` | `""` | machine file; `init --join` copies it from the profile | one whole rclone destination for this machine's copies, e.g. `shared:backups`; empty means every copy stays on this machine |
| `offsite.timeout_minutes` | `120` | machine file | give up on an offsite copy that has not finished in this long |
| `encryption.enabled` | `true` | machine file | whether snapshots are encrypted; on by default, and with no recipient the backup stops rather than writing one anybody can read |
| `encryption.tool` | `"age"` | machine file | `age` or `gpg`. Public keys only - a passphrase would have to live beside the copies it protects |
| `encryption.recipients` | `[]` | machine file; `init --join` copies it from the profile | public keys backups are encrypted to |
| `encryption.recipients_file` | `""` | machine file | a file with one recipient per line, added to the list above rather than replacing it |
| `component` | `[]` | machine file, as `[[component]]` tables | what this machine keeps by hand, in the order it is kept; in `auto` mode, on top of what the rule finds. See [backup.md](backup.md) |
| `backup.mode` | `""` | machine file; written by `holdfast init --backup-mode`, `auto` unless told otherwise | `auto`: the rule works out what a Docker host keeps at every backup. `manual`: the backup takes `[[component]]` plus `components.toml`, which `holdfast backup --discover` writes. Empty means `manual`, the behaviour of every installation before 0.3.0 |
| `backup.exclude` | `[]` | machine file | what the rule must not take, as `"volume:<name>"`, `"path:<absolute path>"`, `"database:<container>/<database>"` or `"container:<name>"`. Checked in both modes; a malformed entry stops the run |
| `backup.root` | `"/opt/backups"` | machine file | where snapshots land: one directory per run, named by timestamp |
| `backup.retention_days` | `7` | machine file | snapshots older than this many days are deleted; `0` turns rotation off. This means what it says - `backup_s1` kept them a day longer |
| `backup.min_free_gb` | `8` | machine file | refuse to start with less than this free, before anything is written - and, in `auto` mode, refuse a run whose estimated size would leave less than this behind |
| `backup.zstd_level` | `10` | machine file | compression level |
| `backup.zstd_threads` | `0` | machine file | compression threads; `0` means as many as there are cores |
| `backup.lock_file` | `"/run/holdfast-backup.lock"` | machine file | held for a run, so the nightly timer and a manual run cannot each produce a snapshot at once |
| `jobs.dir` | `"/var/lib/holdfast/jobs"` | machine file | where each job kind records its last run and its last successful one |
| `api.token` | `""` | written into the machine file by `holdfast init` - the one secret that lives in that file; `HOLDFAST_API_TOKEN` overrides it | authenticates callers of holdfast's HTTP API; not consumed by this release, which ships no API |
| `heartbeat.url` | `""` | environment only, `HOLDFAST_HEARTBEAT_URL` | where this machine sends its liveness ping; not consumed by this release |

## The profile

A profile carries exactly three blocks, each with a fixed set of keys, plus
one top-level key:

| Block | Allowed keys |
|---|---|
| (top level) | `collection` |
| `alerts` | `chat_id` |
| `offsite` | `remote` |
| `encryption` | `recipients` |

This is an allow-list, not a filter: any other top-level key, any other
block, or any key inside `alerts`, `offsite` or `encryption` that is not
listed above is rejected, and nothing is written. `holdfast init --join`
fails loudly on a profile like that rather than silently dropping the parts
it does not recognize. That command is the only place a profile is ever read,
which is what makes this list binding: a key it refuses has no other way in.
`offsite.timeout_minutes` is deliberately not on it: it is a property of this
machine's own link, not something a shared profile should hand every machine
alike.

The values are checked too, not only the key names: each must be text, a
finite number, or a list of those. A table where a value belongs -
`[offsite.remote]` instead of `remote = "..."` - is rejected, because
`init --join` could not write it into a machine's config file as TOML.

"Finite" is not pedantry. TOML accepts `nan` and `inf` on the way in, and
they are written back out as `NaN` and `Infinity`, which is JSON's spelling
and not TOML's: `init --join` would report success and leave behind a config
file that every later command fails to read. A value that cannot survive the
round trip is refused at the door instead.

The list is also why a secret cannot end up in a profile: `alerts.bot_token`
and `api.token` are simply not on it. A secret in a profile is not merely
undetected - it is unrepresentable, because a profile is meant to be copied
between machines, pasted into a chat, or committed to a private repository,
and the design that survives that handling is one where there is nothing in
the schema for a secret to occupy.


## The watchdog's own settings

`holdfast audit` reads one block of its own. Nothing in it is a secret, and
all of it uses native TOML types - arrays are arrays, numbers are numbers -
so a mistyped value is a configuration error rather than something that only
shows up as odd behaviour weeks later.

```toml
[audit]
state_dir = "/var/lib/holdfast/audit"

[audit.scan]
max_files = 40000
max_text_bytes = 1000000
```

`state_dir` holds `last.json`, the previous report. It is the only thing the
watchdog writes. Checks that ask "is this the same state as last time" compare
against it; with no file there they report UNKNOWN and say why, rather than
calling an unestablished state clean. The write is atomic, so an interrupted
run cannot leave a truncated file for the next one to misread.

The two scan limits bound every walk of the host tree. A real `/opt` or
`/home` can hold millions of files, and an unbounded scan is how a watchdog
becomes something operators switch off. A scan that reaches its limit reports
UNKNOWN - it has not looked at everything, so it cannot report that everything
is fine.

The empty-value rule applies here as everywhere else: a key present in this
machine's file wins even when its value is empty, and for an array that means
an empty array is an instruction - "stop treating anything as normal here" -
not an absence.


### Indicators of a campaign

Several checks want to know whether a particular address, file name, checksum
or account name has turned up. That list belongs to an incident, not to a
tool: it arrives whole, is replaced whole, and is thrown away whole when the
campaign ends. holdfast therefore ships none, and reads them from a file the
operator points at:

```toml
[audit]
indicators = "/etc/holdfast/indicators.toml"
```

The file itself:

```toml
addresses = ["203.0.113.10"]
filenames = ["planted.php"]
filename_globs = ["dump_*.json"]
accounts = ["backdoor"]

[md5]
"<the checksum>" = "where it was first seen"
```

Every key is optional. A path that points at nothing is an error rather than a
shrug: the operator asked for a sweep, and a sweep with no list that reports
"nothing found" is a lie told confidently.

With no file configured, `known_indicators` reports UNKNOWN and says so. That
is deliberate: it has not established that the machine is clean, it has
established that it was given nothing to look for. Its clean verdict, when
there is a list, names how many of each kind it checked and which file they
came from - "nothing found" without that is worth nothing.

Four other checks consult the same list - `scheduled_jobs`,
`account_changes`, `established_connections` and `container_temp` - and all of
them behave the same way without one: they answer the rest of their question
and stay quiet about indicators.

### Why some lists are empty by default

Three of the lists above ship empty, and none of them is an oversight.

`audit.ssh.allowed_login_addresses` empty means ordinary key logins are not
reported at all. A page of expected logins on every run is exactly how the one
unexpected login goes unnoticed. Password logins and root logins are reported
whatever the list says.

`audit.integrity.watch_files` empty means only the built-in set is watched,
which is already the set that matters on a standard machine.

`audit.network.allowed_out_addresses` empty means every outbound connection is
judged by its port alone. Name a peer here only when it legitimately needs an
odd one.

The rule from the top of this document applies to all of them: a key present
in this machine's file wins even when its value is empty. For an array that
means an empty array is an instruction, not an absence - and the same holds
for a zero. `audit.recent_days = 0` really does stop treating recency as news,
rather than quietly falling back to the default.

## Component keys

Each `[[component]]` table takes `type` and `name`, plus the keys of its type.
`name` is lowercase letters, digits, dash and underscore: it becomes a file
name inside the snapshot.

The same tables, written by `holdfast backup --discover` in `manual` mode,
live in `components.toml` beside `holdfast.toml`; that file is holdfast's and
is rewritten whole on every `--discover`. See [backup.md](backup.md).

| Type | Key | Default | Purpose |
|---|---|---|---|
| all | `type` | — | `path`, `postgres`, `mysql`, `docker_volume` or `command` |
| all | `name` | — | names this component, and its artifacts inside the snapshot |
| `path` | `path` | — | an absolute path to archive |
| `path` | `exclude` | `[]` | tar patterns not to archive |
| `postgres` | `user` | — | the role the dump runs as |
| `postgres` | `container` | `""` | dump through `docker exec`; empty runs the tools directly |
| `postgres` | `databases` | `["*"]` | `["*"]` asks the server; a list takes exactly those |
| `postgres` | `globals` | `true` | also dump roles and tablespaces |
| `postgres` | `defaults_file` | `""` | a libpq password file, passed as `PGPASSFILE`. holdfast never reads it |
| `postgres` | `exclude_databases` | `[]` | databases never dumped, even with `["*"]` |
| `mysql` | `container` | `""` | as above |
| `mysql` | `user` | `""` | passed as `-u`; often unnecessary with a defaults file. The rule sets `root` |
| `mysql` | `databases` | `["*"]` | as above; the server's own schemas - `information_schema`, `performance_schema`, `sys`, `mysql` - are never dumped |
| `mysql` | `defaults_file` | `""` | a my.cnf-style file the tools read themselves. holdfast never reads it |
| `mysql` | `credentials` | `""` | `"container_env"`: the password is the one in the container's own environment, expanded inside the container. Needs `container`; not together with `defaults_file` |
| `mysql` | `password_env` | `""` | with `credentials`, the name of the variable holding the password, e.g. `MYSQL_ROOT_PASSWORD`; a `_FILE` name is read as a file inside the container; empty means no password |
| `mysql` | `exclude_databases` | `[]` | databases never dumped, even with `["*"]` |
| `docker_volume` | `volume` | `""` | one volume, by name |
| `docker_volume` | `container`, `destination` | `""` | one volume, as the one `container` mounts at `destination` - the form for an anonymous volume, whose name is random. Not together with `volume` |
| `docker_volume` | `exclude` | `[]` | with none of the three keys above: volume names never archived |
| `docker_volume` | `max_mb` | `512` | with none of the three keys above: volumes larger than this are left |
| `command` | `produce` | — | a command writing the body to stdout, used exactly as written |
| `command` | `artifact` | `<name>.bin` | the file name inside the snapshot |
| `command` | `check` | `cat >/dev/null` | a command reading the body on stdin, non-zero if it is unreadable |
| `command` | `restore` | — | a table copied into the manifest as the restore recipe |
