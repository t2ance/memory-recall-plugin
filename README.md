# Memory Recall Plugin

Multi-dimension context recall for Claude Code. Automatically surfaces relevant memories, skills, tools, and agent types on every user message.

## Features

- **4 recall dimensions**: memory files, skills, tools (MCP + deferred), agent types
- **3 backends per dimension**: reminder (zero-cost), agentic (Haiku selection), embedding (local RAG)
- **Parallel execution**: all agentic calls run concurrently via Agent SDK
- **Structured logging**: JSONL with precise token/cost from Agent SDK ResultMessage
- **Auto-discovery**: scans plugin cache for skills/agents/MCP servers, falls back to hardcoded for CC built-ins
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
        "memory": "agentic",
        "skills": "agentic",
        "tools": "agentic",
        "agents": "agentic"
      }
    }
  }
}
```

Each dimension accepts: `"off"`, `"reminder"`, `"agentic"`, or `"embedding"`.

### Additional options

| Option | Description | Default |
|--------|-------------|---------|
| `agentic_mode` | `parallel` (one call/dim) or `merged` (single call) | `parallel` |
| `{dim}_input` | What selector sees: `title_desc` or `full` | `title_desc` |
| `{dim}_output` | What gets injected: `title_desc` or `full` | `full` (memory), `title_desc` (others) |
| `max_content_chars` | Global cap on total injected content | `9000` |
| `model` | Agentic model: `haiku` / `sonnet` / `opus` | `haiku` |

## How It Works

On every `UserPromptSubmit` and `SubagentStart`, the hook:

1. **Discovers** available resources per enabled dimension (file scan + hardcoded fallback)
2. **Recalls** relevant items using the configured backend (parallel for agentic)
3. **Injects** results as `additionalContext` into the model's context

Sub-agents and teammates also receive recall context. On `SubagentStart`, the hook extracts the parent agent's prompt from the transcript and runs the full recall pipeline.

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
  memory_recall.py      # Entry point: parallel dispatch + merge + log
  discover.py           # Resource discovery (file scan + hardcoded fallback)
  backends.py           # 3 generic recall implementations
  constants.py          # Hardcoded built-in skills, deferred tools, agent types
  embedding_daemon.py   # Local RAG daemon (sentence-transformers)
  hooks.json            # Hook registration (UserPromptSubmit 30s, SubagentStart 60s)
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

```bash
claude plugin install memory-recall@memory-recall
```

After updating, run `/reload-plugins` to refresh without restarting the session.
