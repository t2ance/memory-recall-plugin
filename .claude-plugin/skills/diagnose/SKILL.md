---
name: diagnose
description: "Interactive troubleshooting for memory-recall plugin. Diagnose hook failures, empty results, timeouts, config issues, cache sync problems, and more. Use when the user says /diagnose or reports a plugin problem."
user_invocable: true
---

# Memory-Recall Plugin Diagnosis

Interactively diagnose memory-recall plugin issues. Ask the user about their symptom, run targeted checks, report findings.

## How to use this skill

1. If the user described a symptom, match it to a scenario below and run the relevant checks.
2. If no symptom given, start with the Quick Health Check, then ask what's wrong.
3. Use AskUserQuestion to narrow down when needed. Run shell commands to gather evidence.
4. Report findings with evidence (actual command output), not speculation.

## Log file

The single most useful diagnostic artifact. Every recall invocation logs here:

```
~/.claude/plugins/data/memory-recall-memory-recall/recall.jsonl
```

Read the last entry first (`tail -30`). Key fields:
- `event`: UserPromptSubmit or SubagentStart
- `dimensions`: which backend each dim used
- `discovered`: resource counts per dim
- `results`: what was selected (files, items, or no_results)
- `elapsed_s`, `per_dim_s`: timing
- `per_dim_usage`: token counts and cost (agentic only)
- `output`: the full additionalContext that was injected

## Quick Health Check

Run these 5 checks first to get a baseline:

1. Plugin cache exists:
   ```bash
   ls ~/.claude/plugins/cache/memory-recall/memory-recall/*/hooks/hooks.json
   ```

2. Config is set:
   ```bash
   python3 -c "import json; print(json.dumps(json.load(open('$HOME/.claude/settings.json')).get('pluginConfigs',{}).get('memory-recall@memory-recall',{}), indent=2))"
   ```

3. Recent log exists:
   ```bash
   tail -5 ~/.claude/plugins/data/memory-recall-memory-recall/recall.jsonl | python3 -c "import sys,json; [print(json.loads(l).get('ts','?'), json.loads(l).get('event','?'), json.loads(l).get('elapsed_s','?')) for l in sys.stdin if l.strip()]"
   ```

4. Agent SDK works (agentic backend):
   ```bash
   python3 -c "from claude_agent_sdk import query; print('OK')"
   ```

5. Memory directories exist:
   ```bash
   ls ~/.claude/plugins/data/memory-recall-memory-recall/global-memory/MEMORY.md
   ```

---

## Scenarios

### 1. Hook not triggering

**Symptom:** No recall context in system-reminder. No new entries in recall.jsonl.

**Checks:**
- Is hooks.json present in the active cache version dir? List all version dirs and check each.
- Does hooks.json contain both `UserPromptSubmit` and `SubagentStart` entries?
- Run `/reload-plugins` -- does it report "2 hooks"?
- Run `/doctor` -- any errors referencing memory-recall?

**Common causes:**
- Plugin not installed or cache dir empty.
- hooks.json missing or malformed.
- Code synced to wrong version dir (e.g., 1.0.0/ while CC loads from 3.0.0/). Check all version dirs.

### 2. Recall results are empty

**Symptom:** Log entries show all `"status": "no_results"`. Or additionalContext is the fallback "CRITICAL: Before responding, check your memory directories...".

**Checks:**
- Read the log entry's `discovered` field. If counts are 0, discovery found nothing.
- For memory: do the memory dirs exist and contain .md files?
  ```bash
  ls ~/.claude/projects/-*/memory/*.md 2>/dev/null | head -5
  ls ~/.claude/plugins/data/memory-recall-memory-recall/global-memory/*.md 2>/dev/null | head -5
  ```
- For skills/tools/agents: does the plugin cache have content?
- Check backend config: are dimensions set to "off"?

**Common causes:**
- All backends at default "off" (only memory defaults to "reminder").
- Memory directory doesn't exist (new project).
- Plugin cache empty (no plugins installed with skills/agents).

### 3. Agentic backend timeout

**Symptom:** Hook killed after 30s (UserPromptSubmit) or 60s (SubagentStart). Log entry may be missing or incomplete.

**Checks:**
- Read recent log entries' `elapsed_s` and `per_dim_s` to find the slow dimension.
- Check `per_dim_usage.duration_api_ms` for API response time.
- Count how many dimensions use agentic backend.

**Fixes to suggest:**
- Reduce agentic dimensions (set some to `reminder` or `off`).
- Switch to `agentic_mode: "merged"` (single Haiku call, faster but lower quality for large catalogs).
- SubagentStart has 60s timeout (vs 30s for UserPromptSubmit).

### 4. Agentic JSON parse error

**Symptom:** Hook crashes. Log may show partial entry or no entry.

**Checks:**
- Test Agent SDK:
  ```bash
  python3 -c "from claude_agent_sdk import query; print('OK')"
  ```
- Check if SDK version is very old:
  ```bash
  pip show claude-agent-sdk 2>/dev/null | grep Version
  ```
- Read the log for any partial output or error hints.

**Common causes:**
- claude-agent-sdk not installed.
- Network/API issues causing empty or truncated Haiku response.
- Very large catalog overwhelming Haiku's output. Try `merged` mode or reduce enabled dimensions.

### 5. Plugin options not taking effect

**Symptom:** Changed settings.json but hook uses old values. Log shows wrong backend.

**Checks:**
- Verify JSON path is correct: `pluginConfigs.memory-recall@memory-recall.options`
- Read the log entry's `dimensions` field to see what was actually used.
- Ask: did you run `/reload-plugins` after editing settings.json?

**Cause:** Plugin options are memoized per session. MUST run `/reload-plugins` after editing settings.json.

### 6. Granularity setting has no effect

**Symptom:** Set `{dim}_input: "full"` or `{dim}_output: "full"` but output unchanged.

**Key knowledge -- N/A combinations by design:**

| Backend | input_granularity | output_granularity |
|---------|-------------------|--------------------|
| reminder | N/A (no selection) | Works |
| agentic | Works | Works |
| embedding (memory) | N/A (daemon reads full) | Works |
| embedding (non-mem) | Works | Works |

**Checks:**
- What backend is the dimension using? If reminder, input_granularity is always N/A.
- For output=full on non-memory: the resource needs a `content_path` (real file). Built-in/hardcoded items from constants.py have no file and fall back to title_desc.
- Dimension is tools? Tools (MCP/deferred) have no content file. full = title_desc for tools.

### 7. SubagentStart issues

**Symptom:** Sub-agent has no recall context, or log shows wrong/empty query for SubagentStart.

**Checks:**
- Does recall.jsonl have `"event": "SubagentStart"` entries? If not, hook not firing.
- Check the `query` field. It should be the parent's prompt to the sub-agent.
- If query is empty, prompt extraction failed. Check transcript manually:
  ```bash
  # Get transcript path from a recent log entry, then:
  tail -100 <transcript_path> | python3 -c "
  import json, sys
  for line in reversed(sys.stdin.read().strip().split('\n')):
      msg = json.loads(line)
      if msg.get('type') == 'assistant':
          for b in msg.get('message',{}).get('content',[]):
              if b.get('type') == 'tool_use' and b.get('name') == 'Agent':
                  print('Found:', b['input']['prompt'][:200])
                  sys.exit(0)
  print('No Agent tool_use found')
  "
  ```

**Common causes:**
- hooks.json missing `SubagentStart` entry.
- Transcript write delay (~100ms fire-and-forget). The 200ms sleep usually suffices.
- hookEventName mismatch in output (must return the actual event name, not hardcoded).

### 8. Cache sync problems

**Symptom:** Code changes don't take effect. Or plugin breaks after syncing.

**Checks:**
- List version dirs:
  ```bash
  ls -la ~/.claude/plugins/cache/memory-recall/memory-recall/
  ```
- Diff source vs cache to find staleness:
  ```bash
  diff <source>/hooks/recall.py ~/.claude/plugins/cache/memory-recall/memory-recall/<version>/hooks/recall.py
  ```

**Critical rule:** `rsync --delete` ONLY on the versioned subdirectory (e.g., 3.0.0/), NEVER on the parent dir. Parent contains multiple version dirs + direct hooks/skills dirs.

**Common causes:**
- --delete on parent dir wiped versioned subdir.
- Version bump created new dir (e.g., 3.0.0/) but code still synced to old dir.
- Stale .pyc files. Delete `__pycache__/` in cache dir.

### 9. Chinese encoding issues

**Symptom:** Log or injected context shows `\u4f60\u597d` instead of readable Chinese.

**Cause:** json.dumps defaults to ensure_ascii=True. The code uses ensure_ascii=False in write_log(). If you see escaped chars, check whether the reading tool handles UTF-8, or whether a different code path is dumping JSON.

### 10. Embedding daemon errors

**Symptom:** Embedding backend fails with socket error or "daemon error".

**Checks:**
- Socket exists?
  ```bash
  ls -la ~/.claude/plugins/data/memory-recall-memory-recall/daemon.sock
  ```
- Daemon log:
  ```bash
  tail -50 ~/.claude/plugins/data/memory-recall-memory-recall/daemon.log
  ```
- Python env works?
  ```bash
  ~/miniconda3/envs/memory-recall/bin/python -c "from sentence_transformers import SentenceTransformer; print('OK')"
  ```

**Common causes:**
- conda env missing or packages not installed. Run `/setup`.
- embedding_python config path wrong.
- Stale socket from crashed daemon. Remove and retry:
  ```bash
  rm ~/.claude/plugins/data/memory-recall-memory-recall/daemon.sock
  ```
- CUDA OOM. Switch to cpu.

### 11. /doctor shows plugin errors

**Symptom:** `/reload-plugins` reports errors. `/doctor` shows issues.

**Check:** Run `/doctor` and read specific errors. Common ones:
- "Invalid hook type": malformed hooks.json.
- "Plugin not found": installPath references missing dir.
- Skills/agents parse failures: bad frontmatter.

**Important:** Errors are often from OTHER plugins, not memory-recall. Check which plugin each error references.

### 12. Last resort: reinstall plugin

If all other diagnostics fail and the plugin is still broken (especially CLI subprocess crashes like `Command failed with exit code 1` from Agent SDK), try a full reinstall:

```bash
claude plugin marketplace update memory-recall
claude plugin install memory-recall@memory-recall
```

Both steps are required in this order. `marketplace update` does `git pull` on the local marketplace clone. `plugin install` re-copies from the updated marketplace to the versioned cache. Without `marketplace update` first, `plugin install` just re-copies stale code.

Note: `claude plugin update` does NOT work for same-version code changes -- it compares the `version` field in `plugin.json` and skips if unchanged.

After reinstalling, run `/reload-plugins` in the active session.
