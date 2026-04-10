# Memory Lifecycle Improvement Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the memory quality problem: memory_save produces too many low-value entries (fixed bugs, UI tweaks, micro-decisions), and nothing cleans them up automatically.

**Architecture:** Two-layer memory lifecycle: (1) improve memory_save's write-time filtering to reduce garbage at source, (2) add memory_curator as periodic consolidation to merge/delete what slips through.

**Tech Stack:** Python, Claude Agent SDK (Haiku), CC plugin hooks system

---

## Problem Statement

Current state: 78 memory files, ~25 are garbage (fixed bugs, completed plans, UI micro-adjustments, duplicates). Root causes:

1. **memory_save (writer) has no global awareness.** It only sees the current turn. It correctly identifies "this is worth remembering" per-turn, but can't know "I already saved 5 statusline-related memories today."

2. **No automated consolidation.** Dream skill exists but is manual (user must remember to run `/dream`). When run, its prompt isn't aggressive enough — user reports running Dream multiple times without the garbage being cleaned.

3. **No feedback loop.** memory_save never learns that its past saves were later deemed garbage. It keeps making the same category of saves.

## Current Architecture (Before)

```
User prompt → recall (inject context)
Agent responds → memory_save (ADD per-turn, no dedup)
                  ↓
              Memory bank grows monotonically
                  ↓
              User manually runs /dream (infrequent, not aggressive enough)
```

## Target Architecture (After)

```
User prompt → recall (inject context)
Agent responds → memory_save (improved filtering, dedup-aware)
                  ↓
              Memory bank grows slowly (fewer garbage entries)
                  ↓
              memory_curator (Stop hook, 4h cooldown, aggressive MERGE/DELETE)
                  ↓
              Memory bank stays lean (~40-50 files max)
```

## Files Overview

| File | Status | Changes |
|------|--------|---------|
| `hooks/memory_save.py` | Existing | Improve system prompt to reject more aggressively |
| `hooks/memory_curator.py` | New (draft exists) | Refine prompt, test, handle edge cases |
| `hooks/utils.py` | Existing | Config keys for curator (already added) |
| `hooks/hooks.json` | Existing | Curator registration (already added) |
| `.claude-plugin/plugin.json` | Existing | Curator config options (already added) |
| `skills/dream/SKILL.md` | Existing | Update to reference curator, align prompt language |

---

## Task 1: Improve memory_save Write-Time Filtering

**Goal:** Reduce garbage at source by making the system prompt reject more categories.

**Files:**
- Modify: `hooks/memory_save.py` (SYSTEM_PROMPT section, lines 33-72)

- [ ] **Step 1: Read current memory_save SYSTEM_PROMPT**

Current prompt says "save if FUTURE UTILITY + NON-RECOVERABLE." This is correct but too permissive. It allows:
- Bug fix records (the fix is in code, but the "lesson learned" passes the filter)
- UI adjustment decisions (technically non-recoverable from code alone)
- Incremental design iterations (each iteration passes individually)

- [ ] **Step 2: Add explicit rejection categories to SYSTEM_PROMPT**

Add to the prompt after the existing "Decision Steps" section:

```
## NEVER Save These (even if they seem useful)

- Bug fixes, error resolutions, or debugging sessions -- the fix lives in code/git
- Completed plans or task lists -- the result lives in code
- Implementation details (file paths, function names, config formats) -- derivable from reading the code
- UI/display formatting decisions -- cosmetic, in the code
- One-time investigation notes -- ephemeral, not reusable
- Incremental iterations of the same design -- only the final decision matters
- Anything that restates what CLAUDE.md already says
```

- [ ] **Step 3: Add dedup awareness to prompt**

Add instruction: "Before proposing ADD, check if an existing memory covers the same topic. If so, propose UPDATE instead of ADD. If the existing memory is already adequate, propose NOOP."

- [ ] **Step 4: Test with a conversation that would previously produce garbage**

Simulate a conversation about fixing a statusline bug. Verify memory_save returns NOOP instead of ADD.

- [ ] **Step 5: Sync and commit**

```bash
rsync -av --delete --exclude='__pycache__' source/ cache/
git add hooks/memory_save.py
git commit -m "feat: memory_save rejects bug fixes, completed plans, UI tweaks"
```

---

## Task 2: Refine memory_curator Prompt

**Goal:** Make curator more aggressive and precise than current Dream skill.

**Files:**
- Modify: `hooks/memory_curator.py` (SYSTEM_PROMPT section)

- [ ] **Step 1: Review current curator SYSTEM_PROMPT**

The draft prompt already has DELETE/MERGE/KEEP criteria. But it needs refinement based on actual memory bank analysis:

Known weakness: The prompt says "aim to reduce by 30-50%" which is a percentage target. Better: give concrete category-based rules.

- [ ] **Step 2: Add concrete examples to the prompt**

Add examples of what to MERGE vs DELETE:

```
## Examples

MERGE these into ONE file:
- "bash_read_ifs_tab_separator_bug" + "statusline_label_truncation_bug" + "statusline_stale_detection_timeout_mismatch"
  -> "statusline_known_issues" (only if the bugs are still relevant; DELETE if all fixed)

DELETE these:
- "memory_recall_plugin_bug_fix_summary_2026_04_10" -- bug fix summary, fixes in code
- "project_naming_refactor_plan" with status COMPLETED -- plan is done, result in code
```

- [ ] **Step 3: Add topic-cluster detection instruction**

Tell the model: "First, group files by topic. If a topic has 3+ files, it MUST be consolidated to 1-2 files max."

- [ ] **Step 4: Handle the MERGE schema complexity**

Current MERGE schema requires `source_files` array + full `content`. This is complex for Haiku. Consider:
- Test with haiku to see if it can reliably produce MERGE actions
- If unreliable, switch to sonnet for curator (cost is acceptable at 4h intervals)
- Or simplify: separate into DELETE pass + ADD pass instead of single MERGE action

- [ ] **Step 5: Sync and commit**

---

## Task 3: Test Curator End-to-End

**Goal:** Run curator on the actual 78-file memory bank and evaluate quality.

**Files:**
- Create: `tests/test_curator_dry_run.py` (or manual test script)

- [ ] **Step 1: Create a dry-run test mode**

Add `--dry-run` flag to memory_curator.py that prints proposed actions without executing them. This allows reviewing decisions before they take effect.

Implementation: add `DRY_RUN = os.environ.get("CURATOR_DRY_RUN", "false") == "true"` and skip `execute_actions()` when set.

- [ ] **Step 2: Run dry-run on current memory bank**

```bash
echo '{"hook_event_name":"Stop","cwd":"/data1/peijia/projects/claude-code-main","session_id":"test"}' | \
  CURATOR_DRY_RUN=true CLAUDE_PLUGIN_OPTION_CURATOR_COOLDOWN_H=0 \
  python3 hooks/memory_curator.py
```

Review the proposed actions in `recall.jsonl`.

- [ ] **Step 3: Evaluate decisions**

Check against our manual analysis:
- Does it DELETE the 25 garbage files we identified?
- Does it MERGE the 12 sidecar research files?
- Does it KEEP the 30 valuable files?
- Does it produce reasonable merged content (not just concatenation)?

- [ ] **Step 4: If quality is poor, iterate on prompt**

Common failure modes:
- Too conservative (KEEPs everything) -> add "aim for 30-50% reduction" pressure
- Too aggressive (DELETEs valuable entries) -> add more KEEP examples
- Bad merge content (concatenation instead of synthesis) -> add "synthesize, don't concatenate" instruction
- Schema errors (Haiku can't produce MERGE actions) -> switch to sonnet or simplify schema

- [ ] **Step 5: Run for real once satisfied**

Remove `--dry-run`, set cooldown to 0, run once. Verify memory bank is cleaner.

---

## Task 4: Handle Edge Cases

**Files:**
- Modify: `hooks/memory_curator.py`

- [ ] **Step 1: Large memory banks**

If 100+ files, the full-content prompt may exceed Haiku's context window. Solutions:
- Send only name+description (title_desc mode) for initial categorization
- Then send full content only for files flagged for MERGE
- Or: process in batches of 30 files

- [ ] **Step 2: Concurrent execution safety**

memory_save and memory_curator both run on Stop hook. They could execute simultaneously (both async). If curator deletes a file that memory_save is about to update:
- memory_save writes to a file that curator just deleted -> file recreated (acceptable)
- curator merges files that memory_save just added -> merged file missing new content (data loss)

Mitigation: curator should run AFTER memory_save completes. Options:
- Add a short delay at curator start (e.g., `time.sleep(5)`)
- Or: check if memory_save state file shows "running" and wait

- [ ] **Step 3: MEMORY.md index consistency**

After MERGE/DELETE, the MEMORY.md index may have stale entries or missing entries. The curator should rebuild the index from the actual files on disk after execution, not incrementally update.

- [ ] **Step 4: Commit**

---

## Task 5: Update Dream Skill

**Goal:** Align manual Dream with automated curator.

**Files:**
- Modify: `skills/dream/SKILL.md`

- [ ] **Step 1: Add note about curator**

Dream skill should mention: "An automated curator runs every 4h on the Stop hook. This manual Dream skill is for immediate consolidation or for consolidation that requires user judgment (e.g., moving entries between project and global memory)."

- [ ] **Step 2: Align deletion criteria**

Copy the DELETE criteria from curator's SYSTEM_PROMPT into Dream skill, so manual and automated have the same standards.

- [ ] **Step 3: Commit**

---

## Open Questions

1. **Should curator also handle global memory?** Currently it only processes project memory. Global memory at `${CLAUDE_PLUGIN_DATA}/global-memory/` may also accumulate garbage.

2. **Should memory_save see existing memories?** Currently memory_save doesn't read the memory bank (only memory_curator does). If memory_save read existing entries before ADD, it could deduplicate at write time. But this adds ~0.5s per turn.

3. **Model choice for curator.** Haiku is cheap but may struggle with complex MERGE schema (7 fields, nested arrays). Sonnet is ~10x more expensive but more reliable. At 4h intervals, even sonnet costs <$1/day.

4. **Should curator produce a user-visible report?** Currently it just logs to recall.jsonl and statusline. Should it also produce a summary that the user can review (e.g., "Curator ran: deleted 15 files, merged 8 into 3")?
