# Swarm SOP

User-facing prompts for operating the agent swarm in this repository.

## Start a feature
> Act as Manager (`.crules/modes/MANAGER.md`). Create a task for: <description>.
> Use an isolated branch/worktree if parallel work would interfere.

## Fix a bug
> Act as Coder (`.crules/modes/CODER.md`). Reproduce, isolate, fix, and verify: <bug>.

## Review work
> Act as Manager. Review tasks in `.crules/tasks/review/` against `project_spec.md`
> and the Git policy. Approve to `done/` or send back to `wip/` with notes.

## Approve and clean up
> Merge the approved branch, delete the worktree/branch, and move the task to `done/`.
