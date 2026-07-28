# omove

Ollama Tiered Storage Manager — safely move models between **hot** (active
Ollama store) and **cold** (archive) storage while preserving canonical
manifest paths and content-addressed blobs.

Primary implementation is **Python** (`src/omove`). The root Bash script
`omove` is deprecated and kept temporarily as a parity reference.

## Requirements

- Linux (systemd)
- Python 3.10+
- External tools: `rsync`, `systemctl`, `mountpoint`, `sudo`, `pgrep`

## Install

```bash
pipx install .
# or for development:
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

## Usage

```bash
omove list [cold|hot]
omove verify [cold|hot] [model ...]
omove freeze <model> [model ...]
omove thaw <model> [model ...]
omove migrate [all|cold|hot]
omove freeze --dry-run <model>
omove list hot --json
omove version
```

Without install (repo checkout):

```bash
./omove-py list hot
# or: PYTHONPATH=src python3 -m omove list hot
```

### Environment

| Variable | Purpose |
|----------|---------|
| `OLLAMA_MODELS` / `OMOVE_HOT_PATH` | Hot model root |
| `OMOVE_COLD_PATH` | Cold archive root |
| `OMOVE_COLD_MOUNT` | Mount that must contain cold storage |
| `OMOVE_OLLAMA_USER` | Service account (default `ollama`) |
| `OMOVE_OLLAMA_SERVICE` | systemd unit (default `ollama.service`) |
| `OMOVE_LOCK_FILE` | Lock path |
| `OMOVE_ALLOW_UNMOUNTED_COLD=1` | Allow cold on non-mount path |
| `OMOVE_ALLOW_LIVE_OLLAMA=1` | Allow mutate while ollama is live |

## Testing

```bash
PYTHONPATH=src python3 -m pytest
# or after editable install:
pytest
```

Manual smoke against real stores: see [docs/SMOKE.md](docs/SMOKE.md).

## Bash retirement

After the Python CLI has been smoke-tested on your stores and installed via
`pipx`, remove or archive the Bash `omove` script so PATH resolves to the
console script from this package.
