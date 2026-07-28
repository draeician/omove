"""argparse CLI for omove."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from omove import __version__
from omove.analyze import analyze_store
from omove.config import (
    Settings,
    default_config_path,
    write_config_template,
)
from omove.errors import OmoveError, UsageError
from omove.logging_util import error, info
from omove.migrate import run_migrate
from omove.package import export_models, import_packages
from omove.store import list_model_names, list_store, verify_store
from omove.system import prepare_mutation, prepare_read_operation
from omove.transfer import set_silent
from omove.transition import transition_models

USAGE = """\
omove - Ollama Tiered Storage Manager

hot   = live model directory Ollama uses
cold  = on-disk archive in Ollama layout (quick freeze/thaw)
export packages = portable .omove.tar.gz files for cloud backup

Usage:
  omove list [cold|hot]
  omove analyze [hot|cold] [model ...]
  omove freeze <model> [model ...] [--analyze]
  omove thaw <model> [model ...]
  omove export <model> [model ...] [--from hot|cold] [-o PATH] [--remove]
  omove import <package.omove.tar.gz> [...] [--to hot|cold]
  omove verify [cold|hot] [model ...]
  omove migrate [all|cold|hot]
  omove migrate cold|hot <model> ...
  omove version | help
  omove config path|show|init

analyze
  Show a tree of each model's blobs and which other models share them.
  With no model names, analyzes every model in the tier (default: hot).
  UNIQUE = would be freed if this model alone were removed/frozen.
  SHARED = kept until every referencing model is gone.
  Example:  omove analyze hot
            omove analyze cold
            omove analyze hot gemma-4-26B-A4B-it-uncensored-GGUF:Q8_0
            omove freeze --analyze           (preview all hot; no freeze)
            omove freeze MODEL --analyze

freeze / thaw
  Quick move between hot and cold stores (same Ollama layout).
  Model names are required (does not freeze/thaw the whole store).

export / import
  Package a model (manifest + all blobs) into a portable .omove.tar.gz
  for cloud backup, or restore it later.
  The package is the FULL model (every digest), even if layers are SHARED
  with other local models — not a UNIQUE-only delta.
  Default export directory: <cold_path>/exports
    override with:  -o PATH  or  export_path in config  or  OMOVE_EXPORT_PATH
  --from hot|cold   which store to read (default: hot)
  --remove          delete the model from that store after a successful export
  --to hot|cold     where import writes (default: hot)

Config file: ~/.config/omove/config.toml  (omove config init)

Paths:
  hot_path / OMOVE_HOT_PATH / OLLAMA_MODELS   live Ollama models
  cold_path / OMOVE_COLD_PATH                freeze/thaw archive
  export_path / OMOVE_EXPORT_PATH            default export packages dir
  cold_mount / OMOVE_COLD_MOUNT              optional disk-mount pin
  allow_unmounted_cold                       allow archive on root disk /
  allow_live_ollama                          allow mutate while ollama runs

Privileges:
  list/verify/analyze run as your user.
  Mutations use sudo only for systemctl stop/start/is-active.
  Progress is on by default; --silent suppresses hash/transfer progress.

Extras: --dry-run  --json  --silent
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
        "--silent",
        action="store_true",
        help="Suppress transfer/hash progress output",
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
    freeze_p.add_argument(
        "models",
        nargs="*",
        help="Models to freeze (required unless --analyze)",
    )
    freeze_p.add_argument("--dry-run", action="store_true")
    freeze_p.add_argument(
        "--analyze",
        action="store_true",
        help="Show reclaim tree for these models in hot; do not freeze",
    )

    thaw_p = sub.add_parser("thaw", add_help=True)
    thaw_p.add_argument(
        "models",
        nargs="+",
        help="Models to thaw",
    )
    thaw_p.add_argument("--dry-run", action="store_true")

    analyze_p = sub.add_parser("analyze", add_help=True)
    analyze_p.add_argument(
        "args",
        nargs="*",
        help="Optional: hot|cold then model names (default: all in hot)",
    )
    analyze_p.add_argument("--json", action="store_true")

    export_p = sub.add_parser("export", add_help=True)
    export_p.add_argument(
        "models",
        nargs="+",
        help="Models to export",
    )
    export_p.add_argument(
        "--from",
        dest="source_tier",
        choices=("hot", "cold"),
        default="hot",
        help="Store to read from (default: hot)",
    )
    export_p.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Output file or directory (default: export_path / <cold>/exports)",
    )
    export_p.add_argument(
        "--remove",
        action="store_true",
        help="Remove model from source store after successful export",
    )
    export_p.add_argument("--dry-run", action="store_true")

    import_p = sub.add_parser("import", add_help=True)
    import_p.add_argument("packages", nargs="+")
    import_p.add_argument(
        "--to",
        dest="dest_tier",
        choices=("hot", "cold"),
        default="hot",
        help="Store to write into (default: hot)",
    )
    import_p.add_argument("--dry-run", action="store_true")

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
    print(f"export_path:           {settings.export_root}")
    print(f"cold_mount (config):   {settings.cold_mount}")
    print(f"cold_mount (detected): {detected or '(unknown)'}")
    print(f"ollama_user:           {settings.ollama_user}")
    print(f"ollama_service:        {settings.ollama_service}")
    print(f"lock_file:             {settings.lock_file}")
    print(f"allow_unmounted_cold:  {settings.allow_unmounted_cold}")
    print(f"allow_live_ollama:     {settings.allow_live_ollama}")
    return 0


def _models_or_all(
    settings: Settings,
    tier: str,
    models: list[str],
) -> list[str]:
    """Return explicit model list, or every model in the tier if empty."""
    if models:
        return list(models)
    names = list_model_names(settings.root_for(tier), settings)
    if not names:
        raise UsageError(f"No models found in {tier} storage.")
    info(f"No models specified; selecting all {len(names)} in {tier}.")
    return names


def _parse_analyze_args(args: list[str]) -> tuple[str, list[str]]:
    tier = "hot"
    rest = list(args)
    if rest and rest[0] in {"cold", "hot"}:
        tier = rest.pop(0)
    return tier, rest


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

    set_silent(bool(getattr(ns, "silent", False)))
    skip = bool(getattr(ns, "skip_privileges", False))

    try:
        settings = Settings.load()
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

        if ns.command == "analyze":
            tier, models = _parse_analyze_args(ns.args)
            session = prepare_read_operation(
                settings, argv=sys.argv, skip_privileges=skip
            )
            with session:
                # Empty models → analyze_store already covers the whole tier.
                if not models:
                    info(f"No models specified; analyzing all in {tier}.")
                return analyze_store(
                    settings, tier, models, as_json=ns.json
                )

        if ns.command == "freeze":
            if ns.analyze:
                session = prepare_read_operation(
                    settings, argv=sys.argv, skip_privileges=skip
                )
                with session:
                    models = _models_or_all(settings, "hot", ns.models)
                    return analyze_store(
                        settings, "hot", models, as_json=False
                    )
            if not ns.models:
                raise UsageError(
                    "freeze requires at least one model name "
                    "(or use --analyze to preview reclaim)."
                )
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

        if ns.command == "export":
            session = prepare_mutation(
                settings, argv=sys.argv, skip_privileges=skip
            )
            with session:
                return export_models(
                    session,
                    ns.models,
                    source_tier=ns.source_tier,
                    output=ns.output,
                    remove=ns.remove,
                    dry_run=ns.dry_run,
                )

        if ns.command == "import":
            session = prepare_mutation(
                settings, argv=sys.argv, skip_privileges=skip
            )
            with session:
                return import_packages(
                    session,
                    ns.packages,
                    dest_tier=ns.dest_tier,
                    dry_run=ns.dry_run,
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
    except KeyboardInterrupt:
        error("Interrupted.")
        return 130
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
