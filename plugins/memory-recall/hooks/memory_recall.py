#!/usr/bin/env python3
"""Three-backend memory recall hook.

Backends (configured via plugin option CLAUDE_PLUGIN_OPTION_BACKEND):
  reminder  -- inject memory paths, ask agent to read (default, zero-cost)
  agentic   -- Agent SDK + Haiku selects files, inject content (~$0.003/query)
  embedding -- local RAG daemon selects files, inject content (zero-cost after setup)

Agentic/embedding fall back to reminder on failure, with a warning injected.
"""

import json
import os
import re
import socket
import subprocess
import sys

# ---------------------------------------------------------------------------
# Config from plugin options (env vars set by CC hook runner)
# ---------------------------------------------------------------------------

BACKEND = os.environ.get("CLAUDE_PLUGIN_OPTION_BACKEND", "reminder")
MODEL = os.environ.get("CLAUDE_PLUGIN_OPTION_MODEL", "haiku")
CONTEXT_MESSAGES = int(os.environ.get("CLAUDE_PLUGIN_OPTION_CONTEXT_MESSAGES", "5"))
CONTEXT_MAX_CHARS = int(os.environ.get("CLAUDE_PLUGIN_OPTION_CONTEXT_MAX_CHARS", "2000"))

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
DAEMON_PYTHON = os.path.join(HOME, "miniconda3/envs/memory-recall/bin/python")

# ---------------------------------------------------------------------------
# Hook I/O
# ---------------------------------------------------------------------------


def output_hook(additional_context):
    """Write hook response JSON to stdout."""
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": additional_context,
        }
    }))


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def compute_memory_dirs(cwd):
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


def build_reminder_text(proj_mem_dir, global_mem_dir):
    return (
        f"CRITICAL: Before responding, check your memory directories for relevant context. "
        f"Read the MEMORY.md index in each directory and Read any topic files relevant to the user's query. "
        f"Also review ~/.claude/CLAUDE.md for global instructions. "
        f"Project memory: {proj_mem_dir} "
        f"Global memory: {global_mem_dir}"
    )


def parse_frontmatter(path):
    """Parse YAML frontmatter from a markdown file (simple key: value)."""
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
            result[key.strip()] = value.strip()
    return result


def build_manifest(proj_mem_dir, global_mem_dir):
    """Build frontmatter manifest of all memory files."""
    entries = []
    for mem_dir in [proj_mem_dir, global_mem_dir]:
        if not os.path.isdir(mem_dir):
            continue
        for fname in sorted(os.listdir(mem_dir)):
            if not fname.endswith(".md") or fname == "MEMORY.md":
                continue
            path = os.path.join(mem_dir, fname)
            fm = parse_frontmatter(path)
            name = fm.get("name", fname)
            desc = fm.get("description", "")
            ftype = fm.get("type", "unknown")
            entries.append(f"- {name} ({ftype}): {desc} [{path}]")
    return "\n".join(entries)


def extract_context(transcript_path):
    """Extract recent conversation context from transcript JSONL."""
    if not transcript_path or not os.path.exists(transcript_path):
        return ""
    result = subprocess.run(
        ["tail", "-n", "50", transcript_path],
        capture_output=True, text=True, timeout=2,
    )
    if result.returncode != 0:
        return ""
    messages = []
    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        msg = json.loads(line)
        role = msg.get("role")
        if role not in ("user", "assistant"):
            continue
        content = msg.get("content", "")
        if isinstance(content, list):
            text_parts = [
                b.get("text", "")
                for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            content = " ".join(text_parts)
        if not isinstance(content, str):
            continue
        messages.append(f"{role}: {content[:500]}")

    recent = messages[-CONTEXT_MESSAGES:]
    context = "\n".join(recent)
    if len(context) > CONTEXT_MAX_CHARS:
        context = context[-CONTEXT_MAX_CHARS:]
    return context


def inject_file_contents(file_paths, proj_mem_dir, global_mem_dir):
    """Read selected files and output as additionalContext."""
    parts = []
    total_chars = 0
    for path in file_paths:
        if not os.path.exists(path):
            continue
        with open(path) as f:
            content = f.read()
        if total_chars + len(content) > 9000:
            break
        parts.append(f"# Memory: {os.path.basename(path)}\n{content}")
        total_chars += len(content)
    if not parts:
        output_hook(build_reminder_text(proj_mem_dir, global_mem_dir))
        return
    memory_content = "\n\n".join(parts)
    output_hook(
        f"As you answer the user's questions, you can use the following context:\n"
        f"{memory_content}\n\n"
        f"Project memory: {proj_mem_dir}\n"
        f"Global memory: {global_mem_dir}"
    )


# ---------------------------------------------------------------------------
# Backend: reminder
# ---------------------------------------------------------------------------


def run_reminder(proj_mem_dir, global_mem_dir):
    output_hook(build_reminder_text(proj_mem_dir, global_mem_dir))


# ---------------------------------------------------------------------------
# Backend: agentic
# ---------------------------------------------------------------------------


def run_agentic(proj_mem_dir, global_mem_dir, prompt, transcript_path):
    import asyncio
    from claude_agent_sdk import query as sdk_query
    from claude_agent_sdk import ClaudeAgentOptions
    from claude_agent_sdk.types import AssistantMessage

    manifest = build_manifest(proj_mem_dir, global_mem_dir)
    assert manifest, "No memory files found for manifest"

    context = extract_context(transcript_path)

    agentic_prompt = f"Catalog:\n{manifest}\n"
    if context:
        agentic_prompt += f"\nRecent conversation:\n{context}\n"
    agentic_prompt += f"\nQuery: {prompt}"

    options = ClaudeAgentOptions(
        system_prompt=(
            'Select 0-3 relevant memory files for the query. '
            'Return ONLY a JSON object: {"files": ["path1", ...]}. '
            'No explanation.'
        ),
        model=MODEL,
        tools=[],
        settings='{"disableAllHooks": true}',
        env={"CLAUDECODE": ""},
        effort="low",
        max_budget_usd=0.01,
        extra_args={"no-session-persistence": None},
    )

    result_text = ""

    async def _run():
        nonlocal result_text
        async for msg in sdk_query(prompt=agentic_prompt, options=options):
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if hasattr(block, "text"):
                        result_text += block.text

    asyncio.run(_run())
    assert result_text, "Empty response from agentic search"

    # Parse JSON (may be wrapped in markdown fences)
    clean = re.sub(r"```json?\s*", "", result_text)
    clean = re.sub(r"```", "", clean).strip()
    parsed = json.loads(clean)

    files = parsed["files"]
    if not files:
        run_reminder(proj_mem_dir, global_mem_dir)
        return

    inject_file_contents(files, proj_mem_dir, global_mem_dir)


# ---------------------------------------------------------------------------
# Backend: embedding
# ---------------------------------------------------------------------------


def query_daemon(query_text, memory_dirs, top_k=3, threshold=0.85):
    """Send query to embedding daemon via unix socket."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(3.0)
    sock.connect(SOCKET_PATH)
    request = json.dumps({
        "query": query_text,
        "memory_dirs": memory_dirs,
        "top_k": top_k,
        "threshold": threshold,
    }).encode()
    sock.sendall(request)
    sock.shutdown(socket.SHUT_WR)
    data = b""
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
    sock.close()
    return json.loads(data.decode())


def ensure_daemon_running():
    """Start embedding daemon in background if not running."""
    if os.path.exists(SOCKET_PATH):
        return  # socket exists, daemon likely running
    daemon_script = os.path.join(PLUGIN_ROOT, "hooks", "embedding_daemon.py")
    assert os.path.isfile(DAEMON_PYTHON), f"Daemon python not found: {DAEMON_PYTHON}"
    assert os.path.isfile(daemon_script), f"Daemon script not found: {daemon_script}"
    log_file = os.path.join(DATA_DIR, "daemon.log")
    os.makedirs(DATA_DIR, exist_ok=True)
    subprocess.Popen(
        [DAEMON_PYTHON, daemon_script],
        stdout=open(log_file, "a"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )


def run_embedding(proj_mem_dir, global_mem_dir, prompt, transcript_path):
    memory_dirs = [d for d in [proj_mem_dir, global_mem_dir] if os.path.isdir(d)]
    assert memory_dirs, "No memory directories exist"

    # Build context-aware query (same as client.py)
    context_parts = []
    context = extract_context(transcript_path)
    if context:
        # Flatten context lines into a single string for embedding
        context_parts.append(context.replace("\n", " "))
    context_parts.append(prompt)
    query_text = " ".join(context_parts)

    ensure_daemon_running()
    response = query_daemon(query_text, memory_dirs)
    assert response["status"] == "ok", f"daemon error: {response.get('error')}"

    results = response["results"]
    if not results:
        run_reminder(proj_mem_dir, global_mem_dir)
        return

    # Inject file contents from daemon results
    parts = []
    for r in results:
        fname = os.path.basename(r["path"])
        parts.append(f"# Memory: {fname}\n{r['content']}")
    memory_content = "\n\n".join(parts)
    output_hook(
        f"As you answer the user's questions, you can use the following context:\n"
        f"{memory_content}\n\n"
        f"Project memory: {proj_mem_dir}\n"
        f"Global memory: {global_mem_dir}"
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    hook_input = json.loads(sys.stdin.read())
    prompt = hook_input.get("prompt", "")
    cwd = hook_input.get("cwd", "")
    transcript_path = hook_input.get("transcript_path", "")

    if not cwd:
        sys.exit(0)

    proj_mem_dir, global_mem_dir = compute_memory_dirs(cwd)

    if BACKEND == "reminder":
        run_reminder(proj_mem_dir, global_mem_dir)
    elif BACKEND == "agentic":
        try:
            run_agentic(proj_mem_dir, global_mem_dir, prompt, transcript_path)
        except Exception as exc:
            warning = (
                f"WARNING: agentic memory search FAILED ({type(exc).__name__}: {exc}). "
                f"Report this to the user immediately so they can investigate.\n\n"
            )
            output_hook(warning + build_reminder_text(proj_mem_dir, global_mem_dir))
    elif BACKEND == "embedding":
        try:
            run_embedding(proj_mem_dir, global_mem_dir, prompt, transcript_path)
        except Exception as exc:
            warning = (
                f"WARNING: embedding memory search FAILED ({type(exc).__name__}: {exc}). "
                f"Report this to the user immediately so they can investigate.\n\n"
            )
            output_hook(warning + build_reminder_text(proj_mem_dir, global_mem_dir))
    else:
        run_reminder(proj_mem_dir, global_mem_dir)


if __name__ == "__main__":
    main()
