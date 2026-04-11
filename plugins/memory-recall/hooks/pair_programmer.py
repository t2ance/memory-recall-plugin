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
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backends import recall_agentic
from discover import discover_memory
from utils import (
    DATA_DIR,
    call_sdk_haiku,
    compute_profile_dir,
    extract_context,
    hook_main,
    load_plugin_config,
    parse_frontmatter,
    read_memory_files,
    write_log, write_status,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

STATE_FILE = os.path.join(DATA_DIR, "pair_programmer_state.json")
PENDING_FEEDBACK_PATH = os.path.join(DATA_DIR, "pp_pending_feedback.json")

SYSTEM_PROMPT = """\
You are a silent sidecar running inside a Claude Code hook. You observe the main agent's actions and provide feedback from the user's perspective. You are NOT in a conversation with the user -- the user cannot see or respond to your output. Your ONLY job is to evaluate and return structured feedback via the output tool. Never ask questions, never explain, never converse.

You are the user's pair programmer -- a stage pacer that monitors the main agent's progress and decides whether it should continue, adjust, or stop for user clarification. You have access to two knowledge sources:

1. User Profile: distilled thinking patterns, values, and judgment heuristics specific to this user. These represent durable traits learned over many sessions. Profile is your primary reference.
2. Memory Bank: individual user preferences, past experience, and project context. These are episodic evidence that supplements the profile.

You evaluate across three dimensions:

(a) Preference Alignment: Does this action match how the user works?
    Check the profile and recalled preferences. Flag if the agent is doing something the user would do differently.

(b) Experience Recall: Has this situation been seen before?
    Check the recalled project memories for past solutions or learnings. Flag if the agent is re-exploring something already solved, or missing a known solution.

(c) Strategic Oversight: Is the high-level direction correct?
    Should the agent step back, try a different approach, or search for help first? Flag architectural concerns, missing steps, or better alternatives.

## When to break the stage

Override all three dimensions and output overall: "break" when you encounter any of these:
- The agent's action is about to act on an assumption that contradicts or extends a profile statement into a context the profile does not clearly cover. Do not silently apply the statement; ask the user.
- The agent appears to be in a loop (repeating the same tool call pattern with no progress visible in recent context).
- The agent is about to make a major decision (design choice, architectural change, non-trivial refactor, dependency addition) where the user's stance cannot be predicted from profile alone.

When emitting break, set stage_break_reason to a strongly-worded directive that:
1. Explicitly instructs main agent to stop all further tool calls
2. Names the specific question to surface to the user
3. Says "do not proceed on assumptions"

Example stage_break_reason:
"STOP CURRENT STAGE. Do not invoke any more tools. Before continuing, you must ask the user: 'You rejected defensive programming in ML code, but this looks like a web input validation path -- should I add validation here or follow the no-defensive-programming rule?' Do not proceed on assumptions."

Rules:
- Only flag genuinely useful observations. If nothing notable, return overall: "ok" with all dimensions null.
- Be specific and actionable: "Search web for this error before patching" not "Be careful."
- 1-2 sentences per dimension max.
- For suggest: you suggest, the agent decides. Never force or block.
- For break: the directive must be unambiguous and strongly worded.
- Always provide a tldr: one actionable sentence under 80 chars for the user dashboard. If nothing notable, use "ok". If breaking, prefix tldr with "BREAK:"."""

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
                "enum": ["ok", "suggest", "break"],
            },
            "tldr": {
                "type": "string",
                "description": "One-sentence actionable summary for the user dashboard. Under 80 chars. Prefix with 'BREAK:' when overall is 'break'.",
            },
            "stage_break_reason": {
                "type": "string",
                "description": "Only set when overall is 'break'. Contains the strongly-worded directive text that will be injected verbatim into the main agent's additionalContext.",
            },
        },
        "required": ["preference", "experience", "strategy", "overall", "tldr"],
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


def check_cooldown(pp):
    """Returns (passed, remaining_seconds). pp is config['pair_programmer']."""
    cooldown = pp['cooldown_s']
    if cooldown <= 0:
        return True, 0
    state = read_state()
    last_eval = state.get("last_eval_ts", 0)
    elapsed = time.time() - last_eval
    if elapsed >= cooldown:
        return True, 0
    return False, int(cooldown - elapsed)


# ---------------------------------------------------------------------------
# Gate logic
# ---------------------------------------------------------------------------

def should_evaluate(hook_input, pp):
    """Returns (should_eval, skip_reason_or_None). pp is config['pair_programmer'].

    skip_reason=None  -> silent skip (disabled/sub-agent): no status line.
    skip_reason=str   -> visible skip (cooldown/sampling): show reason in status.
    """
    if not pp['enabled']:
        return False, None

    # Sub-agent tool calls are irrelevant to the user
    if hook_input.get("agent_id"):
        return False, None

    # Sampling
    sample_rate = pp['sample_rate']
    if sample_rate < 1.0 and random.random() > sample_rate:
        return False, "sampled out"

    # Cooldown — store absolute end timestamp for dynamic countdown in statusline
    passed, remaining = check_cooldown(pp)
    if not passed:
        cooldown_until = int(time.time()) + remaining
        return False, f"cooldown:{cooldown_until}"

    return True, None


# ---------------------------------------------------------------------------
# Trajectory building
# ---------------------------------------------------------------------------

def build_trajectory(hook_input, pp):
    """pp is config['pair_programmer']."""
    parts = []

    # Recent conversation context from transcript
    transcript_path = hook_input.get("transcript_path", "")
    context = extract_context(
        transcript_path,
        pp['context_messages'],
        pp['context_max_chars'],
    )
    if context:
        parts.append(f"## Recent Conversation\n{context}")

    # Current tool call (only present in PostToolUse-style events; empty in Stop)
    tool_name = hook_input.get("tool_name", "")
    if tool_name:
        raw_input = hook_input.get("tool_input", "")
        raw_output = hook_input.get("tool_response", "")

        # Serialize if not already string
        tool_input_str = raw_input if isinstance(raw_input, str) else json.dumps(raw_input, indent=2, ensure_ascii=False)
        tool_output_str = raw_output if isinstance(raw_output, str) else json.dumps(raw_output, indent=2, ensure_ascii=False)

        # Truncate large values
        max_input = pp['max_tool_input_chars']
        max_output = pp['max_tool_output_chars']
        if len(tool_input_str) > max_input:
            tool_input_str = tool_input_str[:max_input] + "\n...(truncated)"
        if len(tool_output_str) > max_output:
            tool_output_str = tool_output_str[:max_output] + "\n...(truncated)"

        parts.append(
            f"## Current Action\nTool: {tool_name}\nInput:\n{tool_input_str}\nOutput:\n{tool_output_str}"
        )

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Profile reading (PP's private knowledge, not shown to main agent)
# ---------------------------------------------------------------------------

def read_profile():
    """Read all profile files. Returns formatted text for PP's Haiku prompt.
    Profile is PP's private knowledge source -- distilled user thinking patterns.
    This text goes into PP's eval prompt, NOT into the main agent's context."""
    profile_dir = compute_profile_dir()
    if not os.path.isdir(profile_dir):
        return ""
    parts = []
    for fname in sorted(os.listdir(profile_dir)):
        if not fname.endswith(".md") or fname == "MEMORY.md":
            continue
        path = os.path.join(profile_dir, fname)
        with open(path) as f:
            content = f.read()[:3000]
        name = fname[:-3]
        for line in content.split("\n"):
            if line.startswith("name:"):
                name = line.partition(":")[2].strip()
                break
        parts.append(f"### {name}\n{content}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Memory recall for pair programmer
# ---------------------------------------------------------------------------

async def recall_context(trajectory, cwd, pp):
    """Recall memories relevant to the current trajectory. pp is config['pair_programmer']."""
    resources, proj_mem_dir, global_mem_dir = discover_memory(cwd)
    if not resources:
        return "", {}

    result, usage = await recall_agentic(
        "memory", resources, trajectory, "",
        pp['model'],
        input_granularity="title_desc",
        effort="low",
    )

    if not result or result.get("type") != "memory_files":
        return "", usage

    parts = []
    max_files = pp['max_recall_files']
    max_file_chars = pp['max_memory_file_chars']
    for path in result.get("files", [])[:max_files]:
        if not os.path.exists(path):
            continue
        with open(path) as f:
            content = f.read()[:max_file_chars]
        basename = os.path.splitext(os.path.basename(path))[0]
        parts.append(f"### {basename}\n{content}")

    return "\n\n".join(parts) if parts else "", usage


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

async def evaluate(trajectory, memories_text, profile_text, pp):
    """Single merged Haiku call evaluating all 3 dimensions + break decision. pp is config['pair_programmer']."""
    prompt_parts = [trajectory]
    if profile_text:
        prompt_parts.append(f"## User Profile (distilled thinking patterns)\n{profile_text}")
    if memories_text:
        prompt_parts.append(f"## User Preferences & Past Experience (from Memory Bank)\n{memories_text}")
    prompt_parts.append("## Task\nEvaluate the agent's current action across all three dimensions. Also decide if the stage should break for user clarification.")
    prompt = "\n\n".join(prompt_parts)

    parsed, usage = await call_sdk_haiku(
        prompt, SYSTEM_PROMPT, EVAL_SCHEMA,
        model=pp['model'],
        effort=pp['effort'],
    )
    return parsed, usage


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def format_output(parsed):
    """Format evaluation results into feedback body string (no trailer protocol).

    - ok: returns None (no feedback)
    - suggest: dimension feedback body (plain)
    - break: stage_break_reason body (plain)

    Body is wrapped later by render_feedback_for_injection() with self-describing
    header (age, verdict, evaluation time), so no trailer directive is needed
    here -- main agent reads the rendered envelope and naturally understands
    this is async feedback about a past turn.
    """
    if not parsed:
        return None

    overall = parsed.get("overall", "ok")

    if overall == "break":
        reason = parsed.get("stage_break_reason", "")
        return reason if reason else None

    if overall != "suggest":
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
        if obs or sug:
            sections.append(f"[{label}] {obs} -- {sug}")

    if not sections:
        return None

    return "Pair programmer feedback:\n" + "\n".join(sections)


def format_status_summary(parsed):
    """Format a single-line summary for statusline (visible to user)."""
    if not parsed or parsed.get("overall") == "ok":
        return "ok"
    tldr = parsed.get("tldr", "ok")
    if parsed.get("overall") == "break" and not tldr.startswith("BREAK:"):
        return f"BREAK: {tldr}"
    return tldr


# ---------------------------------------------------------------------------
# Pending feedback file IO (atomic, CC-managed lifecycle — no daemon)
# ---------------------------------------------------------------------------

def atomic_write_feedback(data):
    """Write feedback dict to PENDING_FEEDBACK_PATH atomically.

    Called by stop_main after Haiku eval produces a feedback body.
    Uses tempfile + os.rename (POSIX-atomic) so a concurrent read from
    user_prompt_main cannot see a partial file.
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=DATA_DIR, prefix=".pp_pending_", suffix=".tmp")
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, ensure_ascii=False)
        os.rename(tmp_path, PENDING_FEEDBACK_PATH)
    except Exception:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise


def consume_pending_feedback():
    """Atomically consume PENDING_FEEDBACK_PATH. Returns dict or None.

    Called by user_prompt_main. Rename-to-.consumed first so a concurrent
    Stop-hook write cannot re-populate the file between our read and unlink.
    Consume-once semantics: each feedback is injected at most once.
    """
    consumed_path = PENDING_FEEDBACK_PATH + ".consumed"
    try:
        os.rename(PENDING_FEEDBACK_PATH, consumed_path)
    except FileNotFoundError:
        return None
    try:
        with open(consumed_path) as f:
            data = json.load(f)
    finally:
        try:
            os.unlink(consumed_path)
        except FileNotFoundError:
            pass
    return data


def render_feedback_for_injection(feedback):
    """Render pending feedback dict into self-describing injection string.

    Self-describing header tells main agent this is async feedback about
    the previous turn (not about the user's current message), including
    age, verdict, and evaluation duration. No trailer directive needed —
    main agent responds to the content naturally.
    """
    age_s = int(time.time() - feedback.get("evaluated_at_unix", time.time()))
    verdict = feedback.get("verdict", "?")
    elapsed = feedback.get("eval_elapsed_s", 0)
    body = feedback.get("body", "")
    header = (
        "Pair Programmer async feedback (delayed delivery):\n"
        "  Evaluated at end of previous turn\n"
        f"  Evaluation age: ~{age_s}s ago\n"
        f"  Evaluation took: {elapsed}s\n"
        f"  Verdict: {verdict}\n"
        "\n"
        "--- Evaluation body ---\n"
    )
    return header + body


# ---------------------------------------------------------------------------
# Stop hook handler: synchronously evaluate the just-finished turn
# ---------------------------------------------------------------------------

def stop_main(hook_input):
    """Stop hook handler: run Haiku evaluation of the just-finished turn.

    Blocks the Stop hook for ~30-72s (Haiku eval). CC's Stop hook blocks
    CC's own tool loop, NOT the user — the user is reading the main agent's
    last response or typing the next prompt, so the latency is invisible.
    Writes feedback to pending file for user_prompt_main to pick up on the
    user's next message.
    """
    t_start = time.time()
    config = load_plugin_config()
    pp = config['pair_programmer']

    should_eval, skip_reason = should_evaluate(hook_input, pp)
    if not should_eval:
        if skip_reason is not None:
            if skip_reason.startswith("cooldown:"):
                cooldown_until = int(skip_reason.split(":")[1])
                write_status("pair_programmer", "done", hook_input,
                             skipped=True, cooldown_until=cooldown_until)
            else:
                write_status("pair_programmer", "done", hook_input, summary=skip_reason)
        return

    write_status("pair_programmer", "running", hook_input, timeout_s=300)

    cwd = hook_input.get("cwd", "")
    if not cwd:
        return

    trajectory = build_trajectory(hook_input, pp)
    profile_text = read_profile()
    memories_text, recall_usage = asyncio.run(recall_context(trajectory, cwd, pp))
    parsed, eval_usage = asyncio.run(evaluate(trajectory, memories_text, profile_text, pp))
    write_state({"last_eval_ts": time.time()})

    body = format_output(parsed)
    elapsed = round(time.time() - t_start, 2)

    write_log({
        "event": "pair_programmer",
        "tool_name": hook_input.get("tool_name"),
        "verdict": parsed.get("overall") if parsed else "no_response",
        "has_feedback": body is not None,
        "has_profile": bool(profile_text),
        "recall_usage": recall_usage,
        "eval_usage": eval_usage,
        "elapsed_s": elapsed,
    })

    status_summary = format_status_summary(parsed)
    pp_cost = (eval_usage.get("cost_usd", 0) if eval_usage else 0) + \
              (recall_usage.get("cost_usd", 0) if recall_usage else 0)
    write_status("pair_programmer", "done", hook_input,
                 summary=status_summary,
                 elapsed_s=elapsed, cost_usd=pp_cost, model=pp['model'])

    if body:
        atomic_write_feedback({
            "body": body,
            "verdict": parsed.get("overall") if parsed else "no_response",
            "evaluated_at_unix": time.time(),
            "eval_elapsed_s": elapsed,
        })


# ---------------------------------------------------------------------------
# UserPromptSubmit hook handler: inject pending feedback via additionalContext
# ---------------------------------------------------------------------------

def user_prompt_main(hook_input):
    """UserPromptSubmit hook handler: consume pending feedback, inject via additionalContext.

    Fast path, typically <50ms: reads pending file (if any), renders the
    self-describing injection envelope, prints hookSpecificOutput JSON.
    CC routes the return through processUserInput.ts:227-240, producing a
    hook_additional_context attachment (messages.ts:4117-4128) -- the
    correct meta-context path (not queued_command pseudo-user-prompt).
    """
    pending = consume_pending_feedback()
    if not pending:
        return
    rendered = render_feedback_for_injection(pending)
    output = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": rendered,
        }
    }
    print(json.dumps(output))


# ---------------------------------------------------------------------------
# Main entrypoint: dispatch by hook event name
# ---------------------------------------------------------------------------

def main():
    hook_input = json.loads(sys.stdin.read())
    event = hook_input.get("hook_event_name")
    if event == "Stop":
        stop_main(hook_input)
    elif event == "UserPromptSubmit":
        user_prompt_main(hook_input)
    # Other events: silently ignore (should not be registered in hooks.json)


if __name__ == "__main__":
    hook_main(main)
