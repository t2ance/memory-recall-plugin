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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backends import (
    ensure_daemon_running,
    extract_context,
    recall_agentic,
    recall_agentic_merged,
    recall_embedding_generic,
    recall_embedding_memory,
    recall_reminder,
)
from discover import discover_agents, discover_memory, discover_skills, discover_tools

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DIMENSIONS = ["memory", "skills", "tools", "agents"]

HOME = os.path.expanduser("~")
DATA_DIR = os.environ.get(
    "CLAUDE_PLUGIN_DATA",
    os.path.join(HOME, ".claude/plugins/data/memory-recall-memory-recall"),
)
PLUGIN_ROOT = os.environ.get(
    "CLAUDE_PLUGIN_ROOT",
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
)
SOCKET_PATH = os.path.join(DATA_DIR, "daemon.sock")


def load_config():
    return {
        # Per-dimension backend: off | reminder | agentic | embedding
        "memory": os.environ.get("CLAUDE_PLUGIN_OPTION_MEMORY", "reminder"),
        "skills": os.environ.get("CLAUDE_PLUGIN_OPTION_SKILLS", "off"),
        "tools": os.environ.get("CLAUDE_PLUGIN_OPTION_TOOLS", "off"),
        "agents": os.environ.get("CLAUDE_PLUGIN_OPTION_AGENTS", "off"),
        # Shared options
        "agentic_mode": os.environ.get("CLAUDE_PLUGIN_OPTION_AGENTIC_MODE", "parallel"),  # parallel | merged
        # Per-dimension granularity
        "memory_input": os.environ.get("CLAUDE_PLUGIN_OPTION_MEMORY_INPUT", "title_desc"),
        "memory_output": os.environ.get("CLAUDE_PLUGIN_OPTION_MEMORY_OUTPUT", "full"),
        "skills_input": os.environ.get("CLAUDE_PLUGIN_OPTION_SKILLS_INPUT", "title_desc"),
        "skills_output": os.environ.get("CLAUDE_PLUGIN_OPTION_SKILLS_OUTPUT", "title_desc"),
        "tools_input": os.environ.get("CLAUDE_PLUGIN_OPTION_TOOLS_INPUT", "title_desc"),
        "tools_output": os.environ.get("CLAUDE_PLUGIN_OPTION_TOOLS_OUTPUT", "title_desc"),
        "agents_input": os.environ.get("CLAUDE_PLUGIN_OPTION_AGENTS_INPUT", "title_desc"),
        "agents_output": os.environ.get("CLAUDE_PLUGIN_OPTION_AGENTS_OUTPUT", "title_desc"),
        "model": os.environ.get("CLAUDE_PLUGIN_OPTION_MODEL", "haiku"),
        "context_messages": int(os.environ.get("CLAUDE_PLUGIN_OPTION_CONTEXT_MESSAGES", "5")),
        "context_max_chars": int(os.environ.get("CLAUDE_PLUGIN_OPTION_CONTEXT_MAX_CHARS", "2000")),
        "max_content_chars": int(os.environ.get("CLAUDE_PLUGIN_OPTION_MAX_CONTENT_CHARS", "9000")),
        # Embedding-specific
        "embedding_model": os.environ.get("CLAUDE_PLUGIN_OPTION_EMBEDDING_MODEL", "intfloat/multilingual-e5-small"),
        "embedding_python": os.path.expanduser(
            os.environ.get("CLAUDE_PLUGIN_OPTION_EMBEDDING_PYTHON", "~/miniconda3/envs/memory-recall/bin/python")
        ),
        "embedding_threshold": float(os.environ.get("CLAUDE_PLUGIN_OPTION_EMBEDDING_THRESHOLD", "0.85")),
        "embedding_top_k": int(os.environ.get("CLAUDE_PLUGIN_OPTION_EMBEDDING_TOP_K", "3")),
        "embedding_device": os.environ.get("CLAUDE_PLUGIN_OPTION_EMBEDDING_DEVICE", "cpu"),
    }


# ---------------------------------------------------------------------------
# Parallel dispatch
# ---------------------------------------------------------------------------


async def dispatch_one(dim, backend, resources, query, context, config, memory_dirs):
    """Run recall for a single dimension with its configured backend.

    Returns (dim, result, elapsed_s, usage_dict).
    usage_dict has token/cost info for agentic, empty dict otherwise.
    """
    import time as _time
    t0 = _time.time()
    usage = {}

    if backend == "reminder":
        result = recall_reminder(dim, resources)

    elif backend == "agentic":
        input_gran = config.get(f"{dim}_input", "title_desc")
        result, usage = await recall_agentic(dim, resources, query, context, config["model"], input_gran)

    elif backend == "embedding":
        ensure_daemon_running(
            SOCKET_PATH, PLUGIN_ROOT, DATA_DIR,
            config["embedding_python"], config["embedding_model"], config["embedding_device"],
        )
        input_gran = config.get(f"{dim}_input", "title_desc")
        if dim == "memory":
            result = recall_embedding_memory(
                resources, query, SOCKET_PATH, memory_dirs,
                config["embedding_top_k"], config["embedding_threshold"],
            )
        else:
            result = recall_embedding_generic(
                dim, resources, query, SOCKET_PATH,
                config["embedding_top_k"], config["embedding_threshold"],
                input_gran,
            )

    else:
        assert False, f"Unknown backend: {backend}"

    elapsed = round(_time.time() - t0, 2)
    return dim, result, elapsed, usage


async def run_all(tasks, query, context, config, memory_dirs):
    """Run all dimension recalls. Parallel or merged depending on agentic_mode."""
    import time as _time

    # Split tasks into agentic vs non-agentic
    agentic_tasks = [(dim, resources) for dim, backend, resources in tasks if backend == "agentic"]
    other_tasks = [(dim, backend, resources) for dim, backend, resources in tasks if backend != "agentic"]

    use_merged = config.get("agentic_mode") == "merged" and len(agentic_tasks) >= 2

    if use_merged and agentic_tasks:
        # Single Haiku call for all agentic dimensions
        t0 = _time.time()
        merged_results = await recall_agentic_merged(
            agentic_tasks, query, context, config["model"]
        )
        elapsed = round(_time.time() - t0, 2)
        # Convert to standard (dim, result, elapsed, usage) tuples
        agentic_tuples = []
        for dim, _ in agentic_tasks:
            result, usage = merged_results.get(dim, (None, {}))
            agentic_tuples.append((dim, result, elapsed, usage))
    else:
        # Parallel: one Haiku call per dimension (existing behavior)
        agentic_coros = [
            dispatch_one(dim, "agentic", resources, query, context, config, memory_dirs)
            for dim, resources in agentic_tasks
        ]
        agentic_tuples = list(await asyncio.gather(*agentic_coros)) if agentic_coros else []

    # Run non-agentic tasks (reminder/embedding) in parallel
    other_coros = [
        dispatch_one(dim, backend, resources, query, context, config, memory_dirs)
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


def merge_results(results, proj_mem_dir, global_mem_dir, max_chars, config=None, dim_resources=None):
    """Merge all dimension results into a single additionalContext string."""
    config = config or {}
    dim_resources = dim_resources or {}
    sections = []

    remaining = max_chars
    for dim, result in results:
        if dim == "memory":
            text = format_memory_result(
                result, proj_mem_dir, global_mem_dir, remaining,
                config.get("memory_output", "full"),
            )
        else:
            text = format_recommendation_result(
                result, dim_resources.get(dim, []),
                config.get(f"{dim}_output", "title_desc"),
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
            f"Project memory: {proj_mem_dir} "
            f"Global memory: {global_mem_dir}"
        )

    header = "As you answer the user's questions, you can use the following context:\n"
    footer = f"\n\nProject memory: {proj_mem_dir}\nGlobal memory: {global_mem_dir}"
    return header + "\n\n".join(sections) + footer


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def write_log(entry):
    """Append a structured JSON log entry to the recall log file."""
    log_path = os.path.join(DATA_DIR, "recall.jsonl")
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(log_path, "a") as f:
        f.write(json.dumps(entry, indent=2, ensure_ascii=False) + "\n\n")


def summarize_result(dim, result):
    """Extract a compact summary of a single dimension's result for logging."""
    if result is None:
        return {"dim": dim, "status": "no_results"}
    if result.get("type") == "memory_files":
        return {"dim": dim, "status": "ok", "files": result["files"]}
    if result.get("type") == "recommendations":
        return {"dim": dim, "status": "ok", "items": [i["name"] for i in result["items"]]}
    return {"dim": dim, "status": "unknown"}


def extract_agent_prompt(transcript_path, max_lines=100):
    """Extract the last Agent tool_use prompt from the main agent's transcript."""
    import subprocess
    result = subprocess.run(
        ["tail", "-n", str(max_lines), transcript_path],
        capture_output=True, text=True, timeout=2,
    )
    assert result.returncode == 0, f"tail failed: {result.stderr}"
    for line in reversed(result.stdout.strip().split("\n")):
        if not line:
            continue
        msg = json.loads(line)
        if msg.get("type") != "assistant":
            continue
        for block in msg.get("message", {}).get("content", []):
            if block.get("type") == "tool_use" and block.get("name") == "Agent":
                return block["input"]["prompt"]
    return ""


def main():
    import time
    t_start = time.time()

    hook_input = json.loads(sys.stdin.read())
    event = hook_input.get("hook_event_name", "UserPromptSubmit")
    cwd = hook_input.get("cwd", "")
    transcript_path = hook_input.get("transcript_path", "")

    if event == "SubagentStart":
        # Wait for transcript flush (void recordTranscript is fire-and-forget, ~100ms)
        time.sleep(0.2)
        prompt = extract_agent_prompt(transcript_path) if transcript_path else ""
    else:
        prompt = hook_input.get("prompt", "")

    if not cwd:
        sys.exit(0)

    config = load_config()

    # Discover resources for each enabled dimension
    tasks = []
    proj_mem_dir = global_mem_dir = ""
    memory_dirs = []
    discovery_counts = {}

    for dim in DIMENSIONS:
        backend = config[dim]
        if backend == "off":
            continue

        if dim == "memory":
            resources, proj_mem_dir, global_mem_dir = discover_memory(cwd)
            memory_dirs = [d for d in [proj_mem_dir, global_mem_dir] if os.path.isdir(d)]
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
        from discover import _compute_memory_dirs
        proj_mem_dir, global_mem_dir = _compute_memory_dirs(cwd, DATA_DIR)

    # Extract conversation context (shared across dimensions)
    context = ""
    needs_context = any(b in ("agentic", "embedding") for _, b, _ in tasks)
    if needs_context:
        context = extract_context(transcript_path, config["context_messages"], config["context_max_chars"])

    # Run all in parallel
    t_before_recall = time.time()
    raw_results = asyncio.run(run_all(tasks, prompt, context, config, memory_dirs))
    t_after_recall = time.time()

    # Unpack: raw_results is [(dim, result, per_dim_elapsed, usage), ...]
    results = [(dim, result) for dim, result, _, _ in raw_results]
    per_dim_times = {dim: elapsed for dim, _, elapsed, _ in raw_results}
    per_dim_usage = {dim: usage for dim, _, _, usage in raw_results if usage}

    t_elapsed = round(time.time() - t_start, 2)

    # Merge output
    dim_resources = {dim: resources for dim, _, resources in tasks}
    additional_context = merge_results(results, proj_mem_dir, global_mem_dir, config["max_content_chars"], config, dim_resources)

    # Compute totals
    total_input_tokens = sum(u.get("input_tokens", 0) for u in per_dim_usage.values())
    total_output_tokens = sum(u.get("output_tokens", 0) for u in per_dim_usage.values())
    total_cost_usd = sum(u.get("cost_usd", 0) for u in per_dim_usage.values())
    # Context injection cost: estimate tokens from additionalContext length (~3 chars/token mixed)
    injection_tokens_est = len(additional_context) // 3

    # Log
    log_entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
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

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": additional_context,
        }
    }))


if __name__ == "__main__":
    main()
