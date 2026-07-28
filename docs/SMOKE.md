# Manual smoke checklist (real stores)

Run against real hot/cold Ollama stores only after `pytest` passes.
Expect `omove --version` to match [CHANGELOG.md](../CHANGELOG.md)
(currently **0.7.x**).

## Setup

```bash
cd /path/to/omove
./omove-py version
# or: pipx-installed `omove version`
# or: PYTHONPATH=src python3 -m omove version
```

Prefer config over one-off env:

```bash
omove config init
omove config show
```

Optional env overrides:

```bash
export OMOVE_HOT_PATH=...
export OMOVE_COLD_PATH=...
export OMOVE_COLD_MOUNT=...          # optional mount pin
export OMOVE_EXPORT_PATH=...         # optional package dir
# OMOVE_ALLOW_UNMOUNTED_COLD=1       # only for intentional / archives
```

Confirm you can read (and for mutations, write) hot/cold **without** full
root. Mutations should only prompt for sudo if your systemctl rules are not
NOPASSWD — ideally passwordless via the README sudoers snippet.

## Read-only first

1. `omove list hot` — columns aligned even with long names
2. `omove list cold`
3. `omove verify hot` — expect progress lines on large blobs
4. `omove verify cold`
5. `omove analyze hot` — UNIQUE vs SHARED tree
6. Optional: `omove list hot --json` / `omove analyze hot --json`
7. Optional: `omove --silent verify hot` — no progress bars

## Dry-run mutations

1. Pick a small model in hot: `MODEL=...`
2. `omove freeze --dry-run "$MODEL"`
3. Confirm stores unchanged (`list` / `verify`)
4. `omove freeze --analyze "$MODEL"` — preview reclaim; no freeze

## Live freeze / thaw (one small model)

1. `omove freeze "$MODEL"` — Ollama stopped via `sudo systemctl`, then restarted
2. Model in cold, gone from hot; note reclaim size vs `analyze`
3. `omove thaw "$MODEL"`
4. Restored to hot; `verify` both tiers

## Export / import

1. `omove export "$MODEL" --dry-run`
2. `omove export "$MODEL"` — package under `export_path` / `<cold>/exports`
3. Confirm archive size reflects **full** model (shared layers included)
4. `omove import /path/to/*.omove.tar.gz --dry-run`
5. Import to a scratch tier or after removing the hot copy, then `verify`

## Migrate (if legacy layouts exist)

1. `omove migrate --dry-run`
2. `omove migrate all` (or `cold` / `hot`)

## Interrupt / lock

1. With another omove holding the lock (or while waiting), press Ctrl+C —
   expect a short “Interrupted.” / clean exit, not a traceback.

## Bash reference

`./omove.bash` (if present) still works but prints a deprecation warning.
Suppress with `OMOVE_SUPPRESS_DEPRECATION=1` if needed for scripts. Prefer
the Python `omove` on PATH.
