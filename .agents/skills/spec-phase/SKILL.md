---
name: spec-phase
description: Spec-driven, phase-bounded development — implement exactly one approved phase, verify, stop
---

# Spec-Phase Development

Use for any non-trivial feature or project work. The core rule: **one phase at a time, stop at the boundary.**

## Procedure

1. **Spec first**: Before code, write or update the specification (`project_spec.md` or a dedicated spec file). Each phase must define:
   - Scope: exactly what is in and out of this phase.
   - Success criteria: verifiable checks (tests passing, command output, file state).
   - Explicit non-goals: what must NOT be touched.
2. **Approval gate**: Present the phase plan and wait for user approval before implementing.
3. **Implement one phase**: Minimum code that satisfies the success criteria. Nothing speculative. Track the work as a task file in `.crules/tasks/wip/`.
4. **Verify**: Run the success criteria checks. Loop until they pass deterministically (tests must run via the project's standard runner, no network).
5. **Report and STOP**: Summarize what was done, show verification results, state what the next phase would be — then stop. Do NOT begin the next phase without explicit approval.
6. **Commit atomically**: One phase, one commit (use the `commit` skill).

## Anti-patterns to refuse

- "While I'm here" changes outside phase scope.
- Starting phase N+1 because phase N went well.
- Marking a phase complete with failing or skipped verification.
- Rewriting the spec mid-phase to match what was built instead of what was agreed.
