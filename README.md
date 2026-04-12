# Memory Recall Plugin

Multi-dimension context recall for Claude Code. Automatically surfaces relevant memories, skills, tools, and agent types on every user message.

## Features

- **4 hooks**: recall (UserPromptSubmit/SubagentStart), memory save (Stop), pair programmer (PostToolUse), curator (Stop)
- **4 recall dimensions**: memory files, skills, tools (MCP + deferred), agent types
- **3 backends per dimension**: reminder (zero-cost), agentic (Haiku selection), embedding (local RAG)
- **Pair programmer**: evaluates agent actions against user preferences, past experience, strategic direction
- **Memory save**: auto-saves conversation knowledge to Memory Bank after each turn
- **Configurable sync/async**: each hook can run synchronously or asynchronously
- **4 skills**: `/dream` (consolidation), `/remember` (quick save), `/setup` (config), `/diagnose` (troubleshooting)

## Installation

```bash
claude plugin marketplace add t2ance/memory-recall-plugin
claude plugin install memory-recall@memory-recall
```

Then configure via `/setup` or manually in `~/.claude/settings.json`:

```json
{
  "pluginConfigs": {
    "memory-recall@memory-recall": {
      "options": {
        "recall_memory_backend": "agentic",
        "recall_skills_backend": "agentic",
        "recall_tools_backend": "agentic",
        "recall_agents_backend": "agentic"
      }
    }
  }
}
```

Each dimension backend accepts: `"off"`, `"reminder"`, `"agentic"`, or `"embedding"`.

### Configuration Reference

**Recall options:**

| Option | Description | Default |
|--------|-------------|---------|
| `recall_enabled` | Enable recall on UserPromptSubmit/SubagentStart | `true` |
| `recall_model` | Model for agentic backend: `haiku`/`sonnet`/`opus` | `haiku` |
| `recall_effort` | Effort for recall calls: `low` or `""` | `low` |
| `recall_async` | Run recall hook asynchronously | `false` |
| `recall_agentic_mode` | `parallel` (one call/dim) or `merged` (single call) | `parallel` |
| `recall_context_messages` | Recent messages for search context | `5` |
| `recall_context_max_chars` | Max chars of conversation context | `2000` |
| `recall_max_content_chars` | Global cap on total injected content | `9000` |
| `recall_{dim}_backend` | Backend per dimension: `off`, `reminder`, `agentic`, `embedding` | `agentic` |
| `recall_{dim}_input` | What selector sees: `title_desc` or `full` | `title_desc` |
| `recall_{dim}_output` | What gets injected: `title_desc` or `full` | `full` (memory), `title_desc` (others) |
| `recall_embedding_model` | HuggingFace model name | `intfloat/multilingual-e5-small` |
| `recall_embedding_python` | Python path with sentence-transformers | `~/miniconda3/envs/memory-recall/bin/python` |
| `recall_embedding_threshold` | Cosine similarity threshold | `0.85` |
| `recall_embedding_top_k` | Max results per dimension | `3` |
| `recall_embedding_device` | `cpu` or `cuda` | `cpu` |

Where `{dim}` is one of: `memory`, `skills`, `tools`, `agents`.

**Memory save options:**

| Option | Description | Default |
|--------|-------------|---------|
| `memory_save_enabled` | Enable auto-save after each turn | `true` |
| `memory_save_model` | Model for save analysis | `haiku` |
| `memory_save_effort` | Effort level for save calls | `""` |
| `memory_save_async` | Run save hook asynchronously | `true` |
| `memory_save_targets` | `native` (project), `global`, or `both` | `native` |
| `memory_save_context_turns` | Conversation turns for analysis | `3` |

**Pair programmer options:**

| Option | Description | Default |
|--------|-------------|---------|
| `pair_programmer_enabled` | Enable pair programmer | `true` |
| `pair_programmer_model` | Model for evaluation | `haiku` |
| `pair_programmer_effort` | Effort level | `""` |
| `pair_programmer_async` | Run pair programmer asynchronously | `true` |
| `pair_programmer_sample_rate` | Probability of evaluating each tool call (0-1) | `1.0` |
| `pair_programmer_cooldown_s` | Min seconds between evaluations | `120` |
| `pair_programmer_context_messages` | Recent messages for trajectory | `5` |
| `pair_programmer_context_max_chars` | Max conversation context chars | `3000` |
| `pair_programmer_max_tool_input_chars` | Max tool input chars in trajectory | `2000` |
| `pair_programmer_max_tool_output_chars` | Max tool output chars in trajectory | `1000` |

**Curator options:**

| Option | Description | Default |
|--------|-------------|---------|
| `curator_enabled` | Enable periodic memory consolidation | `true` |
| `curator_model` | Model for curator analysis | `haiku` |
| `curator_effort` | Effort level for curator calls | `""` |
| `curator_async` | Run curator asynchronously | `true` |
| `curator_cooldown_h` | Min hours between curator runs | `1` |

**Distiller options:**

| Option | Description | Default |
|--------|-------------|---------|
| `distiller_enabled` | Enable profile distillation (curator Phase 6) | `true` |

## How It Works

### Recall (UserPromptSubmit / SubagentStart)

On every user message and sub-agent spawn, the hook:

1. **Discovers** available resources per enabled dimension (file scan + hardcoded fallback)
2. **Recalls** relevant items using the configured backend (parallel for agentic)
3. **Injects** results as `additionalContext` into the model's context

Sub-agents and teammates also receive recall context. On `SubagentStart`, the hook extracts the parent agent's prompt from the transcript and runs the full recall pipeline.

### Memory Save (Stop)

After each assistant turn, the hook:

1. Extracts recent conversation turns from transcript
2. Calls Haiku to decide what knowledge to persist (ADD/UPDATE/DELETE/NOOP)
3. Writes memory files and updates MEMORY.md index

Config: `memory_save_enabled` (default true), `memory_save_model` (default haiku), `memory_save_targets` (native/global/both), `memory_save_context_turns` (default 3).

### Pair Programmer (PostToolUse)

After action tools (Edit/Write/Bash/NotebookEdit), the hook:

1. Builds trajectory from current tool call + recent conversation
2. Passes three memory directory paths (profile, global memory, project memory) to a Haiku agent loop
3. Haiku uses `Read` / `Grep` / `Glob` tools to fetch only the memory files relevant to the current action (agent-centric retrieval, no pre-loading)
4. Haiku evaluates across 3 dimensions (preference alignment, experience recall, strategic oversight) and emits structured output
5. Outputs one of 3 states:
   - `ok`: nothing to flag
   - `suggest`: soft suggestion via `additionalContext`, main agent prefixes response with `[PP preference]`
   - `break`: strongly-worded directive to stop and ask user for clarification, main agent prefixes with `[PP break]`

Enabled by default. 120s cooldown between evaluations. Profile files are produced by the curator's DISTILL phase and contain distilled user thinking patterns; Haiku reads them on-demand via `Read`/`Grep` just like any other memory file.

### Async Support

Each hook can run synchronously (blocking) or asynchronously (non-blocking):

| Option | Default | Effect |
|--------|---------|--------|
| `recall_async` | `false` | Recall must usually be sync (context needed before agent responds) |
| `memory_save_async` | `true` | Save runs in background after turn completes |
| `pair_programmer_async` | `true` | Pair programmer feedback arrives at next tool call |
| `curator_async` | `true` | Curator runs in background after session |

### Backends

| Backend | How | Cost | Latency |
|---------|-----|------|---------|
| `reminder` | Lists all resources, model decides | Free | ~0s |
| `agentic` | Haiku selects top relevant items | ~$0.005/dim | ~10s (parallel) |
| `embedding` | Local vector similarity via daemon | Free after setup | <1s |

### Dimensions

| Dimension | What it recalls | Discovery |
|-----------|----------------|-----------|
| `memory` | Memory topic files (project + global) | Scans `~/.claude/projects/*/memory/` and plugin data dir |
| `skills` | Slash commands (plugin + built-in) | Scans plugin cache + hardcoded built-in list |
| `tools` | MCP servers + CC deferred tools | Reads plugin `.mcp.json` + hardcoded deferred tools |
| `agents` | Agent types (custom + built-in) | Scans `.claude/agents/` + plugin agents + hardcoded |

## Logging

Every hook invocation logs to `~/.claude/plugins/data/memory-recall-memory-recall/recall.jsonl` (indented JSONL):

```json
{
  "ts": "2026-04-07T17:21:27",
  "query": "help me debug a training loss spike",
  "dimensions": {"memory": "agentic", "skills": "agentic"},
  "discovered": {"memory": 12, "skills": 36},
  "results": [
    {"dim": "memory", "status": "ok", "files": ["..."]},
    {"dim": "skills", "status": "ok", "items": ["wandb-monitor", "training-monitor"]}
  ],
  "per_dim_s": {"memory": 8.91, "skills": 8.02},
  "per_dim_usage": {
    "memory": {"input_tokens": 1160, "output_tokens": 691, "cost_usd": 0.004615},
    "skills": {"input_tokens": 1660, "output_tokens": 536, "cost_usd": 0.00434}
  },
  "haiku_cost_usd": 0.008955,
  "elapsed_s": 8.92,
  "output": "..."
}
```

## Skills

- **`/dream`** -- Full memory consolidation across project memory, global memory, and `~/.claude/CLAUDE.md`
- **`/remember`** -- Quick save from current conversation to memory
- **`/setup`** -- Interactive configuration wizard for dimensions and backends
- **`/diagnose`** -- Interactive troubleshooting (11 scenarios, runs targeted checks)

## Embedding Backend Setup

If using the `embedding` backend for any dimension:

```bash
conda create -n memory-recall python=3.11 -y
conda run -n memory-recall pip install torch sentence-transformers numpy
```

The embedding daemon starts automatically on first use. Configure model and device via `/setup`.

## Code Structure

```
hooks/
  recall.py             # Recall hook: parallel dispatch + merge + inject
  memory_save.py        # Memory save hook: Haiku CRUD on conversation knowledge
  pair_programmer.py    # Pair programmer hook: 3-dimension evaluation + TL;DR
  memory_curator.py     # Curator hook: periodic memory consolidation + profile distillation
  discover.py           # Resource discovery (file scan + hardcoded fallback)
  backends.py           # 3 recall backend implementations
  utils.py              # Shared: Agent SDK wrapper, config, logging, async mode, profile dir
  constants.py          # Hardcoded built-in skills, deferred tools, agent types
  embedding_daemon.py   # Local RAG daemon (sentence-transformers)
  hooks.json            # Hook registration (4 hooks)
skills/
  dream/SKILL.md        # Memory consolidation
  remember/SKILL.md     # Quick save
  setup/SKILL.md        # Interactive config
  diagnose/SKILL.md     # Interactive troubleshooting
```

## Troubleshooting

Run `/diagnose` for interactive troubleshooting. It covers: hook not triggering, empty results, agentic timeouts, granularity issues, cache sync problems, SubagentStart issues, embedding daemon errors, and more.

Quick check -- read the last recall log entry:

```bash
tail -30 ~/.claude/plugins/data/memory-recall-memory-recall/recall.jsonl
```

## Updating

Must update the marketplace first, then re-install:

```bash
claude plugin marketplace update memory-recall
claude plugin install memory-recall@memory-recall
```

`plugin install` copies from the local marketplace cache, not from GitHub directly. Without `marketplace update` first, it re-installs the old cached version.

Note: `claude plugin update` does NOT work for same-version code changes -- it compares the `version` field in `plugin.json` and skips if unchanged.

After updating, run `/reload-plugins` to refresh without restarting the session.
