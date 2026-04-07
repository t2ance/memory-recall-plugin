---
name: setup
description: "Interactive setup for memory-recall plugin. Configure recall dimensions (memory, skills, tools, agents) and backends (reminder/agentic/embedding). Use when the user says /setup or wants to configure the memory-recall plugin."
user_invocable: true
---

# Memory-Recall Plugin Setup (v3.0)

Guide the user through configuring the memory-recall plugin. 4 dimensions x 3 backends.

## Configuration location

Plugin options in `~/.claude/settings.json` under:

```json
{
  "pluginConfigs": {
    "memory-recall@memory-recall": {
      "options": {
        "memory": "reminder",
        "skills": "off",
        "tools": "off",
        "agents": "off",
        ...
      }
    }
  }
}
```

The hook reads these as `CLAUDE_PLUGIN_OPTION_*` env vars. Changes require session restart or `/reload-plugins`.

## Step 1: Read current config

Read `~/.claude/settings.json` and extract `pluginConfigs.memory-recall@memory-recall.options`. Show current settings. If missing, all at defaults.

## Step 2: Configure each dimension

Use AskUserQuestion to ask about each recall dimension. Each dimension can be independently set to one of 4 backends:

| Backend | Description | Requirements | Cost |
|---------|-------------|--------------|------|
| `off` | Disabled. No recall for this dimension. | None | Free |
| `reminder` | Lists all available resources. Agent decides what to use. | None | Free |
| `agentic` | Haiku selects the most relevant resources. Precise. | `claude-agent-sdk` | ~$0.003/dim/query |
| `embedding` | Local vector similarity search. Fast after setup. | conda env + sentence-transformers | Free after setup |

### Dimensions

| Dimension | What it recalls | Discovery |
|-----------|----------------|-----------|
| `memory` | Memory topic files from project + global memory dirs | Scans `~/.claude/projects/*/memory/` and global memory |
| `skills` | Slash commands (plugin skills + built-in skills) | Scans plugin cache + hardcoded built-in list |
| `tools` | MCP servers + CC deferred tools (WebFetch, WebSearch, etc.) | Reads settings.json mcpServers + hardcoded deferred tools |
| `agents` | Agent types (general-purpose, Explore, debugger, etc.) | Scans .claude/agents/ + plugin agents + hardcoded built-in |

Recommend starting with `memory: agentic` and one or two other dimensions on `agentic`. All agentic dimensions run in parallel (~5s total latency regardless of count).

## Step 3: Backend-specific options

**If any dimension uses `agentic`:**
- `model`: Which model? Options: `haiku` (fast/cheap, recommended), `sonnet` (smarter but costlier). Default: `haiku`.
- `agentic_mode`: `parallel` (one Haiku call per dimension, better quality) or `merged` (single call for all, faster). Default: `parallel`.

**Per-dimension granularity (ask if user wants fine-grained control):**

Each dimension has independent input and output granularity:
- `{dim}_input`: What the selection backend sees. `title_desc` (name+description) or `full` (file content). Default: `title_desc`.
- `{dim}_output`: What gets injected into main model. `title_desc` or `full` (file content). Default: `full` for memory, `title_desc` for others.

All 8 configs: `memory_input`, `memory_output`, `skills_input`, `skills_output`, `tools_input`, `tools_output`, `agents_input`, `agents_output`.

Note: `reminder` backend ignores `input` (it returns everything, no selection step). `embedding` memory ignores `input` (daemon always searches file content).

**If any dimension uses `embedding`:**
- `embedding_model`: HuggingFace model name. Default: `intfloat/multilingual-e5-small`.
- `embedding_python`: Path to Python with sentence-transformers. Default: `~/miniconda3/envs/memory-recall/bin/python`.
- `embedding_threshold`: Cosine similarity threshold (0-1). Default: `0.85`.
- `embedding_top_k`: Max results per dimension (1-10). Default: `3`.
- `embedding_device`: `cpu` or `cuda`. Default: `cpu`.

**Shared options (ask for any non-off dimension):**
- `context_messages`: Recent conversation messages for search context (0-20). Default: `5`.
- `context_max_chars`: Max chars of context (0-10000). Default: `2000`.
- `max_content_chars`: Global cap on total injected content across all dimensions (1000-10000). Default: `9000`.

Only ask about options the user wants to customize. Defaults are fine for most users.

## Step 4: Apply config

Read `~/.claude/settings.json`, merge new options into `pluginConfigs.memory-recall@memory-recall.options`, write back. Preserve all other settings. Create keys if missing.

## Step 5: Environment setup (embedding only)

If any dimension uses `embedding`:

1. **Create conda env** (skip if exists):
   ```bash
   conda create -n memory-recall python=3.11 -y
   ```

2. **Install dependencies**:
   ```bash
   conda run -n memory-recall pip install torch --index-url https://download.pytorch.org/whl/cpu
   conda run -n memory-recall pip install sentence-transformers numpy
   ```
   For `embedding_device: cuda`: `conda run -n memory-recall pip install torch` (with CUDA).

3. **Pre-download model**:
   ```bash
   conda run -n memory-recall python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('MODEL_NAME')"
   ```

4. **Start daemon** (it auto-starts on first use, but can pre-start):
   ```bash
   PLUGIN_DATA="${CLAUDE_PLUGIN_DATA:-$HOME/.claude/plugins/data/memory-recall-memory-recall}"
   EMBEDDING_MODEL="MODEL_NAME" EMBEDDING_DEVICE="DEVICE" nohup ~/miniconda3/envs/memory-recall/bin/python "$(find ~/.claude -path '*/memory-recall/hooks/embedding_daemon.py' | head -1)" >> "${PLUGIN_DATA}/daemon.log" 2>&1 &
   ```

## Step 6: Verify

**agentic:** `python3 -c "from claude_agent_sdk import query; print('OK')"`

**embedding:** Test daemon connectivity via socket query.

**reminder:** No verification needed.

## Step 7: Summary

Show configured dimensions and backends. Remind user to restart session or `/reload-plugins` for changes to take effect.
