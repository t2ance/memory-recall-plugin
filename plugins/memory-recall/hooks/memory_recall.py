#!/usr/bin/env python3
"""Three-backend memory recall hook.

Backends (configured via plugin option CLAUDE_PLUGIN_OPTION_BACKEND):
  reminder  -- inject memory paths, ask agent to read (default, zero-cost)
  agentic   -- Agent SDK + Haiku selects files, inject content (~$0.003/query)
  embedding -- local RAG daemon selects files, inject content (zero-cost after setup)

Errors crash the hook visibly (CC shows "hook error" to user).
"""

import json
import os
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

EMBEDDING_MODEL = os.environ.get("CLAUDE_PLUGIN_OPTION_EMBEDDING_MODEL", "intfloat/multilingual-e5-small")
EMBEDDING_PYTHON = os.path.expanduser(os.environ.get("CLAUDE_PLUGIN_OPTION_EMBEDDING_PYTHON", "~/miniconda3/envs/memory-recall/bin/python"))
EMBEDDING_THRESHOLD = float(os.environ.get("CLAUDE_PLUGIN_OPTION_EMBEDDING_THRESHOLD", "0.85"))
EMBEDDING_TOP_K = int(os.environ.get("CLAUDE_PLUGIN_OPTION_EMBEDDING_TOP_K", "3"))
EMBEDDING_DEVICE = os.environ.get("CLAUDE_PLUGIN_OPTION_EMBEDDING_DEVICE", "cpu")
MAX_CONTENT_CHARS = int(os.environ.get("CLAUDE_PLUGIN_OPTION_MAX_CONTENT_CHARS", "9000"))

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

# ---------------------------------------------------------------------------
# Hook I/O
# ---------------------------------------------------------------------------


def output_hook(additional_context):
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": additional_context,
        }
    }))


def output_memory_content(parts, proj_mem_dir, global_mem_dir):
    output_hook(
        "As you answer the user's questions, you can use the following context:\n"
        + "\n\n".join(parts)
        + f"\n\nProject memory: {proj_mem_dir}\nGlobal memory: {global_mem_dir}"
    )


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
    entries = []
    for mem_dir in [proj_mem_dir, global_mem_dir]:
        if not os.path.isdir(mem_dir):
            continue
        for fname in sorted(os.listdir(mem_dir)):
            if not fname.endswith(".md") or fname == "MEMORY.md":
                continue
            path = os.path.join(mem_dir, fname)
            fm = parse_frontmatter(path)
            entries.append(f"- {fm.get('name', fname)} ({fm.get('type', 'unknown')}): {fm.get('description', '')} [{path}]")
    return "\n".join(entries)


def extract_context(transcript_path):
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
            content = " ".join(
                b.get("text", "")
                for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            )
        if not isinstance(content, str):
            continue
        messages.append(f"{role}: {content[:500]}")

    recent = messages[-CONTEXT_MESSAGES:]
    context = "\n".join(recent)
    if len(context) > CONTEXT_MAX_CHARS:
        context = context[-CONTEXT_MAX_CHARS:]
    return context


def read_and_format_files(file_paths, proj_mem_dir, global_mem_dir):
    parts = []
    total_chars = 0
    for path in file_paths:
        if not os.path.exists(path):
            continue
        with open(path) as f:
            content = f.read()
        if total_chars + len(content) > MAX_CONTENT_CHARS:
            break
        parts.append(f"# Memory: {os.path.basename(path)}\n{content}")
        total_chars += len(content)
    if not parts:
        output_hook(build_reminder_text(proj_mem_dir, global_mem_dir))
        return
    output_memory_content(parts, proj_mem_dir, global_mem_dir)


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
    import re
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

    clean = re.sub(r"```json?\s*", "", result_text)
    clean = re.sub(r"```", "", clean).strip()
    parsed = json.loads(clean)

    files = parsed["files"]
    if not files:
        note = "NOTE: agentic search ran but found no relevant memories.\n\n"
        output_hook(note + build_reminder_text(proj_mem_dir, global_mem_dir))
        return

    read_and_format_files(files, proj_mem_dir, global_mem_dir)


# ---------------------------------------------------------------------------
# Backend: embedding
# ---------------------------------------------------------------------------


def query_daemon(query_text, memory_dirs, top_k=EMBEDDING_TOP_K, threshold=EMBEDDING_THRESHOLD):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(3.0)
    try:
        sock.connect(SOCKET_PATH)
        sock.sendall(json.dumps({
            "query": query_text,
            "memory_dirs": memory_dirs,
            "top_k": top_k,
            "threshold": threshold,
        }).encode())
        sock.shutdown(socket.SHUT_WR)
        data = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
    finally:
        sock.close()
    return json.loads(data.decode())


def ensure_daemon_running():
    if os.path.exists(SOCKET_PATH):
        return
    daemon_script = os.path.join(PLUGIN_ROOT, "hooks", "embedding_daemon.py")
    assert os.path.isfile(EMBEDDING_PYTHON), f"Daemon python not found: {EMBEDDING_PYTHON}"
    assert os.path.isfile(daemon_script), f"Daemon script not found: {daemon_script}"
    os.makedirs(DATA_DIR, exist_ok=True)
    log_handle = open(os.path.join(DATA_DIR, "daemon.log"), "a")
    env = os.environ.copy()
    env["EMBEDDING_MODEL"] = EMBEDDING_MODEL
    env["EMBEDDING_DEVICE"] = EMBEDDING_DEVICE
    subprocess.Popen(
        [EMBEDDING_PYTHON, daemon_script],
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    log_handle.close()


def run_embedding(proj_mem_dir, global_mem_dir, prompt, transcript_path):
    memory_dirs = [d for d in [proj_mem_dir, global_mem_dir] if os.path.isdir(d)]
    assert memory_dirs, "No memory directories exist"

    context_parts = []
    context = extract_context(transcript_path)
    if context:
        context_parts.append(context.replace("\n", " "))
    context_parts.append(prompt)
    query_text = " ".join(context_parts)

    ensure_daemon_running()
    response = query_daemon(query_text, memory_dirs)
    assert response["status"] == "ok", f"daemon error: {response.get('error')}"

    results = response["results"]
    if not results:
        note = "NOTE: embedding search ran but found no relevant memories.\n\n"
        output_hook(note + build_reminder_text(proj_mem_dir, global_mem_dir))
        return

    parts = [f"# Memory: {os.path.basename(r['path'])}\n{r['content']}" for r in results]
    output_memory_content(parts, proj_mem_dir, global_mem_dir)


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
        run_agentic(proj_mem_dir, global_mem_dir, prompt, transcript_path)
    elif BACKEND == "embedding":
        run_embedding(proj_mem_dir, global_mem_dir, prompt, transcript_path)
    else:
        assert False, f"Unknown backend: {BACKEND}"


if __name__ == "__main__":
    main()
