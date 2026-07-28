---
name: context-resume
description: Generate a Context Resume for handing work off to a new session or a different AI tool
---

# Context Resume

Run this when the user says "/summary", "summary", "handoff", or asks to transfer context to a new chat/tool. Stop all other processing and produce EXACTLY this structure:

1. **Context Mode:** (e.g., Development, Infrastructure, Refactoring, Philosophy, Creative, Research)
2. **The Goal:** A single sentence on the ultimate objective.
3. **Current State:**
   - *IF CODE:* List functional vs. broken components, citing specific variables, functions, filenames, or regex patterns being worked on.
   - *IF CONCEPTUAL:* List agreed-upon premises, infrastructure states, or definitions established so far.
4. **Immediate Next Step:** The exact first command or text the user needs in the new session. One step, not a menu.
5. **Context Injection:** Specific filenames to feed back in (via `cb -a file1 file2`) or exact quotes/summaries to paste.

## Rules

- Be complete but terse; the resume must be pasteable as-is.
- Cite concrete identifiers (function names, file paths, versions), never vague descriptions.
- Consult `.crules/journal.md` (append-only decision record) when building Current State; append a handoff entry to it summarizing where things stand.
- Also update the repo flight recorder when present: write a one-line state to `summary.txt` and the pending action to `instructions.txt`.
- Check token size before recommending large files: `cb -t file`.
