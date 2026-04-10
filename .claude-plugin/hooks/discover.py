"""Resource discovery for each recall dimension.

Each discover_* function returns a list of dicts with uniform schema:
    [{"name": str, "description": str, "id": str}, ...]

- name: human-readable display name
- description: what this resource does (used for agentic/embedding matching)
- id: unique identifier (file path for memory, skill name for skills, etc.)
"""

import json
import os

from constants import BUILTIN_AGENTS, BUILTIN_SKILLS, DEFERRED_TOOLS
from utils import parse_frontmatter as _parse_frontmatter, HOME


# -- Memory ------------------------------------------------------------------


def discover_memory(cwd):
    """Discover memory files from project and global memory directories."""
    data_dir = os.environ.get(
        "CLAUDE_PLUGIN_DATA",
        os.path.join(HOME, ".claude/plugins/data/memory-recall-memory-recall"),
    )
    proj_mem_dir, global_mem_dir = _compute_memory_dirs(cwd, data_dir)

    entries = []
    for mem_dir in [proj_mem_dir, global_mem_dir]:
        if not os.path.isdir(mem_dir):
            continue
        for fname in sorted(os.listdir(mem_dir)):
            if not fname.endswith(".md") or fname == "MEMORY.md":
                continue
            path = os.path.join(mem_dir, fname)
            fm = _parse_frontmatter(path)
            entries.append({
                "name": fm.get("name", fname),
                "description": fm.get("description", ""),
                "id": path,
            })
    return entries, proj_mem_dir, global_mem_dir


def _compute_memory_dirs(cwd, data_dir):
    sanitized = cwd.replace("/", "-").lstrip("-")
    proj_candidates = [
        os.path.join(HOME, ".claude", "projects", f"-{sanitized}", "memory"),
        os.path.join(HOME, ".claude", "projects", sanitized, "memory"),
    ]
    proj_mem_dir = next(
        (p for p in proj_candidates if os.path.isdir(p)),
        proj_candidates[0],
    )
    global_mem_dir = os.path.join(data_dir, "global-memory")
    return proj_mem_dir, global_mem_dir


# -- Skills -------------------------------------------------------------------


def discover_skills():
    """Discover skills from plugin cache + hardcoded built-in list."""
    entries = []
    seen = set()

    # 1. Scan plugin cache for installed plugin skills
    cache_dir = os.path.join(HOME, ".claude", "plugins", "cache")
    if os.path.isdir(cache_dir):
        for marketplace in os.listdir(cache_dir):
            mp_dir = os.path.join(cache_dir, marketplace)
            if not os.path.isdir(mp_dir):
                continue
            for plugin in os.listdir(mp_dir):
                plugin_dir = os.path.join(mp_dir, plugin)
                if not os.path.isdir(plugin_dir):
                    continue
                # Find the latest version directory or direct skills dir
                skills_dir = _find_versioned_subdir(plugin_dir, "skills")
                if not skills_dir:
                    continue
                for skill_name in os.listdir(skills_dir):
                    skill_md = os.path.join(skills_dir, skill_name, "SKILL.md")
                    if not os.path.isfile(skill_md):
                        continue
                    fm = _parse_frontmatter(skill_md)
                    name = fm.get("name", skill_name)
                    if name in seen:
                        continue
                    seen.add(name)
                    entries.append({
                        "name": name,
                        "description": fm.get("description", ""),
                        "id": name,
                        "content_path": skill_md,
                    })

    # 2. Merge hardcoded built-in skills (skip if already discovered)
    for skill in BUILTIN_SKILLS:
        if skill["name"] not in seen:
            entries.append({"name": skill["name"], "description": skill["description"], "id": skill["name"]})

    return entries


def _find_versioned_subdir(plugin_dir, subdir_name):
    """Find a subdirectory within a plugin, handling versioned layouts."""
    direct = os.path.join(plugin_dir, subdir_name)
    if os.path.isdir(direct):
        return direct
    versions = []
    for entry in os.listdir(plugin_dir):
        entry_path = os.path.join(plugin_dir, entry, subdir_name)
        if os.path.isdir(entry_path):
            versions.append((entry, entry_path))
    if versions:
        versions.sort(reverse=True)
        return versions[0][1]
    return None


# -- Tools (MCP + Deferred) ---------------------------------------------------


def discover_tools():
    """Discover MCP servers from settings.json + plugin MCP defs + hardcoded deferred tools."""
    entries = []
    seen = set()

    # 1. Read MCP servers from settings.json
    settings_path = os.path.join(HOME, ".claude", "settings.json")
    if os.path.isfile(settings_path):
        with open(settings_path) as f:
            settings = json.load(f)
        mcp_servers = settings.get("mcpServers", {})
        for server_name, server_config in mcp_servers.items():
            desc = server_config.get("description", f"MCP server: {server_name}")
            if isinstance(desc, dict):
                desc = str(desc)
            seen.add(server_name)
            entries.append({
                "name": f"MCP:{server_name}",
                "description": desc,
                "id": f"mcp:{server_name}",
            })

    # 2. Scan plugin cache for plugin-defined MCP servers (.mcp.json files)
    cache_dir = os.path.join(HOME, ".claude", "plugins", "cache")
    if os.path.isdir(cache_dir):
        for marketplace in os.listdir(cache_dir):
            mp_dir = os.path.join(cache_dir, marketplace)
            if not os.path.isdir(mp_dir):
                continue
            for plugin in os.listdir(mp_dir):
                plugin_dir = os.path.join(mp_dir, plugin)
                if not os.path.isdir(plugin_dir):
                    continue
                mcp_json = _find_mcp_json(plugin_dir)
                if not mcp_json:
                    continue
                with open(mcp_json) as f:
                    mcp_defs = json.load(f)
                # Also read plugin.json for description
                plugin_json_path = _find_plugin_json(plugin_dir)
                plugin_desc = ""
                if plugin_json_path:
                    with open(plugin_json_path) as f:
                        plugin_desc = json.load(f).get("description", "")
                for server_name in mcp_defs:
                    if server_name in seen:
                        continue
                    seen.add(server_name)
                    desc = plugin_desc or f"MCP server: {server_name} (plugin: {plugin})"
                    entries.append({
                        "name": f"MCP:{server_name}",
                        "description": desc,
                        "id": f"mcp:{server_name}",
                    })

    # 3. Hardcoded deferred tools
    for tool in DEFERRED_TOOLS:
        if tool["name"] not in seen:
            entries.append({
                "name": tool["name"],
                "description": tool["description"],
                "id": f"deferred:{tool['name']}",
            })

    return entries


def _find_plugin_json(plugin_dir):
    """Find .claude-plugin/plugin.json within a plugin directory."""
    direct = os.path.join(plugin_dir, ".claude-plugin", "plugin.json")
    if os.path.isfile(direct):
        return direct
    for entry in sorted(os.listdir(plugin_dir), reverse=True):
        candidate = os.path.join(plugin_dir, entry, ".claude-plugin", "plugin.json")
        if os.path.isfile(candidate):
            return candidate
    return None


def _find_mcp_json(plugin_dir):
    """Find .mcp.json within a plugin directory (may be versioned)."""
    direct = os.path.join(plugin_dir, ".mcp.json")
    if os.path.isfile(direct):
        return direct
    # Check versioned subdirs, skip orphaned ones
    for entry in sorted(os.listdir(plugin_dir), reverse=True):
        entry_path = os.path.join(plugin_dir, entry)
        if not os.path.isdir(entry_path):
            continue
        if os.path.exists(os.path.join(entry_path, ".orphaned_at")):
            continue
        candidate = os.path.join(entry_path, ".mcp.json")
        if os.path.isfile(candidate):
            return candidate
    return None


# -- Agents -------------------------------------------------------------------


def discover_agents(cwd):
    """Discover agent types from .claude/agents/ dirs + hardcoded built-in list."""
    entries = []
    seen = set()

    # 1. Scan .claude/agents/ directories (project, user, global)
    agent_dirs = [
        os.path.join(cwd, ".claude", "agents"),
        os.path.join(HOME, ".claude", "agents"),
    ]
    for agent_dir in agent_dirs:
        if not os.path.isdir(agent_dir):
            continue
        for fname in os.listdir(agent_dir):
            if not fname.endswith(".md"):
                continue
            path = os.path.join(agent_dir, fname)
            fm = _parse_frontmatter(path)
            name = fm.get("name", fname.replace(".md", ""))
            if name in seen:
                continue
            seen.add(name)
            entries.append({
                "name": name,
                "description": fm.get("description", fm.get("whenToUse", "")),
                "id": name,
                "content_path": path,
            })

    # 2. Scan plugin cache for plugin-defined agents
    cache_dir = os.path.join(HOME, ".claude", "plugins", "cache")
    if os.path.isdir(cache_dir):
        for marketplace in os.listdir(cache_dir):
            mp_dir = os.path.join(cache_dir, marketplace)
            if not os.path.isdir(mp_dir):
                continue
            for plugin in os.listdir(mp_dir):
                plugin_dir = os.path.join(mp_dir, plugin)
                if not os.path.isdir(plugin_dir):
                    continue
                agents_dir = _find_versioned_subdir(plugin_dir, "agents")
                if not agents_dir:
                    continue
                for fname in os.listdir(agents_dir):
                    if not fname.endswith(".md"):
                        continue
                    path = os.path.join(agents_dir, fname)
                    fm = _parse_frontmatter(path)
                    name = fm.get("name", fname.replace(".md", ""))
                    if name in seen:
                        continue
                    seen.add(name)
                    entries.append({
                        "name": name,
                        "description": fm.get("description", fm.get("whenToUse", "")),
                        "id": name,
                        "content_path": path,
                    })

    # 3. Hardcoded built-in agents
    for agent in BUILTIN_AGENTS:
        if agent["name"] not in seen:
            entries.append({"name": agent["name"], "description": agent["description"], "id": agent["name"]})

    return entries


