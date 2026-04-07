---
name: remember
description: "Quickly save what you learned in this conversation to memory. Use when the user says /remember, or asks to save/remember something from the current conversation."
user-invocable: true
---

# Remember: Quick Memory Save

Save what you learned in this conversation to the appropriate memory directory. No need to scan transcripts or reorganize existing memories -- just write what's new.

Two memory directories:

- **Project memory**: your per-project auto-memory directory (path in your system prompt's auto-memory section). For context specific to this project.
- **Global memory**: `${CLAUDE_PLUGIN_DATA}/global-memory/`. For context that applies across all projects. Create this directory if it doesn't exist.

## What to do

1. Review what happened in this conversation so far
2. Identify anything worth persisting: user preferences, corrections, decisions, facts learned, bugs found, patterns discovered
3. For each item, decide by **content**: project-specific -> project memory, cross-project -> global memory
4. Write or update topic files using the memory file format (frontmatter with name/description/type) from your system prompt's auto-memory section
5. Update MEMORY.md index in the relevant directory

Keep it fast -- don't over-organize, don't scan transcripts, don't prune unrelated files. Just save what's new from this conversation.
