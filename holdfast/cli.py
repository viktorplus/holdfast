"""Command line entry point.

Subcommands are added by the stage that implements them; this file only routes.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import holdfast

from .checkup import complaints, format_complaints
from .config import ConfigError, load_config
from .profile import ProfileError

DEFAULT_CONFIG_DIR = "/etc/holdfast"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="holdfast",
        description="Back up a Linux server and watch it for tampering.",
    )
    parser.add_argument(
        "--config-dir",
        default=DEFAULT_CONFIG_DIR,
        help=f"where holdfast.toml lives (default: {DEFAULT_CONFIG_DIR})",
    )
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("version", help="print the version and exit")
    config_cmd = commands.add_parser("config", help="inspect the configuration")
    config_actions = config_cmd.add_subparsers(dest="action")
    config_actions.add_parser("check", help="report what is missing")
    init_cmd = commands.add_parser("init", help="set this machine up")
    mode = init_cmd.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--fresh", action="store_true", help="a standalone machine that shares nothing"
    )
    mode.add_argument(
        "--join",
        metavar="PROFILE",
        help="join a collection described by a profile file",
    )
    init_cmd.add_argument(
        "--host-label",
        default=None,
        help="how this machine signs itself (default: hostname)",
    )
    backup_cmd = commands.add_parser("backup", help="make a snapshot of this machine")
    # One shows what this machine could declare, the other what the declaration
    # would do. Asking for both at once is asking two different questions.
    backup_mode = backup_cmd.add_mutually_exclusive_group()
    backup_mode.add_argument(
        "--dry-run",
        action="store_true",
        help="print what would run, and produce nothing",
    )
    backup_mode.add_argument(
        "--discover",
        action="store_true",
        help="in manual mode, write what this machine keeps to components.toml",
    )
    restore_cmd = commands.add_parser("restore", help="put a snapshot back")
    restore_cmd.add_argument(
        "action",
        nargs="?",
        choices=["list", "verify"],
        default=None,
        help="list what is inside, verify it reads back, or restore it",
    )
    restore_cmd.add_argument(
        "--snapshot", required=True, metavar="DIR", help="the snapshot directory"
    )
    restore_cmd.add_argument(
        "--identity",
        metavar="FILE",
        help="the private key: an age identity or a gpg secret key. "
        "Read for this run and copied nowhere",
    )
    restore_cmd.add_argument(
        "--identity-passphrase-file",
        metavar="FILE",
        help="a file holding the passphrase of a protected gpg key",
    )
    restore_cmd.add_argument(
        "--root", default="/", metavar="DIR", help="where path recipes land"
    )
    restore_cmd.add_argument(
        "--component", metavar="NAME", help="restore only this component"
    )
    restore_cmd.add_argument(
        "--dry-run", action="store_true", help="print the plan and change nothing"
    )
    restore_cmd.add_argument(
        "--yes", action="store_true", help="skip the confirmation. For scripts"
    )
    restore_cmd.add_argument(
        "--no-stop", action="store_true", help="do not stop or start containers"
    )
    restore_cmd.add_argument(
        "--from-other-host",
        action="store_true",
        help="allow a snapshot taken on a different machine",
    )
    restore_cmd.add_argument(
        "--pg-container", metavar="NAME", help="override the container in the recipe"
    )
    restore_cmd.add_argument(
        "--pg-user", metavar="NAME", help="override the postgres user in the recipe"
    )
    audit_cmd = commands.add_parser("audit", help="look the machine over")
    audit_cmd.add_argument(
        "--json", action="store_true", help="print the report as JSON"
    )
    audit_cmd.add_argument(
        "--coverage",
        action="store_true",
        help="print the table of checks and exit without running them",
    )
    audit_cmd.add_argument(
        "--markdown",
        action="store_true",
        help="with --coverage, print the table as docs/checks.md",
    )
    audit_cmd.add_argument(
        "--baseline",
        action="store_true",
        help="after the report, print what this machine would call normal",
    )
    audit_cmd.add_argument(
        "--no-docker",
        action="store_true",
        help="do not talk to a Docker daemon at all",
    )
    audit_cmd.add_argument(
        "--no-offsite",
        action="store_true",
        help="do not talk to the offsite storage at all",
    )
    audit_cmd.add_argument(
        "--host",
        default="/",
        metavar="ROOT",
        help="where the filesystem to audit is mounted (default: /)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "version":
        print(holdfast.__version__)
        return 0

    if args.command == "init":
        from .install import InstallError, init_fresh, init_join

        config_dir = Path(args.config_dir)
        try:
            if args.fresh:
                path = init_fresh(config_dir, args.host_label)
            else:
                path = init_join(config_dir, Path(args.join), args.host_label)
        except (InstallError, ProfileError, OSError) as exc:
            # OSError too: /etc/holdfast needs root, and the very first
            # command a reader runs should explain itself, not traceback.
            print(f"holdfast init: {exc}", file=sys.stderr)
            return 1
        print(f"wrote {path}")
        return 0

    if args.command == "config" and args.action == "check":
        import os

        config_dir = Path(args.config_dir)
        try:
            cfg = load_config(machine=config_dir / "holdfast.toml", env=os.environ)
        except ConfigError as exc:
            print(f"holdfast config check: {exc}", file=sys.stderr)
            return 1
        found = complaints(cfg)
        print(format_complaints(found))
        return 1 if found else 0

    if args.command == "backup":
        import os

        from .backup import BackupBusy, BackupError
        from .backup.discover import discover
        from .backup.engine import dry_run, run_backup
        from .backup.probe import Probe

        config_dir = Path(args.config_dir)
        components_file = config_dir / "components.toml"
        try:
            cfg = load_config(machine=config_dir / "holdfast.toml", env=os.environ)
            if args.discover:
                print(discover(cfg, Probe(), components_file), end="")
                return 0
            if args.dry_run:
                print(
                    dry_run(cfg, probe=Probe(), components_file=components_file),
                    end="",
                )
                return 0
            result = run_backup(cfg, components_file=components_file)
        except BackupBusy as exc:
            # Not an error: the nightly timer overlapping a manual run. A
            # non-zero code here would page somebody every such night.
            print(f"holdfast backup: {exc}")
            return 0
        except (ConfigError, BackupError) as exc:
            print(f"holdfast backup: {exc}", file=sys.stderr)
            return 1
        for warning in result.warnings:
            print(f"holdfast backup: {warning}", file=sys.stderr)
        print(
            f"{result.directory} ({result.total_bytes} bytes in "
            f"{len(result.artifacts)} artifacts)"
        )
        for skip in result.skipped:
            print(f"skipped {skip.what}: {skip.reason}")
        return 0

    if args.command == "restore":
        import os

        from .backup.decrypt import open_identity
        from .backup.model import BackupError, RestoreError
        from .backup.probe import Probe
        from .backup.restore import render_list, restore, verify
        from .backup.snapshot import load_snapshot

        config_dir = Path(args.config_dir)
        try:
            # A missing config is not an error: a snapshot and a key are meant
            # to be enough, and the machine being restored onto may have
            # nothing on it yet.
            cfg = load_config(machine=config_dir / "holdfast.toml", env=os.environ)
            snapshot = load_snapshot(Path(args.snapshot))
            if args.action == "list":
                print(render_list(snapshot))
                return 0

            jobs_dir = Path(str(cfg.get("jobs.dir")))
            identity = Path(args.identity) if args.identity else None
            phrase = (
                Path(args.identity_passphrase_file)
                if args.identity_passphrase_file
                else None
            )
            with open_identity(identity, passphrase_file=phrase) as key:
                if args.action == "verify":
                    from .backup.engine import run_line

                    result = verify(snapshot, key, jobs_dir=jobs_dir, run=run_line)
                    for failure in result.failures:
                        print(f"holdfast restore verify: {failure}", file=sys.stderr)
                    print(f"checked {result.checked} artifacts in {snapshot.snapshot}")
                    return 0 if result.ok else 1

                from .backup.engine import run_line

                done = restore(
                    snapshot,
                    key,
                    jobs_dir=jobs_dir,
                    probe=Probe(),
                    run=run_line,
                    root=args.root,
                    component=args.component,
                    dry_run=args.dry_run,
                    assume_yes=args.yes,
                    stop=not args.no_stop,
                    from_other_host=args.from_other_host,
                    this_label=str(cfg.get("host_label") or ""),
                    pg_container=args.pg_container,
                    pg_user=args.pg_user,
                    # No tty means nobody to confirm with, and the restore says
                    # so rather than blocking on a read that will never return.
                    ask=input if sys.stdin.isatty() else None,
                )
        except (ConfigError, RestoreError, BackupError) as exc:
            print(f"holdfast restore: {exc}", file=sys.stderr)
            return 1
        print(f"restored {len(done.done)} artifacts from {done.snapshot}")
        return 0

    if args.command == "audit":
        import os

        from .audit import (
            Context,
            exit_code,
            load_last,
            render_baseline,
            render_checks_markdown,
            render_coverage,
            render_json,
            render_text,
            run_audit,
            save_last,
        )

        if args.coverage:
            print(render_checks_markdown() if args.markdown else render_coverage())
            return 0

        config_dir = Path(args.config_dir)
        try:
            # A missing holdfast.toml is not an error here. The watchdog has
            # to be able to look over a machine nobody has configured yet -
            # that is often exactly the machine worth looking at.
            cfg = load_config(machine=config_dir / "holdfast.toml", env=os.environ)
        except ConfigError as exc:
            print(f"holdfast audit: {exc}", file=sys.stderr)
            return 1

        state_dir = Path(str(cfg.get("audit.state_dir")))
        ctx = Context(
            host=Path(args.host),
            config=cfg,
            previous=load_last(state_dir),
            docker_enabled=not args.no_docker,
            offsite_enabled=not args.no_offsite,
        )
        report = run_audit(ctx)
        print(render_json(report) if args.json else render_text(report))
        if args.baseline:
            print()
            print(render_baseline(ctx))
        try:
            save_last(state_dir, report.as_dict())
        except OSError as exc:
            # Losing the baseline is worth saying out loud, but it must not
            # discard a report that has already been produced.
            print(f"holdfast audit: could not save state: {exc}", file=sys.stderr)
        return exit_code(report)

    parser.print_usage(sys.stderr)
    return 2


def run() -> None:
    sys.exit(main())
