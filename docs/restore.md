# Restore

```sh
holdfast restore list   --snapshot /srv/backups/20260920-231500
holdfast restore verify --snapshot /srv/backups/20260920-231500 --identity key.txt
holdfast restore        --snapshot /srv/backups/20260920-231500 --identity key.txt
```

Three commands over one snapshot, and they differ in how much they claim.

| Command | Needs a key | Writes | Answers |
|---|---|---|---|
| `list` | no | nothing | what is in this copy |
| `verify` | yes, if encrypted | the journal only | whether it reads back |
| `restore` | yes, if encrypted | the machine | puts it back |

`list` opens nothing, because looking inside a copy comes before locating the
key it was sealed with, and often before deciding whether this is the copy
worth restoring at all.

`verify` reads every artifact through the key and through its own format check
— an archive's table of contents, a dump's signature — and writes nothing but
the journal. It checks every artifact rather than stopping at the first
failure: a second run in the middle of an incident is another half hour, and
the answer wanted then is the whole list. It checks the artifacts no recipe can
put back as well, because those bytes are in the copy too and a copy is either
whole or it is not.

## What is checked before anything is written

In this order, while nothing has been touched:

1. **the machine.** The manifest carries the `host_label` of the machine the
   snapshot was taken on. If it differs from this machine's, the restore stops
   and names both. Restoring one machine's data onto another is almost never
   meant, and `--from-other-host` is how to say that it is this time. A machine
   with no label of its own is refused too: there is then nothing to check
   against, and saying so is more use than guessing.
2. **every recipe**, including the ones a narrowed run will not touch. A
   snapshot carrying an instruction this version cannot carry out is not one
   that can be honestly restored in part.
3. **every checksum** of the artifacts about to be used. Not "run `verify`
   first, then trust it": that means writing a corrupt artifact into production
   the one time somebody was in a hurry.
4. **the key**, against the smallest artifact of each kind of encryption
   present. The smallest, because this is pure waiting and a run that will fail
   on the key should fail in a second rather than after a multi-gigabyte dump.
5. **the operator.** The plan is printed — every recipe, every file, the
   containers that will be stopped — and then the machine's label has to be
   typed back. `--yes` skips it, and is meant for scripts.

With no terminal and no `--yes`, the restore says so and stops rather than
blocking on a read that will never return. That is the first thing that happens
in CI or in a cron job.

## The order, and why it is not the manifest's

| Phase | What goes in | Why here |
|---|---|---|
| 1 | `path`, `docker_volume` | they write files a running service holds open |
| — | the containers come back up | |
| 2 | `pg_globals`, then `pg_database`, then `mysql_database` | a dump is loaded by a running server |

Globals strictly before the databases their roles own: a database restored
before its roles exist belongs to nobody.

The containers to stop come from two places. A database recipe names its
container, because the component declared one, and so does an anonymous
volume's recipe. A named volume names nothing — a volume says nothing about
who mounts it — so Docker is asked, and asked about stopped containers too: a
stopped one will be started again, and it must not come back to a volume that
was replaced underneath it.

## An anonymous volume

A volume compose created without a name gets a random one, and a different one
on every machine the application is brought up on. So its recipe does not
keep the name. It keeps the container and the path the volume is mounted at,
and the restore asks Docker which volume that container mounts there **now**,
and pours the data into that one - emptied first, as any volume is. Creating a
volume under the old name would put the data where nothing reads it, and the
site would come up empty.

That question is asked before anything is stopped. With no such container, or
no volume at that path, the restore stops there with nothing changed:

```
holdfast restore: docker-volumes/myapp-wordpress-1--var-www-html.tar.zst.age: the container 'myapp-wordpress-1' has no volume at /var/www/html; bring the application up first (docker compose up -d) and try again. Nothing has been changed.
```

On a new machine the order is therefore: put the project directory back,
`docker compose up -d` in it, then restore the volumes and the databases. A
named volume is restored by its name, and created if it is not there.

## A MySQL database and its password

A dump goes back in with the credentials it came out with. A recipe with
`credentials = "container_env"` loads through the container's own shell and
the variable its recipe names, exactly as the dump ran; a recipe with a
`defaults_file` passes it to `mysqladmin` and `mysql` as the first flag. A
restore on a new machine therefore needs the database container started with
the same variable, or the same file at the same path inside it.

The server's `mysql` schema - users and grants - is not in a dump taken by
0.3.0 or later, so a restored database is used through the users the new
container was started with.

A container that will not stop ends the restore instead of being skipped,
because the next step writes into files it has open.

## When it fails in the middle

The containers this run took down go back up, and the message says the machine
is **PARTIALLY RESTORED** — some recipes ran and some did not — and that the
same command can be run again once the cause is fixed. Half-restored and
switched off is worse than either on its own.

The recipes can be repeated. A path extracts over itself, a volume is emptied
and refilled, a database is dropped and recreated by `pg_restore --clean`.

## The key

```sh
holdfast restore --snapshot DIR --identity /media/usb/key.txt
```

The key is read where it lies, for the length of one run, and copied nowhere.
An age identity is recognised by what is in it. Anything else is tried as a gpg
secret key, which needs a keyring — so it gets a throwaway one, and that
keyring **and the agent gpg starts behind it** are destroyed on the way out,
including out of a failed run. Removing the directory alone would leave the
agent running with the private key in memory.

A gpg key kept offline is normally passphrase-protected, and a rescue box has
no pinentry for gpg to ask through; `--identity-passphrase-file` supplies it
and the key is opened without a terminal.

Which tool encrypted an artifact is read from the file's own name, never from
the manifest. A snapshot is opened on the worst day a machine has, often on a
machine with nothing configured, and the promise is that the artifacts and the
key are enough.

## Rehearsing

```sh
holdfast restore --snapshot DIR --identity key.txt --root /tmp/drill \
  --component config --no-stop
```

`--root` re-bases every `path` recipe, so the files land in a scratch directory
instead of over the live ones. It is the only way anybody finds out whether a
snapshot restores before the day they need it to, and `verify` plus a rehearsal
is what the watchdog's `restore_tested` check will be reading.

Only files can be rehearsed this way. A volume or a database has no scratch
copy to go into, so a `--root` run whose scope includes one is refused before
anything happens; `--component` narrows it to a file component.

`--dry-run` prints the plan and changes nothing at all, including the
containers.

`--component NAME` narrows the work to one component — but every recipe in the
snapshot is still checked, because the question "can this snapshot be restored"
does not have a partial answer.

## What this cannot do

A Docker volume is restored by writing into the directory Docker says the
volume lives in. holdfast running **inside a container**, without that
directory mounted, cannot do it, and will say so rather than appear to succeed:
the mountpoint will not be a directory it can write to. The same is true under
rootless Docker and under a non-local volume driver.

An artifact from a `command` component with no declared `restore` table is not
put back. It is named in `list`, named again in the plan, and named in the
result, so that a restore cannot report "done" about data it never touched.
