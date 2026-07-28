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
omove analyze [hot|cold] [model ...]
omove verify [cold|hot] [model ...]
omove freeze <model> [model ...]
omove thaw <model> [model ...]
omove export <model> [--from hot|cold] [-o PATH] [--remove]
omove import <package.omove.tar.gz> [--to hot|cold]
omove migrate [all|cold|hot]
omove freeze --dry-run <model>
omove freeze --analyze
omove list hot --json
omove --silent export MODEL
omove version
```

Without install (repo checkout):

```bash
./omove-py list hot
# or: PYTHONPATH=src python3 -m omove list hot
```

### Environment / config

Optional file: `~/.config/omove/config.toml`

```bash
omove config init    # write a starter file
omove config show    # effective settings (+ detected disk mount)
omove config path    # print the file path
```

Example:

```toml
hot_path = "/usr/share/ollama/.ollama/models"
cold_path = "/opt/md2/.../models/ollama_archive"
# export_path = "/opt/md2/.../omove_exports"   # default: <cold_path>/exports
# cold_mount is optional; omove auto-detects the disk (e.g. /opt/md2)
```

| Setting | Meaning |
|---------|---------|
| `hot_path` | Live Ollama models directory |
| `cold_path` | Archive directory for freeze/thaw |
| `export_path` | Default directory for `.omove.tar.gz` packages |
| `cold_mount` | Optional pin to a disk mount (e.g. `/opt/md2`) |
| `allow_unmounted_cold` | Allow archive on root disk `/` (usually unsafe) |
| `allow_live_ollama` | Allow mutate while Ollama is still running |

Env vars `OMOVE_*` / `OLLAMA_MODELS` still work and override the file.
Precedence: **environment > config file > defaults**.

### Privileges (no full sudo re-exec)

`list` / `verify` / `analyze` run as your user (no password prompt).
Mutations (`freeze` / `thaw` / `export` / `import` / `migrate`) call
`sudo systemctl …` only to stop/start Ollama. Give yourself read/write on
the hot and cold stores (group or ACL); do **not** need root for the whole
CLI.

Example sudoers (`visudo -f /etc/sudoers.d/omove`):

```sudoers
Cmnd_Alias OMOVE_SYSTEMCTL = \
  /usr/bin/systemctl is-active ollama.service, \
  /usr/bin/systemctl stop ollama.service, \
  /usr/bin/systemctl start ollama.service, \
  /bin/systemctl is-active ollama.service, \
  /bin/systemctl stop ollama.service, \
  /bin/systemctl start ollama.service
YOURUSER ALL=(root) NOPASSWD: OMOVE_SYSTEMCTL
```

If `/run/lock/omove.lock` is not writable, omove falls back to
`$XDG_RUNTIME_DIR/omove.lock` or `~/.cache/omove/omove.lock`.

Hash and transfer progress is on by default. Pass ``--silent`` to suppress
it (e.g. ``omove --silent export MODEL``).


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
