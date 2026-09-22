"""The backup checks."""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime, timedelta

from support import context, posix_only, snapshot_dir, write

from holdfast import jobs, rclone
from holdfast.audit.checks import backups
from holdfast.audit.model import FAIL, PASS, UNKNOWN, WARN


def age(path, days: float) -> None:
    when = time.time() - days * 86400
    os.utime(path, (when, when))


# -- backup_freshness --------------------------------------------------------


def test_no_backup_root_configured_is_unknown(tmp_path):
    status, detail, _ = backups._backup_freshness(context(tmp_path))
    assert status == UNKNOWN
    assert "not set" in detail


def test_a_missing_backup_directory_is_unknown(tmp_path):
    ctx = context(tmp_path, **{"audit.backup.root": "/srv/backups"})
    assert backups._backup_freshness(ctx)[0] == UNKNOWN


def test_an_empty_backup_directory_fails(tmp_path):
    (tmp_path / "srv" / "backups").mkdir(parents=True)
    ctx = context(tmp_path, **{"audit.backup.root": "/srv/backups"})
    status, detail, _ = backups._backup_freshness(ctx)
    assert status == FAIL
    assert "not one snapshot" in detail


def test_a_fresh_snapshot_passes(tmp_path):
    (tmp_path / "srv" / "backups" / "2026-09-20").mkdir(parents=True)
    ctx = context(tmp_path, **{"audit.backup.root": "/srv/backups"})
    status, detail, _ = backups._backup_freshness(ctx)
    assert status == PASS
    assert "2026-09-20" in detail


def test_a_stale_snapshot_warns(tmp_path):
    path = tmp_path / "srv" / "backups" / "2026-09-01"
    path.mkdir(parents=True)
    age(path, 1.5)
    ctx = context(tmp_path, **{"audit.backup.root": "/srv/backups"})
    assert backups._backup_freshness(ctx)[0] == WARN


def test_a_long_stopped_backup_fails(tmp_path):
    path = tmp_path / "srv" / "backups" / "2026-08-01"
    path.mkdir(parents=True)
    age(path, 30)
    ctx = context(tmp_path, **{"audit.backup.root": "/srv/backups"})
    status, detail, _ = backups._backup_freshness(ctx)
    assert status == FAIL
    assert "not running" in detail


def test_the_storage_scope_question_stays_unanswerable(tmp_path):
    status, _, manual = backups._storage_credentials_scope(context(tmp_path))
    assert status == UNKNOWN
    assert "its own user" in manual


# -- _backup_root -------------------------------------------------------


def test_backup_root_falls_back_to_an_existing_backup_root(tmp_path):
    (tmp_path / "srv" / "real-backups").mkdir(parents=True)
    ctx = context(tmp_path, **{"backup.root": "/srv/real-backups"})
    root, shown = backups._backup_root(ctx)
    assert root == tmp_path / "srv" / "real-backups"
    assert shown == "/srv/real-backups"


def test_backup_root_does_not_fall_back_to_a_missing_backup_root(tmp_path):
    ctx = context(tmp_path, **{"backup.root": "/srv/real-backups"})
    root, shown = backups._backup_root(ctx)
    assert root is None
    assert shown == ""


def test_backup_freshness_uses_the_fallback_root(tmp_path):
    (tmp_path / "srv" / "real-backups" / "2026-09-20").mkdir(parents=True)
    ctx = context(tmp_path, **{"backup.root": "/srv/real-backups"})
    status, detail, _ = backups._backup_freshness(ctx)
    assert status == PASS
    assert "2026-09-20" in detail


def test_missing_fallback_root_names_both_settings(tmp_path):
    ctx = context(tmp_path, **{"backup.root": "/srv/nope"})
    status, detail, _ = backups._backup_freshness(ctx)
    assert status == UNKNOWN
    assert "not set" in detail
    assert "audit.backup.root" in detail
    assert "/srv/nope" in detail


def test_an_explicit_audit_backup_root_beats_an_existing_fallback(tmp_path):
    (tmp_path / "srv" / "real-backups" / "2026-09-20").mkdir(parents=True)
    (tmp_path / "srv" / "chosen" / "2026-09-01").mkdir(parents=True)
    ctx = context(
        tmp_path,
        **{"audit.backup.root": "/srv/chosen", "backup.root": "/srv/real-backups"},
    )
    root, shown = backups._backup_root(ctx)
    assert shown == "/srv/chosen"
    assert root == tmp_path / "srv" / "chosen"


# -- _snapshots ---------------------------------------------------------


def test_snapshots_drop_latest_and_dotted_directories(tmp_path):
    root = tmp_path / "backups"
    (root / "latest").mkdir(parents=True)
    (root / ".20260920-100000.tmp").mkdir(parents=True)
    (root / "20260920-090000").mkdir(parents=True)
    names = [p.name for p in backups._snapshots(root)]
    assert names == ["20260920-090000"]


def test_snapshots_of_a_missing_root_is_empty(tmp_path):
    assert backups._snapshots(tmp_path / "nowhere") == []


@posix_only
def test_snapshots_drop_a_symlink_even_when_not_named_latest(tmp_path):
    """An operator's own `current -> 20260920-090000` link must not be
    counted as a second, independent snapshot of its target."""
    root = tmp_path / "backups"
    real = root / "20260920-090000"
    real.mkdir(parents=True)
    (root / "current").symlink_to(real, target_is_directory=True)
    names = [p.name for p in backups._snapshots(root)]
    assert names == ["20260920-090000"]


def test_freshness_picks_the_snapshot_newest_by_mtime_not_by_name(tmp_path):
    # Name order and mtime order disagree here on purpose: a directory named
    # as though it were written later, but whose actual mtime is old (the way
    # a restored or copied snapshot would look), must not be read as fresh.
    named_later_but_stale = tmp_path / "srv" / "backups" / "2026-09-20"
    named_earlier_but_fresh = tmp_path / "srv" / "backups" / "2026-09-01"
    named_later_but_stale.mkdir(parents=True)
    named_earlier_but_fresh.mkdir(parents=True)
    age(named_later_but_stale, 10)
    age(named_earlier_but_fresh, 0.1)
    ctx = context(tmp_path, **{"audit.backup.root": "/srv/backups"})
    status, detail, _ = backups._backup_freshness(ctx)
    assert status == PASS
    assert "2026-09-01" in detail


# -- backup_encrypted -----------------------------------------------------


def test_a_manifest_saying_enabled_passes(tmp_path):
    root = tmp_path / "backups"
    snapshot_dir(root, encryption={"enabled": True, "tool": "age", "suffix": ".age"})
    ctx = context(tmp_path, **{"audit.backup.root": "/backups"})
    assert backups._backup_encrypted(ctx)[0] == PASS


def test_a_manifest_saying_disabled_fails(tmp_path):
    root = tmp_path / "backups"
    snapshot_dir(root, encryption={"enabled": False, "tool": "none", "suffix": ""})
    ctx = context(tmp_path, **{"audit.backup.root": "/backups"})
    status, detail, _ = backups._backup_encrypted(ctx)
    assert status == FAIL
    assert "in the clear" in detail


def test_no_manifest_but_an_age_file_passes_with_a_caveat(tmp_path):
    root = tmp_path / "backups" / "20260920-090000"
    root.mkdir(parents=True)
    write(tmp_path, "/backups/20260920-090000/db.sql.age", "cipher")
    ctx = context(tmp_path, **{"audit.backup.root": "/backups"})
    status, detail, _ = backups._backup_encrypted(ctx)
    assert status == PASS
    assert "no manifest" in detail


def test_no_manifest_and_nothing_encrypted_fails(tmp_path):
    root = tmp_path / "backups" / "20260920-090000"
    root.mkdir(parents=True)
    write(tmp_path, "/backups/20260920-090000/db.sql", "plain")
    ctx = context(tmp_path, **{"audit.backup.root": "/backups"})
    status, _, _ = backups._backup_encrypted(ctx)
    assert status == FAIL


def test_a_manifest_that_does_not_parse_falls_back_to_suffixes(tmp_path):
    root = tmp_path / "backups" / "20260920-090000"
    root.mkdir(parents=True)
    write(tmp_path, "/backups/20260920-090000/manifest.json", "{not json")
    write(tmp_path, "/backups/20260920-090000/db.sql.gpg", "cipher")
    ctx = context(tmp_path, **{"audit.backup.root": "/backups"})
    status, _, _ = backups._backup_encrypted(ctx)
    assert status == PASS


def test_no_snapshots_is_unknown_for_encryption_too(tmp_path):
    (tmp_path / "backups").mkdir(parents=True)
    ctx = context(tmp_path, **{"audit.backup.root": "/backups"})
    assert backups._backup_encrypted(ctx)[0] == UNKNOWN


def test_a_missing_root_is_unknown_for_encryption_too(tmp_path):
    ctx = context(tmp_path, **{"audit.backup.root": "/backups"})
    assert backups._backup_encrypted(ctx)[0] == UNKNOWN


def test_manifest_with_no_encryption_field_is_judged_by_suffix(tmp_path):
    root = tmp_path / "backups" / "20260920-090000"
    root.mkdir(parents=True)
    write(tmp_path, "/backups/20260920-090000/manifest.json", json.dumps({"format": 1}))
    write(tmp_path, "/backups/20260920-090000/db.sql.age", "cipher")
    ctx = context(tmp_path, **{"audit.backup.root": "/backups"})
    status, detail, _ = backups._backup_encrypted(ctx)
    assert status == PASS
    assert "nothing about encryption" in detail
    assert "did not parse" not in detail


def test_a_missing_enabled_key_is_not_read_as_encrypted(tmp_path):
    # The exact case the review asked about: a manifest that parses and has
    # an "encryption" object, but no "enabled" key in it, must not be treated
    # as though it said "enabled": true.
    root = tmp_path / "backups" / "20260920-090000"
    root.mkdir(parents=True)
    manifest = json.dumps({"format": 1, "encryption": {"tool": "age"}})
    write(tmp_path, "/backups/20260920-090000/manifest.json", manifest)
    write(tmp_path, "/backups/20260920-090000/db.sql", "plain")
    ctx = context(tmp_path, **{"audit.backup.root": "/backups"})
    status, detail, _ = backups._backup_encrypted(ctx)
    assert status == FAIL
    assert "nothing about encryption" in detail


# -- restore_tested -------------------------------------------------------


def jobs_context(tmp_path, **overrides):
    return context(tmp_path, **{"jobs.dir": "/jobs", **overrides})


def set_restore_at(jobs_dir, at: str) -> None:
    """Rewrite the journal's timestamp directly - `jobs.record` always stamps
    `now`, and the age tests need a stamp of their own choosing."""
    path = jobs_dir / "restore.json"
    entry = json.loads(path.read_text(encoding="utf-8"))
    entry["last_run"]["at"] = at
    if entry["last_success"] is not None:
        entry["last_success"]["at"] = at
    path.write_text(json.dumps(entry), encoding="utf-8")


def test_no_journal_at_all_fails(tmp_path):
    status, detail, _ = backups._restore_tested(jobs_context(tmp_path))
    assert status == FAIL
    assert "restore" in detail
    assert "never" in detail


def test_a_successful_verify_with_no_restore_warns_with_both_halves(tmp_path):
    jobs.record(tmp_path / "jobs", "verify", ok=True, snapshot="20260920-090000")
    status, detail, _ = backups._restore_tested(jobs_context(tmp_path))
    assert status == WARN
    assert "verif" in detail.lower()
    assert "restore" in detail.lower()
    assert "never" in detail.lower() or "not" in detail.lower()


def test_a_restore_from_yesterday_passes_with_the_date(tmp_path):
    jobs_dir = tmp_path / "jobs"
    jobs.record(jobs_dir, "restore", ok=True, snapshot="a")
    at = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    set_restore_at(jobs_dir, at)

    status, detail, _ = backups._restore_tested(jobs_context(tmp_path))
    assert status == PASS
    assert at in detail


def test_a_restore_200_days_old_warns_with_the_count(tmp_path):
    jobs_dir = tmp_path / "jobs"
    jobs.record(jobs_dir, "restore", ok=True, snapshot="a")
    at = (datetime.now(UTC) - timedelta(days=200)).isoformat()
    set_restore_at(jobs_dir, at)

    status, detail, _ = backups._restore_tested(jobs_context(tmp_path))
    assert status == WARN
    assert "200" in detail


def test_a_failed_attempt_alone_is_not_proof(tmp_path):
    jobs.record(tmp_path / "jobs", "restore", ok=False, snapshot="a", error="boom")
    status, detail, _ = backups._restore_tested(jobs_context(tmp_path))
    assert status == FAIL
    assert "fail" in detail.lower() or "not" in detail.lower()


def test_an_unparseable_date_does_not_crash_the_check(tmp_path):
    """A recorded success is still a success, but the value must not be
    presented as though it were a date the check could actually read."""
    jobs_dir = tmp_path / "jobs"
    jobs.record(jobs_dir, "restore", ok=True, snapshot="a")
    set_restore_at(jobs_dir, "not-a-date")

    status, detail, _ = backups._restore_tested(jobs_context(tmp_path))
    assert status == PASS
    assert "not-a-date" in detail
    assert "days" not in detail
    assert "not a timestamp" in detail


# -- backup_offsite_copy --------------------------------------------------


SNAPSHOT = "20260920-090000"
TARGET = "remote:bucket/web-1/" + SNAPSHOT


def offsite_context(tmp_path, **overrides):
    return context(
        tmp_path,
        **{
            "jobs.dir": "/jobs",
            "offsite.remote": "remote:bucket",
            "host_label": "web-1",
            **overrides,
        },
    )


def refuse_directories(monkeypatch):
    """Fails the test if the network is reached - the same trick
    `test_audit_application.py` uses for `http_endpoint`."""

    def refuse(*args, **kwargs):
        raise AssertionError("the network must not be asked here")

    monkeypatch.setattr(rclone, "directories", refuse)


def force_on_host(monkeypatch):
    """`context()` always builds a host that is not the real root, so
    `ctx.on_host` is False by default - right for the dedicated mounted-tree
    test below, wrong for every other test in this section, which is about
    scenarios the on_host guard would otherwise short-circuit before they
    get exercised."""
    monkeypatch.setattr(backups.Context, "on_host", property(lambda self: True))


def record_sent(tmp_path, snapshot: str = SNAPSHOT, target: str = TARGET) -> None:
    jobs.record(tmp_path / "jobs", "offsite", ok=True, snapshot=snapshot, target=target)


def test_no_remote_configured_fails_without_asking_the_network(tmp_path, monkeypatch):
    refuse_directories(monkeypatch)
    ctx = offsite_context(tmp_path, **{"offsite.remote": ""})
    status, detail, _ = backups._backup_offsite_copy(ctx)
    assert status == FAIL
    assert "offsite.remote" in detail


def test_no_journal_fails_without_asking_the_network(tmp_path, monkeypatch):
    refuse_directories(monkeypatch)
    ctx = offsite_context(tmp_path)
    status, detail, _ = backups._backup_offsite_copy(ctx)
    assert status == FAIL
    assert "never" in detail


def test_sent_and_present_in_storage_passes(tmp_path, monkeypatch):
    force_on_host(monkeypatch)
    record_sent(tmp_path)
    monkeypatch.setattr(rclone, "directories", lambda *a, **k: [SNAPSHOT])
    ctx = offsite_context(tmp_path)
    status, detail, _ = backups._backup_offsite_copy(ctx)
    assert status == PASS
    assert SNAPSHOT in detail


def test_sent_but_missing_from_storage_fails_with_the_snapshot_name(
    tmp_path, monkeypatch
):
    force_on_host(monkeypatch)
    record_sent(tmp_path)
    monkeypatch.setattr(rclone, "directories", lambda *a, **k: [])
    ctx = offsite_context(tmp_path)
    status, detail, _ = backups._backup_offsite_copy(ctx)
    assert status == FAIL
    assert SNAPSHOT in detail


def test_missing_host_directory_fails_saying_there_is_no_directory_at_all(
    tmp_path, monkeypatch
):
    force_on_host(monkeypatch)
    record_sent(tmp_path)

    def not_found(*a, **k):
        raise rclone.RcloneError("directory not found", code=rclone.DIRECTORY_NOT_FOUND)

    monkeypatch.setattr(rclone, "directories", not_found)
    ctx = offsite_context(tmp_path)
    status, detail, _ = backups._backup_offsite_copy(ctx)
    assert status == FAIL
    # Distinct from "it is not among the directories ... the copy is gone"
    # (test_sent_but_missing_from_storage_fails_with_the_snapshot_name),
    # which also contains "no" (via "not among") and "director[y|ies]".
    assert "no directory in the offsite storage at all" in detail


def test_any_other_rclone_error_is_unknown_not_fail(tmp_path, monkeypatch):
    force_on_host(monkeypatch)
    record_sent(tmp_path)

    def broken(*a, **k):
        raise rclone.RcloneError("timed out")

    monkeypatch.setattr(rclone, "directories", broken)
    ctx = offsite_context(tmp_path)
    status, detail, _ = backups._backup_offsite_copy(ctx)
    assert status == UNKNOWN
    assert "timed out" in detail


def test_offsite_disabled_warns_from_the_journal_without_asking_the_network(
    tmp_path, monkeypatch
):
    """--no-offsite takes priority over the on_host guard below: it is the
    operator's own acknowledgement that the storage should not be asked, on
    the real host or a mounted one alike, and deserves the softer WARN."""
    refuse_directories(monkeypatch)
    record_sent(tmp_path)
    ctx = offsite_context(tmp_path)
    ctx.offsite_enabled = False
    status, detail, _ = backups._backup_offsite_copy(ctx)
    assert status == WARN
    assert SNAPSHOT in detail


def test_a_fresher_local_snapshot_than_what_was_sent_warns(tmp_path, monkeypatch):
    force_on_host(monkeypatch)
    older = tmp_path / "backups" / SNAPSHOT
    newer = tmp_path / "backups" / "20260921-090000"
    older.mkdir(parents=True)
    newer.mkdir(parents=True)
    age(older, 2)
    age(newer, 0.1)
    record_sent(tmp_path)
    monkeypatch.setattr(rclone, "directories", lambda *a, **k: [SNAPSHOT])
    ctx = offsite_context(tmp_path, **{"audit.backup.root": "/backups"})
    status, detail, _ = backups._backup_offsite_copy(ctx)
    assert status == WARN
    assert SNAPSHOT in detail
    assert "20260921-090000" in detail
    # The wording states only what was compared - never claims the local
    # snapshot "has not gone out yet", which the mtime comparison alone
    # cannot prove (see test_a_restored_snapshot_does_not_claim_it_is_unsent).
    assert "has not gone out" not in detail


def test_a_restored_snapshot_does_not_claim_it_is_unsent(tmp_path, monkeypatch):
    """A restored or copied snapshot keeps its old name but gets a fresh
    mtime - `_snapshots`'s own reason for sorting by mtime rather than name
    (see its docstring). Here that snapshot's name is *older* than the one
    the journal says was sent, so the old wording ("is newer locally and has
    not gone out yet") would say something false in both halves: it is not
    newer by name, and for all this check knows it may have gone out long
    ago under this very name."""
    force_on_host(monkeypatch)
    restored = tmp_path / "backups" / "20260601-000000"
    restored.mkdir(parents=True)
    age(restored, 0.01)
    record_sent(tmp_path, snapshot="20260920-090000")
    monkeypatch.setattr(rclone, "directories", lambda *a, **k: ["20260920-090000"])
    ctx = offsite_context(tmp_path, **{"audit.backup.root": "/backups"})
    status, detail, _ = backups._backup_offsite_copy(ctx)
    assert status == WARN
    assert "20260601-000000" in detail
    assert "20260920-090000" in detail
    assert "newer locally" not in detail
    assert "has not gone out" not in detail


def test_no_resolvable_backup_root_skips_the_freshness_comparison(
    tmp_path, monkeypatch
):
    """`_backup_root` returning nothing must not manufacture a complaint about
    delivery the watchdog cannot actually see."""
    force_on_host(monkeypatch)
    record_sent(tmp_path)
    monkeypatch.setattr(rclone, "directories", lambda *a, **k: [SNAPSHOT])
    ctx = offsite_context(tmp_path, **{"backup.root": "/nowhere"})
    status, detail, _ = backups._backup_offsite_copy(ctx)
    assert status == PASS
    assert SNAPSHOT in detail


def test_the_listing_is_called_exactly_once(tmp_path, monkeypatch):
    force_on_host(monkeypatch)
    record_sent(tmp_path)
    calls = []

    def counting(*a, **k):
        calls.append(a)
        return [SNAPSHOT]

    monkeypatch.setattr(rclone, "directories", counting)
    ctx = offsite_context(tmp_path)
    backups._backup_offsite_copy(ctx)
    assert len(calls) == 1


# -- backup_offsite_copy under --host ----------------------------------


def test_a_mounted_tree_is_unknown_without_asking_the_network(tmp_path, monkeypatch):
    """The journal under `--host <mounted tree>` is the audited machine's,
    but `offsite.remote` and `host_label` are read straight from the
    auditing machine's own config and cannot be remapped - so this must
    answer UNKNOWN rather than compare one machine's snapshot against
    another machine's storage (the finding this guards against)."""
    refuse_directories(monkeypatch)
    record_sent(tmp_path)
    ctx = offsite_context(tmp_path)  # on_host is False by default - unpatched
    status, detail, _ = backups._backup_offsite_copy(ctx)
    assert status == UNKNOWN
    assert SNAPSHOT in detail
    assert "mounted tree" in detail
