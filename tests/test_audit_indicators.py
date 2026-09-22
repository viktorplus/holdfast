"""The indicator list, which holdfast ships empty on purpose."""

from __future__ import annotations

import hashlib

import pytest
from support import context

from holdfast.audit.indicators import IndicatorError, load_indicators

# Addresses here come from RFC 5737, the documentation ranges. Anything else
# would be someone's machine, and the privacy guard is right to refuse it.
# The checksum is computed, not written out: a 32-character hex literal is
# indistinguishable from a token, and the privacy guard refuses one.
SAMPLE_MD5 = hashlib.md5(b"").hexdigest()

SAMPLE = f"""
addresses = ["203.0.113.10"]
filenames = ["planted.php"]
filename_globs = ["dump_*.json"]
accounts = ["backdoor"]

[md5]
"{SAMPLE_MD5}" = "first seen in the web root"
"""


def test_no_path_gives_an_empty_list():
    assert load_indicators(None).empty is True


def test_a_blank_path_gives_an_empty_list():
    assert load_indicators("   ").empty is True


def test_a_missing_file_is_an_error_not_a_silence(tmp_path):
    """The operator asked for a sweep; running without one would fake it."""
    with pytest.raises(IndicatorError) as caught:
        load_indicators(tmp_path / "absent.toml")
    assert "does not exist" in str(caught.value)


def test_broken_toml_is_reported_with_the_path(tmp_path):
    path = tmp_path / "iocs.toml"
    path.write_text("addresses = \n", encoding="utf-8")
    with pytest.raises(IndicatorError) as caught:
        load_indicators(path)
    assert "iocs.toml" in str(caught.value)


def test_a_key_of_the_wrong_type_names_the_key(tmp_path):
    path = tmp_path / "iocs.toml"
    path.write_text('addresses = "203.0.113.10"\n', encoding="utf-8")
    with pytest.raises(IndicatorError) as caught:
        load_indicators(path)
    assert "addresses" in str(caught.value)


def test_a_loaded_list_matches_names_exactly_and_by_glob(tmp_path):
    path = tmp_path / "iocs.toml"
    path.write_text(SAMPLE, encoding="utf-8")
    indicators = load_indicators(path)
    assert indicators.empty is False
    assert indicators.matches_name("planted.php")
    assert indicators.matches_name("dump_admins.json")
    assert not indicators.matches_name("index.php")


def test_describe_names_the_source_so_a_clean_result_means_something(tmp_path):
    path = tmp_path / "iocs.toml"
    path.write_text(SAMPLE, encoding="utf-8")
    described = load_indicators(path).describe()
    assert "iocs.toml" in described
    assert "1 checksum(s)" in described


def test_the_context_reads_the_list_once(tmp_path):
    path = tmp_path / "iocs.toml"
    path.write_text(SAMPLE, encoding="utf-8")
    ctx = context(tmp_path, **{"audit.indicators": str(path)})
    first = ctx.indicators()
    path.unlink()
    assert ctx.indicators() is first
