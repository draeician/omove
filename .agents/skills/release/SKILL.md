---
name: release
description: Release protocol — verify version, changelog from commits, tag, push, announce
---

# Release Protocol

Run when the user says "release". Act as Manager.

## Steps

a. **Verify**: Run `python3 -m <pkg> --version` (or project equivalent) and confirm it exactly matches the version in `pyproject.toml`/metadata. Abort on mismatch.
b. **Changelog**: Summarize all `feat` and `fix` commits since the last git tag; ensure CHANGELOG.md has an entry for this version (Keep a Changelog format) with today's date.
c. **Tag**: Create an annotated git tag for the current version (e.g., `v0.13.0`).
d. **Push**: `git push origin main --tags`.
e. **Announce**: Provide a concise summary suitable for a GitHub Release description: highlights first, then notable fixes, then upgrade notes if behavior changed.

## Rules

- Never tag a version whose tests are failing; run the suite first.
- Never re-tag or force-push an existing tag; bump instead.
- If the working tree is dirty, stop and resolve (commit or stash) before tagging.
