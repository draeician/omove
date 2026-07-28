# Manual smoke checklist (real stores)

Run against real hot/cold Ollama stores only after fixtures pass.

## Setup

```bash
cd /path/to/omove
./omove-py version          # expect: omove 0.1.0
# or: PYTHONPATH=src python3 -m omove version
```

Optional env (same as Bash):

```bash
export OMOVE_HOT_PATH=...
export OMOVE_COLD_PATH=...
export OMOVE_COLD_MOUNT=...
# OMOVE_ALLOW_UNMOUNTED_COLD=1 only for non-mount test paths
```

## Read-only first

1. `./omove-py list hot`
2. `./omove-py list cold`
3. `./omove-py verify hot`
4. `./omove-py verify cold`
5. Optional: `./omove-py list hot --json`

## Dry-run mutations

1. Pick a small model present in hot: `MODEL=...`
2. `./omove-py freeze --dry-run "$MODEL"`
3. Confirm stores unchanged (`list` / `verify` again)

## Live round-trip (one small model)

1. `./omove-py freeze "$MODEL"`
2. Confirm model appears in cold, gone from hot
3. `./omove-py thaw "$MODEL"`
4. Confirm restored to hot; verify both tiers

## Migrate (if legacy layouts exist)

1. `./omove-py migrate --dry-run`
2. `./omove-py migrate all` (or `cold` / `hot`)

## Bash

Bash `./omove` still works but prints a deprecation warning. Suppress with
`OMOVE_SUPPRESS_DEPRECATION=1` if needed for scripts.
