#!/usr/bin/env python3
"""Memory curator hook: automated Dream -- periodic consolidation of memory files.

Runs on Stop hook with long cooldown (default 4h). Implements Dream's 5 phases
as a 3-call LLM pipeline:

  Phase 1 (Python): Orient -- collect memory files, CLAUDE.md, transcript, git log
  Phase 2 (LLM Call 1): Analyze -- classify each file as DELETE/MERGE/KEEP
  Phase 3 (LLM Call 2): Synthesize -- produce merged content for MERGE groups
  Phase 4 (LLM Call 3): Verify -- double-check DELETE decisions
  Phase 5 (Python): Execute -- apply actions, rebuild MEMORY.md index
"""

import asyncio
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from utils import (
    DATA_DIR, STATUS_DIR, HOME,
    call_sdk_haiku,
    compute_memory_dirs,
    compute_profile_dir,
    extract_context,
    hook_main, maybe_go_async,
    load_plugin_config,
    parse_frontmatter,
    read_memory_files,
    to_filename,
    write_log, write_status,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

STATE_FILE = os.path.join(DATA_DIR, "curator_state.json")
DRY_RUN = os.environ.get("CURATOR_DRY_RUN", "false") == "true"
MAX_WAIT_FOR_SAVE_S = 30
WAIT_POLL_INTERVAL_S = 2
MIN_FILES_FOR_CURATION = 10

# ---------------------------------------------------------------------------
# Phase 2: Analysis (LLM Call 1)
# ---------------------------------------------------------------------------

ANALYSIS_PROMPT = """\
You are a memory curator performing a reflective pass over a coding AI assistant's memory bank.
Your goal is a lean, high-signal memory bank that a fresh AI agent can read in under 2 minutes.

## Context

### User's Global Instructions (CLAUDE.md)
{claude_md}

### Recent Session Activity
{transcript}

### Recent Git Activity
{git_log}

## Memory Bank ({n_files} files)

{file_listing}

## Task

Step 1: Group files by topic. If a topic has 3+ files, it MUST be consolidated to 1-2 files max.

Step 2: Classify each file into one action:

DELETE (remove without hesitation):
- Fixed bugs, error resolutions, debugging sessions -- the fix lives in code/git
- Completed plans or task lists -- the work is done, result is in code
- Implementation details derivable from reading the code
- One-time investigation notes -- ephemeral, not reusable
- UI micro-decisions (formatting tweaks, display adjustments)
- Stale project status updates superseded by newer ones
- Bug fix summaries -- the fixes are in git history
- Anything that restates what CLAUDE.md already says

MERGE (combine into merge_group):
- Multiple files about the SAME SPECIFIC TOPIC -> merge keeping essential insight
- Fragmented research notes from iterative exploration -> consolidate
- Multiple feedback entries expressing the SAME principle -> merge
- Only group files that are genuinely about the same narrow topic.
  "code quality" is NOT one topic -- "no magic numbers" and "consistent naming" are separate topics.

KEEP:
- User preferences and working style
- Non-obvious reference knowledge not derivable from code
- Active project context a fresh agent genuinely needs
- Principles and patterns that apply across future sessions
- UNRESOLVED questions or open issues still being investigated
- Files referenced by or complementary to CLAUDE.md

Return a JSON object classifying EVERY file."""

ANALYSIS_SCHEMA = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "decisions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "file": {"type": "string"},
                        "action": {"type": "string"},
                        "merge_group": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["file", "action", "reason"],
                },
            },
        },
        "required": ["decisions"],
    },
}

# ---------------------------------------------------------------------------
# Phase 3: Synthesis (LLM Call 2)
# ---------------------------------------------------------------------------

SYNTHESIS_PROMPT = """\
You are synthesizing multiple related memory files into consolidated files.

For each merge group below, produce ONE file that:
- Captures the essential insight from all source files
- Is structured for quick scanning (headers, bullet points)
- Records WHY decisions were made, not just WHAT was implemented
- Removes outdated or contradicted information
- Is a coherent document, NOT a concatenation of sources

{merge_groups_content}

Return a JSON object with the synthesized files."""

SYNTHESIS_SCHEMA = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "merged_files": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "merge_group": {"type": "string"},
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                        "memory_type": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["merge_group", "name", "description",
                                 "memory_type", "content"],
                },
            },
        },
        "required": ["merged_files"],
    },
}

# ---------------------------------------------------------------------------
# Phase 4: Verification (LLM Call 3)
# ---------------------------------------------------------------------------

VERIFICATION_PROMPT = """\
You are verifying DELETE decisions for a memory bank. Each file below was flagged for deletion.

For EACH file, check ALL of these:
1. Is the information truly recoverable from code, git history, or environment?
2. Is this file referenced or needed by CLAUDE.md (shown below)?
3. Is this an open/unresolved question that future sessions need to know about?
4. Could a fresh AI agent benefit from this in a future session?
5. Was this information "absorbed" into another file? If so, is the unique insight actually preserved?

If ANY check fails, override the decision to KEEP.

## User's CLAUDE.md
{claude_md}

## Files Flagged for Deletion

{delete_candidates_content}

Return a JSON object with your verified decisions."""

VERIFICATION_SCHEMA = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "verified": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "file": {"type": "string"},
                        "action": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["file", "action", "reason"],
                },
            },
        },
        "required": ["verified"],
    },
}

# ---------------------------------------------------------------------------
# Context collection (Phase 1)
# ---------------------------------------------------------------------------


def read_claude_md():
    """Read user's global CLAUDE.md."""
    path = os.path.join(HOME, ".claude", "CLAUDE.md")
    if os.path.isfile(path):
        with open(path) as f:
            return f.read()
    return "(no CLAUDE.md found)"


def get_git_log(cwd, n=20):
    """Get recent git log from cwd."""
    result = subprocess.run(
        ["git", "log", "--oneline", f"-{n}"],
        cwd=cwd, capture_output=True, text=True, timeout=5,
    )
    return result.stdout.strip() or "(no git history)"


def read_file_content(entry):
    """Read full content of a memory file."""
    path = entry["id"]
    if os.path.isfile(path):
        with open(path) as f:
            return f.read()
    return "(file not found)"


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------


def build_analysis_prompt(memory_entries, claude_md, transcript, git_log):
    """Build Phase 2 analysis prompt with title+desc and context."""
    lines = []
    for e in memory_entries:
        lines.append(f"- [{e['file']}] {e['name']}: {e['description']} (type: {e['type']})")
    file_listing = "\n".join(lines)

    return ANALYSIS_PROMPT.format(
        claude_md=claude_md,
        transcript=transcript or "(no transcript available)",
        git_log=git_log,
        n_files=len(memory_entries),
        file_listing=file_listing,
    )


def build_synthesis_prompt(merge_groups, memory_entries):
    """Build Phase 3 synthesis prompt with full content of merge sources."""
    entry_map = {e["file"]: e for e in memory_entries}
    parts = []
    for group_name, files in merge_groups.items():
        parts.append(f"## Merge Group: {group_name}")
        parts.append(f"Source files: {files}")
        for fname in files:
            entry = entry_map.get(fname)
            if entry:
                content = read_file_content(entry)
                parts.append(f"### [{fname}] {entry['name']}\n{content}")
            else:
                parts.append(f"### [{fname}] (not found)")
        parts.append("")
    return SYNTHESIS_PROMPT.format(merge_groups_content="\n".join(parts))


def build_verification_prompt(delete_files, memory_entries, claude_md):
    """Build Phase 4 verification prompt with full content of delete candidates."""
    entry_map = {e["file"]: e for e in memory_entries}
    parts = []
    for fname, reason in delete_files:
        entry = entry_map.get(fname)
        if entry:
            content = read_file_content(entry)
            parts.append(f"### [{fname}] {entry['name']}")
            parts.append(f"Original DELETE reason: {reason}")
            parts.append(f"{content}")
            parts.append("")
        else:
            parts.append(f"### [{fname}] (not found)")
            parts.append(f"Original DELETE reason: {reason}")
            parts.append("")
    return VERIFICATION_PROMPT.format(
        claude_md=claude_md,
        delete_candidates_content="\n".join(parts),
    )


# ---------------------------------------------------------------------------
# Cooldown & synchronization
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
# Execute actions (Phase 5)
# ---------------------------------------------------------------------------


def execute_deletes(verified_deletes, memory_dir):
    """Delete files confirmed by verification."""
    executed = []
    for fname, reason in verified_deletes:
        path = os.path.join(memory_dir, fname)
        if os.path.isfile(path):
            os.remove(path)
            executed.append({"action": "DELETE", "file": fname, "reason": reason})
    return executed


def execute_merges(merge_results, merge_groups, memory_dir):
    """Write merged files and delete sources."""
    executed = []
    for merged in merge_results:
        group = merged.get("merge_group", "")
        source_files = merge_groups.get(group, [])
        name = merged.get("name", "merged")
        desc = merged.get("description", "")
        mtype = merged.get("memory_type", "project")
        content = merged.get("content", "")
        if not content or not source_files:
            continue

        # Delete source files
        for sf in source_files:
            spath = os.path.join(memory_dir, sf)
            if os.path.isfile(spath):
                os.remove(spath)

        # Write merged file
        fname = _to_filename(name)
        path = os.path.join(memory_dir, fname)
        with open(path, "w") as f:
            f.write(f"---\nname: {name}\ndescription: {desc}\ntype: {mtype}\n---\n\n{content}\n")
        executed.append({"action": "MERGE", "sources": source_files,
                         "merged_into": fname, "reason": ""})
    return executed


_to_filename = to_filename  # alias for backward compat within this file


def rebuild_index(memory_dir):
    """Rebuild MEMORY.md from all .md files on disk (except MEMORY.md itself)."""
    entries = read_memory_files(memory_dir)
    index_path = os.path.join(memory_dir, "MEMORY.md")
    with open(index_path, "w") as f:
        for e in sorted(entries, key=lambda x: x["file"]):
            desc = e["description"][:100]
            f.write(f"- [{e['name']}]({e['file']}) -- {desc}\n")


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


async def run_pipeline(memory_entries, proj_dir, hook_input, cu):
    """Run the 3-call curator pipeline. cu is config['curator']."""
    model = cu['model']
    effort = cu['effort']
    cwd = hook_input.get("cwd", "")
    transcript_path = hook_input.get("transcript_path", "")
    total_usage = {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0}

    def merge_usage(u):
        if not u:
            return
        total_usage["input_tokens"] += u.get("input_tokens", 0)
        total_usage["output_tokens"] += u.get("output_tokens", 0)
        total_usage["cost_usd"] += u.get("cost_usd", 0)

    # ---- Phase 1: Collect context ----
    claude_md = read_claude_md()
    transcript = extract_context(transcript_path, 30, 10000) if transcript_path else ""
    git_log = get_git_log(cwd) if cwd else ""

    # ---- Phase 2: Analysis (Call 1) ----
    analysis_prompt = build_analysis_prompt(memory_entries, claude_md, transcript, git_log)
    t1 = time.time()
    analysis, usage1 = await call_sdk_haiku(
        analysis_prompt, "Return structured JSON only.", ANALYSIS_SCHEMA,
        model=model, effort=effort,
    )
    t1_s = round(time.time() - t1, 2)
    merge_usage(usage1)

    if not analysis:
        return None, total_usage, {"phase": "analysis", "t1_s": t1_s}

    decisions = analysis.get("decisions", [])

    # Parse decisions into groups
    delete_candidates = []
    merge_groups = {}  # group_name -> [files]
    keep_files = []

    for d in decisions:
        action = d.get("action", "KEEP").upper()
        fname = d.get("file", "")
        reason = d.get("reason", "")
        if action == "DELETE":
            delete_candidates.append((fname, reason))
        elif action == "MERGE":
            group = d.get("merge_group", "ungrouped")
            merge_groups.setdefault(group, []).append(fname)
        else:
            keep_files.append(fname)

    # ---- Phase 3: Synthesis (Call 2) -- only if there are merge groups ----
    merge_results = []
    t2_s = 0
    if merge_groups:
        synthesis_prompt = build_synthesis_prompt(merge_groups, memory_entries)
        t2 = time.time()
        synthesis, usage2 = await call_sdk_haiku(
            synthesis_prompt, "Return structured JSON only.", SYNTHESIS_SCHEMA,
            model=model, effort=effort,
        )
        t2_s = round(time.time() - t2, 2)
        merge_usage(usage2)
        if synthesis:
            merge_results = synthesis.get("merged_files", [])

    # ---- Phase 4: Verification (Call 3) -- only if there are delete candidates ----
    verified_deletes = []
    verification_overrides = []
    t3_s = 0
    if delete_candidates:
        verification_prompt = build_verification_prompt(
            delete_candidates, memory_entries, claude_md,
        )
        t3 = time.time()
        verification, usage3 = await call_sdk_haiku(
            verification_prompt, "Return structured JSON only.", VERIFICATION_SCHEMA,
            model=model, effort=effort,
        )
        t3_s = round(time.time() - t3, 2)
        merge_usage(usage3)
        if verification:
            for v in verification.get("verified", []):
                vaction = v.get("action", "KEEP").upper()
                vfile = v.get("file", "")
                vreason = v.get("reason", "")
                if vaction == "DELETE":
                    verified_deletes.append((vfile, vreason))
                else:
                    verification_overrides.append((vfile, vreason))

    return {
        "analysis": decisions,
        "delete_candidates": delete_candidates,
        "merge_groups": merge_groups,
        "keep_files": keep_files,
        "merge_results": merge_results,
        "verified_deletes": verified_deletes,
        "verification_overrides": verification_overrides,
        "timings": {"analysis_s": t1_s, "synthesis_s": t2_s, "verification_s": t3_s},
        "claude_md": claude_md,
        "transcript": transcript,
    }, total_usage, None


# ---------------------------------------------------------------------------
# Phase 6: DISTILL -- extract user profile from memory bank + transcript
# ---------------------------------------------------------------------------

DISTILL_PROMPT = """\
You are the PP profile distiller. Your job is to maintain a concise record
of the user's durable thinking patterns, judgment heuristics, and values
boundaries -- stored as natural-language markdown files.

## Inputs

### Existing profile files (what we already know about the user)
{profile_listing}

### Memory bank user + feedback entries (evidence sources)
{evidence_listing}

### User's global CLAUDE.md
{claude_md}

### Recent transcript excerpt (grounding only)
{transcript}

## Your task

For each potential user pattern visible in the inputs, apply three
qualitative verifications. All three must hold for the pattern to qualify
as a profile entry.

**V1 -- Cross-scenario**: Does the pattern appear across distinctly different
contexts, not just one conversation topic? If the only evidence is a single
conversation or a single narrow topic, V1 fails.

**V2 -- Generative**: Can the pattern predict the user's stance on a novel
situation they have not explicitly discussed? If the pattern only restates
what the user already said without generalization, V2 fails.

**V3 -- Distinctive**: Is the pattern specific to this user, or is it common
sense among skilled programmers? If every competent developer would agree
with it by default, V3 fails. Do not store common sense.

If all three hold, decide:
- New pattern, no existing profile file covers it -> ADD
- Existing profile file covers it and is still accurate -> NOOP
- Existing profile file covers it but needs refinement -> UPDATE
- Existing profile file is contradicted by a principled shift in user
  stance (not just an exception) -> DELETE or UPDATE to reflect the shift

Profile file content must use:
- Frontmatter: name (short snake_case id), description (one line), type: profile
- Body: natural language statement of the pattern
- **Why**: grounded evidence in prose. Cite evidence sources naturally.
- **How to apply**: when should PP flag deviations? When should PP escalate
  to break because context is ambiguous?

Do not distill:
- Factual profile (role, tech stack) -- stays in memory bank user type
- Project-specific decisions -- stays in memory bank project type
- Bug fixes, implementation details, one-time investigation notes

Return a JSON object with your actions."""

DISTILL_SCHEMA = {
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


def build_distill_prompt(profile_entries, evidence_entries, claude_md, transcript):
    """Build the DISTILL prompt with all input data."""
    # Profile listing (full content)
    if profile_entries:
        profile_lines = []
        for e in profile_entries:
            content = read_file_content(e)
            profile_lines.append(f"### [{e['file']}] {e['name']}\n{content}")
        profile_listing = "\n\n".join(profile_lines)
    else:
        profile_listing = "(no profile files yet)"

    # Evidence listing (user + feedback entries from memory bank, full content)
    if evidence_entries:
        evidence_lines = []
        for e in evidence_entries:
            content = read_file_content(e)
            evidence_lines.append(f"### [{e['file']}] {e['name']} (type: {e['type']})\n{content}")
        evidence_listing = "\n\n".join(evidence_lines)
    else:
        evidence_listing = "(no user/feedback entries in memory bank)"

    return DISTILL_PROMPT.format(
        profile_listing=profile_listing,
        evidence_listing=evidence_listing,
        claude_md=claude_md or "(no CLAUDE.md found)",
        transcript=transcript or "(no transcript available)",
    )


def execute_distill_actions(actions, profile_dir):
    """Execute DISTILL actions (ADD/UPDATE/DELETE) against the profile dir.
    Reuses the same logic as memory_save's execute pattern."""
    os.makedirs(profile_dir, exist_ok=True)
    executed = []

    for a in actions:
        act = a.get("action", "NOOP").upper()

        if act == "ADD":
            name = a.get("name", "untitled")
            desc = a.get("description", "")
            content = a.get("content", "")
            reason = a.get("reason", "")
            if not content:
                continue
            fname = _to_filename(name)
            path = os.path.join(profile_dir, fname)
            if os.path.exists(path):
                base = fname[:-3]
                found_slot = False
                for i in range(2, 20):
                    fname = f"{base}_{i}.md"
                    path = os.path.join(profile_dir, fname)
                    if not os.path.exists(path):
                        found_slot = True
                        break
                assert found_slot, f"All filename slots taken for {base}.md"
            with open(path, "w") as f:
                f.write(f"---\nname: {name}\ndescription: {desc}\ntype: profile\n---\n\n{content}\n")
            executed.append({"action": "add", "file": fname, "reason": reason})

        elif act == "UPDATE":
            target = a.get("target_file", "")
            path = os.path.join(profile_dir, target)
            if not target or not os.path.isfile(path):
                continue
            fm = parse_frontmatter(path)
            name = fm.get("name", target.replace(".md", ""))
            desc = a.get("description", fm.get("description", ""))
            content = a.get("content", "")
            reason = a.get("reason", "")
            if not content:
                continue
            with open(path, "w") as f:
                f.write(f"---\nname: {name}\ndescription: {desc}\ntype: profile\n---\n\n{content}\n")
            executed.append({"action": "update", "file": target, "reason": reason})

        elif act == "DELETE":
            target = a.get("target_file", "")
            path = os.path.join(profile_dir, target)
            if not target or not os.path.isfile(path):
                continue
            reason = a.get("reason", "")
            os.remove(path)
            executed.append({"action": "delete", "file": target, "reason": reason})

    return executed


async def run_distill(memory_entries, claude_md, transcript, hook_input, cu):
    """Run the DISTILL phase: read evidence, call LLM, write profile files."""
    profile_dir = compute_profile_dir()
    profile_entries = read_memory_files(profile_dir)

    # Filter memory bank to user + feedback types only (evidence for distillation)
    evidence_entries = [e for e in memory_entries if e.get("type") in ("user", "feedback")]

    if not evidence_entries and not profile_entries:
        return None, {}, "no evidence or profile to distill from"

    prompt = build_distill_prompt(profile_entries, evidence_entries, claude_md, transcript)
    t_start = time.time()
    parsed, usage = await call_sdk_haiku(
        prompt, "Return structured JSON only.", DISTILL_SCHEMA,
        model=cu['model'], effort=cu['effort'],
    )
    distill_s = round(time.time() - t_start, 2)

    if not parsed:
        return None, usage, f"no response (distill_s={distill_s})"

    actions = parsed.get("actions", [])
    actionable = [a for a in actions if a.get("action", "").upper() != "NOOP"]

    executed = []
    if actionable and not DRY_RUN:
        executed = execute_distill_actions(actionable, profile_dir)
        if executed:
            rebuild_index(profile_dir)

    return {
        "actions": actions,
        "executed": executed,
        "distill_s": distill_s,
        "profile_before": len(profile_entries),
        "evidence_count": len(evidence_entries),
        "dry_run": DRY_RUN,
    }, usage, None


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
    cu = config['curator']
    maybe_go_async(cu['async'])

    if not cu['enabled']:
        return

    cooldown_h = cu['cooldown_h']
    if not check_cooldown(cooldown_h):
        return

    cwd = hook_input.get("cwd", "")
    if not cwd:
        return

    write_status("curator", "running", hook_input, timeout_s=600)

    # Wait for memory_save to finish first (both run on Stop hook)
    session_id = hook_input.get("session_id", "")
    if session_id:
        wait_for_memory_save(session_id)

    # Read all memory files
    proj_dir, glob_dir = compute_memory_dirs(cwd)
    memory_entries = read_memory_files(proj_dir)

    if len(memory_entries) < MIN_FILES_FOR_CURATION:
        write_status("curator", "done", hook_input,
                     summary=f"{len(memory_entries)} files, skip (< {MIN_FILES_FOR_CURATION})")
        update_cooldown()
        return

    # Run 3-call pipeline
    result, usage, error = asyncio.run(
        run_pipeline(memory_entries, proj_dir, hook_input, cu)
    )

    if error:
        elapsed = round(time.time() - t_start, 2)
        write_log({"event": "curator", "status": "error", "error": error,
                    "elapsed_s": elapsed, "usage": usage})
        write_status("curator", "done", hook_input,
                     summary=f"error in {error.get('phase', '?')}")
        update_cooldown()
        return

    merge_groups = result["merge_groups"]
    merge_results = result["merge_results"]
    verified_deletes = result["verified_deletes"]
    overrides = result["verification_overrides"]
    timings = result["timings"]

    # Dry-run: log proposed actions without executing
    if DRY_RUN:
        elapsed = round(time.time() - t_start, 2)
        write_log({
            "event": "curator", "status": "dry_run",
            "files_before": len(memory_entries),
            "proposed_deletes": len(result["delete_candidates"]),
            "verified_deletes": len(verified_deletes),
            "verification_overrides": [(f, r) for f, r in overrides],
            "merge_groups": {k: v for k, v in merge_groups.items()},
            "merge_results_count": len(merge_results),
            "keeps": len(result["keep_files"]),
            "timings": timings,
            "usage": usage,
            "elapsed_s": elapsed,
        })
        n_del = len(verified_deletes)
        n_merge = len(merge_results)
        n_override = len(overrides)
        summary = f"dry run: {n_del} delete, {n_merge} merge, {n_override} overrides"
        write_status("curator", "done", hook_input, summary=summary,
                     elapsed_s=elapsed, cost_usd=usage.get("cost_usd", 0),
                     model=cu['model'])
        return

    # Execute
    del_executed = execute_deletes(verified_deletes, proj_dir)
    merge_executed = execute_merges(merge_results, merge_groups, proj_dir)
    all_executed = del_executed + merge_executed

    if all_executed:
        rebuild_index(proj_dir)

    update_cooldown()

    elapsed = round(time.time() - t_start, 2)
    write_log({
        "event": "curator", "status": "executed",
        "files_before": len(memory_entries),
        "deletes": len(del_executed),
        "merges": len(merge_executed),
        "keeps": len(result["keep_files"]),
        "verification_overrides": [(f, r) for f, r in overrides],
        "executed": all_executed,
        "timings": timings,
        "usage": usage,
        "elapsed_s": elapsed,
    })

    # ---- Phase 6: DISTILL (profile extraction) ----
    # Intentionally uses pre-CLEAN memory_entries: CLEAN may delete stale entries
    # but their content is still valid evidence for distillation. The profile's
    # Why section inlines evidence as prose, so it survives source deletion.
    distill_result = None
    distill_usage = {}
    distill_error = None
    distiller_enabled = config['distiller']['enabled']

    if distiller_enabled:
        distill_result, distill_usage, distill_error = asyncio.run(
            run_distill(memory_entries, result["claude_md"], result["transcript"],
                        hook_input, cu)
        )

        if distill_error:
            write_log({"event": "distill", "status": "error", "error": distill_error,
                        "usage": distill_usage})
        elif distill_result:
            write_log({
                "event": "distill",
                "status": "dry_run" if DRY_RUN else "executed",
                "profile_before": distill_result["profile_before"],
                "evidence_count": distill_result["evidence_count"],
                "actions": [{"action": a.get("action"), "name": a.get("name", a.get("target_file", ""))}
                            for a in distill_result["actions"]],
                "executed": distill_result["executed"],
                "distill_s": distill_result["distill_s"],
                "usage": distill_usage,
            })

    # Final summary
    total_cost = usage.get("cost_usd", 0) + distill_usage.get("cost_usd", 0)
    elapsed = round(time.time() - t_start, 2)

    distill_suffix = ""
    if distiller_enabled and distill_result and not DRY_RUN:
        n_distilled = len(distill_result.get("executed", []))
        if n_distilled:
            distill_suffix = f", {n_distilled} profile"

    summary = (f"{len(memory_entries)} files: "
               f"{len(del_executed)} deleted, {len(merge_executed)} merged"
               + (f", {len(overrides)} saved by verification" if overrides else "")
               + distill_suffix)
    write_status("curator", "done", hook_input,
                 summary=summary, elapsed_s=elapsed,
                 cost_usd=total_cost,
                 model=cu['model'])


if __name__ == "__main__":
    hook_main(main)
