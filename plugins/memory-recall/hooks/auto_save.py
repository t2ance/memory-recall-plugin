#!/usr/bin/env python3
"""Auto-save hook: analyzes each assistant turn and persists valuable knowledge.

Runs on Stop hook (after each assistant response). Uses Agent SDK (via utils)
to call Haiku for CRUD decisions (ADD/UPDATE/DELETE/NOOP), then writes memory
files in CC native format.
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
    extract_messages,
    hook_main, maybe_go_async,
    load_plugin_config,
    parse_frontmatter,
    read_memory_files,
    write_log, write_status,
)

# ---------------------------------------------------------------------------
# Haiku prompt & schema
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are a silent sidecar agent running inside a Claude Code hook. You are NOT in a conversation with the user -- the user cannot see or respond to your output. Your ONLY job is to analyze conversation turns and return CRUD decisions via the structured output tool. Never ask questions, never explain, never converse. Just analyze and return.

You are a memory curator for a coding AI assistant. Analyze conversation exchanges and decide what knowledge should be persisted as long-term memory.

## Core Principle

Save information ONLY when BOTH conditions are met:
1. FUTURE UTILITY -- it will likely be useful in a future session
2. NON-RECOVERABLE -- a new AI starting fresh cannot re-derive it from the codebase, environment, or git history

Save WHY things are the way they are, not WHAT. The WHAT lives in code/environment; the WHY lives only in conversation.

## Decision Steps

For each candidate:
1. "Will a fresh Claude benefit from this in a future session?" -- NO -> skip
2. "Can it figure this out from code, commands, or git?" -- YES -> skip
3. "Already covered by an existing memory?" -- YES & unchanged -> skip; needs update -> UPDATE
4. All passed -> ADD

## Memory Types

- user: role, preferences, expertise, communication style
- feedback: guidance on approach (corrections AND validated approaches). Include Why + How to apply.
- project: work context, decisions, constraints. Include Why + How to apply.
- reference: pointers to external resources

## Output

Return ONLY a JSON object (no markdown, no explanation before/after):
{"actions": [<action>, ...]}

Each action is one of:
- {"action": "ADD", "name": "short_name", "description": "one-line", "memory_type": "user|feedback|project|reference", "content": "markdown body", "reason": "why"}
- {"action": "UPDATE", "target_file": "filename.md", "description": "updated desc", "content": "new body", "reason": "why"}
- {"action": "DELETE", "target_file": "filename.md", "reason": "why"}
- {"action": "NOOP", "reason": "brief explanation"}

Output at least one action. Use NOOP if nothing worth saving."""

AUTO_SAVE_SCHEMA = {
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
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                        "memory_type": {"type": "string"},
                        "content": {"type": "string"},
                        "target_file": {"type": "string"},
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
# Prompt building
# ---------------------------------------------------------------------------

def build_prompt(recent_turns, memory_entries):
    parts = []
    if memory_entries:
        lines = ["## Existing Memories"]
        for e in memory_entries:
            lines.append(f"- [{e['file']}] {e['name']}: {e['description']} (type: {e['type']})")
        parts.append("\n".join(lines))
    else:
        parts.append("## Existing Memories\nNone yet.")

    parts.append("## Recent Conversation")
    for turn in recent_turns:
        label = "User" if turn["role"] == "user" else "Assistant"
        parts.append(f"### {label}:\n{turn['text']}")

    parts.append("## Task\nAnalyze the conversation above. Return ONLY a JSON object with actions array.")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# CRUD on memory files
# ---------------------------------------------------------------------------

def execute_actions(actions, memory_dir):
    os.makedirs(memory_dir, exist_ok=True)
    executed = []

    for a in actions:
        act = a.get("action", "NOOP").upper()

        if act == "ADD":
            name = a.get("name", "untitled")
            desc = a.get("description", "")
            mtype = a.get("memory_type", "project")
            content = a.get("content", "")
            reason = a.get("reason", "")
            if not content:
                continue

            fname = _to_filename(name)
            path = os.path.join(memory_dir, fname)
            if os.path.exists(path):
                base = fname[:-3]
                for i in range(2, 20):
                    fname = f"{base}_{i}.md"
                    path = os.path.join(memory_dir, fname)
                    if not os.path.exists(path):
                        break

            with open(path, "w") as f:
                f.write(f"---\nname: {name}\ndescription: {desc}\ntype: {mtype}\n---\n\n{content}\n")
            _update_index(memory_dir, fname, name, desc, "add")
            executed.append({"action": "ADD", "file": fname, "reason": reason})

        elif act == "UPDATE":
            target = a.get("target_file", "")
            path = os.path.join(memory_dir, target)
            if not target or not os.path.isfile(path):
                continue
            fm = parse_frontmatter(path)
            name = fm.get("name", target.replace(".md", ""))
            mtype = fm.get("type", "project")
            desc = a.get("description", fm.get("description", ""))
            content = a.get("content", "")
            reason = a.get("reason", "")
            if not content:
                continue
            with open(path, "w") as f:
                f.write(f"---\nname: {name}\ndescription: {desc}\ntype: {mtype}\n---\n\n{content}\n")
            _update_index(memory_dir, target, name, desc, "update")
            executed.append({"action": "UPDATE", "file": target, "reason": reason})

        elif act == "DELETE":
            target = a.get("target_file", "")
            path = os.path.join(memory_dir, target)
            if not target or not os.path.isfile(path):
                continue
            reason = a.get("reason", "")
            os.remove(path)
            _update_index(memory_dir, target, "", "", "delete")
            executed.append({"action": "DELETE", "file": target, "reason": reason})

    return executed


def _to_filename(name):
    s = "".join(c if c.isalnum() or c in "-_" else "_" for c in name.lower().strip())
    while "__" in s:
        s = s.replace("__", "_")
    s = s.strip("_") or "memory"
    return s[:60].rstrip("_") + ".md"


def _update_index(memory_dir, fname, name, desc, action):
    index_path = os.path.join(memory_dir, "MEMORY.md")
    if action == "delete":
        if os.path.isfile(index_path):
            with open(index_path) as f:
                lines = f.readlines()
            with open(index_path, "w") as f:
                f.writelines(l for l in lines if f"({fname})" not in l)
        return

    new_line = f"- [{name}]({fname}) -- {desc[:100]}\n"
    if action == "update" and os.path.isfile(index_path):
        with open(index_path) as f:
            lines = f.readlines()
        for i, line in enumerate(lines):
            if f"({fname})" in line:
                lines[i] = new_line
                with open(index_path, "w") as f:
                    f.writelines(lines)
                return
    with open(index_path, "a") as f:
        f.write(new_line)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    t_start = time.time()
    hook_input = json.loads(sys.stdin.read())
    event = hook_input.get("hook_event_name", "")

    if event != "Stop":
        write_status("auto_save", "done", hook_input, skipped=True)
        return
    if hook_input.get("stop_hook_active", False):
        write_status("auto_save", "done", hook_input, skipped=True)
        return

    config = load_plugin_config()
    maybe_go_async("memory_save_async", config)
    if not config["auto_save_enabled"]:
        write_status("auto_save", "done", hook_input, skipped=True)
        return

    write_status("auto_save", "running", hook_input, timeout_s=120)

    cwd = hook_input.get("cwd", "")
    last_msg = hook_input.get("last_assistant_message", "")
    if not cwd or not last_msg:
        return

    transcript_path = hook_input.get("transcript_path", "")

    # 1. Extract conversation context
    turns = extract_messages(transcript_path, config["auto_save_context_turns"])
    if not turns:
        turns = [{"role": "assistant", "text": last_msg}]

    # Use last_assistant_message if more complete than transcript
    if turns and turns[-1]["role"] == "assistant" and len(last_msg) > len(turns[-1]["text"]):
        turns[-1]["text"] = last_msg

    # 2. Read existing memory index
    targets = config["auto_save_targets"]
    proj_dir, glob_dir = compute_memory_dirs(cwd)
    target_dirs = []
    if targets in ("native", "both"):
        target_dirs.append(("native", proj_dir))
    if targets in ("global", "both"):
        target_dirs.append(("global", glob_dir))

    memory_entries = []
    for _, mem_dir in target_dirs:
        for entry in read_memory_files(mem_dir):
            memory_entries.append(entry)

    # 3. Call Haiku via Agent SDK
    prompt = build_prompt(turns, memory_entries)
    t_haiku = time.time()
    parsed, usage = asyncio.run(
        call_sdk_haiku(prompt, SYSTEM_PROMPT, AUTO_SAVE_SCHEMA, config["model"],
                       effort=config["auto_save_effort"])
    )
    haiku_s = round(time.time() - t_haiku, 2)

    if not parsed:
        elapsed = round(time.time() - t_start, 2)
        write_log({"event": "auto_save", "status": "no_response",
                    "haiku_s": haiku_s, "elapsed_s": elapsed,
                    "usage": usage})
        print(json.dumps({"systemMessage": f"Memory save: no response | {elapsed}s"}))
        return

    actions = parsed.get("actions", [])
    actionable = [a for a in actions if a.get("action", "").upper() != "NOOP"]

    # 4. Execute CRUD
    executed = []
    if actionable and target_dirs:
        executed = execute_actions(actionable, target_dirs[0][1])

    # 5. Log
    write_log({
        "event": "auto_save",
        "status": "executed" if executed else "noop",
        "existing_memories": len(memory_entries),
        "haiku_actions": [{"action": a.get("action"), "name": a.get("name", a.get("target_file", ""))} for a in actions],
        "executed": executed,
        "usage": usage,
        "haiku_s": haiku_s,
        "elapsed_s": round(time.time() - t_start, 2),
    })

    save_model = config.get("model", "haiku")
    save_cost = usage.get("cost_usd", 0) if usage else 0
    save_elapsed = round(time.time() - t_start, 2)
    save_summary = ", ".join(f"{a['action']} {a['file']}" for a in executed) if executed else "nothing to save"
    write_status("auto_save", "done", hook_input,
                 summary=save_summary, elapsed_s=save_elapsed,
                 cost_usd=save_cost, model=save_model)


if __name__ == "__main__":
    hook_main(main)
