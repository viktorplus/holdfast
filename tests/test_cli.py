import os
from pathlib import Path

import pytest
from support import ORPHAN_VOLUME, needs_sh, posix_only

import holdfast
from holdfast import jobs
from holdfast.cli import main


def test_version_prints_the_version(capsys):
    assert main(["version"]) == 0
    assert holdfast.__version__ in capsys.readouterr().out


def test_no_command_explains_itself(capsys):
    assert main([]) == 2
    assert "usage" in capsys.readouterr().err.lower()


def test_unknown_command_is_rejected():
    with pytest.raises(SystemExit) as caught:
        main(["nonsense"])
    assert caught.value.code == 2


def test_config_check_reports_an_empty_directory(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("HOLDFAST_API_TOKEN", raising=False)
    code = main(["--config-dir", str(tmp_path), "config", "check"])
    assert code == 1
    assert "host_label" in capsys.readouterr().out


def test_config_check_explains_a_broken_machine_file(tmp_path, capsys):
    (tmp_path / "holdfast.toml").write_text("host_label = \n", encoding="utf-8")

    code = main(["--config-dir", str(tmp_path), "config", "check"])

    assert code == 1
    assert "holdfast.toml" in capsys.readouterr().err


def test_config_check_ignores_a_profile_in_the_directory(tmp_path, capsys, monkeypatch):
    """Not even a broken one is read: a profile is install-time material."""
    monkeypatch.delenv("HOLDFAST_API_TOKEN", raising=False)
    (tmp_path / "profile.toml").write_text("collection = \n", encoding="utf-8")

    code = main(["--config-dir", str(tmp_path), "config", "check"])

    captured = capsys.readouterr()
    assert code == 1
    assert "profile.toml" not in captured.err
    assert "host_label" in captured.out


def test_init_join_explains_a_broken_profile(tmp_path, capsys):
    profile = tmp_path / "profile.toml"
    profile.write_text("collection = \n", encoding="utf-8")

    code = main(["--config-dir", str(tmp_path / "etc"), "init", "--join", str(profile)])

    assert code == 1
    assert "profile.toml" in capsys.readouterr().err
    assert not (tmp_path / "etc").exists()


def test_init_fresh_writes_a_file(tmp_path, capsys):
    code = main(
        ["--config-dir", str(tmp_path), "init", "--fresh", "--host-label", "web-1"]
    )
    assert code == 0
    assert (tmp_path / "holdfast.toml").is_file()
    assert "wrote" in capsys.readouterr().out


def test_init_explains_a_permission_error(tmp_path, capsys, monkeypatch):
    """Without root, /etc/holdfast refuses: say so rather than traceback."""

    def refuse(*args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "mkdir", refuse)

    code = main(["--config-dir", str(tmp_path / "etc"), "init", "--fresh"])

    assert code == 1
    assert "Permission denied" in capsys.readouterr().err


def test_init_refuses_without_a_mode():
    with pytest.raises(SystemExit) as caught:
        main(["init"])
    assert caught.value.code == 2


def test_init_fresh_writes_the_chosen_backup_mode(tmp_path, capsys):
    code = main(
        [
            "--config-dir",
            str(tmp_path),
            "init",
            "--fresh",
            "--backup-mode",
            "manual",
        ]
    )
    assert code == 0
    text = (tmp_path / "holdfast.toml").read_text(encoding="utf-8")
    assert 'mode = "manual"' in text


def test_init_refuses_an_unknown_backup_mode():
    with pytest.raises(SystemExit) as caught:
        main(["init", "--fresh", "--backup-mode", "nonsense"])
    assert caught.value.code == 2


def _audit_config(config_dir: Path, state_dir: Path, extra: str = "") -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "holdfast.toml").write_text(
        f'host_label = "example"\n{extra}\n'
        f'[audit]\nstate_dir = "{state_dir.as_posix()}"\n',
        encoding="utf-8",
    )


def test_audit_coverage_lists_the_checks(capsys):
    from holdfast.audit import all_checks

    assert main(["audit", "--coverage"]) == 0
    out = capsys.readouterr().out
    assert f"{len(all_checks())} checks" in out
    assert "technique" in out


@posix_only
def test_audit_on_a_clean_tree_returns_zero(tmp_path, capsys):
    """On Windows every file reports mode 0o666, so world_writable fires."""
    host = tmp_path / "host"
    (host / "etc" / "ssh").mkdir(parents=True)
    (host / "etc" / "ssh" / "sshd_config").write_text(
        "Port 22\nPermitRootLogin prohibit-password\nPasswordAuthentication no\n",
        encoding="utf-8",
    )
    (host / "etc" / "fail2ban").mkdir(parents=True)
    (host / "etc" / "fail2ban" / "jail.local").write_text(
        "[sshd]\nenabled = true\nport = 22\n", encoding="utf-8"
    )
    (host / "etc" / "passwd").write_text(
        "root:x:0:0::/root:/usr/sbin/nologin\n", encoding="utf-8"
    )
    # A machine in order also rotates its container logs, ships its logs
    # elsewhere, sends its snapshots offsite and has been restored from.
    (host / "etc" / "docker").mkdir(parents=True)
    (host / "etc" / "docker" / "daemon.json").write_text(
        '{"log-opts": {"max-size": "10m"}}\n', encoding="utf-8"
    )
    (host / "etc" / "rsyslog.d").mkdir(parents=True)
    (host / "etc" / "rsyslog.d" / "50-remote.conf").write_text(
        "*.* @@logs.example.com:514\n", encoding="utf-8"
    )
    journal = host / "var" / "lib" / "holdfast" / "jobs"
    jobs.record(journal, "offsite", ok=True, snapshot="20260920-231500")
    jobs.record(journal, "restore", ok=True, snapshot="20260920-231500")
    _audit_config(
        tmp_path / "config",
        tmp_path / "state",
        extra='\n[offsite]\nremote = "shared:backups"\n',
    )
    code = main(
        [
            "--config-dir",
            str(tmp_path / "config"),
            "audit",
            "--host",
            str(host),
            "--no-offsite",
        ]
    )
    out = capsys.readouterr().out
    assert code == 0, out
    assert "failed" in out.splitlines()[0]


def test_audit_on_a_tampered_tree_returns_one(tmp_path, capsys):
    host = tmp_path / "host"
    (host / "etc" / "ssh").mkdir(parents=True)
    (host / "etc" / "ssh" / "sshd_config").write_text(
        "PermitRootLogin yes\n", encoding="utf-8"
    )
    _audit_config(tmp_path / "config", tmp_path / "state")
    code = main(
        ["--config-dir", str(tmp_path / "config"), "audit", "--host", str(host)]
    )
    assert code == 1
    assert "PermitRootLogin yes" in capsys.readouterr().out


def test_audit_json_is_parseable_and_is_written_to_the_state_file(tmp_path, capsys):
    import json

    host = tmp_path / "host"
    host.mkdir()
    state_dir = tmp_path / "state"
    _audit_config(tmp_path / "config", state_dir)
    main(
        [
            "--config-dir",
            str(tmp_path / "config"),
            "audit",
            "--host",
            str(host),
            "--json",
        ]
    )
    printed = json.loads(capsys.readouterr().out)
    assert printed["host_label"] == "example"
    saved = json.loads((state_dir / "last.json").read_text(encoding="utf-8"))
    assert [f["id"] for f in saved["findings"]] == [
        f["id"] for f in printed["findings"]
    ]


def test_audit_runs_without_a_config_file(tmp_path, capsys):
    """An unconfigured machine is often exactly the one worth looking at."""
    host = tmp_path / "host"
    host.mkdir()
    code = main(
        [
            "--config-dir",
            str(tmp_path / "absent"),
            "audit",
            "--host",
            str(host),
            "--coverage",
        ]
    )
    assert code == 0
    assert capsys.readouterr().out.strip()


def test_baseline_prints_what_is_observed_and_writes_nothing(tmp_path, capsys):
    """Crossing lines out before pasting is the point of the step."""
    host = tmp_path / "host"
    (host / "var" / "log").mkdir(parents=True)
    (host / "var" / "log" / "auth.log").write_text(
        "Sep 20 10:00:00 host sshd[1]: Accepted publickey for deploy from "
        "203.0.113.10 port 51000 ssh2\n",
        encoding="utf-8",
    )
    config_dir = tmp_path / "config"
    _audit_config(config_dir, tmp_path / "state")
    before = (config_dir / "holdfast.toml").read_text(encoding="utf-8")

    main(
        [
            "--config-dir",
            str(config_dir),
            "audit",
            "--no-docker",
            "--baseline",
            "--host",
            str(host),
        ]
    )

    out = capsys.readouterr().out
    assert "[audit.ssh]" in out
    assert '"203.0.113.10"' in out
    assert "Cross out" in out
    assert (config_dir / "holdfast.toml").read_text(encoding="utf-8") == before


def test_no_offsite_reaches_the_context_and_the_network_is_not_asked(
    tmp_path, capsys, monkeypatch
):
    """`--no-offsite` has to thread through to `Context.offsite_enabled` -
    proven here by a journal that would otherwise trigger a real listing."""
    from holdfast import jobs, rclone

    host = tmp_path / "host"
    host.mkdir()
    jobs.record(
        host / "jobs",
        "offsite",
        ok=True,
        snapshot="20260920-090000",
        target="remote:bucket/web-1/20260920-090000",
    )

    def refuse(*args, **kwargs):
        raise AssertionError("--no-offsite must keep the check off the network")

    monkeypatch.setattr(rclone, "directories", refuse)

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "holdfast.toml").write_text(
        'host_label = "web-1"\n\n'
        "[offsite]\n"
        'remote = "remote:bucket"\n\n'
        "[jobs]\n"
        'dir = "/jobs"\n\n'
        "[audit]\n"
        f'state_dir = "{(tmp_path / "state").as_posix()}"\n',
        encoding="utf-8",
    )

    code = main(
        [
            "--config-dir",
            str(config_dir),
            "audit",
            "--no-offsite",
            "--no-docker",
            "--host",
            str(host),
        ]
    )
    out = capsys.readouterr().out
    assert code in (0, 1)
    assert "backup_offsite_copy" in out
    assert "[WARN]" in out


def test_baseline_on_a_bare_machine_says_there_is_nothing_to_declare(tmp_path, capsys):
    host = tmp_path / "host"
    host.mkdir()
    _audit_config(tmp_path / "config", tmp_path / "state")
    main(
        [
            "--config-dir",
            str(tmp_path / "config"),
            "audit",
            "--no-docker",
            "--baseline",
            "--host",
            str(host),
        ]
    )
    assert "Nothing observed that would need declaring" in capsys.readouterr().out


# --------------------------------------------------------------------------
# backup
# --------------------------------------------------------------------------


def a_machine(tmp_path: Path, extra: str = "") -> Path:
    directory = tmp_path / "etc"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "holdfast.toml").write_text(
        'host_label = "web-1"\n\n'
        "[backup]\n"
        f'root = "{(tmp_path / "backups").as_posix()}"\n'
        # Not the default under /run: nobody but root may write there.
        f'lock_file = "{(tmp_path / "holdfast.lock").as_posix()}"\n\n'
        "[jobs]\n"
        f'dir = "{(tmp_path / "jobs").as_posix()}"\n\n'
        "[encryption]\n"
        "enabled = false\n\n"
        "[[component]]\n"
        'type = "command"\n'
        'name = "greeting"\n'
        "produce = \"printf 'hello holdfast'\"\n" + extra,
        encoding="utf-8",
    )
    return directory


def test_backup_help_mentions_the_dry_run(capsys):
    with pytest.raises(SystemExit):
        main(["backup", "--help"])
    assert "--dry-run" in capsys.readouterr().out


def test_a_backup_problem_is_explained_rather_than_traced(tmp_path, capsys):
    """The command an operator runs first has to make sense on its own."""
    directory = a_machine(tmp_path)
    (directory / "holdfast.toml").write_text(
        "[encryption]\nenabled = false\n\n"
        '[[component]]\ntype = "nonsense"\nname = "a"\n',
        encoding="utf-8",
    )

    code = main(["--config-dir", str(directory), "backup"])

    assert code == 1
    captured = capsys.readouterr()
    assert "nonsense" in captured.err
    assert "Traceback" not in captured.err


def test_a_dry_run_shows_the_lines_and_writes_nothing(tmp_path, capsys):
    directory = a_machine(tmp_path)

    code = main(["--config-dir", str(directory), "backup", "--dry-run"])

    out = capsys.readouterr().out
    assert code == 0
    assert "greeting.bin" in out
    assert "printf 'hello holdfast'" in out
    assert "mode: manual" in out
    assert not (tmp_path / "backups").exists()


def test_an_auto_dry_run_says_what_it_would_leave_out(tmp_path, capsys, monkeypatch):
    import holdfast.backup.probe as probe_module

    class Machine(probe_module.Probe):
        def inspect_containers(self):
            return []

        def volumes(self):
            return [ORPHAN_VOLUME]

    monkeypatch.setattr(probe_module, "Probe", Machine)
    directory = a_machine(tmp_path)
    toml = directory / "holdfast.toml"
    toml.write_text(
        toml.read_text("utf-8").replace("[backup]\n", '[backup]\nmode = "auto"\n'),
        encoding="utf-8",
    )

    code = main(["--config-dir", str(directory), "backup", "--dry-run"])

    out = capsys.readouterr().out
    assert code == 0
    assert "mode: auto" in out
    assert "not taken:" in out
    assert f"volume {ORPHAN_VOLUME}: no container uses it" in out
    assert not (tmp_path / "backups").exists()


@needs_sh
def test_a_backup_says_where_it_put_the_snapshot(tmp_path, capsys):
    directory = a_machine(tmp_path)

    code = main(["--config-dir", str(directory), "backup"])

    out = capsys.readouterr().out
    assert code == 0
    assert "backups" in out
    snapshots = [
        p for p in (tmp_path / "backups").iterdir() if p.is_dir() and not p.is_symlink()
    ]
    assert len(snapshots) == 1
    assert (snapshots[0] / "manifest.json").is_file()


@posix_only
def test_a_backup_already_running_is_not_a_failure(tmp_path, capsys):
    """The nightly timer overlapping a manual run. Reporting it as an error
    every such night is how real errors stop being read."""
    import fcntl

    lock = tmp_path / "holdfast.lock"  # the one a_machine names
    directory = a_machine(tmp_path)

    with open(lock, "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        code = main(["--config-dir", str(directory), "backup"])

    assert code == 0
    assert "already running" in capsys.readouterr().out


def test_backup_discover_and_dry_run_are_different_questions():
    """One shows what the machine could declare, the other what a declaration
    would do."""
    with pytest.raises(SystemExit) as caught:
        main(["backup", "--discover", "--dry-run"])

    assert caught.value.code == 2


def _an_empty_docker_host(monkeypatch):
    import holdfast.backup.probe as probe_module

    class Machine(probe_module.Probe):
        def inspect_containers(self):
            return []

        def volumes(self):
            return [ORPHAN_VOLUME]

    monkeypatch.setattr(probe_module, "Probe", Machine)


def test_backup_discover_in_manual_writes_components_toml_into_the_config_dir(
    tmp_path, capsys, monkeypatch
):
    """No holdfast.toml yet is still manual mode, and still a place to write."""
    _an_empty_docker_host(monkeypatch)
    directory = tmp_path / "etc"

    code = main(["--config-dir", str(directory), "backup", "--discover"])

    out = capsys.readouterr().out
    assert code == 0
    assert (directory / "components.toml").exists()
    assert out.startswith(f"wrote {directory / 'components.toml'} (0 components)")
    assert f"volume {ORPHAN_VOLUME}: no container uses it" in out


def test_backup_discover_without_root_says_so_instead_of_tracing_back(
    tmp_path, capsys, monkeypatch
):
    """/etc/holdfast is root's; a plain user gets PermissionError from it."""
    _an_empty_docker_host(monkeypatch)

    def refuse(*args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr("holdfast.backup.components_file.atomic.write_text", refuse)

    code = main(["--config-dir", str(tmp_path / "etc"), "backup", "--discover"])

    err = capsys.readouterr().err
    assert code == 1
    assert "holdfast backup: writing" in err
    assert "Permission denied" in err
    assert "Traceback" not in err


def test_backup_discover_in_auto_writes_nothing(tmp_path, capsys, monkeypatch):
    _an_empty_docker_host(monkeypatch)
    directory = tmp_path / "etc"
    directory.mkdir()
    (directory / "holdfast.toml").write_text(
        '[backup]\nmode = "auto"\n', encoding="utf-8"
    )

    code = main(["--config-dir", str(directory), "backup", "--discover"])

    assert code == 0
    assert capsys.readouterr().out.startswith('backup.mode is "auto"')
    assert not (directory / "components.toml").exists()


def test_backup_discover_reports_a_bad_configuration(tmp_path, capsys, monkeypatch):
    _an_empty_docker_host(monkeypatch)
    directory = tmp_path / "etc"
    directory.mkdir()
    (directory / "holdfast.toml").write_text(
        '[backup]\nmode = "sometimes"\n', encoding="utf-8"
    )

    code = main(["--config-dir", str(directory), "backup", "--discover"])

    assert code == 1
    assert "holdfast backup: backup.mode is 'sometimes'" in capsys.readouterr().err
    assert not (directory / "components.toml").exists()


# --------------------------------------------------------------------------
# restore
# --------------------------------------------------------------------------


def _restore_config(config_dir: Path, jobs_dir: Path) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "holdfast.toml").write_text(
        f'[jobs]\ndir = "{jobs_dir.as_posix()}"\n',
        encoding="utf-8",
    )


def test_restore_needs_a_snapshot():
    with pytest.raises(SystemExit) as caught:
        main(["restore"])
    assert caught.value.code == 2


def test_restore_list_prints_and_leaves(tmp_path, capsys):
    from support import PATH_RECIPE, artifact, snapshot_dir

    directory = snapshot_dir(tmp_path, artifact("a.bin", PATH_RECIPE))
    code = main(["restore", "list", "--snapshot", str(directory)])

    assert code == 0
    assert "a.bin" in capsys.readouterr().out


def test_restore_explains_a_directory_that_is_not_a_snapshot(tmp_path, capsys):
    code = main(["restore", "list", "--snapshot", str(tmp_path)])

    assert code == 1
    captured = capsys.readouterr()
    assert "snapshot" in captured.err
    assert "Traceback" not in captured.err


@needs_sh
@pytest.mark.skipif(os.name == "nt", reason="Git Bash's cat cannot open a Windows path")
def test_restore_verify_passes_on_a_whole_snapshot(tmp_path, capsys):
    from support import PATH_RECIPE, artifact, snapshot_dir

    directory = snapshot_dir(tmp_path, artifact("a.bin", PATH_RECIPE))
    _restore_config(tmp_path / "etc", tmp_path / "jobs")
    code = main(
        [
            "--config-dir",
            str(tmp_path / "etc"),
            "restore",
            "verify",
            "--snapshot",
            str(directory),
        ]
    )

    assert code == 0
    assert "checked 1" in capsys.readouterr().out


@needs_sh
def test_restore_verify_fails_on_a_changed_byte(tmp_path, capsys):
    from support import PATH_RECIPE, artifact, snapshot_dir

    directory = snapshot_dir(tmp_path, artifact("a.bin", PATH_RECIPE))
    (directory / "a.bin").write_bytes(b"a bodY")
    _restore_config(tmp_path / "etc", tmp_path / "jobs")

    code = main(
        [
            "--config-dir",
            str(tmp_path / "etc"),
            "restore",
            "verify",
            "--snapshot",
            str(directory),
        ]
    )

    assert code == 1
    assert "a.bin" in capsys.readouterr().err


def test_a_restore_with_nobody_to_ask_says_so_instead_of_hanging(
    tmp_path, capsys, monkeypatch
):
    """The first thing that happens in CI or in cron."""
    from support import PATH_RECIPE, artifact, snapshot_dir

    directory = snapshot_dir(tmp_path, artifact("a.bin", PATH_RECIPE))
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    _restore_config(tmp_path / "etc", tmp_path / "jobs")

    code = main(
        [
            "--config-dir",
            str(tmp_path / "etc"),
            "restore",
            "--snapshot",
            str(directory),
            "--from-other-host",
        ]
    )

    assert code == 1
    assert "--yes" in capsys.readouterr().err


def test_restore_verify_prints_a_lost_journal_as_a_warning(
    tmp_path, capsys, monkeypatch
):
    from support import PATH_RECIPE, artifact, snapshot_dir

    from holdfast.backup import restore
    from holdfast.backup.restore import VerifyResult

    directory = snapshot_dir(tmp_path, artifact("a.bin", PATH_RECIPE))
    _restore_config(tmp_path / "etc", tmp_path / "jobs")
    monkeypatch.setattr(
        restore,
        "verify",
        lambda *a, **k: VerifyResult("s", 1, [], ["could not record this verify"]),
    )

    code = main(
        [
            "--config-dir",
            str(tmp_path / "etc"),
            "restore",
            "verify",
            "--snapshot",
            str(directory),
        ]
    )

    captured = capsys.readouterr()
    assert code == 0
    assert "checked 1 artifacts in " in captured.out
    assert "holdfast restore verify: could not record this verify" in captured.err
