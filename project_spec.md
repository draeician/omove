# Project Specification: omove

Ollama Tiered Storage Manager — safely move models between hot (active
Ollama store) and cold (archive) storage while preserving canonical
manifest paths and content-addressed blobs.

## Status

- Primary implementation: **Python** package `src/omove/` (**0.7.x**)
- Public repo: https://github.com/draeician/omove
- Legacy: `omove.bash` (Bash 3.2.0 behavioral baseline; deprecated)
- Agent system: customized (`AGENTS.md` → `[CUSTOMIZED]`)
- Package lives under `src/` because root naming during the port occupied
  the historical `omove` path

## Tech stack

- **Language**: Python 3.10+
- **CLI**: argparse; console script `omove` via hatchling / pipx
- **Stdlib**: `json`, `hashlib`, `pathlib`, `fcntl`, `subprocess`, `tarfile`
- **External tools**: `rsync` (sparse blob copy), `systemctl`, `mountpoint`,
  `sudo` (systemctl; confirmed `chmod`/`chown` for store dirs), `pgrep`,
  `findmnt`
- **Platform**: Linux only (systemd + Ollama on-disk layout)

## Testing

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
PYTHONPATH=src python3 -m pytest
PYTHONPATH=src python3 -m omove --version
```

Manual smoke: [docs/SMOKE.md](docs/SMOKE.md).

## Architecture

```text
src/omove/       # Python package
tests/           # pytest against synthetic stores
omove.bash       # deprecated Bash reference
omove-py         # thin launcher for repo checkouts
docs/SMOKE.md    # real-store checklist
```

| Module | Responsibility |
|--------|----------------|
| `config` | File + env → `Settings` |
| `paths` | Canonicalize / query match / display names |
| `manifest` | Load/validate Ollama manifests; digests |
| `store` | list / verify / enumerate |
| `analyze` | UNIQUE vs SHARED blob reclaim trees |
| `transfer` | Verified sparse copy; progress / `--silent` |
| `transition` | freeze / thaw / GC |
| `package` | export / import `.omove.tar.gz` |
| `migrate` | Layout migration + cold-from-hot repair |
| `system` | Lock, mount checks, systemctl / confirmed chmod+chown |
| `cli` | argparse dispatch |

## Safety rules (never violate)

1. Hot and cold roots must differ and must not nest.
2. Refuse symlink `manifests/` or `blobs/` directories.
3. Cold archive must not land on root `/` unless
   `allow_unmounted_cold` / `OMOVE_ALLOW_UNMOUNTED_COLD=1`.
   `cold_mount` is optional; otherwise auto-detect via `findmnt`.
4. Mutations stop systemd Ollama and refuse a live `ollama` process unless
   `OMOVE_ALLOW_LIVE_OLLAMA=1`.
5. Blob copies are content-verified (sha256) before rename; source
   manifests are removed only after destination commit.
6. Preserve canonical `host/namespace/model/tag` and legacy path forms.
7. Do not silently `chown` store files. Report permission failures clearly.
   Mutations require write access on hot and cold store directories
   (including nested dirs under `manifests/`) and collect every failure
   before aborting. On an interactive TTY, omove may offer
   `sudo chmod g+w`, then if needed
   `sudo chown ollama_user:ollama_user`, after confirmation.
8. Do not re-exec the whole CLI under `sudo`; elevate only `systemctl`
   and confirmed `chmod`/`chown` for store directory permission fixes.

## Behavioral notes

- **Progress** is on by default; global `--silent` opts out.
- **Export** packages the full model (all digests), not UNIQUE-only deltas.
- **Analyze** with no models = entire tier; **freeze/thaw/export** require
  explicit model names (`freeze --analyze` is the all-hot preview).
- Lock defaults to `/run/lock/omove.lock`, with user-writable fallbacks.
- Ctrl+C during lock wait exits cleanly (130).

## Versioning

- SemVer in `pyproject.toml` and `src/omove/__init__.py` (must match).
- Bash `3.2.0` is historical parity baseline, not SemVer lineage.
- Conventional commits; Manager bump policy on commit.

## Configuration

File: `~/.config/omove/config.toml` (`omove config init|show|path`).

Env overrides (highest precedence):

- `OLLAMA_MODELS` / `OMOVE_HOT_PATH`
- `OMOVE_COLD_PATH` / `OMOVE_COLD_MOUNT`
- `OMOVE_EXPORT_PATH`
- `OMOVE_OLLAMA_USER` / `OMOVE_OLLAMA_SERVICE`
- `OMOVE_LOCK_FILE`
- `OMOVE_ALLOW_UNMOUNTED_COLD` / `OMOVE_ALLOW_LIVE_OLLAMA`

## Commands

```text
omove list [cold|hot]
omove analyze [hot|cold] [model ...]
omove verify [cold|hot] [model ...]
omove freeze <model> ... [--dry-run] [--analyze]
omove thaw <model> ... [--dry-run]
omove export <model> ... [--from hot|cold] [-o PATH] [--remove] [--dry-run]
omove import <package.omove.tar.gz> ... [--to hot|cold] [--dry-run]
omove migrate [all|cold|hot] [--dry-run]
omove config path|show|init
omove version | help
```

Global: `--silent`, `--json` (list/verify/analyze), `--dry-run` (mutations).

## Roadmap (completed through 0.7)

1. [x] Bootstrap + Python core
2. [x] Read ops: list / verify
3. [x] Mutations: freeze / thaw / GC / dry-run
4. [x] Migrate + packaging (pipx)
5. [x] Config file + cold-mount auto-detect
6. [x] Export / import packages + progress
7. [x] Analyze UNIQUE/SHARED
8. [x] Option B privileges (sudo systemctl only)
9. [x] `--silent` / progress-by-default
10. [ ] Retire `omove.bash` when unused
