"""argparse CLI for omove."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from omove import __version__
from omove.config import Settings
from omove.errors import OmoveError, UsageError
from omove.logging_util import error
from omove.migrate import run_migrate
from omove.store import list_store, verify_store
from omove.system import prepare_mutation, prepare_read_operation
from omove.transition import transition_models

USAGE = """\
omove - Ollama Tiered Storage Manager

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

Model names may be supplied in normal Ollama forms, for example:
  llama3.2
  llama3.2:latest
  team/model:production
  registry.example.com:5000/team/model:production

Environment overrides:
  OLLAMA_MODELS                 Effective Ollama model root
  OMOVE_HOT_PATH                Overrides OLLAMA_MODELS
  OMOVE_COLD_PATH               Cold archive root
  OMOVE_COLD_MOUNT              Mount point that must contain cold storage
  OMOVE_OLLAMA_USER             Ollama service account, default: ollama
  OMOVE_OLLAMA_SERVICE          systemd unit, default: ollama.service
  OMOVE_LOCK_FILE               Lock file, default: /run/lock/omove.lock
  OMOVE_ALLOW_UNMOUNTED_COLD=1  Permit cold storage on a non-mount-point path
  OMOVE_ALLOW_LIVE_OLLAMA=1     Permit mutation while an Ollama process is live

Python extras:
  --dry-run                     Plan freeze/thaw/migrate without mutating
  --json                        Machine-readable list/verify output

The freeze, thaw, and migrate commands stop an active systemd Ollama service
and restart it when the operation finishes. A manually started Ollama process
causes the operation to abort unless OMOVE_ALLOW_LIVE_OLLAMA=1 is set.
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

    sub.add_parser("version", add_help=True)
    sub.add_parser("help", add_help=True)
    return parser


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

    settings = Settings.from_env()
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


if __name__ == "__main__":
    raise SystemExit(main())
