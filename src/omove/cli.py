"""argparse CLI for omove."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from omove import __version__
from omove.config import (
    Settings,
    default_config_path,
    write_config_template,
)
from omove.errors import OmoveError, UsageError
from omove.logging_util import error
from omove.migrate import run_migrate
from omove.store import list_store, verify_store
from omove.system import prepare_mutation, prepare_read_operation
from omove.transition import transition_models

USAGE = """\
omove - Ollama Tiered Storage Manager

Moves Ollama models between:
  hot  = the live model directory Ollama uses
  cold = an archive directory you choose (often on a bigger/slower disk)

Usage:
  omove list [cold|hot]
  omove freeze <model> [model ...]
  omove thaw <model> [model ...]
  omove verify [cold|hot] [model ...]
  omove migrate [all|cold|hot]
  omove migrate cold <model> [model ...]
  omove migrate hot <model> [model ...]
  omove version
  omove help
  omove config path|show|init

Config file (recommended):
  ~/.config/omove/config.toml
  Create with:  omove config init
  Precedence:   environment > config file > defaults

What the path settings mean:
  hot_path / OMOVE_HOT_PATH / OLLAMA_MODELS
      Live Ollama models directory.
  cold_path / OMOVE_COLD_PATH
      Archive directory for frozen models.
  cold_mount / OMOVE_COLD_MOUNT
      Optional. Disk mount that should hold the archive (example: /opt/md2).
      If omitted, omove detects it automatically. It does NOT need to be the
      parent folder of cold_path.
  allow_unmounted_cold / OMOVE_ALLOW_UNMOUNTED_COLD=1
      Allow the archive to live on the root disk (/). Off by default because
      that often means a removable disk was not mounted.
  allow_live_ollama / OMOVE_ALLOW_LIVE_OLLAMA=1
      Allow changes while Ollama is still running. Off by default because that
      can corrupt models.

Other:
  OMOVE_OLLAMA_USER      Service account (default: ollama)
  OMOVE_OLLAMA_SERVICE   systemd unit (default: ollama.service)
  OMOVE_LOCK_FILE        Lock file path

Python extras:
  --dry-run   Show freeze/thaw/migrate plan without changing files
  --json      Machine-readable list/verify output

freeze/thaw/migrate stop an active systemd Ollama service and restart it
when finished. A manually started ollama process aborts the operation unless
allow_live_ollama is enabled.
"""


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser."""
    parser = argparse.ArgumentParser(
        prog="omove",
        description="Ollama Tiered Storage Manager",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=USAGE,
        add_help=False,
    )
    parser.add_argument(
        "--version",
        "-V",
        action="version",
        version=f"omove {__version__}",
    )
    parser.add_argument(
        "--help",
        "-h",
        action="store_true",
        help="Show help",
    )
    parser.add_argument(
        "--skip-privileges",
        action="store_true",
        help=argparse.SUPPRESS,
    )

    sub = parser.add_subparsers(dest="command")

    list_p = sub.add_parser("list", add_help=True)
    list_p.add_argument(
        "tier",
        nargs="?",
        default="cold",
        choices=("cold", "hot"),
    )
    list_p.add_argument("--json", action="store_true")

    verify_p = sub.add_parser("verify", add_help=True)
    verify_p.add_argument("args", nargs="*")
    verify_p.add_argument("--json", action="store_true")

    freeze_p = sub.add_parser("freeze", add_help=True)
    freeze_p.add_argument("models", nargs="+")
    freeze_p.add_argument("--dry-run", action="store_true")

    thaw_p = sub.add_parser("thaw", add_help=True)
    thaw_p.add_argument("models", nargs="+")
    thaw_p.add_argument("--dry-run", action="store_true")

    migrate_p = sub.add_parser("migrate", add_help=True)
    migrate_p.add_argument("args", nargs="*")
    migrate_p.add_argument("--dry-run", action="store_true")

    config_p = sub.add_parser("config", add_help=True)
    config_p.add_argument(
        "action",
        nargs="?",
        default="show",
        choices=("path", "show", "init"),
    )
    config_p.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing config on init",
    )

    sub.add_parser("version", add_help=True)
    sub.add_parser("help", add_help=True)
    return parser


def _cmd_config(action: str, *, force: bool = False) -> int:
    """Handle config path|show|init."""
    if action == "path":
        print(default_config_path())
        return 0
    if action == "init":
        try:
            path = write_config_template(force=force)
        except FileExistsError as exc:
            error(str(exc))
            error("Re-run with --force to overwrite, or edit the file.")
            return 1
        print(f"Wrote {path}")
        print("Edit hot_path and cold_path, then run: omove config show")
        return 0

    settings = Settings.load()
    path = settings.config_path or default_config_path()
    exists = "yes" if path.is_file() else "no (using defaults/env)"
    from omove.system import filesystem_mount

    detected = filesystem_mount(settings.cold_root)
    print(f"config_file:           {path} ({exists})")
    print(f"hot_path:              {settings.hot_root}")
    print(f"cold_path:             {settings.cold_root}")
    print(f"cold_mount (config):   {settings.cold_mount}")
    print(f"cold_mount (detected): {detected or '(unknown)'}")
    print(f"ollama_user:           {settings.ollama_user}")
    print(f"ollama_service:        {settings.ollama_service}")
    print(f"lock_file:             {settings.lock_file}")
    print(f"allow_unmounted_cold:  {settings.allow_unmounted_cold}")
    print(f"allow_live_ollama:     {settings.allow_live_ollama}")
    return 0


def _parse_verify_args(args: list[str]) -> tuple[str, list[str]]:
    tier = "cold"
    rest = list(args)
    if rest and rest[0] in {"cold", "hot"}:
        tier = rest.pop(0)
    return tier, rest


def _parse_migrate_args(args: list[str]) -> tuple[str, list[str]]:
    tier = "all"
    rest = list(args)
    if rest and rest[0] in {"all", "cold", "hot"}:
        tier = rest.pop(0)
    return tier, rest


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint."""
    argv_list = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    try:
        ns = parser.parse_args(argv_list)
    except SystemExit as exc:
        code = exc.code
        return int(code) if isinstance(code, int) else 1

    if ns.help or ns.command in {None, "help"}:
        if ns.command is None and not ns.help:
            print(USAGE, file=sys.stderr)
            return 2
        print(USAGE)
        return 0

    if ns.command == "version":
        print(f"omove {__version__}")
        return 0

    if ns.command == "config":
        return _cmd_config(ns.action, force=ns.force)

    settings = Settings.load()
    skip = bool(getattr(ns, "skip_privileges", False))

    try:
        if ns.command == "list":
            session = prepare_read_operation(
                settings, argv=sys.argv, skip_privileges=skip
            )
            with session:
                return list_store(settings, ns.tier, as_json=ns.json)

        if ns.command == "verify":
            tier, models = _parse_verify_args(ns.args)
            session = prepare_read_operation(
                settings, argv=sys.argv, skip_privileges=skip
            )
            with session:
                return verify_store(
                    settings, tier, models, as_json=ns.json
                )

        if ns.command == "freeze":
            session = prepare_mutation(
                settings, argv=sys.argv, skip_privileges=skip
            )
            with session:
                return transition_models(
                    session, "freeze", ns.models, dry_run=ns.dry_run
                )

        if ns.command == "thaw":
            session = prepare_mutation(
                settings, argv=sys.argv, skip_privileges=skip
            )
            with session:
                return transition_models(
                    session, "thaw", ns.models, dry_run=ns.dry_run
                )

        if ns.command == "migrate":
            tier, models = _parse_migrate_args(ns.args)
            session = prepare_mutation(
                settings, argv=sys.argv, skip_privileges=skip
            )
            with session:
                return run_migrate(
                    session, tier, models, dry_run=ns.dry_run
                )

        raise UsageError(f"Unknown command: {ns.command}")
    except UsageError as exc:
        error(str(exc))
        print(USAGE, file=sys.stderr)
        return exc.exit_code
    except OmoveError as exc:
        error(str(exc))
        return exc.exit_code
    except ValueError as exc:
        error(str(exc))
        return 1
    except PermissionError as exc:
        error(f"Permission denied: {exc}")
        return 1
    except OSError as exc:
        error(f"OS error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())