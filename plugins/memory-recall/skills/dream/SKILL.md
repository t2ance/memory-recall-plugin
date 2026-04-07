---
name: dream
description: "Run memory consolidation. Use when the user asks to consolidate, organize, or clean up memories, or says /dream. Synthesizes recent session context into durable, well-organized memory files."
user-invocable: true
---

# Dream: Memory Consolidation

You are performing a dream -- a reflective pass over your memory files. Synthesize what you've learned recently into durable, well-organized memories so that future sessions can orient quickly.

You have two memory directories:

- **Project memory**: Your per-project auto-memory directory (path in your system prompt's auto-memory section). Stores context specific to this project.
- **Global memory**: `${CLAUDE_PLUGIN_DATA}/global-memory/`. Stores context that applies across all projects. Create this directory if it doesn't exist.

Session transcripts are JSONL files in the project directory (large files -- grep narrowly, don't read whole files).

---

## Phase 1 -- Orient

- `ls` both memory directories to see what already exists
- Read both `MEMORY.md` indexes
- Skim existing topic files so you improve them rather than creating duplicates

## Phase 2 -- Gather recent signal

Look for new information worth persisting from the current project. Sources in rough priority order:

1. **Daily logs** (`logs/YYYY/MM/YYYY-MM-DD.md`) if present
2. **Existing memories that drifted** -- facts that contradict something you see in the codebase now
3. **Transcript search** -- grep the JSONL transcripts for narrow terms if you need specific context. Don't exhaustively read transcripts.

## Phase 3 -- Consolidate

For each thing worth remembering, decide where it belongs based on its **content**, not its type label:

- **Project-bound**: references this project's specific files, architecture, bugs, decisions, or workflows → write to **project memory**
- **General**: applies regardless of which project the user is in (personal preferences, coding style, communication style, tool preferences, broadly applicable lessons) → write to **global memory**
- **Ambiguous**: when in doubt, keep it in project memory. It can be promoted to global later.

Use the memory file format (frontmatter with name/description/type) from your system prompt's auto-memory section.

Focus on:
- Merging new signal into existing topic files rather than creating near-duplicates
- Converting relative dates to absolute dates
- Deleting contradicted facts at the source
- If a piece of information already exists in global memory, don't duplicate it in project memory

## Phase 4 -- Prune and index

Update `MEMORY.md` in **both** directories. Each should stay under 200 lines and ~25KB. Each entry should be one line under ~150 characters: `- [Title](file.md) -- one-line hook`.

- Remove stale or superseded pointers
- Shorten verbose entries -- move detail into topic files
- Resolve contradictions between files

---

Return a brief summary of what you consolidated, updated, or pruned in each directory. If nothing changed, say so.
