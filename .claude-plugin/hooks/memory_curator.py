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
    DATA_DIR,
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


def build_prompt(memory_entries, memory_dir):
    """Build prompt with full content of all memory files."""
    parts = [f"## Memory Bank ({len(memory_entries)} files in {memory_dir})\n"]
    for entry in memory_entries:
        path = entry["id"]
        if os.path.isfile(path):
            with open(path) as f:
                content = f.read()
        else:
            content = "(file not found)"
        parts.append(f"### [{entry['file']}] {entry['name']}\n{content}\n")
    parts.append("## Task\nAnalyze ALL files above. Return actions for every file.")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Execute actions
# ---------------------------------------------------------------------------


def execute_actions(actions, memory_dir):
    """Execute MERGE and DELETE actions. Returns list of executed actions."""
    executed = []

    for a in actions:
        act = a.get("action", "KEEP").upper()

        if act == "DELETE":
            target = a.get("target_file", "")
            path = os.path.join(memory_dir, target)
            if not target or not os.path.isfile(path):
                continue
            os.remove(path)
            _remove_from_index(memory_dir, target)
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

            # Delete source files
            for sf in source_files:
                spath = os.path.join(memory_dir, sf)
                if os.path.isfile(spath):
                    os.remove(spath)
                    _remove_from_index(memory_dir, sf)

            # Write merged file
            fname = _to_filename(name)
            path = os.path.join(memory_dir, fname)
            with open(path, "w") as f:
                f.write(f"---\nname: {name}\ndescription: {desc}\ntype: {mtype}\n---\n\n{content}\n")
            _add_to_index(memory_dir, fname, name, desc)
            executed.append({"action": "MERGE", "sources": source_files,
                             "merged_into": fname,
                             "reason": a.get("reason", "")})

    return executed


def _to_filename(name):
    s = "".join(c if c.isalnum() or c in "-_" else "_" for c in name.lower().strip())
    while "__" in s:
        s = s.replace("__", "_")
    s = s.strip("_") or "memory"
    return s[:60].rstrip("_") + ".md"


def _remove_from_index(memory_dir, fname):
    index_path = os.path.join(memory_dir, "MEMORY.md")
    if not os.path.isfile(index_path):
        return
    with open(index_path) as f:
        lines = f.readlines()
    with open(index_path, "w") as f:
        f.writelines(l for l in lines if f"({fname})" not in l)


def _add_to_index(memory_dir, fname, name, desc):
    index_path = os.path.join(memory_dir, "MEMORY.md")
    with open(index_path, "a") as f:
        f.write(f"- [{name}]({fname}) -- {desc[:100]}\n")


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

    # Execute
    executed = execute_actions(actions, proj_dir)

    # Update cooldown
    update_cooldown()

    # Log
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

    # Status
    merge_count = sum(1 for e in executed if e["action"] == "MERGE")
    delete_count = sum(1 for e in executed if e["action"] == "DELETE")
    summary = f"{len(memory_entries)} files: {delete_count} deleted, {merge_count} merged"
    cost = usage.get("cost_usd", 0) if usage else 0
    write_status("curator", "done", hook_input,
                 summary=summary, elapsed_s=elapsed,
                 cost_usd=cost, model=config.get("model", "haiku"))


if __name__ == "__main__":
    hook_main(main)
