"""Tests for the privacy guard.

Every offending value here is assembled from parts. Written as a literal, it
would be found by the guard when it scans this repository - and the honest fix
is to build the sample, not to teach the guard to skip its own tests.
"""

import re
from pathlib import Path

from tools.privacy_guard import main, scan_text, scan_tree

ROUTABLE = ".".join(["9", "9", "9", "9"])  # noqa: FLY002 - a literal here would be found by the guard
OTHER_ROUTABLE = ".".join(["8", "8", "4", "4"])  # noqa: FLY002 - a literal here would be found by the guard
KEY_BLOCK = "-----BEGIN " + "OPENSSH PRIVATE" + " KEY-----"
PGP_BLOCK = "-----BEGIN " + "PGP PRIVATE KEY" + " BLOCK-----"
AGE_KEY = "AGE-SECRET-KEY-" + "1" + "QPZRY9X8GF2TVDW0S3JN54KHCE6MUA7L" * 2
BOT_TOKEN = "1234567890" + ":" + "AAF" + "abcdefghijklmnopqrstuvwxyz0123456"
REAL_MAIL = "someone" + "@" + "somecompany" + ".net"
LONG_TOKEN = "deadbeef" * 8  # the shape of a live api.token, built in parts
PLACEHOLDER_TOKEN = "0" * 64


def test_documentation_addresses_are_allowed():
    """RFC 5737 ranges exist so that examples do not name real machines."""
    assert scan_text("connect to 203.0.113.5 and 192.0.2.1") == []


def test_private_ranges_are_allowed():
    assert scan_text("127.0.0.1 10.0.0.5 192.168.1.1 172.16.0.1") == []


def test_a_routable_address_is_caught():
    hits = scan_text(f"server lives at {OTHER_ROUTABLE}")
    assert [rule for _, rule, _ in hits] == ["routable_ipv4"]


def test_a_private_key_block_is_caught():
    hits = scan_text(KEY_BLOCK)
    assert [rule for _, rule, _ in hits] == ["private_key"]


def test_a_gpg_key_block_is_caught():
    """The backup's own key formats are the ones this project is likeliest to
    leak, and the PGP banner ends in BLOCK where the others end in KEY."""
    hits = scan_text(PGP_BLOCK)
    assert [rule for _, rule, _ in hits] == ["private_key"]


def test_an_age_identity_is_caught():
    hits = scan_text(f"key = {AGE_KEY}")
    assert [rule for _, rule, _ in hits] == ["age_identity"]


def test_a_bot_token_is_caught():
    hits = scan_text(f"token = {BOT_TOKEN}")
    assert [rule for _, rule, _ in hits] == ["bot_token"]


def test_a_long_hex_token_is_caught():
    """A holdfast.toml carrying its api.token is the leak most likely here."""
    hits = scan_text(f'token = "{LONG_TOKEN}"')
    assert [rule for _, rule, _ in hits] == ["long_token"]


def test_an_all_identical_run_is_an_obvious_placeholder():
    """The example config's token is 64 zeros: fake on its face, not a leak."""
    assert scan_text(f'token = "{PLACEHOLDER_TOKEN}"') == []


def test_a_config_file_with_a_live_token_is_caught(tmp_path: Path):
    (tmp_path / "holdfast.toml").write_text(
        f'host_label = "web-1"\n\n[api]\ntoken = "{LONG_TOKEN}"\n', encoding="utf-8"
    )
    hits = scan_tree(tmp_path)
    assert [rule for _, _, rule, _ in hits] == ["long_token"]


def test_an_example_mail_address_is_allowed():
    assert scan_text("write to admin@example.com") == []


def test_a_subdomain_of_an_example_domain_is_allowed():
    assert scan_text("write to admin@mail.example.com") == []


def test_a_domain_that_merely_ends_in_an_example_domain_is_caught():
    """notexample.com is somebody's real domain, not a documentation one."""
    hits = scan_text("write to ops@" + "notexample" + ".com")
    assert [rule for _, rule, _ in hits] == ["mail_address"]


def test_a_domain_that_merely_contains_example_is_caught():
    hits = scan_text("write to ops@" + "myexample" + ".net")
    assert [rule for _, rule, _ in hits] == ["mail_address"]


def test_a_real_looking_mail_address_is_caught():
    hits = scan_text(f"write to {REAL_MAIL}")
    assert [rule for _, rule, _ in hits] == ["mail_address"]


def test_extra_patterns_catch_what_shapes_cannot():
    hits = scan_text("deploy to bigcorp-prod-01", extra=[re.compile("bigcorp")])
    assert [rule for _, rule, _ in hits] == ["extra"]


def test_an_extra_patterns_file_is_read_line_by_line(tmp_path: Path):
    """Comments, indented comments and blank lines are not patterns."""
    patterns = tmp_path / "estate-patterns.txt"
    patterns.write_text(
        "# what this estate calls its machines\n"
        "  # (indented, and not a valid regex if it were compiled\n"
        "\n"
        "bigcorp\n",
        encoding="utf-8",
    )
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "deploy.md").write_text("deploy to bigcorp-prod-01\n", encoding="utf-8")

    hits = scan_tree(tree, patterns)

    assert [(rule, found) for _, _, rule, found in hits] == [("extra", "bigcorp")]


def test_a_clean_tree_passes(tmp_path: Path):
    (tmp_path / "ok.py").write_text("host = '203.0.113.9'\n", encoding="utf-8")
    assert scan_tree(tmp_path) == []


def test_a_planted_file_is_found(tmp_path: Path):
    (tmp_path / "ok.py").write_text("nothing here\n", encoding="utf-8")
    (tmp_path / "leak.md").write_text(f"ssh root@{ROUTABLE}\n", encoding="utf-8")
    hits = scan_tree(tmp_path)
    assert len(hits) == 1
    assert hits[0][0].name == "leak.md"


def test_build_output_is_not_scanned(tmp_path: Path):
    """egg-info copies pyproject.toml back into the tree; that is not a leak."""
    egg = tmp_path / "holdfast.egg-info"
    egg.mkdir()
    (egg / "PKG-INFO").write_text(f"Author-email: {REAL_MAIL}\n", encoding="utf-8")
    assert scan_tree(tmp_path) == []


def test_a_leak_in_another_single_byte_encoding_is_caught(tmp_path: Path):
    """cp1251 used to be skipped outright, and the leak in it with it."""
    leak = f"сервер ssh root@{ROUTABLE}\n"
    (tmp_path / "notes.txt").write_bytes(leak.encode("cp1251"))

    hits = scan_tree(tmp_path)

    assert [rule for _, _, rule, _ in hits] == ["routable_ipv4"]


def test_a_file_the_rules_cannot_read_is_reported_not_skipped(tmp_path: Path):
    """utf-16 stores the address with a zero between every character.

    No rule here can match that, so the honest answer is to say the file was
    not scanned rather than to pass it in silence.
    """
    leak = f"ssh root@{ROUTABLE}\n"
    (tmp_path / "wide.txt").write_bytes(leak.encode("utf-16"))

    hits = scan_tree(tmp_path)

    assert [rule for _, _, rule, _ in hits] == ["unscannable"]


def test_a_known_binary_suffix_is_still_skipped(tmp_path: Path):
    """The NUL rule must not turn every image in a tree into a finding."""
    (tmp_path / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0d")
    assert scan_tree(tmp_path) == []


def test_an_env_file_is_refused_by_name_alone(tmp_path: Path):
    (tmp_path / ".env").write_text("nothing incriminating\n", encoding="utf-8")
    hits = scan_tree(tmp_path)
    assert [rule for _, _, rule, _ in hits] == ["env_file"]


def test_the_guard_exits_zero_on_a_clean_tree(tmp_path: Path):
    (tmp_path / "ok.py").write_text("fine\n", encoding="utf-8")
    assert main([str(tmp_path)]) == 0


def test_the_guard_exits_nonzero_on_a_planted_tree(tmp_path: Path, capsys):
    """The guard that never fires has never been shown to work."""
    (tmp_path / "leak.md").write_text(f"ssh root@{ROUTABLE}\n", encoding="utf-8")
    assert main([str(tmp_path)]) == 1
    assert "leak.md" in capsys.readouterr().out
