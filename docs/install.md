# Installing holdfast

## Requirements

- Linux with systemd. Ubuntu is what CI tests; other distributions are
  expected to work, with anything they cannot answer reporting `UNKNOWN`.
- Python 3.11 or newer. Nothing from PyPI: holdfast uses the standard library
  only.
- Root. The watchdog reads files only root may read, and the backup reads
  whatever it is told to keep.

For `holdfast backup` and `holdfast restore`, also:

- `bash`: every artifact is a pipeline, and only bash reports a stage that
  failed in the middle of one. The `sh` of Ubuntu, dash, cannot.
- `tar` and `zstd`: every archive and dump is compressed with zstd.
- `age`, or `gpg` if `encryption.tool = "gpg"`. Encryption is on by default,
  and a backup refuses to start without the tool rather than write a plaintext
  copy.
- The database clients, where the components need them: `pg_dump`,
  `pg_dumpall` and `psql` to back up PostgreSQL, plus `pg_restore` and
  `pg_isready` to restore it; `mysqldump` and `mysql` for MySQL. With
  `container` set they are run inside that container, and the host needs
  `docker` instead.
- [`rclone`](https://rclone.org), only when `offsite.remote` is set - it is
  what sends the second copy. A machine that keeps every snapshot local does
  not need it. See `docs/backup.md`.

Docker is optional throughout. A machine without it is served in full, and
the checks that would ask Docker report `UNKNOWN`.

On Ubuntu or Debian:

```sh
apt-get install python3 python3-venv zstd age rclone
```

## Installing

holdfast is not on PyPI. Install it from a clone, into its own virtual
environment so that it never touches the system's Python:

```sh
git clone https://github.com/viktorplus/holdfast.git
python3 -m venv /opt/holdfast
/opt/holdfast/bin/pip install ./holdfast
ln -s /opt/holdfast/bin/holdfast /usr/local/bin/holdfast
holdfast version
```

To upgrade, pull the clone and run the same `pip install` again, then
`holdfast version` to see the new number. The configuration and everything
holdfast has recorded live outside `/opt/holdfast`, and an upgrade does not
touch them. Coming from 0.2, see "Upgrading from 0.2" in `docs/backup.md`.

## A standalone machine

```sh
holdfast init --fresh
```

This writes `holdfast.toml` in the configuration directory, `/etc/holdfast`
by default. It refuses to run if that file already exists, so a second `init`
never resets a machine by accident.

`--backup-mode auto` (the default) or `--backup-mode manual` decides how the
backup finds what to keep: worked out from Docker at every run, or written
down by `holdfast backup --discover` and kept until it is run again. It is
written as `backup.mode` and changed later by editing that line; see
`docs/backup.md`.

`--config-dir` overrides that directory, and it belongs before the
subcommand:

```sh
holdfast --config-dir /srv/holdfast init --fresh
```

After the subcommand - `holdfast init --config-dir /srv/holdfast --fresh` -
argparse rejects it as an unrecognized argument.

The file it writes:

- `host_label` - how this machine signs itself in alerts and the directory it
  owns in shared storage. Defaults to the machine's hostname; set it with
  `--host-label` when the hostname is not a good label.
- `collection` - empty. A standalone machine belongs to no collection.
- `alerts.chat_id`, `offsite.remote`, `encryption.recipients` - empty.
  Nothing is shared until something is.
- `backup.mode` - `auto`, or what `--backup-mode` said.
- `api.token` - a fresh 64-character secret, generated on the spot and never
  reused between machines.

The file is written with owner-only read and write permissions, because
`api.token` is a secret and it is in the file. That one exception is
deliberate: the token identifies this machine, so it is generated where the
machine is installed. Every other secret stays out of the file, in the
environment, as the comment at its top says - so the file is worth protecting
like any other credential, and does not belong in a backup, a shared
repository or a support ticket.

## Joining a collection

```sh
holdfast init --join /path/to/profile.toml
```

The profile is validated before anything is written: it is rejected, and
nothing is created, if it carries a block or a key holdfast does not
recognize as safe to share (see `docs/configuration.md` for the exact
allow-list). A profile cannot carry a secret by construction, so rejection
here is a schema mismatch, never a leak.

What is taken from the profile, when the profile sets it:

- `collection`
- `alerts.chat_id`
- `offsite.remote`
- `encryption.recipients`

What always stays this machine's own, never taken from the profile:

- `host_label` - from the hostname or `--host-label`, same as `--fresh`.
- `api.token` - generated fresh for this machine, so losing one machine's
  token never exposes another's.

A block the profile omits is left at its empty default, not filled in from
anywhere else: the machine keeps its own value for that block.

The copying happens once. `init --join` writes the profile's values into this
machine's `holdfast.toml` and the profile file itself plays no further part:
it is not a configuration layer, and a copy of it left in the configuration
directory is not read. Editing the profile later changes nothing on a machine
that has already joined.

## Joining a machine that is already installed

There is no command for it. `init` refuses to run when a `holdfast.toml`
already exists - that refusal is what keeps a second `init` from resetting a
working machine, and it is worth more than the convenience of re-joining.

So do it by hand. Open the machine's `holdfast.toml` and copy in the values
the profile sets, key for key:

- `collection`
- `alerts.chat_id`
- `offsite.remote`
- `encryption.recipients`

Leave `host_label` and `api.token` as they are: they belong to this machine
and to no collection. Then run `holdfast config check` to confirm the result
is complete.

Those same keys are how a value shared by a whole collection changes after
installation - on each machine, one file each. A collection here is a
convention about what machines have in common, not a control plane that can
push a change to them.

## Environment variables

| Variable | Purpose | If absent |
|---|---|---|
| `HOLDFAST_TELEGRAM_BOT_TOKEN` | The bot token used to deliver alerts to `alerts.chat_id`. | `config check` reports it missing whenever a `chat_id` is configured without it: a channel with no way to reach it. |
| `HOLDFAST_API_TOKEN` | Authenticates callers of holdfast's HTTP API, overriding the `api.token` in the machine file. | `config check` reports `api.token` missing when the machine file has none either. Nothing consumes it yet: there is no HTTP API, and `docs/how-it-works.md` says what is built. |
| `HOLDFAST_HEARTBEAT_URL` | Where this machine sends its liveness ping. | Not flagged by `config check`. Nothing in this release consumes it yet. |

## Checking the result

```sh
holdfast config check
```

reads this machine's `holdfast.toml` and the environment, then reports what
is missing - the key, why it matters, and the exact fix:

```
1 thing(s) missing:

  api.token
    why: this token is what authenticates callers of the HTTP API, which this release does not serve yet
    fix: run holdfast init, or set HOLDFAST_API_TOKEN
```

When nothing is missing it prints `configuration is complete`. The command's
exit code is `0` when the configuration is complete and `1` otherwise, so it
is safe to use in a script or a systemd health check.


## The first audit

Run the watchdog before declaring the installation finished:

```sh
holdfast audit
```

On a machine that has been running for years this reports a great deal, and
most of it will be ordinary. The allow-lists ship empty on purpose - see
`docs/configuration.md` for why - so the first run is where they get filled
in:

```sh
holdfast audit --baseline
```

That prints the report, and after it a block of TOML describing what is on
this machine right now: which ports Docker publishes, which ports the machine
talks out on, which addresses have logged in by key. Cross out whatever is not
meant to be there and paste the rest into `holdfast.toml`.

It does not write the file itself. Writing TOML back out with the standard
library would drop every comment in it, including the ones that explain what
each setting does - and crossing lines out is the part of this step that
matters.

Two more things belong in the configuration before the watchdog is much use on
a machine that serves something:

```toml
[audit.web]
framework = "laravel"   # what the application is built with
server = "nginx"        # what serves it
```

Without them the eight Application checks report UNKNOWN rather than guessing.
That is deliberate: a guess that happens to be wrong reads exactly like a
clean bill of health.

Finally, the drift checks need one run to have something to compare against.
The first `holdfast audit` records a baseline and accuses nobody; from the
second run onward a changed `sshd_config` or a new administrator is named.

## The first backup

Before the first `holdfast backup`, `holdfast.toml` needs a public key to
encrypt to. What to keep is worked out by the rule in `auto` mode; in
`manual` mode `--discover` writes it down first.

```sh
age-keygen -o key.txt        # on another machine; keep key.txt off this one
```

The `age1...` line it prints goes into `encryption.recipients` - two of them,
a key and a spare, is better. Then:

```sh
holdfast backup --discover   # manual mode only: write components.toml
holdfast backup --dry-run    # what will be taken, what will not, and why
holdfast backup
holdfast restore list --snapshot /opt/backups/latest
```

Read the dry run's `not taken:` list as carefully as what it takes, and
exclude what should not be kept with `backup.exclude`. Anything outside
Docker - a directory on the host itself - is declared by hand as a
`[[component]]`. `restore list` needs no key, so it runs here; `restore
verify` and a rehearsal need the private key and therefore run on another
machine. `docs/backup.md` describes the rule and the components, and
`docs/restore.md` the rehearsal that proves a snapshot restores.

## Running it on a schedule

holdfast has no scheduler of its own yet. cron or a systemd timer runs it,
and acts on its exit codes: `backup` exits 1 when the backup failed - and 0
when another backup was already running, which is not a failure - and
`audit` exits 1 when any check failed. In `/etc/cron.d/holdfast`:

```
# m  h  dom mon dow  user  command
30   2  *   *   *    root  /usr/local/bin/holdfast backup
0    7  *   *   *    root  /usr/local/bin/holdfast audit > /dev/null
```

Alerts are not built yet either, so nothing is sent anywhere when a run
fails: whatever runs the command is what has to notice.
