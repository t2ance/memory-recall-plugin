---
name: dream
description: "Run memory consolidation. Use when the user asks to consolidate, organize, or clean up memories, or says /dream. Synthesizes recent session context into durable, well-organized memory files."
user-invocable: true
---

# Dream: Memory Consolidation

An automated **memory_curator** runs every 4h on the Stop hook, performing aggressive MERGE/DELETE consolidation. This manual Dream skill is for immediate consolidation or for consolidation that requires user judgment (e.g., moving entries between project and global memory, tidying CLAUDE.md).

You are performing a dream -- a reflective pass over your memory files and instructions. Synthesize what you've learned recently into durable, well-organized memories so that future sessions can orient quickly.

You manage three sources:

- **Project memory**: Your per-project auto-memory directory (path in your system prompt's auto-memory section). Stores context specific to this project. You can read and write freely.
- **Global memory**: `${CLAUDE_PLUGIN_DATA}/global-memory/`. Stores context that applies across all projects. Create this directory if it doesn't exist. You can read and write freely.
- **Global instructions**: `~/.claude/CLAUDE.md`. The user's persistent directives. You can read freely, but MUST ask the user via AskUserQuestion before making any changes.

Session transcripts are JSONL files in the project directory (large files -- grep narrowly, don't read whole files).

---

## Phase 1 -- Orient

- `ls` both memory directories to see what already exists
- Read both `MEMORY.md` indexes
- Read `~/.claude/CLAUDE.md`
- Skim existing topic files so you improve them rather than creating duplicates

## Phase 2 -- Gather recent signal

Look for new information worth persisting from the current project. Sources in rough priority order:

1. **Daily logs** (`logs/YYYY/MM/YYYY-MM-DD.md`) if present
2. **Existing memories that drifted** -- facts that contradict something you see in the codebase now
3. **Transcript search** -- grep the JSONL transcripts for narrow terms if you need specific context. Don't exhaustively read transcripts.

## Phase 3 -- Consolidate memories

For each thing worth remembering, decide where it belongs based on its **content**, not its type label:

- **Project-bound**: references this project's specific files, architecture, bugs, decisions, or workflows -> write to **project memory**
- **General**: applies regardless of which project the user is in (personal preferences, coding style, communication style, tool preferences, broadly applicable lessons) -> write to **global memory**
- **Ambiguous**: when in doubt, keep it in project memory. It can be promoted to global later.

Use the memory file format (frontmatter with name/description/type) from your system prompt's auto-memory section.

Focus on:
- Merging new signal into existing topic files rather than creating near-duplicates
- Converting relative dates to absolute dates
- Deleting contradicted facts at the source
- If a piece of information already exists in global memory, don't duplicate it in project memory

## Phase 4 -- Tidy CLAUDE.md (requires user approval)

Review `~/.claude/CLAUDE.md` for entries that are memory-like rather than instruction-like:

- **Memory-like** (should move to global memory): learned facts about the user, past incidents, specific tool/library notes, reference pointers. These are context, not directives.
- **Instruction-like** (should stay in CLAUDE.md): rules, prohibitions, required behaviors, output format requirements. These are directives.

If you find entries that should move:
1. Prepare a summary of proposed changes: which lines to move out, and where they would go in global memory
2. Use **AskUserQuestion** to present the proposal and get approval
3. Only after the user approves: create the global memory topic file, then remove the line from CLAUDE.md

Also check the reverse: if global memory contains entries that are actually hard directives ("never do X", "always do Y"), propose moving them to CLAUDE.md via AskUserQuestion.

**Never edit CLAUDE.md without asking first.**

## Phase 5 -- Prune and index

### Delete without hesitation

- Fixed bugs, error resolutions, or debugging sessions -- the fix lives in code/git
- Completed plans or task lists -- the result lives in code
- Implementation details derivable from reading the code (file paths, function names, config formats)
- UI/display formatting decisions -- cosmetic, in the code
- One-time investigation notes -- ephemeral, not reusable
- Stale project status updates superseded by newer ones

### Merge aggressively

- If a topic has 3+ files, consolidate to 1-2 files max
- Synthesize the essential insight, don't concatenate

### Update MEMORY.md

Update `MEMORY.md` in **both** memory directories. Each should stay under 200 lines and ~25KB. Each entry should be one line under ~150 characters: `- [Title](file.md) -- one-line hook`.

- Remove stale or superseded pointers
- Shorten verbose entries -- move detail into topic files
- Resolve contradictions between files

---

Return a brief summary of what you consolidated, updated, or pruned in each location. If nothing changed, say so.
