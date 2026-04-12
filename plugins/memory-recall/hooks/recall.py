#!/usr/bin/env python3
"""Multi-dimension recall hook.

Recalls relevant resources across 4 dimensions (memory, skills, tools, agents),
each independently configurable with 3 backends (reminder, agentic, embedding).

Entry point for UserPromptSubmit hook. Dispatches to dimension-specific
discovery and generic backend functions in parallel.
"""

import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backends import (
    ensure_daemon_running,
    recall_agentic,
    recall_agentic_merged,
    recall_embedding_generic,
    recall_embedding_memory,
    recall_reminder,
)
from discover import discover_agents, discover_memory, discover_skills, discover_tools
from utils import (
    DATA_DIR, PLUGIN_ROOT, SOCKET_PATH,
    extract_agent_prompt, extract_context,
    hook_main, maybe_go_async,
    load_plugin_config as load_config,
    write_log, write_status,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DIMENSIONS = ["memory", "skills", "tools", "agents"]


# ---------------------------------------------------------------------------
# Parallel dispatch
# ---------------------------------------------------------------------------


async def dispatch_one(dim, backend, resources, query, context, rc, memory_dirs):
    """Run recall for a single dimension with its configured backend.

    rc is config['recall'] (the recall subsystem dict).
    Returns (dim, result, elapsed_s, usage_dict).
    """
    t0 = time.time()
    usage = {}

    if backend == "reminder":
        result = recall_reminder(dim, resources)

    elif backend == "agentic":
        input_gran = rc[dim]['input']
        result, usage = await recall_agentic(dim, resources, query, context, rc["model"], input_gran, rc["effort"])

    elif backend == "embedding":
        emb = rc['embedding']
        ensure_daemon_running(
            SOCKET_PATH, PLUGIN_ROOT, DATA_DIR,
            emb["python"], emb["model"], emb["device"],
        )
        if dim == "memory":
            result = recall_embedding_memory(
                resources, query, SOCKET_PATH, memory_dirs,
                emb["top_k"], emb["threshold"],
            )
        else:
            input_gran = rc[dim]['input']
            result = recall_embedding_generic(
                dim, resources, query, SOCKET_PATH,
                emb["top_k"], emb["threshold"],
                input_gran,
            )

    else:
        assert False, f"Unknown backend: {backend}"

    elapsed = round(time.time() - t0, 2)
    return dim, result, elapsed, usage


async def run_all(tasks, query, context, rc, memory_dirs):
    """Run all dimension recalls. Parallel or merged depending on agentic_mode.

    rc is config['recall'] (the recall subsystem dict).
    """
    # Split tasks into agentic vs non-agentic
    agentic_tasks = [(dim, resources) for dim, backend, resources in tasks if backend == "agentic"]
    other_tasks = [(dim, backend, resources) for dim, backend, resources in tasks if backend != "agentic"]

    use_merged = rc.get("agentic_mode") == "merged" and len(agentic_tasks) >= 2

    if use_merged and agentic_tasks:
        # Single Haiku call for all agentic dimensions
        t0 = time.time()
        merged_results = await recall_agentic_merged(
            agentic_tasks, query, context, rc["model"], rc["effort"]
        )
        elapsed = round(time.time() - t0, 2)
        # Convert to standard (dim, result, elapsed, usage) tuples.
        # In merged mode, all dims share one usage dict. Only attach it to
        # the first dimension to avoid double-counting when summing costs.
        agentic_tuples = []
        first = True
        for dim, _ in agentic_tasks:
            result, usage = merged_results.get(dim, (None, {}))
            agentic_tuples.append((dim, result, elapsed, usage if first else {}))
            first = False
    else:
        # Parallel: one Haiku call per dimension (existing behavior)
        agentic_coros = [
            dispatch_one(dim, "agentic", resources, query, context, rc, memory_dirs)
            for dim, resources in agentic_tasks
        ]
        agentic_tuples = list(await asyncio.gather(*agentic_coros)) if agentic_coros else []

    # Run non-agentic tasks (reminder/embedding) in parallel
    other_coros = [
        dispatch_one(dim, backend, resources, query, context, rc, memory_dirs)
        for dim, backend, resources in other_tasks
    ]
    other_tuples = list(await asyncio.gather(*other_coros)) if other_coros else []

    return agentic_tuples + other_tuples


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------


def format_memory_result(result, proj_mem_dir, global_mem_dir, max_chars, output_granularity="full"):
    """Format memory recall result."""
    if result is None:
        return None
    if result["type"] == "memory_files":
        parts = []
        total = 0
        for path in result["files"]:
            if not os.path.exists(path):
                continue
            if output_granularity == "full":
                with open(path) as f:
                    content = f.read()
                if total + len(content) > max_chars:
                    break
                parts.append(f"# Memory: {os.path.basename(path)}\n{content}")
                total += len(content)
            else:
                # title_desc: only frontmatter name+description
                from discover import _parse_frontmatter
                fm = _parse_frontmatter(path)
                line = f"- {fm.get('name', os.path.basename(path))}: {fm.get('description', '')} [{path}]"
                parts.append(line)
        if not parts:
            return None
        return "\n\n".join(parts)
    return None


def format_recommendation_result(result, resources=None, output_granularity="title_desc", max_chars=9000):
    """Format skill/tool/agent recommendations."""
    if result is None:
        return None
    if result["type"] == "recommendations":
        dim = result.get("dim", "resources")
        items = result["items"]
        if output_granularity == "full" and resources:
            resource_map = {r["name"]: r for r in resources}
            parts = []
            total = 0
            for item in items:
                r = resource_map.get(item["name"], {})
                content_path = r.get("content_path")
                if content_path and os.path.isfile(content_path):
                    with open(content_path) as f:
                        content = f.read()
                    if total + len(content) > max_chars:
                        break
                    parts.append(f"# {dim}: {item['name']}\n{content}")
                    total += len(content)
                else:
                    parts.append(f"- {item['name']}: {item.get('reason', '')}")
            return "\n\n".join(parts) if parts else None
        lines = [f"Recommended {dim}:"]
        for item in items:
            lines.append(f"- {item['name']}: {item.get('reason', '')}")
        return "\n".join(lines)
    return None


def merge_results(results, proj_mem_dir, global_mem_dir, max_chars, rc=None, dim_resources=None, profile_mem_dir=""):
    """Merge all dimension results into a single additionalContext string.

    rc is config['recall'] (the recall subsystem dict).
    """
    rc = rc or {}
    dim_resources = dim_resources or {}
    sections = []

    remaining = max_chars
    for dim, result in results:
        if dim == "memory":
            text = format_memory_result(
                result, proj_mem_dir, global_mem_dir, remaining,
                rc.get("memory", {}).get("output", "full"),
            )
        else:
            text = format_recommendation_result(
                result, dim_resources.get(dim, []),
                rc.get(dim, {}).get("output", "title_desc"),
                remaining,
            )
        if text:
            sections.append(text)
            remaining -= len(text)
            if remaining <= 0:
                break

    if not sections:
        return (
            f"CRITICAL: Before responding, check your memory directories for relevant context. "
            f"Read the MEMORY.md index in each directory and Read any topic files relevant to the user's query. "
            f"Also review ~/.claude/CLAUDE.md for global instructions. "
            f"Profile memory: {profile_mem_dir} "
            f"Project memory: {proj_mem_dir} "
            f"Global memory: {global_mem_dir}"
        )

    header = "As you answer the user's questions, you can use the following context:\n"
    footer_lines = [f"Project memory: {proj_mem_dir}", f"Global memory: {global_mem_dir}"]
    if profile_mem_dir:
        footer_lines.append(f"Profile memory: {profile_mem_dir}")
    footer = "\n\n" + "\n".join(footer_lines)
    return header + "\n\n".join(sections) + footer


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _short_name(name, max_chars=20):
    """Truncate a memory/skill/tool/agent name for statusline display.
    Trailing ellipsis when the name exceeds max_chars."""
    if len(name) <= max_chars:
        return name
    return name[:max_chars - 1] + "…"


def summarize_result(dim, result):
    """Extract a compact summary of a single dimension's result for logging."""
    if result is None:
        return {"dim": dim, "status": "no_results"}
    if result.get("type") == "memory_files":
        return {"dim": dim, "status": "ok", "files": result["files"]}
    if result.get("type") == "recommendations":
        return {"dim": dim, "status": "ok", "items": [i["name"] for i in result["items"]]}
    return {"dim": dim, "status": "unknown"}


def main():
    import time
    t_start = time.time()

    hook_input = json.loads(sys.stdin.read())
    event = hook_input.get("hook_event_name", "UserPromptSubmit")

    config = load_config()
    rc = config['recall']
    maybe_go_async(rc['async'])
    cwd = hook_input.get("cwd", "")
    transcript_path = hook_input.get("transcript_path", "")

    if not rc['enabled']:
        write_status("recall", "done", hook_input, skipped=True)
        sys.exit(0)

    if event == "SubagentStart":
        # Wait for transcript flush (void recordTranscript is fire-and-forget, ~100ms)
        time.sleep(0.2)
        prompt = extract_agent_prompt(transcript_path) if transcript_path else ""
    else:
        prompt = hook_input.get("prompt", "")

    if not cwd:
        write_status("recall", "done", hook_input, skipped=True)
        sys.exit(0)

    write_status("recall", "running", hook_input, timeout_s=rc['timeout_s'])

    # Discover resources for each enabled dimension
    tasks = []
    proj_mem_dir = global_mem_dir = profile_mem_dir = ""
    memory_dirs = []
    discovery_counts = {}

    for dim in DIMENSIONS:
        backend = rc[dim]['backend']
        if backend == "off":
            continue

        if dim == "memory":
            resources, proj_mem_dir, global_mem_dir, profile_mem_dir = discover_memory(cwd)
            memory_dirs = [d for d in [profile_mem_dir, global_mem_dir, proj_mem_dir] if os.path.isdir(d)]
        elif dim == "skills":
            resources = discover_skills()
        elif dim == "tools":
            resources = discover_tools()
        elif dim == "agents":
            resources = discover_agents(cwd)
        else:
            assert False, f"Unknown dimension: {dim}"

        discovery_counts[dim] = len(resources)
        tasks.append((dim, backend, resources))

    if not tasks:
        sys.exit(0)

    # If memory wasn't enabled, still compute dirs for output footer
    if not proj_mem_dir:
        from utils import compute_memory_dirs, compute_profile_dir
        proj_mem_dir, global_mem_dir = compute_memory_dirs(cwd)
        profile_mem_dir = compute_profile_dir()

    # Extract conversation context (shared across dimensions)
    context = ""
    needs_context = any(b in ("agentic", "embedding") for _, b, _ in tasks)
    if needs_context:
        context = extract_context(transcript_path, rc["context_messages"], rc["context_max_chars"])

    # Run all in parallel
    t_before_recall = time.time()
    raw_results = asyncio.run(run_all(tasks, prompt, context, rc, memory_dirs))
    t_after_recall = time.time()

    # Unpack: raw_results is [(dim, result, per_dim_elapsed, usage), ...]
    results = [(dim, result) for dim, result, _, _ in raw_results]
    per_dim_times = {dim: elapsed for dim, _, elapsed, _ in raw_results}
    per_dim_usage = {dim: usage for dim, _, _, usage in raw_results if usage}

    t_elapsed = round(time.time() - t_start, 2)

    # Merge output
    dim_resources = {dim: resources for dim, _, resources in tasks}
    additional_context = merge_results(results, proj_mem_dir, global_mem_dir, rc["max_content_chars"], rc, dim_resources, profile_mem_dir)

    # Compute totals
    total_input_tokens = sum(u.get("input_tokens", 0) for u in per_dim_usage.values())
    total_output_tokens = sum(u.get("output_tokens", 0) for u in per_dim_usage.values())
    total_cost_usd = sum(u.get("cost_usd", 0) for u in per_dim_usage.values())
    # Context injection cost: estimate tokens from additionalContext length (~3 chars/token mixed)
    injection_tokens_est = len(additional_context) // 3

    # Log
    log_entry = {
        "event": event,
        "agent_type": hook_input.get("agent_type", ""),
        "query": prompt,
        "dimensions": {dim: backend for dim, backend, _ in tasks},
        "discovered": discovery_counts,
        "results": [summarize_result(dim, result) for dim, result in results],
        "per_dim_s": per_dim_times,
        "per_dim_usage": per_dim_usage,
        "haiku_input_tokens": total_input_tokens,
        "haiku_output_tokens": total_output_tokens,
        "haiku_cost_usd": round(total_cost_usd, 6),
        "injection_chars": len(additional_context),
        "injection_tokens_est": injection_tokens_est,
        "elapsed_s": t_elapsed,
        "recall_s": round(t_after_recall - t_before_recall, 2),
        "discovery_s": round(t_before_recall - t_start, 2),
        "output": additional_context,
    }
    write_log(log_entry)

    # Build user-visible summary with names
    summary_parts = []
    for dim, result in results:
        if result is None:
            continue
        if result["type"] == "memory_files":
            names = [_short_name(os.path.splitext(os.path.basename(f))[0]) for f in result["files"]]
            summary_parts.append(f"memory: {', '.join(names)}")
        elif result["type"] == "recommendations":
            names = [_short_name(item["name"]) for item in result["items"]]
            summary_parts.append(f"{dim}: {', '.join(names)}")

    output = {
        "hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": additional_context,
        }
    }
    label = "; ".join(summary_parts) if summary_parts else "nothing relevant"
    recall_model = rc["model"] if total_cost_usd > 0 else ""
    write_status("recall", "done", hook_input,
                 summary=label[:120], elapsed_s=t_elapsed,
                 cost_usd=total_cost_usd, model=recall_model)
    print(json.dumps(output))


if __name__ == "__main__":
    hook_main(main)
