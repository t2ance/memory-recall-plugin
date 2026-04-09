---
name: setup
description: "Interactive setup for memory-recall plugin. Configure recall dimensions (memory, skills, tools, agents) and backends (reminder/agentic/embedding). Use when the user says /setup or wants to configure the memory-recall plugin."
user_invocable: true
---

# Memory-Recall Plugin Setup (v3.0)

Guide the user through configuring the memory-recall plugin. 4 hooks, 4 recall dimensions x 3 backends, pair programmer, async support.

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
- `model`: Which model? Options: `haiku` (fast/cheap, recommended), `sonnet` (smarter but costlier), `opus` (most capable, most expensive). Default: `haiku`.
- `agentic_mode`: `parallel` (one call per dimension, better quality) or `merged` (single call for all, faster). Default: `parallel`.

**Per-dimension granularity (ask if user wants fine-grained control):**

Each dimension has independent input and output granularity:
- `{dim}_input`: What the selection backend (Haiku/embedding) sees when choosing resources.
- `{dim}_output`: What gets injected into the main model's context after selection.

Both accept `title_desc` or `full`:

| Value | As input (what selector sees) | As output (what main model gets) |
|-------|-------------------------------|----------------------------------|
| `title_desc` | Resource name + one-line description | Name + reason (from Haiku's recommendation) |
| `full` | File content (truncated to 500 chars) | Full file content injected into context |

Concrete examples:
- `memory_output: full` (default) = selected memory files are **read in full** and injected
- `memory_output: title_desc` = only file paths injected, model must Read files itself
- `skills_output: full` = selected skills' SKILL.md content injected
- `skills_output: title_desc` (default) = only skill name + recommendation reason

Defaults: all `{dim}_input` default to `title_desc`. `memory_output` defaults to `full`, all others to `title_desc`.

All 8 configs: `memory_input`, `memory_output`, `skills_input`, `skills_output`, `tools_input`, `tools_output`, `agents_input`, `agents_output`.

**N/A combinations** (setting has no effect):

| Backend | `{dim}_input` | `{dim}_output` |
|---------|---------------|----------------|
| `reminder` | N/A (no selection step, returns everything) | Works |
| `agentic` | Works | Works |
| `embedding` (memory) | N/A (daemon always searches full content) | Works |
| `embedding` (non-memory) | Works | Works |

**If any dimension uses `embedding`:**
- `embedding_model`: HuggingFace model name. Default: `intfloat/multilingual-e5-small`.
- `embedding_python`: Path to Python with sentence-transformers. Default: `~/miniconda3/envs/memory-recall/bin/python`.
- `embedding_threshold`: Cosine similarity threshold (0-1). Default: `0.85`.
- `embedding_top_k`: Max results per dimension (1-10). Default: `3`.
- `embedding_device`: `cpu` or `cuda`. Default: `cpu`.

**Shared options (ask for any non-off dimension):**
- `context_messages`: Recent conversation messages for search context (0-20). Default: `5`.
- `context_max_chars`: Max chars of context (0-10000). Default: `2000`.
- `max_content_chars`: Global cap on total injected content across all dimensions (1000-10000). Default: `9000`. This is a shared budget -- dimensions are processed in order (memory, skills, tools, agents), and each dimension's output decrements the remaining budget. If memory uses 7000 chars, only 2000 chars remain for the other dimensions.

Only ask about options the user wants to customize. Defaults are fine for most users.

## Step 4: Memory Save Configuration

The memory save hook (Stop) auto-saves conversation knowledge after each turn.

| Option | Description | Default |
|--------|-------------|---------|
| `auto_save_enabled` | Enable/disable auto-save | `true` |
| `auto_save_targets` | Where to save: `native` (project memory), `global`, or `both` | `native` |
| `auto_save_context_turns` | How many recent conversation turns to analyze | `3` |
| `auto_save_effort` | Effort level for Haiku analysis: `""` (default) or `low` | `""` |

## Step 5: Pair Programmer Configuration

The pair programmer evaluates agent actions (Edit/Write/Bash) against user preferences and past experience. Default off.

| Option | Description | Default |
|--------|-------------|---------|
| `pp_enabled` | Master switch | `false` |
| `pp_model` | Model for evaluation: `haiku` (fast/cheap) or `sonnet` (smarter) | `haiku` |
| `pp_sample_rate` | Probability of evaluating each tool call (0.0-1.0) | `1.0` |
| `pp_cooldown_s` | Min seconds between evaluations (prevents rapid-fire) | `0` |
| `pp_context_messages` | Recent conversation messages for trajectory | `5` |
| `pp_context_max_chars` | Max chars of conversation context | `3000` |
| `pp_effort` | Effort level for evaluation calls | `""` |
| `pp_max_tool_input_chars` | Max chars of tool input in trajectory | `2000` |
| `pp_max_tool_output_chars` | Max chars of tool output in trajectory | `1000` |
| `pp_max_recall_files` | Max memory files to recall for context | `5` |
| `pp_max_memory_file_chars` | Max chars per recalled memory file | `2000` |

Recommend starting with `pp_enabled: true` with defaults.

## Step 6: Async Configuration

Each hook can run synchronously (blocking) or asynchronously (non-blocking).

| Option | Default | Explanation |
|--------|---------|-------------|
| `recall_async` | `false` | Recall usually needs sync -- context must be injected before agent responds |
| `memory_save_async` | `true` | Save runs after turn; safe to background |
| `pp_async` | `true` | Pair programmer feedback arrives at next tool call instead of blocking current one |

Only change these if you have a specific reason. Defaults are good for most users.

## Step 7: Apply config

Read `~/.claude/settings.json`, merge new options into `pluginConfigs.memory-recall@memory-recall.options`, write back. Preserve all other settings. Create keys if missing.

## Step 8: Environment setup (embedding only)

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

## Step 9: Verify

**agentic:** `python3 -c "from claude_agent_sdk import query; print('OK')"`

**embedding:** Test daemon connectivity via socket query.

**reminder:** No verification needed.

## Step 10: Summary

Show configured dimensions and backends. Remind user to restart session or `/reload-plugins` for changes to take effect.

## Troubleshooting

If something isn't working after setup, tell the user to run `/diagnose` for interactive troubleshooting.
