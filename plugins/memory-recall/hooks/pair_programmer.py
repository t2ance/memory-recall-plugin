#!/usr/bin/env python3
"""Pair programmer hook: evaluates agent actions via PostToolUse.

Fires after action tools (Edit, Write, Bash, etc.). Recalls user preferences
and past experience from Memory Bank, evaluates alignment across 3 dimensions,
and injects soft suggestions via additionalContext.
"""

import asyncio
import json
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backends import recall_agentic
from discover import discover_memory
from utils import (
    DATA_DIR,
    call_sdk_haiku,
    extract_context,
    hook_main,
    load_plugin_config,
    parse_frontmatter,
    write_log,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

STATE_FILE = os.path.join(DATA_DIR, "pp_state.json")

SYSTEM_PROMPT = """\
You are a silent sidecar running inside a Claude Code hook. You observe the main agent's actions and provide feedback from the user's perspective. You are NOT in a conversation with the user -- the user cannot see or respond to your output. Your ONLY job is to evaluate and return structured feedback via the output tool. Never ask questions, never explain, never converse.

You are the user's pair programmer -- an experienced Navigator to the agent's Driver. You observe what the agent is doing, recall relevant user preferences and past experience, and provide soft suggestions when you notice something worth flagging.

You evaluate across three dimensions:

(a) Preference Alignment: Does this action match how the user works?
    Check the recalled user preferences below. Flag if the agent is doing something the user would do differently.

(b) Experience Recall: Has this situation been seen before?
    Check the recalled project memories for past solutions or learnings. Flag if the agent is re-exploring something already solved, or missing a known solution.

(c) Strategic Oversight: Is the high-level direction correct?
    Should the agent step back, try a different approach, or search for help first? Flag architectural concerns, missing steps, or better alternatives.

Rules:
- Only flag genuinely useful observations. If nothing notable, return all null.
- Be specific and actionable: "Search web for this error before patching" not "Be careful."
- 1-2 sentences per dimension max.
- You suggest, the agent decides. Never force or block."""

EVAL_SCHEMA = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "preference": {
                "anyOf": [
                    {
                        "type": "object",
                        "properties": {
                            "observation": {"type": "string"},
                            "suggestion": {"type": "string"},
                        },
                        "required": ["observation", "suggestion"],
                    },
                    {"type": "null"},
                ],
            },
            "experience": {
                "anyOf": [
                    {
                        "type": "object",
                        "properties": {
                            "observation": {"type": "string"},
                            "suggestion": {"type": "string"},
                        },
                        "required": ["observation", "suggestion"],
                    },
                    {"type": "null"},
                ],
            },
            "strategy": {
                "anyOf": [
                    {
                        "type": "object",
                        "properties": {
                            "observation": {"type": "string"},
                            "suggestion": {"type": "string"},
                        },
                        "required": ["observation", "suggestion"],
                    },
                    {"type": "null"},
                ],
            },
            "overall": {
                "type": "string",
                "enum": ["ok", "suggest"],
            },
        },
        "required": ["preference", "experience", "strategy", "overall"],
    },
}


# ---------------------------------------------------------------------------
# State management (cooldown)
# ---------------------------------------------------------------------------

def read_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def write_state(state):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def check_cooldown(config):
    cooldown = config.get("pp_cooldown_s", 0)
    if cooldown <= 0:
        return True
    state = read_state()
    last_eval = state.get("last_eval_ts", 0)
    return (time.time() - last_eval) >= cooldown


# ---------------------------------------------------------------------------
# Gate logic
# ---------------------------------------------------------------------------

def should_evaluate(hook_input, config):
    if not config.get("pp_enabled", False):
        return False

    # Skip sub-agent tool calls
    if hook_input.get("agent_id"):
        return False

    # Sampling
    sample_rate = config.get("pp_sample_rate", 1.0)
    if sample_rate < 1.0 and random.random() > sample_rate:
        return False

    # Cooldown
    if not check_cooldown(config):
        return False

    return True


# ---------------------------------------------------------------------------
# Trajectory building
# ---------------------------------------------------------------------------

def build_trajectory(hook_input, config):
    parts = []

    # Recent conversation context from transcript
    transcript_path = hook_input.get("transcript_path", "")
    context = extract_context(
        transcript_path,
        config.get("pp_context_messages", 5),
        config.get("pp_context_max_chars", 3000),
    )
    if context:
        parts.append(f"## Recent Conversation\n{context}")

    # Current tool call
    tool_name = hook_input.get("tool_name", "")
    tool_input = hook_input.get("tool_input", "")
    tool_output = hook_input.get("tool_output", "")

    # Truncate large values
    if len(tool_input) > 2000:
        tool_input = tool_input[:2000] + "\n...(truncated)"
    if len(tool_output) > 1000:
        tool_output = tool_output[:1000] + "\n...(truncated)"

    parts.append(
        f"## Current Action\nTool: {tool_name}\nInput:\n{tool_input}\nOutput:\n{tool_output}"
    )

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Memory recall for pair programmer
# ---------------------------------------------------------------------------

async def recall_context(trajectory, cwd, config):
    """Recall memories relevant to the current trajectory."""
    resources, proj_mem_dir, global_mem_dir = discover_memory(cwd)
    if not resources:
        return ""

    result, usage = await recall_agentic(
        "memory", resources, trajectory, "",
        config.get("pp_model", "haiku"),
        input_granularity="title_desc",
        effort="low",
    )

    if not result or result.get("type") != "memory_files":
        return "", usage

    parts = []
    for path in result.get("files", [])[:5]:
        if not os.path.exists(path):
            continue
        with open(path) as f:
            content = f.read()[:2000]
        basename = os.path.splitext(os.path.basename(path))[0]
        parts.append(f"### {basename}\n{content}")

    return "\n\n".join(parts) if parts else "", usage


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

async def evaluate(trajectory, memories_text, config):
    """Single merged Haiku call evaluating all 3 dimensions."""
    prompt_parts = [trajectory]
    if memories_text:
        prompt_parts.append(f"## User Preferences & Past Experience (from Memory Bank)\n{memories_text}")
    prompt_parts.append("## Task\nEvaluate the agent's current action across all three dimensions.")
    prompt = "\n\n".join(prompt_parts)

    parsed, usage = await call_sdk_haiku(
        prompt, SYSTEM_PROMPT, EVAL_SCHEMA,
        model=config.get("pp_model", "haiku"),
        effort=config.get("pp_effort", ""),
    )
    return parsed, usage


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def format_output(parsed):
    """Format evaluation results into additionalContext string."""
    if not parsed:
        return None

    if parsed.get("overall") == "ok":
        return None

    dim_labels = {
        "preference": "Preference",
        "experience": "Experience",
        "strategy": "Strategy",
    }

    sections = []
    for key, label in dim_labels.items():
        dim = parsed.get(key)
        if dim is None:
            continue
        obs = dim.get("observation", "")
        sug = dim.get("suggestion", "")
        sections.append(f"[{label}] {obs} -- {sug}")

    if not sections:
        return None

    return "Pair programmer feedback:\n" + "\n".join(sections)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    t_start = time.time()
    hook_input = json.loads(sys.stdin.read())

    if hook_input.get("hook_event_name") != "PostToolUse":
        return

    config = load_plugin_config()

    if not should_evaluate(hook_input, config):
        return

    cwd = hook_input.get("cwd", "")
    if not cwd:
        return

    # Build trajectory
    trajectory = build_trajectory(hook_input, config)

    # Recall relevant memories (1 SDK call)
    memories_text, recall_usage = asyncio.run(recall_context(trajectory, cwd, config))

    # Evaluate all dimensions (1 SDK call)
    parsed, eval_usage = asyncio.run(evaluate(trajectory, memories_text, config))

    # Update cooldown state
    write_state({"last_eval_ts": time.time()})

    # Format output
    additional_context = format_output(parsed)

    # Log
    elapsed = round(time.time() - t_start, 2)
    write_log({
        "event": "pair_programmer",
        "tool_name": hook_input.get("tool_name"),
        "verdict": parsed.get("overall") if parsed else "no_response",
        "has_feedback": additional_context is not None,
        "recall_usage": recall_usage,
        "eval_usage": eval_usage,
        "elapsed_s": elapsed,
    })

    # Output JSON
    output = {}
    if additional_context:
        output["hookSpecificOutput"] = {
            "hookEventName": "PostToolUse",
            "additionalContext": additional_context,
        }

    cost = (eval_usage.get("cost_usd", 0) if eval_usage else 0) + \
           (recall_usage.get("cost_usd", 0) if recall_usage else 0)
    verdict = parsed.get("overall", "skip") if parsed else "skip"
    parts = [f"PP: {verdict}", f"{elapsed}s"]
    if cost:
        parts.append(f"${cost:.3f}")
    output["systemMessage"] = " | ".join(parts)

    print(json.dumps(output))


if __name__ == "__main__":
    hook_main(main)
