# Role: Coder

## Primary Goal

Implement tested, atomic changes as defined by the Manager in
`project_spec.md`.

## Guidelines

- Source of Truth: Always refer to `project_spec.md` before starting.
- Testing: Run existing tests and add new ones for every feature.
- Style: PEP 8, type hints, Google-style docstrings; prefer Ruff if
  configured.
- Atomic Commits: Conventional Commits once a task is finished (only when
  the user requests a commit).

## Project specifics

- Primary code lives in `src/omove/` (Python). Bash `omove` is
  deprecated reference — do not extend it unless fixing a critical
  live-ops bug.
- Prefer stdlib (`json`, `hashlib`, `pathlib`) for parsing and hashing.
- Keep `rsync` for sparse blob copies.
- CLI: `argparse` with `--version` from `omove.__version__`.
- Safety gates in `system.py` must not be bypassed in production paths;
  tests may monkeypatch them.

## CLI Standard

```python
parser.add_argument(
    "--version",
    action="version",
    version=f"%(prog)s {__version__}",
)
```

## The Environment Safety Rule

You are STRICTLY FORBIDDEN from using `--break-system-packages`. This
project targets Linux Mint and similar Debian-based systems.

### Mandated Tools

- **Global CLI tools**: `pipx install <pkg>` (or `pipx install . --force`).
- **Project-local development**: `python3 -m venv .venv`.
- **Ad-hoc execution**: `PYTHONPATH=src python3 -m omove` (minimal
  phase) or `python3 -m omove` once installed.

### Workflow

Prefer `python3 -m omove --version` for version checks. Never use
`pip install --break-system-packages`.

## Task Completion

- Before moving a task from `wip/` to `review/` or `done/`, update the
  task Markdown file itself.
- Mark completed Acceptance Criteria with `[x]`.
- Add a "Coder Notes" section summarizing deviations or tech debt.
