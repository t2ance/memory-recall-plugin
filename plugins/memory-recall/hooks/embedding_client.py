#!/usr/bin/env python3
"""Smart hook client for memory-recall RAG.

Reads hook input JSON from stdin, builds a context-aware query from the
session transcript, queries the embedding daemon, and outputs the hook
response JSON with memory contents as additionalContext.

Exit codes:
  0 + JSON output  -- Tier 1 success (RAG results injected)
  0 + empty output -- Tier 1 success but no relevant memories (skip injection)
  non-zero         -- daemon unreachable (caller should fall back to Tier 2)
"""

import json
import os
import socket
import subprocess
import sys

DATA_DIR = os.environ.get(
    "CLAUDE_PLUGIN_DATA",
    os.path.expanduser("~/.claude/plugins/data/memory-recall-memory-recall"),
)
SOCKET_PATH = os.path.join(DATA_DIR, "daemon.sock")
HOME = os.path.expanduser("~")
SOCKET_TIMEOUT = 3.0
HISTORY_ROUNDS = 3
MSG_TRUNCATE = 500


def compute_memory_dirs(cwd):
    """Compute project + global memory directories."""
    sanitized = cwd.replace("/", "-").lstrip("-")
    proj_candidates = [
        os.path.join(HOME, ".claude", "projects", f"-{sanitized}", "memory"),
        os.path.join(HOME, ".claude", "projects", sanitized, "memory"),
    ]
    proj_mem_dir = None
    for p in proj_candidates:
        if os.path.isdir(p):
            proj_mem_dir = p
            break
    if proj_mem_dir is None:
        proj_mem_dir = proj_candidates[0]

    global_mem_dir = os.path.join(DATA_DIR, "global-memory")

    return proj_mem_dir, global_mem_dir


def build_query(prompt, transcript_path):
    """Build embedding query from current prompt + recent transcript history."""
    context_parts = []

    if transcript_path and os.path.exists(transcript_path):
        # Read last 30 lines to find 3 rounds of conversation
        result = subprocess.run(
            ["tail", "-n", "30", transcript_path],
            capture_output=True, text=True, timeout=2,
        )
        if result.returncode == 0:
            messages = []
            for line in result.stdout.strip().split("\n"):
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
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
                messages.append(content[:MSG_TRUNCATE])

            # Take last N rounds (each round = user + assistant)
            recent = messages[-(HISTORY_ROUNDS * 2):]
            if recent:
                context_parts.append(" ".join(recent))

    context_parts.append(prompt)
    return " ".join(context_parts)


def query_daemon(query_text, memory_dirs, top_k=3, threshold=0.85):
    """Send query to daemon via unix socket, return response dict."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(SOCKET_TIMEOUT)
    sock.connect(SOCKET_PATH)

    request = json.dumps({
        "query": query_text,
        "memory_dirs": memory_dirs,
        "top_k": top_k,
        "threshold": 0.85,
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


def format_output(results, proj_mem_dir, global_mem_dir):
    """Format daemon results into hook additionalContext."""
    parts = []
    for r in results:
        fname = os.path.basename(r["path"])
        parts.append(f"# Memory: {fname}\n{r['content']}")

    memory_content = "\n\n".join(parts)
    context = (
        f"As you answer the user's questions, you can use the following context:\n"
        f"{memory_content}\n\n"
        f"Project memory: {proj_mem_dir}\n"
        f"Global memory: {global_mem_dir}\n"
        f"Global instructions: {HOME}/.claude/CLAUDE.md"
    )
    return json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context,
        }
    })


def main():
    hook_input = json.loads(sys.stdin.read())
    prompt = hook_input.get("prompt", "")
    transcript_path = hook_input.get("transcript_path", "")
    cwd = hook_input.get("cwd", "")

    if not cwd:
        sys.exit(1)

    proj_mem_dir, global_mem_dir = compute_memory_dirs(cwd)
    memory_dirs = [d for d in [proj_mem_dir, global_mem_dir] if os.path.isdir(d)]

    if not memory_dirs:
        # No memory dirs exist yet, nothing to search
        sys.exit(1)

    query_text = build_query(prompt, transcript_path)
    response = query_daemon(query_text, memory_dirs)

    assert response["status"] == "ok", f"daemon error: {response.get('error')}"

    results = response["results"]
    if not results:
        # No relevant memories found, output nothing (caller can decide)
        return

    print(format_output(results, proj_mem_dir, global_mem_dir))


if __name__ == "__main__":
    main()
