"""Three generic recall backends that work for any dimension.

Each backend takes a list of resources (uniform schema from discover.py)
and returns recall results. The backends do not know which dimension
they are serving -- they only see resources and a query.
"""

import json
import os
import socket
import subprocess

from utils import call_sdk_haiku

HOME = os.path.expanduser("~")


# -- Reminder -----------------------------------------------------------------


def recall_reminder(dim, resources):
    """Return all resources as structured types. Zero cost, no filtering."""
    if not resources:
        return None
    if dim == "memory":
        return {"type": "memory_files", "files": [r["id"] for r in resources]}
    return {
        "type": "recommendations",
        "dim": dim,
        "items": [{"name": r["name"], "reason": r["description"]} for r in resources],
    }


# -- Agentic ------------------------------------------------------------------


_SIDECAR_PREAMBLE = """\
You are a silent sidecar agent running inside a Claude Code hook. You are NOT in a conversation with the user -- the user cannot see or respond to your output. Your ONLY job is to select relevant resources from a catalog and return them via the structured output tool. Never ask questions, never explain, never converse. Just select and return.

Matching rules:
- Match broadly: if a resource MIGHT be useful, include it. Err on the side of inclusion.
- The query may be in any language. Match by semantic meaning, not surface keywords.
- Short or vague queries (like "test") should still match resources related to testing/debugging."""

_MEMORY_TIERED_INSTRUCTIONS = """\
The memory catalog is organized into three tiers:
- profile: distilled user patterns (working style, preferences, habits)
- global: user-level facts and feedback that apply across all projects
- project: project-specific facts, decisions, and context

Select up to 2 memory files FROM EACH tier, for a total of 0-6 files.

Relevance means EITHER of:
(a) Factual content related to the query -- names, concepts, or facts the user needs for this task.
(b) User preferences, coding style, design principles, or prior feedback that should shape HOW the agent approaches this task -- even if the preference does not literally mention anything in the query.

Your goal: help the main agent complete this task in a way that matches the user's preferences and working style. A "terse answers" preference is relevant to a "plan an auth middleware" query precisely because it governs HOW the plan should be written.

If a tier has nothing relevant, return fewer than 2 files (or zero) for that tier. Never force inclusion."""

AGENTIC_SYSTEM_PROMPTS = {
    "memory": f"{_SIDECAR_PREAMBLE}\n\n{_MEMORY_TIERED_INSTRUCTIONS}",
    "skills": f"{_SIDECAR_PREAMBLE}\n\nSelect 0-3 skills most relevant to the user's task.",
    "tools": f"{_SIDECAR_PREAMBLE}\n\nSelect 0-5 tools/MCP servers most relevant to the user's task.",
    "agents": f"{_SIDECAR_PREAMBLE}\n\nSelect 0-2 agent types best suited for the user's task.",
}

_ITEMS_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["name", "reason"],
            },
        },
    },
    "required": ["items"],
}

_MEMORY_SCHEMA = {
    "type": "object",
    "properties": {
        "profile": {"type": "array", "items": {"type": "string"}, "maxItems": 2},
        "global": {"type": "array", "items": {"type": "string"}, "maxItems": 2},
        "project": {"type": "array", "items": {"type": "string"}, "maxItems": 2},
    },
    "required": ["profile", "global", "project"],
}

AGENTIC_SCHEMAS = {
    "memory": {"type": "json_schema", "schema": _MEMORY_SCHEMA},
    "skills": {"type": "json_schema", "schema": _ITEMS_SCHEMA},
    "tools": {"type": "json_schema", "schema": _ITEMS_SCHEMA},
    "agents": {"type": "json_schema", "schema": _ITEMS_SCHEMA},
}


_MEMORY_TIERS = ("profile", "global", "project")


def _build_memory_resolver(resources):
    """Build a multi-key lookup map: any plausible identifier -> canonical path.

    Haiku is unreliable about which field of `- {name}: {desc} [id={path}]` it
    echoes back. Map every variant (full path, basename, basename without ext,
    frontmatter name) to the same canonical path so post-parse resolution
    survives Haiku's guesswork.
    """
    resolver = {}
    for r in resources:
        path = r["id"]
        keys = {
            path,
            os.path.basename(path),
            os.path.splitext(os.path.basename(path))[0],
            r.get("name", ""),
            os.path.splitext(r.get("name", ""))[0],
        }
        for k in keys:
            if k:
                resolver[k] = path
    return resolver


def _resolve_loose(ident, resolver):
    """Last-ditch fuzzy lookup: try with/without .md, with underscores swapped."""
    candidates = [
        ident.rstrip(".md"),
        ident + ".md",
        ident.replace("-", "_"),
        ident.replace("_", "-"),
    ]
    for c in candidates:
        if c in resolver:
            return resolver[c]
    return None


def _build_memory_catalog(resources):
    """Build a tier-grouped catalog string for memory resources.

    Groups entries by their `tier` field into profile / global / project
    sections so the agentic selector can pick 0-2 per tier.
    """
    by_tier = {t: [] for t in _MEMORY_TIERS}
    for r in resources:
        tier = r.get("tier", "project")
        by_tier.setdefault(tier, []).append(r)

    sections = []
    for tier in _MEMORY_TIERS:
        bucket = by_tier.get(tier, [])
        if not bucket:
            sections.append(f"## {tier} tier (0 files):\n(empty)")
            continue
        lines = [
            f"- {r['name']}: {r['description']} [id={r['id']}]"
            for r in bucket
        ]
        sections.append(f"## {tier} tier ({len(bucket)} files):\n" + "\n".join(lines))
    return "\n\n".join(sections)


async def recall_agentic(dim, resources, query, context, model, input_granularity="title_desc", effort="low"):
    """Use Agent SDK + Haiku to select relevant resources.

    Returns (result_dict, usage_dict) where usage_dict contains token counts and cost.
    input_granularity: 'title_desc' (name+description) or 'full' (entire content for memory files).
    """
    if not resources:
        return None, {}

    if dim == "memory":
        # Memory uses tier-grouped catalog regardless of input_granularity so
        # Haiku can pick 0-2 per tier under the new selection prompt.
        catalog = _build_memory_catalog(resources)
    elif input_granularity == "full":
        lines = []
        for r in resources:
            path = r.get("content_path") or r["id"]
            if os.path.isfile(path):
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

    parsed, usage = await call_sdk_haiku(
        agentic_prompt, AGENTIC_SYSTEM_PROMPTS[dim], AGENTIC_SCHEMAS[dim],
        model=model, max_budget_usd=None, effort=effort,
    )

    if not parsed:
        return None, usage

    if dim == "memory":
        # New tiered schema: merge per-tier lists, capping each tier at 2.
        # Haiku may return identifiers in any of: full path, basename, name --
        # resolve them all back to canonical paths via the resource catalog.
        resolver = _build_memory_resolver(resources)
        files = []
        for tier in _MEMORY_TIERS:
            for ident in parsed.get(tier, [])[:2]:
                resolved = resolver.get(ident) or _resolve_loose(ident, resolver)
                if resolved:
                    files.append(resolved)
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
    "select the most relevant items from EACH catalog independently. "
    "Select 0-3 items per catalog. "
    "For the memory catalog, it is further grouped into profile/global/project tiers: "
    "pick up to 2 files PER TIER. Relevance means either (a) factual content related to the query, "
    "or (b) user preferences / coding style / design principles that should shape HOW the agent "
    "approaches this task, even if they do not literally mention the query topic."
)


async def recall_agentic_merged(dim_resources, query, context, model, effort="low"):
    """Single Haiku call for all dimensions. Returns {dim: (result, usage)}.

    dim_resources: [(dim, resources), ...] for each enabled agentic dimension.
    """
    # Build one prompt with all catalogs
    sections = []
    for dim, resources in dim_resources:
        if dim == "memory":
            catalog = _build_memory_catalog(resources)
            limit = "0-2 files per tier (profile/global/project)"
        else:
            catalog = "\n".join(
                f"- {r['name']}: {r['description']} [id={r['id']}]"
                for r in resources
            )
            limit = "0-3 items"
        sections.append(f"## {dim} catalog ({limit}):\n{catalog}")

    prompt_parts = ["\n\n".join(sections)]
    if context:
        prompt_parts.append(f"\nRecent conversation:\n{context}")
    prompt_parts.append(f"\nQuery: {query}")
    agentic_prompt = "\n".join(prompt_parts)

    dim_schemas = {}
    for dim, _ in dim_resources:
        dim_schemas[dim] = _MEMORY_SCHEMA if dim == "memory" else _ITEMS_SCHEMA
    merged_output_format = {
        "type": "json_schema",
        "schema": {
            "type": "object",
            "properties": dim_schemas,
            "required": list(dim_schemas.keys()),
        },
    }

    parsed, usage = await call_sdk_haiku(
        agentic_prompt, MERGED_SYSTEM_PROMPT, merged_output_format,
        model=model, max_budget_usd=None, effort=effort,
    )

    if not parsed:
        return {}

    # Build resolvers for memory dim (post-parse path normalization)
    memory_resolvers = {}
    for d, res in dim_resources:
        if d == "memory":
            memory_resolvers[d] = _build_memory_resolver(res)

    # Parse per-dimension results
    results = {}
    for dim, _ in dim_resources:
        dim_data = parsed.get(dim, {})
        if dim == "memory":
            # New tiered schema: merge per-tier lists, capping each at 2,
            # resolving any identifier variant back to canonical paths.
            resolver = memory_resolvers[dim]
            files = []
            for tier in _MEMORY_TIERS:
                for ident in dim_data.get(tier, [])[:2]:
                    resolved = resolver.get(ident) or _resolve_loose(ident, resolver)
                    if resolved:
                        files.append(resolved)
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


def recall_embedding_generic(dim, resources, query, socket_path, top_k, threshold, input_granularity="title_desc"):
    """Embedding search over resource descriptions via daemon."""
    resource_data = []
    for r in resources:
        if input_granularity == "full" and r.get("content_path") and os.path.isfile(r["content_path"]):
            with open(r["content_path"]) as f:
                desc = f.read()[:1000]
        else:
            desc = r["description"]
        resource_data.append({"name": r["name"], "description": desc, "id": r["id"]})
    response = _query_daemon(socket_path, {
        "type": "search_descriptions",
        "query": query,
        "resources": resource_data,
        "top_k": top_k,
        "threshold": threshold,
    })
    assert response["status"] == "ok", f"daemon error: {response.get('error')}"
    results = response["results"]
    if not results:
        return None
    return {
        "type": "recommendations",
        "dim": dim,
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
    """Start embedding daemon if not already running.

    Uses a lock file to prevent two concurrent hook calls from each
    starting a daemon.
    """
    if os.path.exists(socket_path):
        return
    lock_path = socket_path + ".starting"
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
    except FileExistsError:
        return  # another process is already starting the daemon
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
    try:
        os.unlink(lock_path)
    except OSError:
        pass
