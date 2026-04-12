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
import re
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backends import recall_agentic
from discover import discover_memory
from utils import (
    DATA_DIR,
    STATUS_DIR,
    call_sdk_haiku,
    compute_profile_dir,
    extract_context,
    extract_messages,
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


def _pending_path(session_id):
    """Per-session pending feedback file path.

    Placed under DATA_DIR/pp_pending/ (NOT inside STATUS_DIR). STATUS_DIR is
    reserved for hook status files that statusline.sh glob-scans; dropping a
    non-status JSON there makes statusline render a spurious '?:' row because
    it has no .hook field. The flat {session_id}.json layout preserves
    cross-session isolation that motivated the original per-session dir.
    """
    pending_dir = os.path.join(DATA_DIR, "pp_pending")
    os.makedirs(pending_dir, exist_ok=True)
    return os.path.join(pending_dir, f"{session_id}.json")

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
- Always provide a tldr: one actionable sentence under 80 chars for the user dashboard. If nothing notable, use "ok". If breaking, prefix tldr with "BREAK:".

## HARD OUTPUT CONTRACT (READ THIS BEFORE EMITTING)

Your output goes through a strict JSON schema validator. If you omit any required field, the entire subprocess will crash with "Command failed with exit code 1" and the user gets NO feedback at all. This is worse than any feedback you could have given. Therefore:

**Every output MUST contain all 5 top-level keys: `preference`, `experience`, `strategy`, `overall`, `tldr`.**

For the three dimension keys (`preference`, `experience`, `strategy`):
- If you have an observation AND a suggestion for that dimension, emit `{"observation": "...", "suggestion": "..."}`
- If you have NOTHING to say for that dimension, you MUST still emit the key with value `null` (JSON null, literally `null`). NEVER omit the key.
- You may NOT emit an empty object `{}` for a dimension — it must be either a full object with both `observation` and `suggestion`, or literally `null`.

For `overall`: always one of `"ok"`, `"suggest"`, `"break"`.

For `tldr`: always a non-empty string. If nothing notable, `"ok"`. If `overall` is `"break"`, prefix with `"BREAK: "`. This field is the only thing the user sees in the statusline — never omit it.

### Example of CORRECT output when only preference has feedback:

```json
{
  "preference": {
    "observation": "Agent is using try/except to suppress file-not-found, but user's CLAUDE.md says fail-fast on unexpected errors.",
    "suggestion": "Remove the try/except; let FileNotFoundError propagate so the caller sees the real problem."
  },
  "experience": null,
  "strategy": null,
  "overall": "suggest",
  "tldr": "Remove defensive try/except; fail-fast per user style"
}
```

Note: `experience` and `strategy` are literally `null`, NOT omitted. This is mandatory.

### Example of CORRECT output when nothing is notable:

```json
{
  "preference": null,
  "experience": null,
  "strategy": null,
  "overall": "ok",
  "tldr": "ok"
}
```

All three dimensions are `null`, not missing. `overall` is `"ok"`, `tldr` is `"ok"`.

### Example of INCORRECT output (this WILL crash the subprocess):

```json
{
  "preference": {"observation": "...", "suggestion": "..."},
  "overall": "suggest",
  "tldr": "..."
}
```

This is wrong because `experience` and `strategy` are omitted entirely. The schema validator will reject it. Always emit all 5 keys, use `null` for dimensions with no content."""

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

def _build_source_excerpt(hook_input):
    """Build a verbatim content anchor that identifies the evaluated moment.

    Returns a string composed of (1) the last 2-3 sentences of the most recent
    assistant message with text content, and (2) the triggering tool name and
    the first 150 chars of its input. The main agent uses this fragment as a
    content-addressable anchor: it searches its own conversation history for
    these exact phrases to decide whether the async feedback still applies to
    its current state. This replaces time-based stale detection (which is
    meaningless to a model without a clock) with content-based self-validation.
    """
    tool_name = hook_input.get("tool_name", "")
    tool_input = hook_input.get("tool_input", "")
    tool_input_str = tool_input if isinstance(tool_input, str) else json.dumps(tool_input, ensure_ascii=False)
    tool_head = tool_input_str[:150]

    transcript_path = hook_input.get("transcript_path", "")
    assistant_tail = ""
    if transcript_path:
        msgs = extract_messages(transcript_path, num_turns=3, max_char_per_msg=3000)
        for m in reversed(msgs):
            if m["role"] == "assistant" and m["text"].strip():
                sentences = re.split(r'(?<=[.!?])\s+', m["text"].strip())
                assistant_tail = " ".join(sentences[-3:]).strip()
                break

    if assistant_tail:
        return f"{assistant_tail}\n  Tool: {tool_name}\n  Input: {tool_head}"
    return f"Tool: {tool_name}\n  Input: {tool_head}"


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
    resources, proj_mem_dir, global_mem_dir, _profile_mem_dir = discover_memory(cwd)
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

def atomic_write_feedback(data, session_id):
    """Write feedback dict to the session's pending path atomically.

    Called by _run_eval inside the detached eval child after Haiku produces
    a feedback body. Uses tempfile + os.rename (POSIX-atomic) so a concurrent
    read from post_tool_main or user_prompt_main cannot see a partial file.
    Per-session path prevents cross-session pollution when multiple CC
    sessions run.
    """
    pending_path = _pending_path(session_id)
    pending_dir = os.path.dirname(pending_path)
    fd, tmp_path = tempfile.mkstemp(dir=pending_dir, prefix=".pp_pending_", suffix=".tmp")
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, ensure_ascii=False)
        os.rename(tmp_path, pending_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise


def consume_pending_feedback(session_id):
    """Atomically consume the session's pending feedback. Returns dict or None.

    Called by post_tool_main (primary) and user_prompt_main (fallback).
    Rename-to-.consumed first so a concurrent eval-child write cannot
    re-populate the file between our read and unlink. Consume-once semantics:
    each feedback is injected at most once, regardless of which handler wins.
    """
    pending_path = _pending_path(session_id)
    consumed_path = pending_path + ".consumed"
    try:
        os.rename(pending_path, consumed_path)
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

    Self-describing header tells main agent this is async feedback about a
    previous action (not about the user's current message). A Context Anchor
    block quotes the exact fragment of the agent's earlier work that was
    evaluated; the main agent locates this fragment in its own conversation
    history to self-judge whether the feedback still applies. No timestamp-
    based stale detection — content anchoring replaces it (the agent has
    no clock, but it can search its own context precisely).
    """
    verdict = feedback.get("verdict", "?")
    elapsed = feedback.get("eval_elapsed_s", 0)
    body = feedback.get("body", "") or ""
    source_excerpt = feedback.get("source_excerpt", "") or ""

    if source_excerpt:
        quoted = "\n".join("> " + line for line in source_excerpt.split("\n"))
        anchor_block = (
            "\n--- Context anchor ---\n"
            "This feedback was generated based on the following fragment of your "
            "recent work. Search your own conversation history for these exact "
            "phrases to verify the feedback still applies. If you cannot find the "
            "fragment, or the fragment has since been superseded by more recent "
            "work, the feedback is stale -- downgrade to a note and do not act on it.\n\n"
            f"{quoted}\n"
        )
    else:
        anchor_block = ""

    # verdict=ok (or empty body): compact "no concerns" variant. Still forces
    # the box to appear so the user always sees PP ran every turn.
    if verdict == "ok" or not body:
        header = (
            "Pair Programmer async feedback (delayed delivery):\n"
            f"  Verdict: ok (no concerns)\n"
            f"  Eval took: {elapsed}s\n"
        )
        display_instruction = (
            "\n--- Display Instruction (MANDATORY) ---\n"
            "Before writing anything else in your next response, emit a 4-line box "
            "with the exact structure shown below. Keep the ★ header line, the ─── "
            "footer line, and the 'PP:' / 'Me:' labels verbatim -- do NOT translate "
            "these fixed tokens. The content AFTER each label must be written by you, "
            "replacing the <REPLACE ...> placeholder.\n\n"
            "  ★ Pair Programmer ─────────────────────────────\n"
            "  PP: <REPLACE with a short third-person line stating that PP had no "
            "concerns about the previous action (verdict=ok). Report it to the user, "
            "do not address PP.>\n"
            "  Me: <REPLACE with a short third-person line stating that no correction "
            "is needed and you are proceeding normally.>\n"
            "  ─────────────────────────────────────────────────\n\n"
            "Language rule: write both PP: and Me: content in the SAME natural "
            "language as the current user conversation, not the language of this "
            "instruction. Detect the language from the user's latest message, not "
            "from this prompt. If the user is speaking Chinese, write the box content "
            "in Chinese; if English, write English; etc. The 'PP:' / 'Me:' labels "
            "themselves stay verbatim regardless of language.\n\n"
            "Then continue with your actual response. This box confirms PP ran and "
            "had no concerns about the previous action. Never skip it — it is the only "
            "way the user sees PP in your reply."
        )
        return header + anchor_block + display_instruction

    # verdict=suggest or break: full body + box with real Issue/Response.
    header = (
        "Pair Programmer async feedback (delayed delivery):\n"
        f"  Verdict: {verdict}\n"
        f"  Eval took: {elapsed}s\n"
    )
    body_block = "\n--- Evaluation body ---\n" + body
    display_instruction = (
        "\n\n--- Display Instruction (MANDATORY) ---\n"
        "Before writing anything else in your next response, emit a 4-line box with "
        "the exact structure shown below. Keep the ★ header line, the ─── footer "
        "line, and the 'PP:' / 'Me:' labels verbatim -- do NOT translate these fixed "
        "tokens. The content AFTER each label must be written by you, replacing the "
        "<REPLACE ...> placeholder.\n\n"
        "  ★ Pair Programmer ─────────────────────────────\n"
        "  PP: <REPLACE with your own third-person narration reporting what PP "
        "pointed out about the previous action and what PP suggested the agent do. "
        "Narrate it to the user as a concise news summary. Do NOT quote PP literally, "
        "do NOT address PP in second person, do NOT write a dialogue. This is a "
        "report ABOUT PP, not a reply TO PP.>\n"
        "  Me: <REPLACE with your own third-person explanation of whether PP is "
        "right, what you did or will do in response, and optionally one key fact PP's "
        "snapshot missed that changes the assessment. Also a report to the user, not "
        "a reply addressed to PP.>\n"
        "  ─────────────────────────────────────────────────\n\n"
        "Language rule: write both PP: and Me: content in the SAME natural language "
        "as the current user conversation, not the language of this instruction. "
        "Detect the language from the user's latest message, not from this prompt. "
        "If the user is speaking Chinese, write the box content in Chinese; if "
        "English, write English; etc. The 'PP:' / 'Me:' labels themselves stay "
        "verbatim regardless of language.\n\n"
        "Voice rule: both lines must be third-person REPORT voice (the agent "
        "narrating to the user what PP said and what the agent did), not first-person "
        "DIALOGUE voice. Bad example (first-person, addresses PP directly): "
        "'PP: You tested in the wrong session! Me: No I did not.'. Good example "
        "(third-person report to user): 'PP: PP pointed out the test ran in the "
        "wrong session. Me: PP's premise has been overridden by /reload-plugins.'. "
        "These examples are English only because this instruction file is English; "
        "the Language rule above still governs the RUNTIME output language, so in a "
        "Chinese conversation the agent writes the narration in Chinese. The examples "
        "illustrate VOICE (third-person report vs first-person dialogue), not LANGUAGE.\n\n"
        "Then continue with your actual response. The box makes PP's influence "
        "visible to the user who cannot see this additionalContext themselves. "
        "Skipping the box violates the visibility contract."
    )
    return header + anchor_block + body_block + display_instruction


# ---------------------------------------------------------------------------
# PostToolUse hook handler: consume prior feedback + spawn next eval
# ---------------------------------------------------------------------------

def _spawn_eval_child(hook_input):
    """Spawn a detached Python subprocess that runs _child_main().

    CRITICAL NON-BLOCKING INVARIANT: this helper MUST return in <1s so the
    parent PostToolUse hook does not block CC's tool loop. The 30-72s Haiku
    evaluation runs in the detached child (new session via start_new_session=
    True / os.setsid). The parent hook exits immediately once Popen returns.

    Why sys.executable + start_new_session over os.fork: spawns a fresh
    Python interpreter, avoiding fork-after-asyncio / fork-after-SDK-import
    state bugs, and the new-session detachment ensures CC's tool-loop kill
    semantics do not cascade to the eval child when the parent hook exits.
    """
    import subprocess
    env = os.environ.copy()
    env["PP_HOOK_INPUT_JSON"] = json.dumps(hook_input)
    subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "--eval-child"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env=env,
        close_fds=True,
    )


def post_tool_main(hook_input):
    """PostToolUse hook handler: consume any prior pending feedback AND spawn
    a new eval for the tool call that just finished.

    Two-in-one design:
    1. CONSUME FIRST. Read any pending feedback file left by a previous
       PostToolUse eval child, atomically rename-and-consume, and emit it as
       hookSpecificOutput.additionalContext so CC routes it through the
       hook_additional_context attachment path (src/services/tools/toolHooks.ts
       lines 133-142; same attachment type as UserPromptSubmit).
    2. THEN SPAWN. Check the cooldown/sample gate; if allowed, spawn a new
       detached eval child that will evaluate THIS tool call and write its
       own pending file for the NEXT PostToolUse (or UserPromptSubmit
       fallback) to consume.

    Consume-first is deliberate: the pending file's consume-once semantics
    (atomic rename-to-.consumed in consume_pending_feedback) must be resolved
    before anything else runs. First-ever invocation simply has no pending
    file; consume_pending_feedback returns None and we proceed straight to
    spawn with no injection emitted.
    """
    session_id = hook_input.get("session_id", "unknown")

    # --- Phase 1: consume prior pending feedback, inject via additionalContext
    pending = consume_pending_feedback(session_id)
    if pending:
        rendered = render_feedback_for_injection(pending)
        output = {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": rendered,
            }
        }
        write_log({
            "event": "pair_programmer_inject",
            "source": "post_tool_main",
            "session_id": session_id,
            "pending_found": True,
            "verdict": pending.get("verdict"),
            "body_chars": len(pending.get("body", "") or ""),
            "rendered_chars": len(rendered),
        })
        print(json.dumps(output))
    else:
        write_log({
            "event": "pair_programmer_inject",
            "source": "post_tool_main",
            "session_id": session_id,
            "pending_found": False,
        })

    # --- Phase 2: gate + spawn a new eval for the current tool call
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

    cwd = hook_input.get("cwd", "")
    if not cwd:
        return

    # Record cooldown immediately in the parent so concurrent PostToolUse
    # hooks don't race-start a second eval child. check_cooldown reads this.
    write_state({"last_eval_ts": time.time()})

    # Write "running" status from the parent so the user sees PP is in flight.
    # The detached child will overwrite this with "done" or "failed" when it
    # completes, without blocking the PostToolUse hook in the meantime.
    write_status("pair_programmer", "running", hook_input, timeout_s=pp['timeout_s'])

    _spawn_eval_child(hook_input)
    # Parent returns immediately. PostToolUse hook exits in ~1s.


def _child_main():
    """Entry point for the detached background eval child.

    Invoked via `python3 pair_programmer.py --eval-child` by post_tool_main's
    _spawn_eval_child helper. Reads hook_input from the PP_HOOK_INPUT_JSON env
    var (env was chosen over stdin so the parent can spawn-and-exit without
    any pipe-write synchronization, and so the child's stdin is a clean DEVNULL).

    Any exception propagates up to hook_main which logs to recall.jsonl.
    On crash, also writes a "failed" status so the statusline does not get
    stuck in "running" state forever.
    """
    hook_input_json = os.environ.get("PP_HOOK_INPUT_JSON", "")
    if not hook_input_json:
        return
    hook_input = json.loads(hook_input_json)
    pp = load_plugin_config()['pair_programmer']
    t_start = time.time()
    try:
        _run_eval(hook_input, pp, t_start)
    except Exception:
        # Ensure statusline reflects the crash instead of being stuck at "running".
        # This is hygiene, not error masking: the exception is re-raised to
        # hook_main which logs the full traceback to recall.jsonl.
        elapsed = round(time.time() - t_start, 2)
        write_status("pair_programmer", "failed", hook_input,
                     summary="child crash (see recall.jsonl)",
                     elapsed_s=elapsed)
        raise


def _run_eval(hook_input, pp, t_start):
    """Core Haiku evaluation logic. Runs only inside the detached child.

    Extracted from post_tool_main for two reasons: (1) keeps post_tool_main
    focused on the consume/spawn boundary; (2) makes the eval logic testable
    in isolation.
    """
    trajectory = build_trajectory(hook_input, pp)
    profile_text = read_profile()
    memories_text, recall_usage = asyncio.run(recall_context(trajectory, hook_input.get("cwd", ""), pp))
    parsed, eval_usage = asyncio.run(evaluate(trajectory, memories_text, profile_text, pp))

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

    # ALWAYS write the pending file, regardless of verdict. The user asked
    # for the box to appear on every turn (2026-04-11), not just when PP has
    # a suggest/break to deliver. render_feedback_for_injection handles the
    # verdict=ok case by emitting a compact "no concerns" variant of the box.
    session_id = hook_input.get("session_id", "unknown")
    source_excerpt = _build_source_excerpt(hook_input)
    atomic_write_feedback({
        "body": body,  # may be None/empty for verdict=ok; renderer handles it
        "verdict": parsed.get("overall") if parsed else "no_response",
        "evaluated_at_unix": time.time(),
        "eval_elapsed_s": elapsed,
        "source_excerpt": source_excerpt,
    }, session_id)


# ---------------------------------------------------------------------------
# UserPromptSubmit hook handler: fallback pending-feedback consumer
# ---------------------------------------------------------------------------

def user_prompt_main(hook_input):
    """UserPromptSubmit fallback consumer of pending feedback.

    In v4.1+, PostToolUse (post_tool_main) is the PRIMARY consumer: every
    tool call reads any leftover pending file and injects it via
    hookSpecificOutput.additionalContext. This handler only delivers value
    when a turn ends with NO tool calls (pure-conversation turns) -- in that
    case PostToolUse never fires, and the pending file from the previous
    tool-call turn still sits unconsumed. Otherwise consume_pending_feedback
    returns None and this is a no-op that only writes an audit log entry.

    The consume_pending_feedback atomic-rename guarantees consume-once
    semantics regardless of which handler wins the race; no extra guard
    needed here.
    """
    session_id = hook_input.get("session_id", "unknown")
    pending = consume_pending_feedback(session_id)
    if not pending:
        write_log({
            "event": "pair_programmer_inject",
            "source": "user_prompt_main",
            "session_id": session_id,
            "pending_found": False,
        })
        return
    rendered = render_feedback_for_injection(pending)
    output = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": rendered,
        }
    }
    write_log({
        "event": "pair_programmer_inject",
        "source": "user_prompt_main",
        "session_id": session_id,
        "pending_found": True,
        "verdict": pending.get("verdict"),
        "body_chars": len(pending.get("body", "")),
        "rendered_chars": len(rendered),
    })
    print(json.dumps(output))


# ---------------------------------------------------------------------------
# Main entrypoint: dispatch by hook event name
# ---------------------------------------------------------------------------

def main():
    # Detached eval child: invoked by post_tool_main._spawn_eval_child via
    # subprocess.Popen with --eval-child argv. Reads hook_input from the
    # PP_HOOK_INPUT_JSON env var, not stdin (stdin is DEVNULL in the child).
    if "--eval-child" in sys.argv:
        _child_main()
        return

    # Normal hook invocation: CC pipes the hook JSON via stdin.
    hook_input = json.loads(sys.stdin.read())
    event = hook_input.get("hook_event_name")
    if event == "PostToolUse":
        post_tool_main(hook_input)
    elif event == "UserPromptSubmit":
        user_prompt_main(hook_input)
    # Other events: silently ignore (should not be registered in hooks.json)


if __name__ == "__main__":
    hook_main(main)
