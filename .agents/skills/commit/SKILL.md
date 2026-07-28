---
name: commit
description: Version-guarded commit protocol — secret scan, SemVer triangulation, metadata sync, runtime validation
---

# Commit Protocol

Run this procedure whenever the user says "commit". Act as Manager and read `.crules/modes/GIT_POLICY.md` first.

## Steps

1. **Secret scan**: Run heuristic file-name and content regex checks (from GIT_POLICY) on all staged changes. If secrets are detected, BLOCK the commit and report findings.
2. **Stage swarm files**: Check for modified but unstaged `.crules/` files (modes, tasks) and `project_spec.md`. Stage them automatically and tell the user which files were added.
3. **Version Initialization Guard**: Verify a `__version__` string exists in the package `__init__.py` and a `version` field exists in `pyproject.toml` (or the project's equivalents, e.g. Cargo.toml, go.mod + ldflags). If either is missing, STOP and ask: "No version string found in [file]. Initialize at 0.1.0?" Proceed only after confirmation.
4. **Triangulate**: Find the highest current version across `pyproject.toml`, `__init__.py`, and Git tags. The highest value is the base version.
5. **Detect Change Type**: Inspect `git diff --cached --diff-filter=A --name-only` and `git diff --cached` for newly added files, classes, or functions. If any are present, commit type is `feat` and the bump floor is `minor`, regardless of what was requested.
6. **Determine bump**: Use the user's explicit "minor"/"major" if given; otherwise patch. Follow GIT_POLICY (feat -> minor, fix/chore -> patch). Step 5's floor overrides downward requests.
7. **Apply** the SemVer bump to the highest version found — never a lower one.
8. **Metadata Sync**: Write the new version into both `pyproject.toml` and the package `__init__.py` BEFORE `git add`. Both must contain the identical string.
9. **Stage** the version files and all other relevant files.
10. **Version Validation**: Run `python3 -m <pkg> --version` (or the project's equivalent) and capture the output.
11. **Abort on Mismatch**: If the runtime version does not exactly match the new metadata version, STOP. Do not commit. Report the discrepancy and fix the code first.

## Commit message

Conventional commits: `<type>[scope]: <description>`, imperative mood, focused and atomic. Types: feat, fix, docs, style, refactor, test, chore.
