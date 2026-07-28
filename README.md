# omove

Ollama Tiered Storage Manager — safely move models between **hot** (active
Ollama store) and **cold** (archive) storage while preserving canonical
manifest paths and content-addressed blobs.

Primary implementation is the Python package `src/omove` (console script
`omove`). The legacy Bash script is kept as `omove.bash` (deprecated).

Current version: see `omove --version` / [CHANGELOG.md](CHANGELOG.md).

## Requirements

- Linux with systemd
- Python 3.10+
- External tools: `rsync`, `systemctl`, `mountpoint`, `sudo`, `pgrep`,
  `findmnt` (recommended)

## Install

```bash
# From this repo
pipx install .

# Or from GitHub
pipx install git+https://github.com/draeician/omove.git

# Force upgrade after a pull
pipx install git+https://github.com/draeician/omove.git --force

# Development
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

Without install (repo checkout):

```bash
./omove-py list hot
# or: PYTHONPATH=src python3 -m omove list hot
```

## Quick command reference

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
omove config path|show|init
omove version
omove help
```

## Concepts

| Term | Meaning |
|------|---------|
| **hot** | Live Ollama models directory (`OLLAMA_MODELS` / `hot_path`) |
| **cold** | On-disk archive in the same Ollama layout (quick freeze/thaw) |
| **export package** | Portable `.omove.tar.gz` (manifest + **all** blobs for that model) |
| **UNIQUE** | Blobs only this model references — freed if it alone is removed/frozen |
| **SHARED** | Blobs also used by other models — stay until every user is gone |

Ollama stores blobs by content hash. Several tags can share one large layer.
`analyze` explains reclaim; `export` always packs a **self-contained** model
(shared layers are included again in the archive).

## Common use cases

### See what is in hot or cold

```bash
omove list hot
omove list cold
omove list hot --json          # scripting
```

Columns widen to fit long Hugging Face-style names.

### Check integrity before moving anything

```bash
omove verify hot
omove verify cold MODEL
```

Large models print per-blob hash progress unless you pass `--silent`.

### Preview how much disk a freeze would free

```bash
omove analyze hot
omove analyze hot some-model:tag
omove freeze --analyze         # all hot models; does not freeze
omove freeze MODEL --analyze
```

If freeze later reports a tiny reclaim (e.g. hundreds of bytes), the big
layers were **SHARED** with models still in hot — that is expected.

### Free hot disk by archiving a model (same machine)

```bash
omove freeze --dry-run MODEL   # plan only
omove freeze MODEL             # stop Ollama, copy hot→cold, GC unreferenced
omove thaw MODEL               # bring it back
```

`freeze` / `thaw` require explicit model names (they do **not** mean “all”).

### Back up a model for cloud / another host

```bash
omove export MODEL                    # → <cold_path>/exports by default
omove export MODEL -o /path/to/dir
omove export MODEL --from cold
omove export MODEL --remove           # after successful package, delete from store
omove import foo.omove.tar.gz
omove import foo.omove.tar.gz --to cold
```

The package is the **full** model (every digest in its manifest), even when
layers are shared with other local models.

### Quiet / scripted runs

```bash
omove --silent export MODEL
omove --silent freeze MODEL
```

`--silent` only suppresses hash/transfer progress bars and related progress
lines. Normal INFO/ERROR messages still appear.

### First-time config

```bash
omove config init
omove config show
omove config path
```

Edit `~/.config/omove/config.toml`, then confirm with `config show`.

### Fix legacy manifest paths

```bash
omove migrate --dry-run
omove migrate all
```

## Configuration

Optional file: `~/.config/omove/config.toml`

Precedence: **environment > config file > built-in defaults**.

| Setting / env | Meaning |
|---------------|---------|
| `hot_path` / `OMOVE_HOT_PATH` / `OLLAMA_MODELS` | Live Ollama models |
| `cold_path` / `OMOVE_COLD_PATH` | Freeze/thaw archive |
| `export_path` / `OMOVE_EXPORT_PATH` | Default `.omove.tar.gz` directory (default: `<cold_path>/exports`) |
| `cold_mount` / `OMOVE_COLD_MOUNT` | Optional pin to a disk mount |
| `allow_unmounted_cold` / `OMOVE_ALLOW_UNMOUNTED_COLD` | Allow archive on root `/` (usually unsafe) |
| `allow_live_ollama` / `OMOVE_ALLOW_LIVE_OLLAMA` | Allow mutate while `ollama` is still running |
| `lock_file` / `OMOVE_LOCK_FILE` | Exclusive flock path (default `/run/lock/omove.lock`) |
| `ollama_service` / `OMOVE_OLLAMA_SERVICE` | Systemd unit (default `ollama.service`) |

Example:

```toml
hot_path = "/usr/share/ollama/.ollama/models"
cold_path = "/opt/md2/.../models/ollama_archive"
# export_path = "/opt/md2/.../omove_exports"
# cold_mount is optional; omove auto-detects the disk for cold_path
```

### Cold mount safety

omove refuses to use a cold archive that resolves onto the root filesystem
`/` unless `allow_unmounted_cold` is set. That usually means a removable or
NAS path is configured but not mounted, and writing would fill the system
disk.

### Privileges

- `list` / `verify` / `analyze` run as your user (no sudo).
- Mutations (`freeze` / `thaw` / `export` / `import` / `migrate`) call
  `sudo systemctl …` only to stop/start Ollama — the CLI itself is **not**
  re-executed as root.
- You need read (and for mutations, write) access to hot and cold yourself
  (group membership or ACLs). Before mutations omove walks nested dirs under
  `manifests/` (and `blobs/`), reports every inaccessible path at once, and
  on a TTY can offer `sudo chmod g+w`, then if needed
  `sudo chown ollama_user:ollama_user` (default `ollama`) on those paths.

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

### Locking and interrupts

Only one omove mutation/read session holds the lock at a time. If another
instance holds it, you see a wait message; **Ctrl+C** cancels cleanly
(exit 130, no traceback).

If `/run/lock/omove.lock` is not writable, omove falls back to
`$XDG_RUNTIME_DIR/omove.lock` or `~/.cache/omove/omove.lock`.

### Progress

Hash and transfer progress is **on by default**. Use `--silent` to suppress
it.

## Testing

```bash
PYTHONPATH=src python3 -m pytest
# or after editable install:
pytest
```

Manual smoke against real stores: [docs/SMOKE.md](docs/SMOKE.md).

## Bash retirement

After smoke-testing on your stores and installing via `pipx`, use the Python
`omove` on PATH. Keep `omove.bash` only as a historical reference; it prints
a deprecation warning (`OMOVE_SUPPRESS_DEPRECATION=1` to silence).
