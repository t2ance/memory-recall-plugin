"""Three generic recall backends that work for any dimension.

Each backend takes a list of resources (uniform schema from discover.py)
and returns recall results. The backends do not know which dimension
they are serving -- they only see resources and a query.
"""

import asyncio
import json
import os
import re
import socket
import subprocess

HOME = os.path.expanduser("~")


# -- Reminder -----------------------------------------------------------------


def recall_reminder(dim, resources):
    """List all resources. Zero cost, no filtering."""
    if not resources:
        return None
    lines = [f"Available {dim}:"]
    for r in resources:
        lines.append(f"- {r['name']}: {r['description']}")
    return "\n".join(lines)


# -- Agentic ------------------------------------------------------------------


AGENTIC_SYSTEM_PROMPTS = {
    "memory": (
        "Select 0-3 memory files most relevant to the query. "
        'Return ONLY JSON: {{"files": ["id1", ...]}}. No explanation.'
    ),
    "skills": (
        "Select 0-3 skills most relevant to the user's task. "
        'Return ONLY JSON: {{"items": [{{"name": "...", "reason": "..."}}]}}. No explanation.'
    ),
    "tools": (
        "Select 0-5 tools/MCP servers most relevant to the user's task. "
        'Return ONLY JSON: {{"items": [{{"name": "...", "reason": "..."}}]}}. No explanation.'
    ),
    "agents": (
        "Select 0-2 agent types best suited for the user's task. "
        'Return ONLY JSON: {{"items": [{{"name": "...", "reason": "..."}}]}}. No explanation.'
    ),
}


async def recall_agentic(dim, resources, query, context, model, input_granularity="title_desc"):
    """Use Agent SDK + Haiku to select relevant resources.

    Returns (result_dict, usage_dict) where usage_dict contains token counts and cost.
    input_granularity: 'title_desc' (name+description) or 'full' (entire content for memory files).
    """
    if not resources:
        return None

    from claude_agent_sdk import query as sdk_query
    from claude_agent_sdk import ClaudeAgentOptions
    from claude_agent_sdk.types import AssistantMessage, ResultMessage

    if input_granularity == "full":
        lines = []
        for r in resources:
            path = r["id"]
            if os.path.exists(path):
                with open(path) as f:
                    content = f.read()
                lines.append(f"- [{r['name']}] {content[:500]} [id={r['id']}]")
            else:
                lines.append(f"- {r['name']}: {r['description']} [id={r['id']}]")
        catalog = "\n".join(lines)
    else:
        catalog = "\n".join(
            f"- {r['name']}: {r['description']} [id={r['id']}]"
            for r in resources
        )
    prompt_parts = [f"Catalog:\n{catalog}"]
    if context:
        prompt_parts.append(f"\nRecent conversation:\n{context}")
    prompt_parts.append(f"\nQuery: {query}")
    agentic_prompt = "\n".join(prompt_parts)

    options = ClaudeAgentOptions(
        system_prompt=AGENTIC_SYSTEM_PROMPTS[dim],
        model=model,
        tools=[],
        settings='{"disableAllHooks": true}',
        env={"CLAUDECODE": "", "CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK": "1"},
        effort="low",
        max_budget_usd=0.01,
        extra_args={"no-session-persistence": None},
    )

    result_text = ""
    usage = {}
    async for msg in sdk_query(prompt=agentic_prompt, options=options):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if hasattr(block, "text"):
                    result_text += block.text
        elif isinstance(msg, ResultMessage):
            usage = {
                "input_tokens": msg.usage.get("input_tokens", 0) if msg.usage else 0,
                "output_tokens": msg.usage.get("output_tokens", 0) if msg.usage else 0,
                "cost_usd": msg.total_cost_usd or 0,
                "duration_api_ms": msg.duration_api_ms,
            }

    assert result_text, f"Empty response from agentic {dim} recall"

    clean = re.sub(r"```json?\s*", "", result_text)
    clean = re.sub(r"```", "", clean).strip()
    decoder = json.JSONDecoder()
    parsed, _ = decoder.raw_decode(clean)

    if dim == "memory":
        files = parsed.get("files", [])
        if not files:
            return None, usage
        return {"type": "memory_files", "files": files}, usage
    else:
        items = parsed.get("items", [])
        if not items:
            return None, usage
        return {"type": "recommendations", "dim": dim, "items": items}, usage


# -- Agentic merged (single call for all dimensions) -------------------------


MERGED_SYSTEM_PROMPT = (
    "You are a context recommender. Given multiple catalogs and a query, "
    "select the most relevant items from EACH catalog independently.\n"
    "Return ONLY a JSON object with one key per catalog:\n"
    '{"memory": {"files": ["id1", ...]}, '
    '"skills": {"items": [{"name": "...", "reason": "..."}]}, '
    '"tools": {"items": [{"name": "...", "reason": "..."}]}, '
    '"agents": {"items": [{"name": "...", "reason": "..."}]}}\n'
    "Only include catalogs that were provided. Select 0-3 items per catalog. "
    "No explanation outside the JSON."
)


async def recall_agentic_merged(dim_resources, query, context, model):
    """Single Haiku call for all dimensions. Returns {dim: (result, usage)}.

    dim_resources: [(dim, resources), ...] for each enabled agentic dimension.
    """
    from claude_agent_sdk import query as sdk_query
    from claude_agent_sdk import ClaudeAgentOptions
    from claude_agent_sdk.types import AssistantMessage, ResultMessage

    # Build one prompt with all catalogs
    sections = []
    for dim, resources in dim_resources:
        catalog = "\n".join(
            f"- {r['name']}: {r['description']} [id={r['id']}]"
            for r in resources
        )
        limit = "0-3 files" if dim == "memory" else "0-3 items"
        sections.append(f"## {dim} catalog ({limit}):\n{catalog}")

    prompt_parts = ["\n\n".join(sections)]
    if context:
        prompt_parts.append(f"\nRecent conversation:\n{context}")
    prompt_parts.append(f"\nQuery: {query}")
    agentic_prompt = "\n".join(prompt_parts)

    options = ClaudeAgentOptions(
        system_prompt=MERGED_SYSTEM_PROMPT,
        model=model,
        tools=[],
        settings='{"disableAllHooks": true}',
        env={"CLAUDECODE": "", "CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK": "1"},
        effort="low",
        max_budget_usd=0.02,
        extra_args={"no-session-persistence": None},
    )

    result_text = ""
    usage = {}
    async for msg in sdk_query(prompt=agentic_prompt, options=options):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if hasattr(block, "text"):
                    result_text += block.text
        elif isinstance(msg, ResultMessage):
            usage = {
                "input_tokens": msg.usage.get("input_tokens", 0) if msg.usage else 0,
                "output_tokens": msg.usage.get("output_tokens", 0) if msg.usage else 0,
                "cost_usd": msg.total_cost_usd or 0,
                "duration_api_ms": msg.duration_api_ms,
            }

    assert result_text, "Empty response from merged agentic recall"

    clean = re.sub(r"```json?\s*", "", result_text)
    clean = re.sub(r"```", "", clean).strip()
    decoder = json.JSONDecoder()
    parsed, _ = decoder.raw_decode(clean)

    # Parse per-dimension results
    results = {}
    for dim, _ in dim_resources:
        dim_data = parsed.get(dim, {})
        if dim == "memory":
            files = dim_data.get("files", [])
            results[dim] = ({"type": "memory_files", "files": files} if files else None, usage)
        else:
            items = dim_data.get("items", [])
            results[dim] = ({"type": "recommendations", "dim": dim, "items": items} if items else None, usage)

    return results


# -- Embedding ----------------------------------------------------------------


def recall_embedding_memory(resources, query, socket_path, memory_dirs, top_k, threshold):
    """Embedding search over memory files via daemon."""
    response = _query_daemon(socket_path, {
        "query": query,
        "memory_dirs": memory_dirs,
        "top_k": top_k,
        "threshold": threshold,
    })
    assert response["status"] == "ok", f"daemon error: {response.get('error')}"
    results = response["results"]
    if not results:
        return None
    return {"type": "memory_files", "files": [r["path"] for r in results]}


def recall_embedding_generic(resources, query, socket_path, top_k, threshold):
    """Embedding search over resource descriptions via daemon."""
    response = _query_daemon(socket_path, {
        "type": "search_descriptions",
        "query": query,
        "resources": [{"name": r["name"], "description": r["description"], "id": r["id"]} for r in resources],
        "top_k": top_k,
        "threshold": threshold,
    })
    assert response["status"] == "ok", f"daemon error: {response.get('error')}"
    results = response["results"]
    if not results:
        return None
    return {
        "type": "recommendations",
        "dim": "generic",
        "items": [{"name": r["name"], "reason": f"similarity={r['score']:.2f}"} for r in results],
    }


def _query_daemon(socket_path, request):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(3.0)
    try:
        sock.connect(socket_path)
        sock.sendall(json.dumps(request).encode())
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


def ensure_daemon_running(socket_path, plugin_root, data_dir, embedding_python, embedding_model, embedding_device):
    """Start embedding daemon if not already running."""
    if os.path.exists(socket_path):
        return
    daemon_script = os.path.join(plugin_root, "hooks", "embedding_daemon.py")
    assert os.path.isfile(embedding_python), f"Daemon python not found: {embedding_python}"
    assert os.path.isfile(daemon_script), f"Daemon script not found: {daemon_script}"
    os.makedirs(data_dir, exist_ok=True)
    log_handle = open(os.path.join(data_dir, "daemon.log"), "a")
    env = os.environ.copy()
    env["EMBEDDING_MODEL"] = embedding_model
    env["EMBEDDING_DEVICE"] = embedding_device
    subprocess.Popen(
        [embedding_python, daemon_script],
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    log_handle.close()


# -- Context extraction -------------------------------------------------------


def extract_context(transcript_path, context_messages, context_max_chars):
    """Extract recent conversation context from transcript."""
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

    recent = messages[-context_messages:]
    context = "\n".join(recent)
    if len(context) > context_max_chars:
        context = context[-context_max_chars:]
    return context
