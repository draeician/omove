# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2026-07-27

### Added

- XDG config file `~/.config/omove/config.toml` with `omove config path|show|init`
- Auto-detect the disk mount that contains `cold_path` via `findmnt`

### Changed

- Cold-mount safety refuses archives on root `/` instead of requiring the
  parent directory to be a mountpoint
- Help/README explain hot/cold paths and safety flags in plain language

## [0.1.0] - 2026-07-27

### Added

- Python port of omove with modular package under `src/omove`
- Commands: `list`, `verify`, `freeze`, `thaw`, `migrate`, `version`, `help`
- Optional XDG config: `~/.config/omove/config.toml` plus `omove config path|show|init`
- Safety parity with Bash 3.2.0: root/sudo, flock, cold mount check, systemd
  stop/restart, live-process guard, verified sparse blob copies, GC
- Extras: `--dry-run` on mutate/migrate, `--json` on list/verify
- pytest suite with synthetic hot/cold store fixtures
- `omove-py` launcher and `docs/SMOKE.md` manual checklist
- Packaging via `pyproject.toml` / hatchling (`pipx install .`)

### Deprecated

- Bash `omove` script (still present; prints a deprecation warning)
