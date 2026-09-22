"""Refuse to publish what belongs to one estate.

The rules here match SHAPES, never values: a routable address, a key block, a
bot token, a long hex secret, a mail address. That is deliberate. A guard that listed the domains it
protects would publish them on every clone - it would be the leak it exists to
prevent. Literals specific to one estate come from --extra-patterns, a file
that is never committed to this repository.
"""

from __future__ import annotations

import argparse
import ipaddress
import re
import sys
from pathlib import Path

SHAPES: dict[str, re.Pattern[str]] = {
    "routable_ipv4": re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
    "private_key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY(?: BLOCK)?-----"),
    # An age identity: the prefix, then 58 characters of bech32 in capitals.
    "age_identity": re.compile(
        r"AGE-SECRET-KEY-1[QPZRY9X8GF2TVDW0S3JN54KHCE6MUA7L]{58}"
    ),
    "bot_token": re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b"),
    "long_token": re.compile(r"\b[0-9a-fA-F]{32,}\b"),
    "mail_address": re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"),
}

DOC_NETS = tuple(
    ipaddress.ip_network(net)
    for net in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")
)

EXAMPLE_DOMAINS = ("example.com", "example.org", "example.net", "localhost")

SKIP_DIRS = {
    ".git",
    "__pycache__",
    ".venv",
    "node_modules",
    ".pytest_cache",
    ".ruff_cache",
    "dist",
    "build",
}

# Build output the guard has no business reading: `pip install -e .` writes
# holdfast.egg-info, whose PKG-INFO copies whatever pyproject.toml says -
# an author mail address, for one - straight back into the tree.
SKIP_DIR_SUFFIXES = (".egg-info",)

BINARY_SUFFIXES = {
    ".png",
    ".jpg",
    ".gif",
    ".ico",
    ".pdf",
    ".zst",
    ".gz",
    ".tar",
    ".whl",
    ".so",
}


def _address_is_allowed(text: str) -> bool:
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return True  # not an address at all, e.g. a version
    if address.is_private or address.is_loopback or address.is_reserved:
        return True
    if address.is_multicast or address.is_link_local or address.is_unspecified:
        return True
    return any(address in net for net in DOC_NETS)


def _token_is_allowed(text: str) -> bool:
    # The shape this project is most likely to leak is its own api.token: 64
    # hex characters in a holdfast.toml. The one long hex run in this tree is
    # the example config's token, written as 64 zeros, and the allowance is
    # drawn exactly around that: a run of one repeated character is an
    # obviously-fake placeholder, the same judgement the RFC 5737 ranges get.
    # Two distinct characters and it is treated as a real secret.
    return len(set(text)) == 1


def _mail_is_allowed(text: str) -> bool:
    host = text.rsplit("@", 1)[-1]
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        # a user@host with an IP host is not mail: routable_ipv4 owns it,
        # and owns it once - this is deduplication, not a loophole
        return True
    # The domain has to BE an example domain, or sit under one. Matching the
    # tail of the whole address would let notexample.com through.
    host = host.lower()
    return any(
        host == domain or host.endswith(f".{domain}") for domain in EXAMPLE_DOMAINS
    )


def scan_text(
    text: str, extra: list[re.Pattern[str]] | None = None
) -> list[tuple[int, str, str]]:
    hits: list[tuple[int, str, str]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        for rule, pattern in SHAPES.items():
            for match in pattern.finditer(line):
                found = match.group(0)
                if rule == "routable_ipv4" and _address_is_allowed(found):
                    continue
                if rule == "long_token" and _token_is_allowed(found):
                    continue
                if rule == "mail_address" and _mail_is_allowed(found):
                    continue
                hits.append((number, rule, found))
        for pattern in extra or []:
            for match in pattern.finditer(line):
                hits.append((number, "extra", match.group(0)))
    return hits


def _load_extra(path: Path | None) -> list[re.Pattern[str]]:
    if path is None:
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    # Strip before deciding it is a comment: an indented `#` used to be
    # compiled as a regex, so a note with a bracket in it crashed the guard
    # instead of reporting what it had found.
    return [
        re.compile(stripped)
        for line in lines
        if (stripped := line.strip()) and not stripped.startswith("#")
    ]


def scan_tree(
    root: Path, extra_path: Path | None = None
) -> list[tuple[Path, int, str, str]]:
    extra = _load_extra(extra_path)
    findings: list[tuple[Path, int, str, str]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(
            part in SKIP_DIRS or part.endswith(SKIP_DIR_SUFFIXES) for part in path.parts
        ):
            continue
        if path.name == ".env" or path.name.startswith(".env."):
            findings.append((path, 0, "env_file", path.name))
            continue
        if path.suffix.lower() in BINARY_SUFFIXES:
            continue
        try:
            raw = path.read_bytes()
        except OSError as exc:
            findings.append((path, 0, "unscannable", type(exc).__name__))
            continue
        if b"\x00" in raw:
            # A NUL means this is not the single-byte text the rules are
            # written for: a binary whose suffix is not on the list above, or
            # text in a wide encoding, where an address is stored with a zero
            # between every character and no rule here can see it. Silence
            # would be the wrong answer - the guard would be reporting on a
            # file it never read. (This comment cannot show the example: the
            # guard reads its own source, as it just proved.)
            findings.append((path, 0, "unscannable", "NUL byte: not scannable text"))
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            # latin-1 maps every byte to a character and cannot fail. The
            # shapes here are ASCII, so a document in cp1251 or another
            # single-byte encoding is scanned for real instead of skipped.
            text = raw.decode("latin-1")
        for number, rule, found in scan_text(text, extra):
            findings.append((path, number, rule, found))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="privacy_guard",
        description="Fail the build when private data reaches this tree.",
    )
    parser.add_argument("root", nargs="?", default=".")
    parser.add_argument(
        "--extra-patterns",
        default=None,
        help="file of regexes specific to one estate; never "
        "committed to this repository",
    )
    args = parser.parse_args(argv)

    extra = Path(args.extra_patterns) if args.extra_patterns else None
    findings = scan_tree(Path(args.root), extra)
    if not findings:
        print("privacy guard: clean")
        return 0

    print(f"privacy guard: {len(findings)} finding(s)")
    for path, number, rule, found in findings:
        where = f"{path}:{number}" if number else str(path)
        print(f"  {where}: {rule}: {found}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
