"""The engine, tested without any particular check in mind."""

from __future__ import annotations

import json
import os
import re
import stat

import pytest
from support import context

from holdfast.audit import model, registry, state
from holdfast.audit.model import FAIL, PASS, UNKNOWN, Check, Finding
from holdfast.audit.report import (
    Report,
    exit_code,
    render_coverage,
    render_json,
    render_text,
)

TECHNIQUE_RE = re.compile(r"^T\d{4}(\.\d{3})?$")


# -- the register -----------------------------------------------------------


def test_the_register_is_not_empty():
    assert registry.all_checks()


def test_every_check_id_is_unique():
    ids = [c.id for c in registry.all_checks()]
    assert len(ids) == len(set(ids))


def test_every_check_declares_a_known_group_and_severity():
    for entry in registry.all_checks():
        assert entry.group in model.GROUPS
        assert entry.severity in model.SEVERITIES
        assert entry.title
        assert TECHNIQUE_RE.fullmatch(entry.technique), entry.id


def test_every_title_is_ascii():
    """The checks are ported from a Russian-language source.

    A title that still carries Cyrillic is a line that was copied rather than
    translated, and this is the cheapest place to catch one.
    """
    for entry in registry.all_checks():
        assert entry.title.isascii(), entry.id


def test_the_register_is_in_report_order():
    order = [model.GROUPS.index(c.group) for c in registry.all_checks()]
    assert order == sorted(order)


def test_an_unknown_group_is_refused():
    with pytest.raises(RuntimeError):
        registry.check("x", "No Such Group", "t", "T1000")(lambda ctx: None)


def test_an_unknown_severity_is_refused():
    with pytest.raises(RuntimeError):
        registry.check("x", model.GROUP_ACCESS, "t", "T1000", "urgent")(
            lambda ctx: None
        )


def test_a_duplicate_id_is_refused():
    taken = registry.all_checks()[0].id
    with pytest.raises(RuntimeError):
        registry.check(taken, model.GROUP_ACCESS, "t", "T1000")(lambda ctx: None)


# -- the context ------------------------------------------------------------


def test_path_maps_onto_the_mounted_host(tmp_path):
    ctx = context(tmp_path)
    assert ctx.path("/etc/ssh") == tmp_path / "etc" / "ssh"


def test_show_renders_the_path_as_the_host_sees_it(tmp_path):
    ctx = context(tmp_path)
    assert ctx.show(ctx.path("/etc/ssh/sshd_config")) == "/etc/ssh/sshd_config"


def test_read_text_refuses_a_file_over_the_limit(tmp_path):
    ctx = context(tmp_path, **{"audit.scan.max_text_bytes": 10})
    big = tmp_path / "big.txt"
    big.write_text("x" * 100, encoding="utf-8")
    assert ctx.read_text(big) is None


def test_read_text_returns_none_for_a_missing_file(tmp_path):
    assert context(tmp_path).read_text(tmp_path / "nope") is None


def test_on_host_is_false_for_a_mounted_tree(tmp_path):
    assert context(tmp_path).on_host is False


def test_conf_list_returns_an_empty_list_for_a_missing_key(tmp_path):
    assert context(tmp_path).conf_list("audit.nothing.here") == []


def test_an_empty_array_in_the_config_beats_a_non_empty_default(tmp_path):
    ctx = context(tmp_path, **{"encryption.recipients": []})
    assert ctx.conf_list("encryption.recipients") == []


def test_conf_int_falls_back_to_zero_on_nonsense(tmp_path):
    ctx = context(tmp_path, **{"audit.scan.max_files": "many"})
    assert ctx.conf_int("audit.scan.max_files") == 0


def test_conf_re_returns_none_on_a_broken_pattern(tmp_path):
    ctx = context(tmp_path, **{"audit.ignore": "[unclosed"})
    assert ctx.conf_re("audit.ignore") is None


def test_walk_reports_truncation_instead_of_a_clean_result(tmp_path):
    root = tmp_path / "opt"
    root.mkdir()
    for index in range(5):
        (root / f"f{index}").write_text("x", encoding="utf-8")
    ctx = context(tmp_path, **{"audit.scan.max_files": 2})
    flags = [truncated for _, _, truncated in ctx.walk(("/opt",))]
    assert flags[-1] is True


def test_walk_skips_noise_directories(tmp_path):
    root = tmp_path / "opt" / "app" / "node_modules"
    root.mkdir(parents=True)
    (root / "buried").write_text("x", encoding="utf-8")
    seen = [path.name for path, _, _ in context(tmp_path).walk(("/opt",))]
    assert "buried" not in seen


def test_previous_data_finds_the_entry_for_a_check(tmp_path):
    ctx = context(tmp_path)
    ctx.previous = {"findings": [{"id": "a", "data": {"k": 1}}]}
    assert ctx.previous_data("a") == {"k": 1}
    assert ctx.previous_data("b") == {}


# -- state ------------------------------------------------------------------


def test_load_last_returns_none_when_there_is_no_file(tmp_path):
    assert state.load_last(tmp_path) is None


def test_load_last_survives_a_corrupt_file(tmp_path):
    (tmp_path / state.STATE_FILE).write_text("{not json", encoding="utf-8")
    assert state.load_last(tmp_path) is None


def test_save_last_then_load_last_round_trips(tmp_path):
    report = {"findings": [{"id": "a", "data": {"k": 1}}]}
    path = state.save_last(tmp_path / "deeper", report)
    assert path.is_file()
    assert state.load_last(tmp_path / "deeper") == report


def test_save_last_leaves_no_temporary_file_behind(tmp_path):
    state.save_last(tmp_path, {"findings": []})
    assert [p.name for p in tmp_path.iterdir()] == [state.STATE_FILE]


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes only")
def test_the_state_file_is_not_world_readable(tmp_path):
    path = state.save_last(tmp_path, {"findings": []})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


# -- the report -------------------------------------------------------------


def finding(**kwargs):
    base = {
        "id": "x",
        "group": model.GROUP_ACCESS,
        "title": "X",
        "technique": "T1000",
        "severity": "high",
        "status": PASS,
        "detail": "",
        "manual": "",
    }
    base.update(kwargs)
    return Finding(**base)


def report(findings):
    return Report(
        started_at="2026-09-20T00:00:00Z",
        host_label="host",
        version="0.1.0",
        findings=findings,
    )


def test_the_summary_comes_first():
    text = render_text(report([finding(status=FAIL), finding(id="y")]))
    assert text.splitlines()[0] == "1 failed, 0 warned, 0 unknown, 1 passed"


def test_empty_groups_are_not_printed():
    text = render_text(report([finding()]))
    assert model.GROUP_BACKUPS not in text


def test_unknown_is_printed_with_the_command_to_settle_it():
    text = render_text(report([finding(status=UNKNOWN, manual="run this")]))
    assert "check by hand: run this" in text


def test_data_never_reaches_the_text():
    text = render_text(report([finding(data={"sha": "deadbeef"})]))
    assert "deadbeef" not in text


def test_json_carries_the_same_findings():
    parsed = json.loads(render_json(report([finding(status=FAIL)])))
    assert [f["id"] for f in parsed["findings"]] == ["x"]
    assert parsed["findings"][0]["status"] == FAIL


def test_exit_code_is_one_only_when_something_failed():
    assert exit_code(report([finding()])) == 0
    assert exit_code(report([finding(status=UNKNOWN)])) == 0
    assert exit_code(report([finding(status=FAIL)])) == 1


def test_a_check_that_raises_becomes_unknown_and_does_not_stop_the_run(tmp_path):
    from holdfast.audit import report as report_module

    def explode(ctx):
        raise ValueError("boom")

    broken = Check(
        id="broken",
        group=model.GROUP_ACCESS,
        title="Broken",
        technique="T1000",
        severity="low",
        run=explode,
    )
    healthy = Check(
        id="healthy",
        group=model.GROUP_ACCESS,
        title="Healthy",
        technique="T1000",
        severity="low",
        run=lambda ctx: (PASS, "fine", ""),
    )
    original = report_module.all_checks
    report_module.all_checks = lambda: [broken, healthy]
    try:
        result = report_module.run_audit(context(tmp_path))
    finally:
        report_module.all_checks = original
    by_id = {f.id: f for f in result.findings}
    assert by_id["broken"].status == UNKNOWN
    assert "ValueError" in by_id["broken"].detail
    assert by_id["healthy"].status == PASS


def test_the_coverage_table_lists_every_check():
    text = render_coverage()
    for entry in registry.all_checks():
        assert entry.id in text
    assert f"{len(registry.all_checks())} checks" in text


def test_the_checks_document_matches_the_register():
    """docs/checks.md is generated. A drifted copy is a lying document."""
    from pathlib import Path

    from holdfast.audit.report import render_checks_markdown

    path = Path(__file__).resolve().parent.parent / "docs" / "checks.md"
    # Line endings are not the subject here: the same redirect produces CRLF
    # on a Windows workstation and LF in CI, and neither says anything about
    # whether the documented coverage matches the registered coverage.
    current = path.read_text(encoding="utf-8").replace("\r\n", "\n").rstrip("\n")
    assert current == render_checks_markdown().rstrip("\n"), (
        "docs/checks.md is stale; regenerate it with "
        "holdfast audit --coverage --markdown > docs/checks.md"
    )


def test_walk_yields_a_file_once_even_when_roots_overlap(tmp_path):
    """/var/www is inside the secrets roots; a file there is one file."""
    from support import write

    write(tmp_path, "/var/www/app/.env", "x")
    seen = [path for path, _, _ in context(tmp_path).walk(("/var/www", "/var"))]
    assert len(seen) == 1
