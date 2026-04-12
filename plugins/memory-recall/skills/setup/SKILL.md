---
name: setup
description: "Interactive setup for memory-recall plugin. Configure recall dimensions (memory, skills, tools, agents) and backends (reminder/agentic/embedding). Use when the user says /setup or wants to configure the memory-recall plugin."
user_invocable: true
---

# Memory-Recall Plugin Setup (v4.1.0)

Guide the user through configuring the memory-recall plugin. 4 subsystems, each with enabled/model/effort/async.

## Configuration location

Plugin options in `~/.claude/settings.json` under:

```json
{
  "pluginConfigs": {
    "memory-recall@memory-recall": {
      "options": {
        "recall_memory_backend": "reminder",
        "recall_skills_backend": "off",
        "recall_tools_backend": "off",
        "recall_agents_backend": "off",
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

Recommend starting with `recall_memory_backend: agentic` and one or two other dimensions on `agentic`. All agentic dimensions run in parallel (~5s total latency regardless of count).

## Step 3: Backend-specific options

**If any dimension uses `agentic`:**
- `recall_model`: Which model? Options: `haiku` (fast/cheap, recommended), `sonnet` (smarter but costlier), `opus` (most capable, most expensive). Default: `haiku`.
- `recall_agentic_mode`: `parallel` (one call per dimension, better quality) or `merged` (single call for all, faster). Default: `parallel`.

**Per-dimension granularity (ask if user wants fine-grained control):**

Each dimension has independent input and output granularity:
- `recall_{dim}_input`: What the selection backend (Haiku/embedding) sees when choosing resources.
- `recall_{dim}_output`: What gets injected into the main model's context after selection.

Both accept `title_desc` or `full`:

| Value | As input (what selector sees) | As output (what main model gets) |
|-------|-------------------------------|----------------------------------|
| `title_desc` | Resource name + one-line description | Name + reason (from Haiku's recommendation) |
| `full` | File content (truncated to 500 chars) | Full file content injected into context |

Concrete examples:
- `recall_memory_output: full` (default) = selected memory files are **read in full** and injected
- `recall_memory_output: title_desc` = only file paths injected, model must Read files itself
- `recall_skills_output: full` = selected skills' SKILL.md content injected
- `recall_skills_output: title_desc` (default) = only skill name + recommendation reason

Defaults: all `recall_{dim}_input` default to `title_desc`. `recall_memory_output` defaults to `full`, all others to `title_desc`.

All 8 configs: `recall_memory_input`, `recall_memory_output`, `recall_skills_input`, `recall_skills_output`, `recall_tools_input`, `recall_tools_output`, `recall_agents_input`, `recall_agents_output`.

**N/A combinations** (setting has no effect):

| Backend | `{dim}_input` | `{dim}_output` |
|---------|---------------|----------------|
| `reminder` | N/A (no selection step, returns everything) | Works |
| `agentic` | Works | Works |
| `embedding` (memory) | N/A (daemon always searches full content) | Works |
| `embedding` (non-memory) | Works | Works |

**If any dimension uses `embedding`:**
- `recall_embedding_model`: HuggingFace model name. Default: `intfloat/multilingual-e5-small`.
- `recall_embedding_python`: Path to Python with sentence-transformers. Default: `~/miniconda3/envs/memory-recall/bin/python`.
- `recall_embedding_threshold`: Cosine similarity threshold (0-1). Default: `0.85`.
- `recall_embedding_top_k`: Max results per dimension (1-10). Default: `3`.
- `recall_embedding_device`: `cpu` or `cuda`. Default: `cpu`.

**Shared recall options (ask for any non-off dimension):**
- `recall_context_messages`: Recent conversation messages for search context (0-20). Default: `5`.
- `recall_context_max_chars`: Max chars of context (0-10000). Default: `2000`.
- `recall_max_content_chars`: Global cap on total injected content across all dimensions (1000-10000). Default: `9000`. This is a shared budget -- dimensions are processed in order (memory, skills, tools, agents), and each dimension's output decrements the remaining budget. If memory uses 7000 chars, only 2000 chars remain for the other dimensions.

**Per-subsystem model selection:**

Each subsystem has its own model config. Ask if user wants different models for different subsystems:
- `recall_model`: Model for recall agentic calls. Default: `haiku`.
- `memory_save_model`: Model for memory save analysis. Default: `haiku`.
- `pair_programmer_model`: Model for pair programmer evaluation. Default: `haiku`.
- `curator_model`: Model for curator consolidation. Default: `haiku`.

**Distiller option:**
- `distiller_enabled`: Enable profile distillation in curator Phase 6. Default: `true`. When enabled, curator extracts user thinking patterns into profile files (`DATA_DIR/profile/`) that the pair programmer reads for evaluation.

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

## Troubleshooting

If something isn't working after setup, tell the user to run `/diagnose` for interactive troubleshooting.

## StatusLine Integration (Hook Visibility)

The plugin writes hook execution status to JSON files. To see this status in your Claude Code statusLine, integrate the reading logic into your statusLine script.

### What it does

Each hook (recall, memory_save, pair_programmer) writes its execution state (running/done/error) to:
```
~/.claude/plugins/data/memory-recall-memory-recall/status/<session_id>/<hook>.json
```

Your statusLine script reads these files and appends one line per hook showing its current state, timing, cost, and summary.

### How to integrate

Read the user's existing statusLine configuration from `~/.claude/settings.json` (the `statusLine.command` field). Then read the script it points to and append the plugin status reading logic.

**Key requirements for the reading logic:**
1. Extract `session_id` from stdin JSON (`.session_id`)
2. Read all `*.json` files from `~/.claude/plugins/data/memory-recall-memory-recall/status/<session_id>/`
3. For each file, parse the JSON and format one line:
   - Main agent (no `agent_id`): always display
   - Subagent (has `agent_id`): only display if `state == "running"`
4. Color coding: green for done, yellow for running, red for error/timeout
5. Stale detection: if state is "running" and `started_at` is older than the `timeout_s` field value (default 60s), show as "timeout"
6. The script MUST exit with code 0 (add `exit 0` at end). CC discards output on non-zero exit.
7. Plugin status lines appear after the user's existing statusLine output.

### Status file fields (read these via jq)

| Field | Purpose |
|---|---|
| `hook` | hook name label |
| `state` | `done` / `running` / `error` / `timeout` |
| `agent_id`, `agent_type` | subagent identifiers (empty for main agent) |
| `summary` | one-line status text |
| `elapsed_s` | duration of last invocation |
| `cost_usd` | cost of **last invocation** |
| `cumulative_cost_usd` | **total session cost** for this hook (sums across all invocations, skipped/running excluded) |
| `model` | model used |
| `started_at`, `finished_at` | timestamps |
| `total_runs`, `skipped_count` | counters |
| `timeout_s` | per-hook timeout for stale detection |
| `cooldown_until` | unix ts; when set and in future, hook is in cooldown |

### Default display format (use this as the template)

Render each hook as one line with:
- **Annotation** (in parentheses, dim gray): `model, Σ$cumulative_cost_usd[, state_tag]` — model and session-cumulative cost live here. State tags (`running Ns`, `cooldown Ns`, `timeout`, `error`) are appended conditionally.
- **Content** (after `:`): `summary | elapsed_s | $cost_usd` — the three parts are separated by dim `|` bars. This is the format the user expects as the default.

```
pair_programmer (haiku, Σ$2.5576): ok | 201.71s | $0.1438
curator (haiku, Σ$0.7234, cooldown 1800s): 24 files | 3.21s | $0.1110
memory_save (haiku, Σ$0.4521): add profile_x.md | 1.23s | $0.0042
recall (haiku, Σ$0.0891, running 5s): 3 items recalled | 0.42s | $0.003
```

Color the hook label + annotation block based on `state`: green for done, yellow for running/cooldown, red for error/timeout.

### If user has no statusLine configured

Create a new script at `~/.claude/statusline-memory-recall.sh` that reads the status files and outputs them per the default format above. Then set `statusLine.command` in `~/.claude/settings.json` to `bash ~/.claude/statusline-memory-recall.sh`.
