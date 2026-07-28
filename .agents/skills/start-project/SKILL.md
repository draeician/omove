---
name: start-project
description: Scaffold a new project to standard defaults and converge it — one command from idea to working skeleton
---

# Start Project

Use when the user wants to begin a new project or asks "where do I start". Produce a working skeleton, not a plan document. Give the user one explicit "start here" command at the end.

## Procedure

1. **Ask the minimum**: project name, one-sentence purpose, primary language (default Python). Do not interview beyond what scaffolding needs.
2. **Scaffold** (Python default; adapt structure per the language rules in AGENTS.md for rust/go/etc.):
   - `pyproject.toml` only (no setup.py/setup.cfg/requirements.txt): hatchling, `[project.scripts]` entry, version 0.1.0.
   - src layout: `src/<pkg>/__init__.py` (with `__version__ = "0.1.0"`), `__main__.py`, `cli.py` (click, `--version` from package metadata).
   - `tests/` with one real smoke test that runs via `pytest`.
   - `README.md` (purpose, install via `pipx install .`, usage), `CHANGELOG.md` (0.1.0 entry), `LICENSE`, `.gitignore`.
3. **Stack defaults**: SQLite for authoritative state (derived state rebuildable); standard library first; logging module, type hints, Google docstrings.
4. **Initialize**: `git init`, initial commit (`feat: initial project scaffold`).
5. **Converge**: run `crules <language>` so the swarm skeleton, AGENTS.md, shims, and skills deploy immediately.
6. **Verify**: `python3 -m venv .venv && .venv/bin/pip install -e . && .venv/bin/pytest` must pass before declaring done.
7. **Hand off**: tell the user the single next command to run, and note that the Bootstrapper interview will customize the swarm on first agent session.
