# Memory-Recall Plugin Diagnosis Guide

Symptom-based troubleshooting for the memory-recall plugin. Each section starts with what you observe, then diagnosis steps and fix.

## Log file

All recall invocations log to:

```
~/.claude/plugins/data/memory-recall-memory-recall/recall.jsonl
```

Read the latest entry (last ~30 lines) to see what happened:

```bash
tail -n 30 ~/.claude/plugins/data/memory-recall-memory-recall/recall.jsonl
```

Key fields: `event`, `dimensions`, `discovered`, `results`, `elapsed_s`, `per_dim_usage`, `output`.

---

## 1. Hook not triggering (no recall context injected)

**Symptom:** No `system-reminder` with "additionalContext" appears. No new entries in `recall.jsonl`.

**Diagnosis:**

1. Check plugin is installed:
   ```bash
   ls ~/.claude/plugins/cache/memory-recall/memory-recall/
   ```
   Should contain a versioned directory (e.g., `3.0.0/`) with `hooks/hooks.json`.

2. Check hooks.json exists in the active version dir:
   ```bash
   cat ~/.claude/plugins/cache/memory-recall/memory-recall/3.0.0/hooks/hooks.json
   ```
   Should list both `UserPromptSubmit` and `SubagentStart`.

3. Run `/reload-plugins` and check output. Should show "2 hooks".

4. Run `/doctor` to check for plugin load errors.

**Common causes:**
- Plugin not installed or cache dir empty.
- hooks.json missing or malformed JSON.
- Wrong versioned directory (e.g., code synced to `1.0.0/` but CC loads from `3.0.0/`). Check which dir CC uses: look at the `installPath` in `~/.claude/plugins/installed_plugins.json`, or check all version dirs for the correct hooks.json.

---

## 2. Recall results are empty (all dimensions return no_results)

**Symptom:** `recall.jsonl` shows entries with all `"status": "no_results"`. Or the `additionalContext` contains only the fallback "CRITICAL: Before responding, check your memory directories...".

**Diagnosis:**

1. Check `discovered` counts in the log entry. If `0` for all dimensions, discovery found nothing.

2. For memory: check that memory directories exist and contain `.md` files:
   ```bash
   ls ~/.claude/projects/-<sanitized-cwd>/memory/
   ls ~/.claude/plugins/data/memory-recall-memory-recall/global-memory/
   ```

3. For skills/tools/agents: check plugin cache has content:
   ```bash
   ls ~/.claude/plugins/cache/
   ```

4. Check backend config -- if all dimensions are `"off"`, nothing runs:
   ```bash
   python3 -c "import json; c=json.load(open('$HOME/.claude/settings.json')); print(json.dumps(c.get('pluginConfigs',{}).get('memory-recall@memory-recall',{}), indent=2))"
   ```

**Common causes:**
- All backends set to `off` (default for skills/tools/agents).
- Memory directory doesn't exist yet (new project, never saved memories).
- Plugin cache is empty (fresh install, no plugins with skills/agents).

---

## 3. Agentic backend timeout

**Symptom:** Hook takes >30s (UserPromptSubmit) or >60s (SubagentStart) and gets killed. Log entry may be missing or truncated.

**Diagnosis:**

1. Check `elapsed_s` and `per_dim_s` in recent log entries to find the bottleneck dimension.

2. Check how many dimensions use agentic:
   ```bash
   grep '"agentic"' ~/.claude/plugins/data/memory-recall-memory-recall/recall.jsonl | tail -5
   ```

3. Check `per_dim_usage` for API response times (`duration_api_ms`).

**Fixes:**
- Reduce number of agentic dimensions (set some to `reminder` or `off`).
- Switch to `agentic_mode: "merged"` (single Haiku call for all dims, faster but lower quality for large catalogs).
- The SubagentStart timeout is 60s (vs 30s for UserPromptSubmit) to accommodate the extra transcript-reading step.

---

## 4. Agentic returns JSON parse error

**Symptom:** Hook crashes with `JSONDecodeError` or assertion `"Empty response from agentic recall"`.

**Diagnosis:**

1. Check daemon log for Haiku output:
   ```bash
   tail -50 ~/.claude/plugins/data/memory-recall-memory-recall/recall.jsonl
   ```
   Look for the raw `result_text` in error context.

2. Haiku sometimes returns extra text after valid JSON. The code uses `json.JSONDecoder().raw_decode()` to handle this. If parsing still fails, the response may be malformed.

**Common causes:**
- `claude-agent-sdk` not installed or wrong version. Test:
  ```bash
  python3 -c "from claude_agent_sdk import query; print('OK')"
  ```
- Network issue or API rate limit causing empty/truncated response.
- Very large catalog (many resources) causing Haiku to produce truncated or malformed JSON. Reduce catalog size or switch to `merged` mode.

---

## 5. Plugin options not taking effect

**Symptom:** Changed settings in `settings.json` but hook still uses old values (e.g., changed `memory: agentic` but log still shows `reminder`).

**Diagnosis:**

1. Verify the JSON path is correct:
   ```json
   {
     "pluginConfigs": {
       "memory-recall@memory-recall": {
         "options": {
           "memory": "agentic"
         }
       }
     }
   }
   ```

2. Run `/reload-plugins`. This clears the memoized plugin options cache and re-reads env vars.

3. Check the log entry `dimensions` field to see which backend each dimension actually used.

**Common cause:** Plugin options are memoized per session. After editing `settings.json`, you MUST run `/reload-plugins` for changes to take effect. A session restart also works.

---

## 6. Granularity setting has no effect

**Symptom:** Set `skills_input: "full"` or `skills_output: "full"` but output looks the same as `title_desc`.

**Diagnosis:**

1. Check which backend the dimension uses. Some backend x granularity combinations are N/A by design:

   | Backend | input_granularity | output_granularity |
   |---------|-------------------|--------------------|
   | reminder | N/A (no selection step) | Works |
   | agentic | Works | Works |
   | embedding (memory) | N/A (daemon reads full content) | Works |
   | embedding (non-memory) | Works | Works |

2. For `output_granularity: "full"` on non-memory dimensions: the resource must have a `content_path` (a real file to read). Built-in/hardcoded skills, tools, and agents have NO file -- they fall back to `title_desc` per item. Only discovered resources from plugin cache or `.claude/agents/` have files.

3. Check the log entry `output` field to see what was actually injected.

**Common causes:**
- Backend is `reminder` and you set `input_granularity` -- reminder ignores it (by design).
- Dimension is `tools` with `output: "full"` -- tools (MCP servers, deferred tools) have no content file, so full = title_desc.
- Hardcoded built-in entries (from `constants.py`) don't have `content_path`.

---

## 7. SubagentStart not injecting context / wrong prompt

**Symptom:** Sub-agent's `system-reminder` has no recall context, or the query in the log doesn't match what was sent to the sub-agent.

**Diagnosis:**

1. Check `recall.jsonl` for entries with `"event": "SubagentStart"`. If none exist, the hook isn't firing for sub-agents.

2. Check the `query` field in the log entry. It should contain the parent agent's prompt to the sub-agent (extracted from transcript).

3. If `query` is empty: the prompt extraction failed. This means either:
   - The transcript doesn't contain an Agent tool_use (check transcript file manually).
   - The transcript wasn't flushed yet. There's a 200ms sleep before reading, but in rare cases transcript write may take longer.

**Diagnosis steps for empty query:**

```bash
# Find the transcript path from the log entry
# Then check if Agent tool_use exists in the last 100 lines:
tail -100 <transcript_path> | python3 -c "
import json, sys
for line in reversed(sys.stdin.read().strip().split('\n')):
    msg = json.loads(line)
    if msg.get('type') == 'assistant':
        for b in msg.get('message',{}).get('content',[]):
            if b.get('type') == 'tool_use' and b.get('name') == 'Agent':
                print('Found:', b['input']['prompt'][:200])
                sys.exit(0)
print('No Agent tool_use found in last 100 lines')
"
```

**Common causes:**
- hooks.json missing `SubagentStart` entry. Check the active cache dir's hooks.json.
- `hookEventName` in output doesn't match the event. The hook must return `"hookEventName": "SubagentStart"` (not hardcoded "UserPromptSubmit").
- Transcript write delay: `void recordTranscript` is fire-and-forget (~100ms). The 200ms sleep usually suffices, but heavy load may cause delays.

---

## 8. Plugin cache sync issues

**Symptom:** Code changes in source dir don't take effect. Or plugin breaks after syncing.

**Diagnosis:**

1. Check which version dir CC is loading from:
   ```bash
   ls -la ~/.claude/plugins/cache/memory-recall/memory-recall/
   ```
   Look for versioned dirs (e.g., `1.0.0/`, `3.0.0/`). The active one matches `plugin.json` version.

2. Compare source and cache:
   ```bash
   diff /data1/peijia/projects/memory-recall-plugin/plugins/memory-recall/hooks/memory_recall.py \
        ~/.claude/plugins/cache/memory-recall/memory-recall/3.0.0/hooks/memory_recall.py
   ```

**Critical rule:** When syncing, use `rsync --delete` ONLY on the versioned subdirectory, NEVER on the parent `memory-recall/memory-recall/` directory. The parent contains multiple version dirs and direct `hooks/`/`skills/` dirs that CC also reads.

```bash
# Correct: sync WITH --delete to versioned dir
rsync -av --delete source/ ~/.claude/plugins/cache/memory-recall/memory-recall/3.0.0/

# Also sync to parent's direct hooks/skills (without --delete on parent)
rsync -av source/hooks/ ~/.claude/plugins/cache/memory-recall/memory-recall/hooks/
rsync -av source/skills/ ~/.claude/plugins/cache/memory-recall/memory-recall/skills/
```

**Common causes:**
- `rsync --delete` on parent dir wiped versioned subdir.
- Version bump in `plugin.json` created a new dir (e.g., 3.0.0/) but code still synced to old dir (1.0.0/).
- Stale `.pyc` files. Delete `__pycache__/` in the cache dir if behavior is inconsistent.

---

## 9. Encoding issues (Chinese characters as \uXXXX)

**Symptom:** Log file or injected context shows `\u4f60\u597d` instead of readable Chinese.

**Cause:** `json.dumps()` defaults to `ensure_ascii=True`.

**Fix:** The code already uses `ensure_ascii=False` in `write_log()`. If you see escaped characters, check whether you're reading the file with a tool that doesn't handle UTF-8, or whether the issue is in a different code path.

---

## 10. Embedding daemon not starting / connection refused

**Symptom:** Embedding backend fails with socket connection error or "daemon error".

**Diagnosis:**

1. Check if daemon is running:
   ```bash
   ls -la ~/.claude/plugins/data/memory-recall-memory-recall/daemon.sock
   ```

2. Check daemon log:
   ```bash
   tail -50 ~/.claude/plugins/data/memory-recall-memory-recall/daemon.log
   ```

3. Test Python environment:
   ```bash
   ~/miniconda3/envs/memory-recall/bin/python -c "from sentence_transformers import SentenceTransformer; print('OK')"
   ```

**Common causes:**
- conda env `memory-recall` doesn't exist or missing packages. Run `/setup` to create it.
- `embedding_python` path in config doesn't match actual Python location.
- Stale socket file from a crashed daemon. Remove it and retry:
  ```bash
  rm ~/.claude/plugins/data/memory-recall-memory-recall/daemon.sock
  ```
- CUDA OOM if `embedding_device: "cuda"` and GPU is busy. Switch to `cpu`.

---

## 11. /doctor shows plugin errors

**Symptom:** `/reload-plugins` reports "N errors during load" and `/doctor` shows issues.

**Diagnosis:** Run `/doctor` and read the specific errors. Common ones:

- **"Invalid hook type"**: hooks.json has a malformed entry.
- **"Plugin X not found"**: installed_plugins.json references a path that doesn't exist.
- **"Timeout exceeded"**: hook took too long during a test invocation.
- **Skills/agents count mismatch**: some skills or agents failed to parse (bad frontmatter in SKILL.md or agent .md).

These errors are often from OTHER plugins, not necessarily memory-recall. Check which plugin each error references.

---

## Quick health check

Run this to verify the full pipeline:

```bash
# 1. Plugin cache exists
ls ~/.claude/plugins/cache/memory-recall/memory-recall/3.0.0/hooks/hooks.json

# 2. Config is set
python3 -c "import json; print(json.dumps(json.load(open('$HOME/.claude/settings.json')).get('pluginConfigs',{}).get('memory-recall@memory-recall',{}), indent=2))"

# 3. Recent log exists and has entries
tail -5 ~/.claude/plugins/data/memory-recall-memory-recall/recall.jsonl | python3 -c "import sys,json; [print(json.loads(l).get('ts','?'), json.loads(l).get('event','?'), json.loads(l).get('elapsed_s','?')) for l in sys.stdin if l.strip()]"

# 4. Agent SDK works (for agentic backend)
python3 -c "from claude_agent_sdk import query; print('OK')"

# 5. Memory directories exist
ls ~/.claude/plugins/data/memory-recall-memory-recall/global-memory/MEMORY.md
```
