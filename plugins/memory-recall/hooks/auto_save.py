#!/usr/bin/env python3
"""Auto-save hook: analyzes each assistant turn and persists valuable knowledge.

Runs on Stop hook (after each assistant response). Uses Haiku to decide
what's worth saving based on first principles:
  - Future utility: will this be useful in a future session?
  - Non-recoverable: can't be re-derived from code/environment/git?

Supports CRUD operations (ADD/UPDATE/DELETE/NOOP) against existing memories,
using mem0-style deduplication.
"""

import asyncio
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

HOME = os.path.expanduser("~")
DATA_DIR = os.environ.get(
    "CLAUDE_PLUGIN_DATA",
    os.path.join(HOME, ".claude/plugins/data/memory-recall-memory-recall"),
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config():
    return {
        "auto_save_enabled": os.environ.get("CLAUDE_PLUGIN_OPTION_AUTO_SAVE_ENABLED", "true") == "true",
        "auto_save_targets": os.environ.get("CLAUDE_PLUGIN_OPTION_AUTO_SAVE_TARGETS", "native"),
        "auto_save_context_turns": int(os.environ.get("CLAUDE_PLUGIN_OPTION_AUTO_SAVE_CONTEXT_TURNS", "3")),
        "model": os.environ.get("CLAUDE_PLUGIN_OPTION_MODEL", "haiku"),
    }


# ---------------------------------------------------------------------------
# Transcript extraction
# ---------------------------------------------------------------------------

def extract_recent_turns(transcript_path, num_turns, last_assistant_message=""):
    """Extract recent user+assistant turns from transcript JSONL.

    Returns list of {"role": "user"|"assistant", "text": str} dicts,
    most recent last. Includes up to num_turns complete exchanges.
    """
    if not transcript_path or not os.path.exists(transcript_path):
        if last_assistant_message:
            return [{"role": "assistant", "text": last_assistant_message}]
        return []

    # Read enough lines from the end to capture num_turns exchanges
    # Each exchange can span many JSONL lines due to tool calls
    max_lines = num_turns * 40  # generous estimate
    result = subprocess.run(
        ["tail", "-n", str(max_lines), transcript_path],
        capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 0, f"tail failed: {result.stderr}"

    messages = []
    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg_type = msg.get("type", "")
        if msg_type not in ("user", "assistant"):
            continue
        content = msg.get("message", {}).get("content", "")
        if isinstance(content, list):
            text_parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text_parts.append(block["text"])
            content = "\n".join(text_parts)
        if not isinstance(content, str) or not content.strip():
            continue
        # Skip system/meta messages
        if msg_type == "user" and msg.get("userType") == "system":
            continue
        messages.append({"role": msg_type, "text": content.strip()})

    # Deduplicate consecutive same-role messages (keep last)
    deduped = []
    for m in messages:
        if deduped and deduped[-1]["role"] == m["role"]:
            deduped[-1] = m
        else:
            deduped.append(m)

    # Take last num_turns * 2 messages (num_turns exchanges)
    recent = deduped[-(num_turns * 2):]

    # If we have last_assistant_message and it's more complete, use it for the last entry
    if last_assistant_message and recent and recent[-1]["role"] == "assistant":
        if len(last_assistant_message) > len(recent[-1]["text"]):
            recent[-1]["text"] = last_assistant_message

    return recent


# ---------------------------------------------------------------------------
# Memory index reading
# ---------------------------------------------------------------------------

def read_memory_index(memory_dir):
    """Read MEMORY.md index and individual file frontmatters from a memory directory.

    Returns list of {"file": filename, "name": str, "description": str, "type": str}.
    """
    if not os.path.isdir(memory_dir):
        return []

    entries = []
    for fname in sorted(os.listdir(memory_dir)):
        if not fname.endswith(".md") or fname == "MEMORY.md":
            continue
        path = os.path.join(memory_dir, fname)
        fm = _parse_frontmatter(path)
        entries.append({
            "file": fname,
            "name": fm.get("name", fname.replace(".md", "")),
            "description": fm.get("description", ""),
            "type": fm.get("type", "project"),
        })
    return entries


def _parse_frontmatter(path):
    """Parse YAML-like frontmatter from a markdown file."""
    with open(path) as f:
        content = f.read(2000)
    if not content.startswith("---"):
        return {}
    end = content.find("---", 3)
    if end == -1:
        return {}
    result = {}
    for line in content[3:end].strip().split("\n"):
        if ":" in line:
            key, _, value = line.partition(":")
            result[key.strip()] = value.strip().strip('"').strip("'")
    return result


# ---------------------------------------------------------------------------
# Haiku auto-save call
# ---------------------------------------------------------------------------

AUTO_SAVE_SYSTEM_PROMPT = """\
You are a memory curator for a coding AI assistant. Analyze conversation exchanges and decide what knowledge should be persisted as long-term memory.

## Core Principle

Save information ONLY when BOTH conditions are met:
1. FUTURE UTILITY -- it will likely be useful in a future session (not just this one)
2. NON-RECOVERABLE -- a new AI starting fresh cannot re-derive it from the codebase, environment, or git history alone

Save WHY things are the way they are, not WHAT they are. The WHAT lives in code and environment; the WHY lives only in conversation and will be lost.

## Decision Steps

For each candidate piece of knowledge:
1. "Will a fresh Claude benefit from knowing this in a future session?" -- NO -> skip
2. "Can it figure this out by reading code, running commands, or checking git?" -- YES -> skip
3. "Is it already covered by an existing memory?" -- YES & unchanged -> skip; YES & needs update -> UPDATE
4. All passed -> ADD

## Memory Types

Classify each memory as one of:
- user: user's role, preferences, expertise, communication style
- feedback: guidance on how to approach work (corrections AND validated approaches). Include Why and How to apply.
- project: ongoing work context, decisions, constraints. Include Why and How to apply.
- reference: pointers to external resources and their purpose

## Examples

SAVE (project): "Retry in api.py uses 30s because upstream has cold start -- removing causes first-request failures after idle"
-- Code shows retry but not WHY 30s. Future Claude might remove "unnecessary" retry.

SKIP: "GPU 0 has 24GB VRAM, 18GB used"
-- Ephemeral, re-queryable tomorrow.

SAVE (feedback): "User prefers bundled PRs over many small ones for refactors"
-- Not in code; affects future behavior.

SKIP: "All tests pass on Python 3.11"
-- Re-runnable; state may change.

SAVE (reference): "Package foo v2.3.1 silently corrupts data when batch_size > 32; pinned to v2.3.0"
-- Very hard to re-discover without this warning."""

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


def build_haiku_prompt(recent_turns, memory_entries):
    """Build the prompt for Haiku's auto-save decision."""
    parts = []

    # Existing memories
    if memory_entries:
        lines = ["## Existing Memories"]
        for entry in memory_entries:
            lines.append(f"- [{entry['file']}] {entry['name']}: {entry['description']} (type: {entry['type']})")
        parts.append("\n".join(lines))
    else:
        parts.append("## Existing Memories\nNone yet.")

    # Recent conversation
    parts.append("\n## Recent Conversation")
    for turn in recent_turns:
        role_label = "User" if turn["role"] == "user" else "Assistant"
        # Truncate very long messages
        text = turn["text"]
        if len(text) > 3000:
            text = text[:3000] + "\n... (truncated)"
        parts.append(f"\n### {role_label}:\n{text}")

    parts.append(
        "\n## Task\n"
        "Analyze the conversation above. For each piece of knowledge worth persisting, output an action.\n"
        "action must be one of: ADD, UPDATE, DELETE, NOOP\n"
        "For ADD: provide name, description, memory_type (user/feedback/project/reference), content (markdown body), reason\n"
        "For UPDATE: provide target_file, description (updated), content (new body), reason\n"
        "For DELETE: provide target_file, reason\n"
        "For NOOP: just {\"action\": \"NOOP\", \"reason\": \"brief explanation\"}\n"
        "Output at least one action (use NOOP if nothing to save)."
    )

    return "\n\n".join(parts)


async def call_haiku(prompt, model="haiku"):
    """Call Haiku via Agent SDK for auto-save decision."""
    from claude_agent_sdk import query as sdk_query
    from claude_agent_sdk import ClaudeAgentOptions
    from claude_agent_sdk.types import ResultMessage

    options = ClaudeAgentOptions(
        system_prompt=AUTO_SAVE_SYSTEM_PROMPT,
        model=model,
        tools=[],
        output_format=AUTO_SAVE_SCHEMA,
        settings='{"disableAllHooks": true}',
        env={"CLAUDECODE": "", "CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK": "1"},
        effort="low",
        max_budget_usd=0.02,
        extra_args={"no-session-persistence": None},
    )

    parsed = None
    usage = {}
    async for msg in sdk_query(prompt=prompt, options=options):
        if isinstance(msg, ResultMessage):
            usage = {
                "input_tokens": msg.usage.get("input_tokens", 0) if msg.usage else 0,
                "output_tokens": msg.usage.get("output_tokens", 0) if msg.usage else 0,
                "cost_usd": msg.total_cost_usd or 0,
                "duration_api_ms": msg.duration_api_ms,
            }
            parsed = msg.structured_output

    return parsed, usage


# ---------------------------------------------------------------------------
# CRUD operations on memory files
# ---------------------------------------------------------------------------

def execute_actions(actions, memory_dir):
    """Execute CRUD actions on memory files. Returns list of executed action summaries."""
    os.makedirs(memory_dir, exist_ok=True)
    executed = []

    for action_data in actions:
        action = action_data.get("action", "NOOP").upper()

        if action == "ADD":
            name = action_data.get("name", "untitled")
            description = action_data.get("description", "")
            memory_type = action_data.get("memory_type", "project")
            content = action_data.get("content", "")
            reason = action_data.get("reason", "")

            # Generate filename from name
            fname = _name_to_filename(name)
            path = os.path.join(memory_dir, fname)

            # Avoid overwriting existing files
            if os.path.exists(path):
                base, ext = os.path.splitext(fname)
                counter = 2
                while os.path.exists(os.path.join(memory_dir, f"{base}_{counter}{ext}")):
                    counter += 1
                fname = f"{base}_{counter}{ext}"
                path = os.path.join(memory_dir, fname)

            # Write memory file
            file_content = (
                f"---\n"
                f"name: {name}\n"
                f"description: {description}\n"
                f"type: {memory_type}\n"
                f"---\n\n"
                f"{content}\n"
            )
            with open(path, "w") as f:
                f.write(file_content)

            # Update MEMORY.md index
            _append_memory_index(memory_dir, fname, name, description)

            executed.append({"action": "ADD", "file": fname, "reason": reason})

        elif action == "UPDATE":
            target_file = action_data.get("target_file", "")
            if not target_file:
                continue
            path = os.path.join(memory_dir, target_file)
            if not os.path.isfile(path):
                continue

            # Read existing frontmatter to preserve name/type
            fm = _parse_frontmatter(path)
            name = fm.get("name", target_file.replace(".md", ""))
            memory_type = fm.get("type", "project")
            description = action_data.get("description", fm.get("description", ""))
            content = action_data.get("content", "")
            reason = action_data.get("reason", "")

            file_content = (
                f"---\n"
                f"name: {name}\n"
                f"description: {description}\n"
                f"type: {memory_type}\n"
                f"---\n\n"
                f"{content}\n"
            )
            with open(path, "w") as f:
                f.write(file_content)

            # Update MEMORY.md index entry
            _update_memory_index(memory_dir, target_file, name, description)

            executed.append({"action": "UPDATE", "file": target_file, "reason": reason})

        elif action == "DELETE":
            target_file = action_data.get("target_file", "")
            if not target_file:
                continue
            path = os.path.join(memory_dir, target_file)
            if not os.path.isfile(path):
                continue
            reason = action_data.get("reason", "")

            os.remove(path)
            _remove_from_memory_index(memory_dir, target_file)

            executed.append({"action": "DELETE", "file": target_file, "reason": reason})

        # NOOP: do nothing

    return executed


def _name_to_filename(name):
    """Convert a memory name to a safe filename."""
    # Lowercase, replace spaces/special chars with underscores
    fname = name.lower().strip()
    fname = "".join(c if c.isalnum() or c in "-_" else "_" for c in fname)
    # Collapse multiple underscores
    while "__" in fname:
        fname = fname.replace("__", "_")
    fname = fname.strip("_")
    if not fname:
        fname = "memory"
    return fname + ".md"


def _append_memory_index(memory_dir, fname, name, description):
    """Append an entry to MEMORY.md index."""
    index_path = os.path.join(memory_dir, "MEMORY.md")
    # Truncate description to keep index concise
    desc_short = description[:100] if description else ""
    line = f"- [{name}]({fname}) -- {desc_short}\n"

    if os.path.isfile(index_path):
        with open(index_path, "a") as f:
            f.write(line)
    else:
        with open(index_path, "w") as f:
            f.write(line)


def _update_memory_index(memory_dir, fname, name, description):
    """Update an existing entry in MEMORY.md index."""
    index_path = os.path.join(memory_dir, "MEMORY.md")
    if not os.path.isfile(index_path):
        _append_memory_index(memory_dir, fname, name, description)
        return

    with open(index_path) as f:
        lines = f.readlines()

    desc_short = description[:100] if description else ""
    new_line = f"- [{name}]({fname}) -- {desc_short}\n"

    updated = False
    for i, line in enumerate(lines):
        if f"({fname})" in line:
            lines[i] = new_line
            updated = True
            break

    if not updated:
        lines.append(new_line)

    with open(index_path, "w") as f:
        f.writelines(lines)


def _remove_from_memory_index(memory_dir, fname):
    """Remove an entry from MEMORY.md index."""
    index_path = os.path.join(memory_dir, "MEMORY.md")
    if not os.path.isfile(index_path):
        return

    with open(index_path) as f:
        lines = f.readlines()

    lines = [l for l in lines if f"({fname})" not in l]

    with open(index_path, "w") as f:
        f.writelines(lines)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def write_log(entry):
    """Append a structured JSON log entry to the recall log file."""
    log_path = os.path.join(DATA_DIR, "recall.jsonl")
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(log_path, "a") as f:
        f.write(json.dumps(entry, indent=2, ensure_ascii=False) + "\n\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def resolve_memory_dirs(cwd, targets):
    """Resolve target memory directories based on config.

    targets: "native" | "global" | "both"
    Returns list of (label, dir_path) tuples.
    """
    from discover import _compute_memory_dirs
    proj_mem_dir, global_mem_dir = _compute_memory_dirs(cwd, DATA_DIR)

    dirs = []
    if targets in ("native", "both"):
        dirs.append(("native", proj_mem_dir))
    if targets in ("global", "both"):
        dirs.append(("global", global_mem_dir))
    return dirs


def main():
    t_start = time.time()

    hook_input = json.loads(sys.stdin.read())

    # Only run on Stop events
    event = hook_input.get("hook_event_name", "")
    if event != "Stop":
        return

    # Recursion guard
    if hook_input.get("stop_hook_active", False):
        return

    config = load_config()
    if not config["auto_save_enabled"]:
        return

    cwd = hook_input.get("cwd", "")
    if not cwd:
        return

    transcript_path = hook_input.get("transcript_path", "")
    last_assistant_message = hook_input.get("last_assistant_message", "")

    # Skip if no assistant message (nothing to analyze)
    if not last_assistant_message:
        return

    # 1. Extract recent conversation turns
    recent_turns = extract_recent_turns(
        transcript_path,
        config["auto_save_context_turns"],
        last_assistant_message,
    )
    if not recent_turns:
        return

    # 2. Resolve target memory directories and read existing indices
    target_dirs = resolve_memory_dirs(cwd, config["auto_save_targets"])
    all_memory_entries = []
    for label, mem_dir in target_dirs:
        entries = read_memory_index(mem_dir)
        for e in entries:
            e["dir_label"] = label
            e["dir_path"] = mem_dir
        all_memory_entries.extend(entries)

    # 3. Build prompt and call Haiku
    prompt = build_haiku_prompt(recent_turns, all_memory_entries)
    t_before_haiku = time.time()
    parsed, usage = asyncio.run(call_haiku(prompt, config["model"]))
    t_after_haiku = time.time()

    if not parsed:
        write_log({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "event": "auto_save",
            "status": "no_response",
            "elapsed_s": round(time.time() - t_start, 2),
            "usage": usage,
        })
        return

    actions = parsed.get("actions", [])

    # Filter out NOOP actions for execution
    actionable = [a for a in actions if a.get("action", "").upper() != "NOOP"]
    noop_reasons = [a.get("reason", "") for a in actions if a.get("action", "").upper() == "NOOP"]

    # 4. Execute CRUD actions
    all_executed = []
    if actionable:
        # Execute against the first target directory (native preferred)
        primary_dir = target_dirs[0][1] if target_dirs else None
        if primary_dir:
            all_executed = execute_actions(actionable, primary_dir)

    # 5. Log
    log_entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "event": "auto_save",
        "status": "executed" if all_executed else "noop",
        "existing_memories": len(all_memory_entries),
        "haiku_actions": [{"action": a.get("action"), "name": a.get("name", a.get("target_file", ""))} for a in actions],
        "executed": all_executed,
        "noop_reasons": noop_reasons,
        "usage": usage,
        "haiku_s": round(t_after_haiku - t_before_haiku, 2),
        "elapsed_s": round(time.time() - t_start, 2),
    }
    write_log(log_entry)

    # Stop hooks don't support additionalContext injection,
    # so we just print empty success JSON
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "Stop"}}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        write_log({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "event": "auto_save_crash",
            "error": traceback.format_exc(),
        })
        raise
