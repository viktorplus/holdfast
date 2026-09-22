# Threat model

## What this tool is

holdfast is a **state scanner taken at a point in time**, not an EDR. It is
designed to read the host filesystem and `/proc` and to query the Docker API.
It does not hook system calls, does not hold a kernel module, and does not
read an event stream. It has no view of anything that happened and vanished between two
runs.

This boundary matters more than any list of checks, because a large share of
what a scanner cannot cover sits outside it by design, not by omission.

## What it sees

- What is **left behind**: a file, a unit, a key, a line in a config, a
  running process, an open port.

## What it cannot see

- What **happened**: a run that has already finished, an injection into
  someone else's process, a one-time transfer of data.
- The **network stream** at all: the volume of outbound traffic, the contents
  of a connection, requests to algorithmically generated domains.

## Coverage by tactic

The rows below follow the shape of the MITRE ATT&CK Enterprise matrix for
Linux. Two of the row labels, `Stealth` and `Defense Impairment`, are not
ATT&CK tactic names as published; they are used here as a working split of
what used to be a single Defense Evasion tactic, because that split gives a
truer picture of what is and is not covered. This table does not claim to be
ATT&CK's taxonomy - it borrows its shape and states its own coverage against
it.

The table grades the coverage of the 52 checks `docs/checks.md` lists, each
tagged with the ATT&CK technique it answers to.

| Tactic | State | Why |
|---|---|---|
| Initial Access | out of reach | a state scanner does not see the moment of entry; indirect coverage comes from checking for pending security updates |
| Execution | out of reach | only what is still running can be seen |
| Persistence | partial | there are many ways to persist on a Linux host; this release covers a subset of them |
| Privilege Escalation | partial | container escape surfaces and sudoers semantics are not covered |
| Stealth | partial | no kernel module inventory, no detection of trace cleanup |
| Defense Impairment | weak | no firewall state, no detection of command history being disabled |
| Credential Access | good | the gap is permissions on `/etc/shadow` and spikes in failed logins |
| Discovery | out of reach | — |
| Lateral Movement | out of reach | — |
| Collection | out of reach | — |
| Command and Control | partial | outbound port exposure is checked; tunnel recognition is not |
| Exfiltration | weak | traffic volume is not visible from host state; this is a boundary, not unfinished work |
| Impact | partial | resource-hijacking (miners) is covered; ransomware activity is not recognized |

## Why UNKNOWN is a result

An honest `UNKNOWN` is not a shortcoming; it is part of the contract. The
Discovery, Lateral Movement and Collection tactics cannot be closed by a state
scanner in principle, and a check that pretended otherwise would be the
unearned green tick this project refuses to print. Where a check cannot
answer, it says so and names the command to run by hand instead.
