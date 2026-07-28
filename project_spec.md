# Project Specification: omove

Ollama Tiered Storage Manager — safely move models between hot (active
Ollama store) and cold (archive) storage while preserving canonical
manifest paths and content-addressed blobs.

## Status

- Primary implementation: **Python** package `src/omove/` (v0.1.0+)
- Legacy: Bash script `omove` (v3.2.0) kept temporarily, deprecated
- Agent system: customized for this repo
- Note: package lives under `src/` because a root file named `omove`
  (Bash) already occupies that path

## Tech stack

- **Primary language**: Python 3.10+
- **CLI**: argparse (`PYTHONPATH=src python -m omove`; console script
  after packaging)
- **Stdlib**: `json`, `hashlib`, `pathlib`, `fcntl`, `subprocess`
- **External tools** (still required): `rsync` (sparse blob copy),
  `systemctl`, `mountpoint`, `sudo`, `pgrep`
- **Legacy**: Bash `omove` — behavioral parity baseline for the port
- **Platform**: Linux only (systemd + Ollama layout)

## Testing

- Install deps (when packaged): `python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"`
- Minimal phase: `PYTHONPATH=src python3 -m pytest`
- Lint (optional): `ruff check src/omove tests`
- Run CLI: `PYTHONPATH=src python3 -m omove --version`
- Manual smoke: `list`/`verify` on real stores before any `freeze`/`thaw`

## Architecture and conventions

### Layout

```text
src/omove/       # Python package
tests/           # pytest fixtures against synthetic stores
omove            # deprecated Bash CLI (parity reference)
```

### Module boundaries

| Module | Responsibility |
|--------|----------------|
| `config` | Env → `Settings` |
| `paths` | Manifest path canonicalize / query match / display names |
| `manifest` | Load/validate Ollama manifests; digest extraction |
| `store` | list / verify / enumerate |
| `transfer` | Verified sparse blob + manifest copy |
| `transition` | freeze / thaw / GC |
| `migrate` | Layout migration + cold-from-hot repair |
| `system` | Lock, mount, sudo-wrapped systemctl, store checks |
| `cli` | argparse dispatch |

### Safety rules (never violate)

1. Hot and cold roots must differ and must not nest.
2. Refuse symlink `manifests/` or `blobs/` directories.
3. Cold archive must not land on the root filesystem `/` unless
   `allow_unmounted_cold` / `OMOVE_ALLOW_UNMOUNTED_COLD=1` is set.
   `cold_mount` is an optional pin; otherwise the mount is auto-detected.
4. Mutations stop systemd Ollama and refuse a live `ollama` process unless
   `OMOVE_ALLOW_LIVE_OLLAMA=1`.
5. Blob copies are content-verified (sha256) before rename; source
   manifests are only removed after destination commit.
6. Do not invent path layouts — preserve canonical
   `host/namespace/model/tag` and legacy forms from Bash.

### Versioning

- Python package `__version__` / `pyproject.toml` start at **0.1.0**.
- Bash `3.2.0` is the **behavioral** baseline, not SemVer lineage.
- Conventional commits; Manager bump policy applies after packaging.

### Environment overrides (parity with Bash)

- `OLLAMA_MODELS` / `OMOVE_HOT_PATH`
- `OMOVE_COLD_PATH` / `OMOVE_COLD_MOUNT`
- `OMOVE_OLLAMA_USER` / `OMOVE_OLLAMA_SERVICE`
- `OMOVE_LOCK_FILE`
- `OMOVE_ALLOW_UNMOUNTED_COLD` / `OMOVE_ALLOW_LIVE_OLLAMA`

## Roadmap

1. [x] Bootstrap project truth (this file + agent modes)
2. [x] Python core: config, paths, manifest, CLI skeleton
3. [x] Read ops: list / verify (+ `--json`)
4. [x] Mutation ops: freeze / thaw / GC / `--dry-run`
5. [x] Migrate + cold repair
6. [x] Deprecate Bash banner + smoke docs
7. [x] Promote to `pyproject.toml` + pipx; plan Bash retirement

## Bash retirement plan

After real-store smoke ([docs/SMOKE.md](docs/SMOKE.md)) and
`pipx install .`, remove the Bash `omove` script (or rename to
`omove.bash` in an archive folder) so `omove` on PATH is the Python
console script only.

## Commands (parity)

```text
omove list [cold|hot]
omove freeze <model> [model ...]
omove thaw <model> [model ...]
omove verify [cold|hot] [model ...]
omove migrate [all|cold|hot]
omove migrate cold|hot <model> ...
omove version | help
```

Python extras: `--dry-run` on mutate/migrate; `--json` on list/verify.
