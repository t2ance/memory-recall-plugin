#!/usr/bin/env python3
"""Memory curator hook: periodic consolidation of memory files.

Runs on Stop hook with long cooldown (default 4h). Reads all memory files,
identifies duplicates/stale/over-fragmented entries, and executes
MERGE/DELETE decisions via Haiku.

Complements memory_save (micro, per-turn) with macro-level maintenance.
"""

import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from utils import (
    DATA_DIR, STATUS_DIR,
    call_sdk_haiku,
    compute_memory_dirs,
    hook_main, maybe_go_async,
    load_plugin_config,
    parse_frontmatter,
    read_memory_files,
    write_log, write_status,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

STATE_FILE = os.path.join(DATA_DIR, "curator_state.json")
DEFAULT_COOLDOWN_H = 4
DRY_RUN = os.environ.get("CURATOR_DRY_RUN", "false") == "true"
MAX_WAIT_FOR_SAVE_S = 30
WAIT_POLL_INTERVAL_S = 2

# ---------------------------------------------------------------------------
# Prompt & schema
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are a memory curator. You receive the FULL contents of a memory bank and must decide what to MERGE, DELETE, or KEEP.

Be AGGRESSIVE. The goal is a lean, high-signal memory bank. A fresh AI agent should be able to read all memories in under 2 minutes and understand everything important.

## Step 1: Group by topic

First, mentally group all files by topic. If a topic has 3+ files, it MUST be consolidated to 1-2 files max.

## Step 2: Apply criteria

### DELETE criteria (remove without hesitation)

- Fixed bugs: the fix is in the code, no need to remember the bug
- Completed plans or task lists: the work is done, the result is in the code
- Implementation details derivable from reading the code (file structure, function signatures, config formats)
- One-time debugging sessions or investigation notes
- UI micro-decisions (formatting tweaks, display adjustments)
- Stale project status updates superseded by newer ones
- Bug fix summaries: the fixes are in git history

### MERGE criteria (combine aggressively)

- Multiple files about the SAME TOPIC -> merge into ONE file keeping only the essential insight
- Fragmented research notes from iterative exploration -> consolidate into one summary
- Multiple feedback entries that express the same principle -> merge into one
- Multiple bug/issue records for the same component -> merge into one "known issues" file (only if bugs are still relevant; DELETE if all fixed)

### KEEP criteria

- User preferences and working style (how they think, what they value)
- Non-obvious reference knowledge that cannot be derived from code or docs
- Active project context that a fresh agent genuinely needs
- Principles and patterns that apply across future sessions

## Examples

DELETE these:
- A "bug fix summary" file listing fixed bugs -- fixes are in git
- A "naming refactor plan" marked COMPLETED -- plan is done, result is in code
- A "one-time investigation" of why something crashed -- ephemeral

MERGE these into ONE file:
- 3 files about "statusline" bugs/issues -> one "statusline_known_issues" (or DELETE all if bugs are fixed)
- 4 files about "sidecar research" from iterative exploration -> one "sidecar_architecture_decisions"
- 2 feedback files both saying "no defensive programming" -> one unified feedback entry

## Output rules

- Return a JSON object with an "actions" array
- Every file MUST appear in exactly one action (MERGE source, DELETE, or KEEP)
- MERGE: provide source_files list and merged content that SYNTHESIZES the essential insight (not concatenation)
- DELETE: brief reason why
- KEEP: no reason needed, just acknowledge"""

CURATOR_SCHEMA = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "actions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string"},
                        "target_file": {"type": "string"},
                        "source_files": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                        "memory_type": {"type": "string"},
                        "content": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["action"],
                },
            },
        },
        "required": ["actions"],
    },
}

# ---------------------------------------------------------------------------
# Cooldown
# ---------------------------------------------------------------------------


def wait_for_memory_save(session_id):
    """Wait for memory_save to finish before curating, to avoid data races."""
    status_path = os.path.join(STATUS_DIR, session_id, "memory_save.json")
    waited = 0
    while waited < MAX_WAIT_FOR_SAVE_S:
        if os.path.isfile(status_path):
            with open(status_path) as f:
                status = json.load(f)
            if status.get("state") == "done":
                return
        time.sleep(WAIT_POLL_INTERVAL_S)
        waited += WAIT_POLL_INTERVAL_S


def check_cooldown(cooldown_h):
    if cooldown_h <= 0:
        return True
    if not os.path.exists(STATE_FILE):
        return True
    with open(STATE_FILE) as f:
        state = json.load(f)
    last_run = state.get("last_curator_ts", 0)
    return (time.time() - last_run) >= cooldown_h * 3600


def update_cooldown():
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump({"last_curator_ts": time.time()}, f)


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------


MAX_PROMPT_CHARS = 400_000  # ~100K tokens, safe for Haiku's 200K context


def build_prompt(memory_entries, memory_dir):
    """Build prompt with full content of all memory files.

    Falls back to title+desc only if total content exceeds MAX_PROMPT_CHARS.
    """
    # First pass: collect all content
    file_contents = {}
    total_chars = 0
    for entry in memory_entries:
        path = entry["id"]
        if os.path.isfile(path):
            with open(path) as f:
                content = f.read()
        else:
            content = "(file not found)"
        file_contents[entry["file"]] = content
        total_chars += len(content)

    # If too large, use title+desc mode
    if total_chars > MAX_PROMPT_CHARS:
        parts = [f"## Memory Bank ({len(memory_entries)} files, title+desc mode -- too large for full content)\n"]
        for entry in memory_entries:
            parts.append(f"- [{entry['file']}] {entry['name']}: {entry['description']} (type: {entry['type']})")
        parts.append("\n## Task\nAnalyze ALL files above. Return actions for every file. Use title+description to decide DELETE vs KEEP. For MERGE, group by topic name similarity.")
        return "\n".join(parts)

    parts = [f"## Memory Bank ({len(memory_entries)} files in {memory_dir})\n"]
    for entry in memory_entries:
        parts.append(f"### [{entry['file']}] {entry['name']}\n{file_contents[entry['file']]}\n")
    parts.append("## Task\nAnalyze ALL files above. Return actions for every file.")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Execute actions
# ---------------------------------------------------------------------------


def execute_actions(actions, memory_dir):
    """Execute MERGE and DELETE actions, then rebuild index from disk."""
    executed = []

    for a in actions:
        act = a.get("action", "KEEP").upper()

        if act == "DELETE":
            target = a.get("target_file") or a.get("name", "")
            path = os.path.join(memory_dir, target)
            if not target or not os.path.isfile(path):
                continue
            os.remove(path)
            executed.append({"action": "DELETE", "file": target,
                             "reason": a.get("reason", "")})

        elif act == "MERGE":
            source_files = a.get("source_files", [])
            name = a.get("name", "merged")
            desc = a.get("description", "")
            mtype = a.get("memory_type", "project")
            content = a.get("content", "")
            if not content or not source_files:
                continue

            for sf in source_files:
                spath = os.path.join(memory_dir, sf)
                if os.path.isfile(spath):
                    os.remove(spath)

            fname = _to_filename(name)
            path = os.path.join(memory_dir, fname)
            with open(path, "w") as f:
                f.write(f"---\nname: {name}\ndescription: {desc}\ntype: {mtype}\n---\n\n{content}\n")
            executed.append({"action": "MERGE", "sources": source_files,
                             "merged_into": fname,
                             "reason": a.get("reason", "")})

    # Rebuild MEMORY.md from actual files on disk
    if executed:
        _rebuild_index(memory_dir)

    return executed


def _to_filename(name):
    s = "".join(c if c.isalnum() or c in "-_" else "_" for c in name.lower().strip())
    while "__" in s:
        s = s.replace("__", "_")
    s = s.strip("_") or "memory"
    return s[:60].rstrip("_") + ".md"


def _rebuild_index(memory_dir):
    """Rebuild MEMORY.md from all .md files on disk (except MEMORY.md itself)."""
    entries = read_memory_files(memory_dir)
    index_path = os.path.join(memory_dir, "MEMORY.md")
    with open(index_path, "w") as f:
        for e in sorted(entries, key=lambda x: x["file"]):
            desc = e["description"][:100]
            f.write(f"- [{e['name']}]({e['file']}) -- {desc}\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    t_start = time.time()
    hook_input = json.loads(sys.stdin.read())

    if hook_input.get("hook_event_name") != "Stop":
        return
    if hook_input.get("stop_hook_active", False):
        return

    config = load_plugin_config()
    maybe_go_async("curator_async", config)

    if not config.get("curator_enabled", True):
        return

    cooldown_h = config.get("curator_cooldown_h", DEFAULT_COOLDOWN_H)
    if not check_cooldown(cooldown_h):
        write_status("curator", "done", hook_input, skipped=True)
        return

    cwd = hook_input.get("cwd", "")
    if not cwd:
        return

    write_status("curator", "running", hook_input, timeout_s=120)

    # Wait for memory_save to finish first (both run on Stop hook)
    session_id = hook_input.get("session_id", "")
    if session_id:
        wait_for_memory_save(session_id)

    # Read all memory files
    proj_dir, glob_dir = compute_memory_dirs(cwd)
    memory_entries = read_memory_files(proj_dir)

    if len(memory_entries) < 10:
        # Not enough files to warrant consolidation
        write_status("curator", "done", hook_input,
                     summary=f"{len(memory_entries)} files, skip (< 10)")
        update_cooldown()
        return

    # Build prompt with all memory content
    prompt = build_prompt(memory_entries, proj_dir)

    # Call Haiku
    t_haiku = time.time()
    parsed, usage = asyncio.run(
        call_sdk_haiku(prompt, SYSTEM_PROMPT, CURATOR_SCHEMA,
                       model=config.get("model", "haiku"),
                       effort=config.get("curator_effort", ""))
    )
    haiku_s = round(time.time() - t_haiku, 2)

    if not parsed:
        elapsed = round(time.time() - t_start, 2)
        write_log({"event": "curator", "status": "no_response",
                    "haiku_s": haiku_s, "elapsed_s": elapsed, "usage": usage})
        write_status("curator", "done", hook_input,
                     summary="no response from model")
        update_cooldown()
        return

    actions = parsed.get("actions", [])
    merges = [a for a in actions if a.get("action", "").upper() == "MERGE"]
    deletes = [a for a in actions if a.get("action", "").upper() == "DELETE"]
    keeps = [a for a in actions if a.get("action", "").upper() == "KEEP"]

    # Execute (or dry-run)
    if DRY_RUN:
        executed = []
        elapsed = round(time.time() - t_start, 2)
        write_log({
            "event": "curator",
            "status": "dry_run",
            "files_before": len(memory_entries),
            "proposed_merges": len(merges),
            "proposed_deletes": len(deletes),
            "proposed_keeps": len(keeps),
            "proposed_actions": actions,
            "usage": usage,
            "haiku_s": haiku_s,
            "elapsed_s": elapsed,
        })
        summary = f"DRY RUN: {len(deletes)} delete, {len(merges)} merge, {len(keeps)} keep"
        write_status("curator", "done", hook_input, summary=summary,
                     elapsed_s=elapsed, cost_usd=usage.get("cost_usd", 0) if usage else 0,
                     model=config.get("model", "haiku"))
        return

    executed = execute_actions(actions, proj_dir)
    update_cooldown()

    elapsed = round(time.time() - t_start, 2)
    write_log({
        "event": "curator",
        "status": "executed",
        "files_before": len(memory_entries),
        "merges": len(merges),
        "deletes": len(deletes),
        "keeps": len(keeps),
        "executed": executed,
        "usage": usage,
        "haiku_s": haiku_s,
        "elapsed_s": elapsed,
    })

    merge_count = sum(1 for e in executed if e["action"] == "MERGE")
    delete_count = sum(1 for e in executed if e["action"] == "DELETE")
    summary = f"{len(memory_entries)} files: {delete_count} deleted, {merge_count} merged"
    cost = usage.get("cost_usd", 0) if usage else 0
    write_status("curator", "done", hook_input,
                 summary=summary, elapsed_s=elapsed,
                 cost_usd=cost, model=config.get("model", "haiku"))


if __name__ == "__main__":
    hook_main(main)
