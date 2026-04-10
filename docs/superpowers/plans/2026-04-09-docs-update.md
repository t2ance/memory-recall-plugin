# Documentation & Memory Update Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring all documentation and memory files in sync with the current plugin state (4 hooks, pair programmer, async support, 40+ config options).

**Architecture:** 7 independent tasks covering README, 2 skills, and 4 memory files. Each can be done in any order.

**Tech Stack:** Markdown only. No code changes.

---

### Task 1: Fix Memory Files (highest priority -- actively misleading)

**Files:**
- Modify: `/home/peijia/.claude/projects/-data1-peijia-projects-claude-code-main/memory/project_memory_recall_plugin.md`
- Modify: `/home/peijia/.claude/projects/-data1-peijia-projects-claude-code-main/memory/pair_programmer_implementation.md`
- Delete: `/home/peijia/.claude/projects/-data1-peijia-projects-claude-code-main/memory/project_sidecar_sync_async_deferred.md`
- Modify: `/home/peijia/.claude/projects/-data1-peijia-projects-claude-code-main/memory/project_agent_steering_vision.md`
- Modify: `/home/peijia/.claude/projects/-data1-peijia-projects-claude-code-main/memory/MEMORY.md`

- [ ] **Step 1: Rewrite project_memory_recall_plugin.md**

Fix version (3.1.0 not 3.2.0), add pair programmer + async features:

```markdown
---
name: Memory Recall Plugin
description: Plugin with 4 hooks (recall, memory-save, pair-programmer), 4 recall dimensions, async support
type: project
---

# Memory Recall Plugin

**Current Version:** v3.1.0 (plugin.json)
**Repo:** t2ance/memory-recall-plugin

## 4 Hooks

| Hook | Script | Event | Timeout | Purpose |
|------|--------|-------|---------|---------|
| Recall | memory_recall.py | UserPromptSubmit, SubagentStart | 30s/60s | Recall memories/skills/tools/agents |
| Memory Save | auto_save.py | Stop | 120s | Auto-save conversation knowledge |
| Pair Programmer | pair_programmer.py | PostToolUse (Edit/Write/Bash) | 30s | Evaluate agent actions against user preferences |

## Key Features
- 4 recall dimensions (memory/skills/tools/agents) x 3 backends (reminder/agentic/embedding)
- Pair programmer: 3-dimension evaluation (preference/experience/strategy)
- Configurable sync/async per hook (recall_async, memory_save_async, pp_async)
- 40+ user-configurable options via plugin.json userConfig
- Memory Bank as dynamic preference source (no static summaries)

## Recent Changes (2026-04-09)
- Added pair_programmer.py PostToolUse hook
- Added configurable sync/async mode for all hooks (maybe_go_async)
- Moved hardcoded limits to userConfig (pp_max_* options)
- Added systemMessage feedback for all hooks (including no-op)
- SDK dedup, sidecar preamble, diagnostics improvements
```

- [ ] **Step 2: Rewrite pair_programmer_implementation.md**

Fix phantom config names, wrong timeout, add async support:

```markdown
---
name: pair_programmer_implementation
description: Pair Programmer PostToolUse hook -- actual config options, async support, 3-dimension evaluation
type: project
---

# Pair Programmer Implementation

**Implemented:** 2026-04-09
**File:** memory-recall-plugin/hooks/pair_programmer.py
**Default:** Disabled (pp_enabled=false)

## Hook Registration
- Event: PostToolUse
- Matcher: Edit|Write|Bash|NotebookEdit
- Timeout: 30s
- Async: configurable via pp_async (default: true)

## Actual Config Options (11 pp_* entries)
- pp_enabled (bool, false) -- master switch
- pp_model (haiku/sonnet, haiku) -- evaluation model
- pp_sample_rate (0-1, 1.0) -- probability of evaluating each tool call
- pp_cooldown_s (0-60, 0) -- minimum seconds between evaluations
- pp_context_messages (1-15, 5) -- recent messages for trajectory
- pp_context_max_chars (500-10000, 3000) -- max conversation context chars
- pp_effort (string, "") -- SDK effort level
- pp_max_tool_input_chars (500-10000, 2000) -- max tool input chars
- pp_max_tool_output_chars (500-10000, 1000) -- max tool output chars
- pp_max_recall_files (1-10, 5) -- max memory files to recall
- pp_max_memory_file_chars (500-10000, 2000) -- max chars per memory file
- pp_async (bool, true) -- run asynchronously

## Enable
In settings.json pluginConfigs.memory-recall@memory-recall.options:
  pp_enabled: true
Then /reload-plugins.

## 3 Evaluation Dimensions
- preference: alignment with user's documented preferences
- experience: whether this situation was encountered before
- strategy: high-level direction and approach quality

## Architecture
2 SDK calls per evaluation (~$0.003):
1. recall_agentic for memory retrieval
2. merged Haiku call for 3-dimension evaluation
Output: additionalContext with soft suggestions (or nothing if verdict=ok)
```

- [ ] **Step 3: Delete project_sidecar_sync_async_deferred.md**

This memory says sync/async is "deferred" but it's implemented. Remove it.

Also remove its line from MEMORY.md.

- [ ] **Step 4: Update project_agent_steering_vision.md status**

Change the status/implementation section from "Design phase. No PoC yet." to reflect that pair_programmer.py IS the PoC and async is implemented. Keep the vision/philosophy content as-is.

- [ ] **Step 5: Update MEMORY.md index**

Remove deleted file entry. Update descriptions for modified files.

- [ ] **Step 6: Commit**

```bash
cd /home/peijia/.claude/projects/-data1-peijia-projects-claude-code-main/memory
# No git here -- these are memory files, not repo files. Just save.
```

---

### Task 2: Update README.md

**Files:**
- Modify: `/data1/peijia/projects/memory-recall-plugin/README.md`

- [ ] **Step 1: Update Features section**

Add pair programmer, memory save, async support to features list:

```markdown
## Features

- **4 hooks**: recall (UserPromptSubmit/SubagentStart), memory save (Stop), pair programmer (PostToolUse)
- **4 recall dimensions**: memory files, skills, tools (MCP + deferred), agent types
- **3 backends per dimension**: reminder (zero-cost), agentic (Haiku selection), embedding (local RAG)
- **Pair programmer**: evaluates agent actions against user preferences, past experience, strategic direction
- **Memory save**: auto-saves conversation knowledge to Memory Bank after each turn
- **Configurable sync/async**: each hook can run synchronously or asynchronously
- **4 skills**: `/dream` (consolidation), `/remember` (quick save), `/setup` (config), `/diagnose` (troubleshooting)
```

- [ ] **Step 2: Update "How It Works" section**

Expand to cover all 4 hooks:

```markdown
## How It Works

### Recall (UserPromptSubmit / SubagentStart)
On every user message and sub-agent spawn, the hook:
1. **Discovers** available resources per enabled dimension
2. **Recalls** relevant items using the configured backend
3. **Injects** results as `additionalContext` into the model's context

### Memory Save (Stop)
After each assistant turn, the hook:
1. Extracts recent conversation turns
2. Calls Haiku to decide what knowledge to persist (ADD/UPDATE/DELETE/NOOP)
3. Writes memory files and updates MEMORY.md index

Config: `auto_save_enabled` (default true), `auto_save_targets` (native/global/both), `auto_save_context_turns`, `auto_save_effort`.

### Pair Programmer (PostToolUse)
After action tools (Edit/Write/Bash/NotebookEdit), the hook:
1. Builds trajectory from current tool call + recent conversation
2. Recalls relevant memories from Memory Bank
3. Evaluates across 3 dimensions (preference/experience/strategy)
4. Injects soft suggestions via `additionalContext`

Default off. Enable: `pp_enabled: true`. Config: 11 `pp_*` options.
```

- [ ] **Step 3: Add Async Support section**

```markdown
### Async Support

Each hook can run synchronously (blocking) or asynchronously (non-blocking). Configure via:

| Option | Default | Effect |
|--------|---------|--------|
| `recall_async` | false | Recall must usually be sync (context needed before agent responds) |
| `memory_save_async` | true | Save runs in background after turn completes |
| `pp_async` | true | Pair programmer feedback arrives at next tool call |
```

- [ ] **Step 4: Expand config options table**

Replace the 5-row table with a comprehensive table organized by feature. Include all recall options, embedding options, auto-save options, pair programmer options, and async options.

- [ ] **Step 5: Update Code Structure**

```markdown
## Code Structure

```
hooks/
  memory_recall.py      # Recall hook: parallel dispatch + merge + inject
  auto_save.py          # Memory save hook: Haiku CRUD on conversation knowledge
  pair_programmer.py    # Pair programmer hook: 3-dimension evaluation of agent actions
  discover.py           # Resource discovery (file scan + hardcoded fallback)
  backends.py           # 3 recall backend implementations
  utils.py              # Shared: Agent SDK wrapper, config, logging, async
  constants.py          # Hardcoded built-in skills, deferred tools, agent types
  embedding_daemon.py   # Local RAG daemon (sentence-transformers)
  hooks.json            # Hook registration (4 hooks)
skills/
  dream/SKILL.md        # Memory consolidation
  remember/SKILL.md     # Quick save
  setup/SKILL.md        # Interactive config
  diagnose/SKILL.md     # Interactive troubleshooting
```
```

- [ ] **Step 6: Commit**

```bash
cd /data1/peijia/projects/memory-recall-plugin
git add README.md
git commit -m "docs: update README with pair programmer, memory save, async support"
```

---

### Task 3: Update Setup Skill

**Files:**
- Modify: `/data1/peijia/projects/memory-recall-plugin/plugins/memory-recall/skills/setup/SKILL.md`

- [ ] **Step 1: Update overview**

Change "3 hooks" to "4 hooks". Add pair programmer and async to overview.

- [ ] **Step 2: Add Step 4: Memory Save Configuration**

After current Step 3, add auto-save config guidance:
- auto_save_enabled (bool, true)
- auto_save_targets (native/global/both, native)
- auto_save_context_turns (1-10, 3)
- auto_save_effort (string, "")

- [ ] **Step 3: Add Step 5: Pair Programmer Configuration**

Guide users through:
- pp_enabled (bool, false) -- explain what it does
- pp_model, pp_sample_rate, pp_cooldown_s -- basic tuning
- pp_max_* options -- advanced limits
- pp_async -- sync vs async tradeoff

- [ ] **Step 4: Add Step 6: Async Configuration**

Explain sync/async per hook:
- recall_async (false) -- why sync is usually better
- memory_save_async (true) -- safe to background
- pp_async (true) -- feedback arrives next tool call

- [ ] **Step 5: Renumber existing steps**

Current Step 4 (Apply) becomes Step 7, Step 5 (Embedding) becomes Step 8, etc.

- [ ] **Step 6: Commit**

```bash
git add plugins/memory-recall/skills/setup/SKILL.md
git commit -m "docs: update setup skill with pair programmer, memory save, async config"
```

---

### Task 4: Update Diagnose Skill

**Files:**
- Modify: `/data1/peijia/projects/memory-recall-plugin/plugins/memory-recall/skills/diagnose/SKILL.md`

- [ ] **Step 1: Update log file key fields**

Add `event: "pair_programmer"` and `event: "auto_save"` to the log field descriptions.

- [ ] **Step 2: Update Quick Health Check**

Update check 1 to expect 4 hooks. Add check for pp_enabled config.

- [ ] **Step 3: Add Scenario 13: Pair programmer not working**

```markdown
### 13. Pair programmer not working

**Symptom:** No "PP:" systemMessage after Edit/Write/Bash. No `event: "pair_programmer"` in recall.jsonl.

**Checks:**
- Is pp_enabled set to true in pluginConfigs?
- Does hooks.json have PostToolUse entry with matcher?
- Is the tool name in the matcher list? (Edit|Write|Bash|NotebookEdit)
- Check recall.jsonl for `event: "pair_programmer"` entries.
- If pp_async=true, feedback appears at NEXT tool call, not current one.

**Common causes:**
- pp_enabled not set or set to "false" (default is false).
- Config not reloaded after change (/reload-plugins needed).
- Tool not in matcher list (e.g., Read doesn't trigger pair programmer).
```

- [ ] **Step 4: Add Scenario 14: Async hook results missing**

```markdown
### 14. Async hook results not appearing

**Symptom:** Hook runs (log entry exists) but additionalContext not visible in agent's context.

**Checks:**
- Is *_async set to true for this hook?
- Async results arrive at the NEXT API call, not the current one.
- If pp_async=true, pair programmer feedback appears after the NEXT tool call.
- Check if recall.jsonl shows the hook completed before the next tool call.

**Common causes:**
- Expected immediate feedback but hook is async (by design).
- Hook timed out before completing (check elapsed_s in log).
- Only one tool call in the turn -- async result has no "next" call to attach to.
```

- [ ] **Step 5: Update scenario 1 (Hook not triggering)**

Change "2 hooks" to "4 hooks" in the `/reload-plugins` check.

- [ ] **Step 6: Commit**

```bash
git add plugins/memory-recall/skills/diagnose/SKILL.md
git commit -m "docs: update diagnose skill with pair programmer and async scenarios"
```

---

### Task 5: Sync + Push

- [ ] **Step 1: Push all commits**

```bash
cd /data1/peijia/projects/memory-recall-plugin
git push
```

- [ ] **Step 2: Rsync to all cache locations**

```bash
SRC="/data1/peijia/projects/memory-recall-plugin/plugins/memory-recall"
rsync -av --delete --exclude='.git' --exclude='__pycache__' "$SRC/" "$HOME/.claude/plugins/cache/memory-recall/memory-recall/1.0.0/"
rsync -av --exclude='.git' --exclude='__pycache__' "$SRC/" "$HOME/.claude/plugins/cache/memory-recall/memory-recall/"
rsync -av --delete --exclude='.git' --exclude='__pycache__' "$SRC/" "$HOME/.claude/plugins/marketplaces/memory-recall/plugins/memory-recall/"
```

- [ ] **Step 3: /reload-plugins and verify "4 hooks"**
