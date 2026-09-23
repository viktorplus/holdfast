# How holdfast works

holdfast is one command-line program with two halves that share one
configuration file:

- **the watchdog**, `holdfast audit`, looks the machine over and reports what
  it finds;
- **the backup**, `holdfast backup` and `holdfast restore`, keeps a copy of
  what matters on the machine and puts it back.

It runs on the server it protects, as root, when something starts it - there
is no daemon and no central server. Everything it remembers is a plain file on
that server. This document follows one server from installation to the day a
restore is needed, and says at each step what the program reads, what it
writes and what it refuses to do. The reference documents it points to hold
the details.

## The commands

| Command | Reads | Writes | Exit code |
|---|---|---|---|
| `holdfast init --fresh` / `--join PROFILE` | the profile, if any | `holdfast.toml` | 0, or 1 if it could not |
| `holdfast config check` | `holdfast.toml`, the environment | nothing | 0 complete, 1 something missing |
| `holdfast audit` | the filesystem, `/proc`, Docker, the journal | `last.json` | 1 if any check failed, else 0 |
| `holdfast backup --discover` | `holdfast.toml`; Docker only in `manual` mode | in `manual` mode `components.toml` (the old one kept as `.prev`); in `auto` mode nothing | 0, or 1 on a bad configuration |
| `holdfast backup --dry-run` | `holdfast.toml`, Docker in `auto` mode, `components.toml` in `manual` mode | nothing (prints what is taken, what is not and why, and the command lines) | 0, or 1 on a bad configuration |
| `holdfast backup` | the same, and what the components name | a snapshot, the journal, the offsite copy | 0, or 1 on failure |
| `holdfast restore list` | a snapshot's manifest | nothing | 0, or 1 on an unreadable snapshot |
| `holdfast restore verify` | a snapshot, the key | the journal | 0 all artifacts read back, 1 otherwise |
| `holdfast restore` | a snapshot, the key | the machine, the journal | 0, or 1 on failure |

`--config-dir DIR` goes before the command and moves the configuration from
`/etc/holdfast`. Every command's `--help` lists its options.

## The files it keeps

With the default configuration:

```
/etc/holdfast/holdfast.toml           this machine's configuration, mode 0600
/etc/holdfast/components.toml         manual mode only: what --discover found, mode 0600
/var/lib/holdfast/audit/last.json     the last audit, and what drift is measured against
/var/lib/holdfast/jobs/backup.json    last run and last success of each job:
                      verify.json       backup, verify, restore and the offsite copy
                      restore.json
                      offsite.json
/opt/backups/20260920-231500/         one snapshot per backup run
/opt/backups/latest                   a link to the newest snapshot
/run/holdfast-backup.lock             held while a backup runs
```

Every path is a configuration key - `backup.root`, `jobs.dir`,
`audit.state_dir`, `backup.lock_file`. `docs/configuration.md` lists them all.

## 1. Installing and describing the machine

```sh
holdfast init --fresh
holdfast config check
```

`init` writes `holdfast.toml` once and refuses to overwrite it afterwards. The
file names the machine (`host_label`, the hostname unless told otherwise) and
carries one secret, the machine's own `api.token`; every other secret stays in
the environment. A machine that belongs to a group of servers is installed
with `init --join profile.toml` instead, which copies the values the group
shares - where the second copies go, whose keys encrypt them - and nothing
else. `docs/install.md` covers both.

The configuration is three layers: built-in defaults, then `holdfast.toml`,
then `HOLDFAST_*` environment variables. `config check` names anything that is
missing, why it matters, and the exact fix.

## 2. Looking the machine over

```sh
holdfast audit
```

The watchdog runs 52 checks, grouped as Access, Network, Secrets and
permissions, Application, Traces of intrusion, Logs and monitoring, and
Backups. `docs/checks.md` lists every one with the ATT&CK technique it
answers to. Each check returns one of four answers:

| Answer | Meaning | Fails the run |
|---|---|---|
| `PASS` | looked, and found it in order | no |
| `WARN` | looked, and found something a person should confirm | no |
| `FAIL` | looked, and found it wrong | yes |
| `UNKNOWN` | could not look - and says why | no |

Every answer, whatever it is, comes with a `check by hand` line: the command
that settles the question without holdfast. `UNKNOWN` is a result, not an
error. A check that cannot read a file, is not allowed to ask Docker, or is
asked something only visible from outside the machine says so instead of
printing a green tick it has not earned. The exit code counts only `FAIL`, so
`holdfast audit` can sit in a timer or a pipeline unchanged; `--json` prints
the same report for a program to read.

**What it reads.** The filesystem and `/proc` under `--host`, which is `/` by
default. Pointing `--host` at a directory where the host's root is mounted -
read-only, from inside a container - audits that host instead, and the checks
that would answer for the container rather than the host (`sshd -T`,
`apt-get`) report `UNKNOWN` there rather than answer for the wrong machine.

**What it runs.** A handful of checks ask a program rather than read a file:
`docker inspect`, and `docker exec ... redis-cli` for the key-value store check;
`sshd -T` and `apt-get -s upgrade` on the host; `openssl x509` on certificates
it finds; `rclone lsjson` to confirm the second copy exists; one HTTP request
to `audit.http.url` if it is set; and `audit.admin.command` if the operator
configured one. `--no-docker` and `--no-offsite` switch off the first and the
fourth; the last two exist only if configured.

**What it writes.** `last.json` in `audit.state_dir` and nothing else. The
watchdog reports; it never changes the system.

**The first run** on a machine that has been up for years reports a lot,
because the allow-lists start empty. `holdfast audit --baseline` prints, after
the report, a block of TOML describing what this machine has now - the ports
Docker publishes, the ports it talks out on, the addresses that log in by
key. Cross out what should not be there and paste the rest into
`holdfast.toml`. The drift checks (`critical_file_hashes`, `config_drift`,
`admin_accounts`) record a baseline on their first run and name changes from
the second run onward.

## 3. Saying what is worth keeping

On a Docker host nobody has to write the list. With `backup.mode = "auto"`,
which `holdfast init` writes unless told otherwise, every backup looks at what
Docker runs and takes, by one rule: each database as a dump, each compose
project directory whole, every volume a container mounts, and every bind
mount outside the project directories. What it does not take - an orphaned
volume, a database's own data directory, system paths, what the operator
excluded in `backup.exclude` - it names, with the reason. In `manual` mode the
same rule runs only when `holdfast backup --discover` is asked to, and writes
what it found to `components.toml`, which later backups read as it stands.
`docs/backup.md` has the rule, the reasons and both modes.

What the rule cannot see - anything outside Docker - is declared in
`holdfast.toml` as `[[component]]` tables, which are also how the operator
overrides the rule for a container or a path:

```toml
[[component]]
type = "path"
name = "shop"
path = "/opt/shop"
exclude = ["*/cache/*"]

[[component]]
type = "postgres"
name = "shop-db"
container = "shop-postgres"
user = "shop"
databases = ["*"]
globals = true
```

Five types: `path`, `postgres`, `mysql`, `docker_volume` and `command` - the
last runs the operator's own command for anything the other four do not
cover. `docs/backup.md` describes each, and `docs/configuration.md` their keys.

```sh
holdfast backup --dry-run
```

prints, per component, where it came from, then each artifact and the exact
shell line the backup will run, and after them what was not taken and why:

```
shop (path, from holdfast.toml)
  shop.tar.zst.age
    ls -d -- /opt/shop >/dev/null && tar --warning=no-file-changed --ignore-failed-read --exclude='*/cache/*' -cf - -C / opt/shop | zstd -T0 -10 -q | age --encrypt -r age1...
```

## 4. Making a snapshot

```sh
holdfast backup
```

A run goes in this order, and the order is the point:

1. **Everything that can be refused is refused first**, before a byte is
   written: an encryption setting that would leave the copy readable, a
   missing `bash` or encryption tool, a component that does not make sense,
   a Docker that does not answer, another backup already running, too little
   free disk for what the rule estimates it will take. A backup already
   running is not an error - the command says so and exits 0, because the
   nightly timer overlapping a manual run is ordinary.
2. **Each component's artifacts are produced** into a hidden directory,
   `.20260920-231500.tmp`. Every artifact is one pipeline - the producer, then
   `zstd`, then `age` or `gpg` - whose output goes straight into the file, so
   the unencrypted bytes never touch the disk. The pipelines run under
   `bash -o pipefail`, so a stage that dies in the middle fails the artifact
   even when the last stage succeeded.
3. **Any failure ends the run.** A component that cannot produce its
   artifacts - a database container that is not running, a directory that is
   not there, a dump that came out empty - takes the whole temporary directory
   with it. So does a run in which nothing was produced at all. A backup that
   skips what it could not reach and reports success is the failure this tool
   exists to prevent.
4. **The snapshot is described**: `manifest.json`, in plaintext, says which
   machine it came from, what each component was, and for each artifact its
   size, `sha256`, the recipe for putting it back and the command that checks
   it. `SHA256SUMS` covers every file in the format `sha256sum -c` reads.
5. **Only then is it renamed** to its final name, and `latest` pointed at it.
   An interrupted run leaves nothing that could be mistaken for a backup.
6. **Old snapshots are rotated** - those older than `backup.retention_days`
   are deleted, but never the newest one, and never before the new one is
   complete.
7. **The journal records the run**, successful or not, in `backup.json`.
8. **The snapshot is sent to the second place**, if one is configured - see
   the next step.

The private key that opens a snapshot is never on this machine. holdfast
encrypts to public keys - list a primary and a spare in
`encryption.recipients` - and a key is needed only to read a snapshot, which
happens somewhere else, later. `docs/backup.md` explains the snapshot layout,
the encryption rules and every refusal.

## 5. The second copy

A snapshot that never leaves the machine is lost with it. When
`offsite.remote` names an [rclone](https://rclone.org) remote, each finished
snapshot is copied there, under the machine's own `host_label`:

```
shared:backups/
  web-1/20260920-231500/
  web-2/20260920-231500/
```

The copy is listed back and counts only if every file arrived with its size.
A failed copy is not a failed backup - the local snapshot is whole - so it
becomes a warning on the finished run, and `offsite.json` records it, which
keeps the watchdog's `backup_offsite_copy` check failing until the copy
works again. holdfast never deletes anything on the remote, so the storage
account it uses needs to write and list, and nothing more. Its credentials
live in `rclone.conf`, outside holdfast's configuration.

## 6. Proving it restores

A backup nobody has restored from is a hope. Three commands, each claiming
more than the one before:

```sh
holdfast restore list   --snapshot /opt/backups/20260920-231500
holdfast restore verify --snapshot /opt/backups/20260920-231500 --identity key.txt
holdfast restore        --snapshot /opt/backups/20260920-231500 --identity key.txt \
  --root /tmp/drill --component shop
```

- `list` reads only the manifest and needs no key: what is in this copy.
- `verify` decrypts every artifact and runs its format check - an archive's
  table of contents, a dump's signature - without writing anything, and
  records the result in `verify.json`.
- `restore --root` is a rehearsal: file components land in a scratch
  directory instead of over the live ones. Volumes and databases have no
  scratch copy to land in, so a rehearsal is limited to file components.

The watchdog's `restore_tested` check reads the journal: it fails on a machine
that has never been restored from, warns while only `verify` has been run -
reading is not restoring - and warns again once the last restore is more than
90 days old.

## 7. The bad day

On the machine being restored - the same one, or a new one installed with
`holdfast init` - fetch the snapshot back if the local copy is gone, and
restore it:

```sh
rclone copy shared:backups/web-1/20260920-231500 /opt/backups/20260920-231500
holdfast restore --snapshot /opt/backups/20260920-231500 --identity /media/usb/key.txt
```

Before anything is written, the restore checks that the snapshot was taken on
this machine (a different `host_label` needs `--from-other-host`), that every
recipe is one it can carry out, every checksum, and the key. It then prints
the plan and asks for the machine's label to be typed back; `--yes` skips
that, for scripts.

It restores in the only order that works, which is not the manifest's: files
and volumes first, with the containers that use them stopped, then the
containers back up, then the database dumps, globals before databases. If it
fails in the middle, the containers it stopped are started again and it says
the machine is `PARTIALLY RESTORED`; the recipes can be repeated, so the same
command is run again once the cause is fixed. `docs/restore.md` has the rest.

## Running it every night

holdfast has no scheduler of its own. cron or a systemd timer runs it, and the
exit codes above are what they act on. For example, in `/etc/cron.d/holdfast`:

```
# m  h  dom mon dow  user  command
30   2  *   *   *    root  /usr/local/bin/holdfast backup
0    7  *   *   *    root  /usr/local/bin/holdfast audit > /dev/null
```

Neither sends a message on its own yet: alerts are not built. Until they are,
whatever runs the command is what notices a non-zero exit.

## What holdfast never does

- change the system while it audits it;
- keep a private key, or write an unencrypted copy when encryption is on;
- delete anything on the remote;
- skip a component it could not back up and report success;
- restore over live data without checking the snapshot, the key and the
  machine first;
- talk to another holdfast. There is no central server; a group of machines is
  a shared profile, not a control plane.

`docs/threat-model.md` says what a point-in-time scanner like this can and
cannot see.
