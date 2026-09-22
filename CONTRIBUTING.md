# Contributing

## Running the tests

```sh
python -m pytest -v
ruff check .
ruff format --check .
python -m tools.privacy_guard .
```

All four are what CI runs. `pytest` is the test suite; the two `ruff`
invocations check linting and formatting without changing anything; the
privacy guard scans the tree for anything that looks like private data,
starting from the repository root by default.

## Commit messages

Write the subject in the imperative mood - "Add", not "Added" or "Adds" - and
say why the change is being made, not only what changed; the diff already
shows what changed. One commit carries one concern: if the message needs
"and" to describe it, it is probably two commits.

## The privacy guard

`tools/privacy_guard.py` matches **shapes**, not values: a routable IPv4
address, a PEM private-key header, a Telegram-style bot token, a long hex
secret such as an `api.token`, a mail address. It does not carry a list of
the domains, hosts or addresses it protects, because a guard that did would
publish them on every clone - it would be the leak it exists to prevent.

A long hex run whose characters are all identical is allowed through, on the
same grounds as the documentation address ranges: a run of 64 zeros is a
placeholder on its face. The allowance stops there - two distinct characters
in the run and the guard treats it as a real secret. A secret shaped like
base64 rather than hex is not matched by this rule.

The rule cannot tell a secret from anything else of that shape, so a full
40-character commit SHA, a sha256 digest or an md5 checksum quoted in a
document or a release note will turn the `privacy` job red. There is no
allow-list to add one to: `--extra-patterns` only ever adds patterns. Keep
such a reference under 32 characters - an abbreviated SHA identifies a
commit well enough - or leave it out. This file cannot show you an example
of the problem, for exactly the same reason.

A version number of four parts is the same story: four numbers joined by
dots is precisely what an address looks like, and the small ones are real,
routable addresses. Quoting another project's four-part build number will
turn the `privacy` job red, and this file cannot show you that example
either. This project numbers itself with three parts, which is never
matched; for someone else's, name the release rather than its number.

Letting the match through when a `v` or the word "version" sits beside it
was considered and refused. A note about a rollout is exactly where an
address appears next to the word version, so the exemption would open a
hole the shape of the problem it solves. The guard is allowed to be wrong
in the direction that costs a sentence, not in the direction that costs a
disclosure.

Anything specific to one estate - a hostname, a codename, an internal
service name - is not a shape the guard can recognize on its own. Pass it a
file of extra regular expressions with `--extra-patterns`:

```sh
python -m tools.privacy_guard . --extra-patterns /path/to/estate-patterns.txt
```

One pattern per line; blank lines and comment lines are ignored, whether the
`#` is indented or not. That file is never committed to this repository; it
lives wherever the estate that needs it keeps its own secrets. If you do
keep it in a clone while you work, call it `estate-patterns.txt`: that name
is in `.gitignore`, alongside `holdfast.toml` and `profile.toml`, so that
trying the tool inside a clone - `holdfast --config-dir . init --fresh` -
cannot commit a live `api.token`.

**On encodings:** the guard used to read every file as UTF-8 and silently
skip whatever failed, so the same address planted in three copies of a file -
UTF-8, cp1251, UTF-16 - was caught in the first and missed in the other two
without a word. Now a file that is not valid UTF-8 is read as Latin-1
instead, which cannot fail and leaves the ASCII shapes intact, so a document
in a single-byte encoding is scanned for real.

A wide encoding is a different matter: in UTF-16 an address is stored with a
zero byte between every character, and no rule here can match that. Rather
than pass such a file in silence, the guard reports it as `unscannable` - a
finding that says "this was not read", not "this is a leak". The same applies
to a binary whose suffix is not in `BINARY_SUFFIXES`. If the `privacy` job
names a file that way, either add its suffix to that list or re-save it as
UTF-8; do not make the guard quiet about it again.

## Adding a check

A check is a function registered with the `check` decorator in
`holdfast/audit/registry.py`. It declares five things about itself:

```python
@check("ld_so_preload", GROUP_INTRUSION, "Preloaded libraries", "T1574.006", "high")
def _ld_so_preload(ctx): ...
```

`group` is how the finding is presented to whoever is on duty; `technique` is
the ATT&CK identifier and exists so coverage can be counted. A check declares
both and never has to choose between them. Both are validated at import time:
an unknown group or severity raises rather than surfacing in a report.

The function takes a `Context` and returns `(status, detail, manual)`, with an
optional fourth element - `data` - carrying material for the next run to
compare against, such as file hashes. Four rules bind it:

- **Read only.** Nothing under `holdfast/audit/` writes to the host. The only
  file the watchdog writes is its own state.
- **Go through the context.** `ctx.path()`, `ctx.read_text()`, `ctx.walk()`.
  A check that opens a host path directly works on a developer's machine and
  audits the wrong filesystem inside a container.
- **UNKNOWN, never a green tick you did not earn.** A bounded scan that hits
  its limit has not established that the host is clean, and must say so.
- **Never quote what you found.** A finding names the file, the line, the
  variable, the count. Reports are rendered in a browser and forwarded to a
  chat; a check that printed its evidence would be the leak it looks for.

New check modules are listed by name in `CHECK_MODULES`. Discovery by walking
the package was rejected: it turns a typo in a filename into "there are fewer
checks today", which nobody notices, because the report still renders and
still says PASS.

`docs/checks.md` is generated - `holdfast audit --coverage --markdown` - and a
test compares the committed file with the register. Editing it by hand only
makes the test fail.

**On writing out a PEM banner:** `holdfast/audit/checks/secrets.py` hunts for
private key material, so it has to know what the banners look like, and the
privacy guard looks for exactly those banners. Both are right. The markers are
therefore assembled from parts rather than written out as literals. If you add
a key format, assemble it the same way; do not teach the guard to ignore the
file.

**On campaign indicators:** the checks that sweep for known-bad addresses,
file names, checksums and accounts read them from a file supplied at run time,
never from a list in this repository. Two reasons, and the second is the one
that matters.

A list of indicators belongs to an incident. It is right for a few weeks and
wrong afterwards, and a stale indicator nobody owns produces a false alarm
that outlives whoever added it. Keys in a config file would have to be cleaned
out by hand; a file can be replaced or deleted whole.

And an indicator is somebody's real address or somebody's real malware sample.
Committing one would publish the details of an incident in a repository that
exists partly to keep such details out. If you add a check that consults the
list, take it from `ctx.indicators()` and do not write one into the source.

With no list configured, such a check reports UNKNOWN rather than PASS. It has
not established that the machine is clean; it has established that it was
handed nothing to look for, and those are different statements.

**On falling back to a default with `or`:** do not. Every setting has its
default in `config.DEFAULTS`, so a value always arrives, and writing
`ctx.conf_int("audit.recent_days") or 14` silently overrides a configured
zero. Zero is a legitimate instruction here, and the whole configuration layer
is built on a key present in a higher layer winning even when its value is
empty.

**On the web profile:** the Application checks ask
`holdfast/audit/webprofile.py` what stack is being served, and that comes from
`audit.web.framework` and `audit.web.server`. An undeclared stack reports
UNKNOWN; a declared one this release does not know reports UNKNOWN with a
different sentence. Both matter: "nothing was declared" and "something was
declared that I cannot read" lead to different actions.

Do not add a fallback that assumes a stack because a familiar directory
happens to exist. A guess that is wrong reads exactly like a clean bill of
health, which is the failure this whole tool is built to avoid. Adding support
for another stack means adding an entry to `FRAMEWORKS` or `SERVERS`, and the
checks pick it up unchanged.

**On `audit --baseline`:** it prints a block of TOML and writes nothing.
Rewriting `holdfast.toml` from a parsed document would drop every comment in
the file - `tomllib` reads, it does not round-trip - and those comments are
where each setting is explained. Crossing lines out before pasting is also the
point of the step: the operator is declaring what is meant to be there, not
accepting whatever happens to be running.
