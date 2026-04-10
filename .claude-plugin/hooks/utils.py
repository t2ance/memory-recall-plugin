"""Shared utilities for memory-recall plugin hooks.

Consolidates: logging, config, frontmatter parsing, transcript reading,
Agent SDK calling, and JSON parsing. Used by recall.py, memory_save.py,
discover.py, and backends.py.
"""

import asyncio
import json
import os
import subprocess
import sys
import time

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
STATUS_DIR = os.path.join(DATA_DIR, "status")


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def write_log(entry):
    """Append structured JSON log entry to recall.jsonl. Auto-adds timestamp."""
    if "ts" not in entry:
        entry["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    log_path = os.path.join(DATA_DIR, "recall.jsonl")
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(log_path, "a") as f:
        f.write(json.dumps(entry, indent=2, ensure_ascii=False) + "\n\n")


def maybe_go_async(config_key, config):
    """If config_key is truthy, emit dynamic async signal so CC backgrounds this hook."""
    if config.get(config_key, False):
        print(json.dumps({"async": True}))
        sys.stdout.flush()


def write_status(hook_name, state, hook_input, summary="", elapsed_s=0, cost_usd=0, model="", timeout_s=60, skipped=False, _cache={}):
    """Write hook status to a JSON file for statusLine visibility.

    Design invariant: EVERY code path produces a complete record with all
    required fields. Statistics (total_runs, skipped_count) are merged from
    the previous file, but the record structure is always built from scratch.
    """
    session_id = hook_input.get("session_id", "unknown")
    agent_id = hook_input.get("agent_id", "")
    agent_type = hook_input.get("agent_type", "")

    session_dir = os.path.join(STATUS_DIR, session_id)
    os.makedirs(session_dir, exist_ok=True)

    fname = f"{hook_name}_{agent_id}.json" if agent_id else f"{hook_name}.json"
    path = os.path.join(session_dir, fname)

    # Read previous record for statistics merge (total_runs, skipped_count).
    prev = {}
    if os.path.exists(path):
        try:
            with open(path) as f:
                prev = json.loads(f.read())
        except (json.JSONDecodeError, OSError):
            pass

    now_hms = time.strftime("%H:%M:%S")
    total_runs = prev.get("total_runs", 0)
    skipped_count = prev.get("skipped_count", 0)

    if skipped:
        # Preserve previous display state, bump skipped_count.
        state = prev.get("state", state)
        summary = prev.get("summary", summary)
        elapsed_s = prev.get("elapsed_s", elapsed_s)
        cost_usd = prev.get("cost_usd", cost_usd)
        model = prev.get("model", model)
        skipped_count += 1
    elif state == "running":
        # Preserve previous result for display while running.
        # StatusLine shows prev summary + "(Running)" suffix.
        summary = prev.get("summary", summary)
        elapsed_s = prev.get("elapsed_s", elapsed_s)
        cost_usd = prev.get("cost_usd", cost_usd)
        model = prev.get("model", model)
        total_runs += 1
        _cache[f"{session_id}:{fname}:started_at"] = now_hms

    # Single code path: always build complete record.
    data = {
        "hook": hook_name,
        "state": state,
        "agent_id": agent_id,
        "agent_type": agent_type,
        "summary": summary,
        "elapsed_s": round(elapsed_s, 2) if isinstance(elapsed_s, (int, float)) else 0,
        "cost_usd": round(cost_usd, 4) if isinstance(cost_usd, (int, float)) else 0,
        "model": model,
        "started_at": _cache.get(f"{session_id}:{fname}:started_at", prev.get("started_at", now_hms)),
        "finished_at": now_hms if state not in ("running", "") else prev.get("finished_at", ""),
        "total_runs": total_runs,
        "timeout_s": timeout_s,
        "skipped_count": skipped_count,
    }

    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        f.write(json.dumps(data))
    os.rename(tmp_path, path)


def hook_main(fn):
    """Unified hook entry point: crash logging."""
    try:
        fn()
    except Exception:
        import traceback
        write_log({"event": "crash", "hook": fn.__name__,
                    "error": traceback.format_exc()})
        raise


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_plugin_config():
    """Load all plugin config from env vars (recall + auto-save options)."""
    return {
        # Recall backends
        "memory": os.environ.get("CLAUDE_PLUGIN_OPTION_MEMORY", "reminder"),
        "skills": os.environ.get("CLAUDE_PLUGIN_OPTION_SKILLS", "off"),
        "tools": os.environ.get("CLAUDE_PLUGIN_OPTION_TOOLS", "off"),
        "agents": os.environ.get("CLAUDE_PLUGIN_OPTION_AGENTS", "off"),
        # Recall options
        "agentic_mode": os.environ.get("CLAUDE_PLUGIN_OPTION_AGENTIC_MODE", "parallel"),
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
        # Embedding
        "embedding_model": os.environ.get("CLAUDE_PLUGIN_OPTION_EMBEDDING_MODEL", "intfloat/multilingual-e5-small"),
        "embedding_python": os.path.expanduser(
            os.environ.get("CLAUDE_PLUGIN_OPTION_EMBEDDING_PYTHON", "~/miniconda3/envs/memory-recall/bin/python")
        ),
        "embedding_threshold": float(os.environ.get("CLAUDE_PLUGIN_OPTION_EMBEDDING_THRESHOLD", "0.85")),
        "embedding_top_k": int(os.environ.get("CLAUDE_PLUGIN_OPTION_EMBEDDING_TOP_K", "3")),
        "embedding_device": os.environ.get("CLAUDE_PLUGIN_OPTION_EMBEDDING_DEVICE", "cpu"),
        # Memory Save
        "memory_save_enabled": os.environ.get("CLAUDE_PLUGIN_OPTION_MEMORY_SAVE_ENABLED", "true") != "false",
        "memory_save_targets": os.environ.get("CLAUDE_PLUGIN_OPTION_MEMORY_SAVE_TARGETS", "native"),
        "memory_save_context_turns": int(os.environ.get("CLAUDE_PLUGIN_OPTION_MEMORY_SAVE_CONTEXT_TURNS", "3")),
        # Effort: "low" is cheaper/faster but incompatible with complex structured output
        "recall_effort": os.environ.get("CLAUDE_PLUGIN_OPTION_RECALL_EFFORT", ""),
        "memory_save_effort": os.environ.get("CLAUDE_PLUGIN_OPTION_MEMORY_SAVE_EFFORT", ""),  # empty = default (no effort param)
        # Pair Programmer
        "pair_programmer_enabled": os.environ.get("CLAUDE_PLUGIN_OPTION_PAIR_PROGRAMMER_ENABLED", "false") != "false",
        "pair_programmer_model": os.environ.get("CLAUDE_PLUGIN_OPTION_PAIR_PROGRAMMER_MODEL", "haiku"),
        "pair_programmer_sample_rate": float(os.environ.get("CLAUDE_PLUGIN_OPTION_PAIR_PROGRAMMER_SAMPLE_RATE", "1.0")),
        "pair_programmer_cooldown_s": float(os.environ.get("CLAUDE_PLUGIN_OPTION_PAIR_PROGRAMMER_COOLDOWN_S", "120")),
        "pair_programmer_context_messages": int(os.environ.get("CLAUDE_PLUGIN_OPTION_PAIR_PROGRAMMER_CONTEXT_MESSAGES", "5")),
        "pair_programmer_context_max_chars": int(os.environ.get("CLAUDE_PLUGIN_OPTION_PAIR_PROGRAMMER_CONTEXT_MAX_CHARS", "3000")),
        "pair_programmer_effort": os.environ.get("CLAUDE_PLUGIN_OPTION_PAIR_PROGRAMMER_EFFORT", ""),
        "pair_programmer_max_tool_input_chars": int(os.environ.get("CLAUDE_PLUGIN_OPTION_PAIR_PROGRAMMER_MAX_TOOL_INPUT_CHARS", "2000")),
        "pair_programmer_max_tool_output_chars": int(os.environ.get("CLAUDE_PLUGIN_OPTION_PAIR_PROGRAMMER_MAX_TOOL_OUTPUT_CHARS", "1000")),
        "pair_programmer_max_recall_files": int(os.environ.get("CLAUDE_PLUGIN_OPTION_PAIR_PROGRAMMER_MAX_RECALL_FILES", "5")),
        "pair_programmer_max_memory_file_chars": int(os.environ.get("CLAUDE_PLUGIN_OPTION_PAIR_PROGRAMMER_MAX_MEMORY_FILE_CHARS", "2000")),
        # Memory Curator
        "curator_enabled": os.environ.get("CLAUDE_PLUGIN_OPTION_CURATOR_ENABLED", "true") != "false",
        "curator_cooldown_h": float(os.environ.get("CLAUDE_PLUGIN_OPTION_CURATOR_COOLDOWN_H", "4")),
        "curator_effort": os.environ.get("CLAUDE_PLUGIN_OPTION_CURATOR_EFFORT", ""),
        # Async mode per hook
        "recall_async": os.environ.get("CLAUDE_PLUGIN_OPTION_RECALL_ASYNC", "false") != "false",
        "memory_save_async": os.environ.get("CLAUDE_PLUGIN_OPTION_MEMORY_SAVE_ASYNC", "true") != "false",
        "pair_programmer_async": os.environ.get("CLAUDE_PLUGIN_OPTION_PAIR_PROGRAMMER_ASYNC", "true") != "false",
        "curator_async": os.environ.get("CLAUDE_PLUGIN_OPTION_CURATOR_ASYNC", "true") != "false",
    }


# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------

def parse_frontmatter(path):
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
# Transcript reading
# ---------------------------------------------------------------------------

def read_transcript_tail(path, num_lines=50):
    """Read last N lines from transcript JSONL, return parsed dicts."""
    if not path or not os.path.exists(path):
        return []
    result = subprocess.run(
        ["tail", "-n", str(num_lines), path],
        capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 0, f"tail failed: {result.stderr}"
    entries = []
    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def extract_messages(transcript_path, num_turns=3, max_char_per_msg=3000):
    """Extract recent user+assistant messages from transcript.

    Returns list of {"role": "user"|"assistant", "text": str}.
    """
    entries = read_transcript_tail(transcript_path, num_turns * 40)
    messages = []
    for msg in entries:
        msg_type = msg.get("type", "")
        if msg_type not in ("user", "assistant"):
            continue
        if msg_type == "user" and msg.get("userType") == "system":
            continue
        content = msg.get("message", {}).get("content", "")
        if isinstance(content, list):
            content = "\n".join(
                b.get("text", "") for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            )
        if not isinstance(content, str) or not content.strip():
            continue
        text = content.strip()
        if len(text) > max_char_per_msg:
            text = text[:max_char_per_msg] + "\n...(truncated)"
        messages.append({"role": msg_type, "text": text})

    # Deduplicate consecutive same-role
    deduped = []
    for m in messages:
        if deduped and deduped[-1]["role"] == m["role"]:
            deduped[-1] = m
        else:
            deduped.append(m)

    return deduped[-(num_turns * 2):]


def extract_agent_prompt(transcript_path, max_lines=100):
    """Extract the last Agent tool_use prompt from the main agent's transcript."""
    entries = read_transcript_tail(transcript_path, max_lines)
    for msg in reversed(entries):
        if msg.get("type") != "assistant":
            continue
        for block in msg.get("message", {}).get("content", []):
            if block.get("type") == "tool_use" and block.get("name") == "Agent":
                return block["input"]["prompt"]
    return ""


def extract_context(transcript_path, context_messages, context_max_chars):
    """Extract recent conversation context for recall (simpler format)."""
    entries = read_transcript_tail(transcript_path, 50)
    messages = []
    for msg in entries:
        msg_type = msg.get("type", "")
        if msg_type not in ("user", "assistant"):
            continue
        if msg_type == "user" and msg.get("userType") == "system":
            continue
        content = msg.get("message", {}).get("content", "")
        if isinstance(content, list):
            content = " ".join(
                b.get("text", "") for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            )
        if not isinstance(content, str):
            continue
        messages.append(f"{msg_type}: {content[:500]}")

    recent = messages[-context_messages:]
    context = "\n".join(recent)
    if len(context) > context_max_chars:
        context = context[-context_max_chars:]
    return context


# ---------------------------------------------------------------------------
# Agent SDK wrapper
# ---------------------------------------------------------------------------

async def call_sdk_haiku(prompt, system_prompt, output_schema, model="haiku", max_budget_usd=None, effort=""):
    """Call Haiku via Agent SDK with structured output.

    Returns (parsed_json_or_None, usage_dict).
    Note: effort="low" is incompatible with complex structured output schemas.
    """
    from claude_agent_sdk import query as sdk_query
    from claude_agent_sdk import ClaudeAgentOptions
    from claude_agent_sdk.types import ResultMessage, AssistantMessage

    stderr_lines = []
    def on_stderr(line):
        stderr_lines.append(line.rstrip()[:200])
        if len(stderr_lines) > 50:
            stderr_lines.pop(0)

    kwargs = dict(
        system_prompt=system_prompt,
        model=model,
        tools=[],
        output_format=output_schema,
        settings='{"disableAllHooks": true}',
        env={"CLAUDECODE": "", "CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK": "1"},
        extra_args={"no-session-persistence": None, "debug-to-stderr": None},
        stderr=on_stderr,
    )
    if max_budget_usd is not None:
        kwargs["max_budget_usd"] = max_budget_usd
    if effort:
        kwargs["effort"] = effort

    options = ClaudeAgentOptions(**kwargs)

    parsed = None
    usage = {}
    raw_texts = []

    try:
        async for msg in sdk_query(prompt=prompt, options=options):
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if hasattr(block, "text"):
                        raw_texts.append(block.text[:500])
            elif isinstance(msg, ResultMessage):
                usage = {
                    "input_tokens": msg.usage.get("input_tokens", 0) if msg.usage else 0,
                    "output_tokens": msg.usage.get("output_tokens", 0) if msg.usage else 0,
                    "cost_usd": msg.total_cost_usd or 0,
                    "duration_api_ms": msg.duration_api_ms,
                }
                parsed = msg.structured_output
                if msg.result:
                    raw_texts.append(f"[result]: {msg.result[:500]}")
    except Exception as e:
        write_log({"event": "sdk_error",
                    "error": str(e)[:300],
                    "stderr_tail": stderr_lines[-10:] if stderr_lines else [],
                    "raw_texts": raw_texts[:5] if raw_texts else []})
        raise

    # Save raw reasoning to debug log when structured_output is None
    if parsed is None and raw_texts:
        write_log({"event": "sdk_no_structured_output",
                    "raw_texts": raw_texts[:5],
                    "usage": usage})

    return parsed, usage


# ---------------------------------------------------------------------------
# JSON parsing
# ---------------------------------------------------------------------------

def parse_json(text):
    """Parse JSON from text, handling markdown code blocks and embedded JSON."""
    text = text.strip()
    # Strip markdown code fences
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines[1:] if not l.strip().startswith("```")]
        text = "\n".join(lines).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Find outermost JSON object
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end])
            except json.JSONDecodeError:
                pass
    return None


# ---------------------------------------------------------------------------
# Memory directory resolution
# ---------------------------------------------------------------------------

def compute_memory_dirs(cwd):
    """Compute project and global memory directory paths."""
    sanitized = cwd.replace("/", "-").lstrip("-")
    proj_candidates = [
        os.path.join(HOME, ".claude", "projects", f"-{sanitized}", "memory"),
        os.path.join(HOME, ".claude", "projects", sanitized, "memory"),
    ]
    proj_mem_dir = next(
        (p for p in proj_candidates if os.path.isdir(p)),
        proj_candidates[0],
    )
    global_mem_dir = os.path.join(DATA_DIR, "global-memory")
    return proj_mem_dir, global_mem_dir


def read_memory_files(memory_dir):
    """List memory files with metadata from a directory."""
    if not os.path.isdir(memory_dir):
        return []
    entries = []
    for fname in sorted(os.listdir(memory_dir)):
        if not fname.endswith(".md") or fname == "MEMORY.md":
            continue
        path = os.path.join(memory_dir, fname)
        fm = parse_frontmatter(path)
        entries.append({
            "name": fm.get("name", fname.replace(".md", "")),
            "description": fm.get("description", ""),
            "type": fm.get("type", "project"),
            "id": path,
            "file": fname,
        })
    return entries
