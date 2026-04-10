# Naming Unification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Unify all hook naming in the memory-recall plugin to three canonical names: `recall`, `memory_save`, `pair_programmer`.

**Architecture:** Pure rename refactor across 7 files. No logic changes. File renames + string replacements + config key renames.

**Tech Stack:** Python, JSON, Markdown

**Source repo:** `/data1/peijia/projects/memory-recall-plugin/plugins/memory-recall/`

**No backward compatibility.** Users must update their `settings.json` pluginConfigs keys manually after this change.

---

## Motivation

The plugin uses inconsistent names for the same three hooks:

| Hook | Current names used | Canonical name |
|------|--------------------|----------------|
| Recall | `recall`, `memory_recall` (file name), `recall_*` (config) | `recall` |
| Auto-save | `auto_save` (hook name, config), `memory_save_async` (async config!) | `memory_save` |
| Pair programmer | `pp` (hook name, status file), `pp_*` (config prefix), `pair_programmer` (file name, log event) | `pair_programmer` |

Problems:
- `pp` is cryptic -- other developers don't know what it means
- `auto_save` vs `memory_save_async` -- same hook, two names
- `memory_recall.py` vs hook name `recall` -- confusing prefix
- Three different naming styles: abbreviation (`pp`), underscore compound (`auto_save`), single word (`recall`)

## Rename Mapping

### File renames
| Old | New |
|-----|-----|
| `hooks/memory_recall.py` | `hooks/recall.py` |
| `hooks/auto_save.py` | `hooks/memory_save.py` |
| `hooks/pair_programmer.py` | (no rename, already correct) |

### Hook display names (write_status first arg)
| Old | New |
|-----|-----|
| `"recall"` | `"recall"` (no change) |
| `"auto_save"` | `"memory_save"` |
| `"pp"` | `"pair_programmer"` |

### Config key prefixes
| Old | New |
|-----|-----|
| `auto_save_enabled` | `memory_save_enabled` |
| `auto_save_targets` | `memory_save_targets` |
| `auto_save_effort` | `memory_save_effort` |
| `auto_save_context_turns` | `memory_save_context_turns` |
| `memory_save_async` | `memory_save_async` (already correct!) |
| `pp_enabled` | `pair_programmer_enabled` |
| `pp_model` | `pair_programmer_model` |
| `pp_sample_rate` | `pair_programmer_sample_rate` |
| `pp_cooldown_s` | `pair_programmer_cooldown_s` |
| `pp_context_messages` | `pair_programmer_context_messages` |
| `pp_context_max_chars` | `pair_programmer_context_max_chars` |
| `pp_effort` | `pair_programmer_effort` |
| `pp_max_tool_input_chars` | `pair_programmer_max_tool_input_chars` |
| `pp_max_tool_output_chars` | `pair_programmer_max_tool_output_chars` |
| `pp_max_recall_files` | `pair_programmer_max_recall_files` |
| `pp_max_memory_file_chars` | `pair_programmer_max_memory_file_chars` |
| `pp_async` | `pair_programmer_async` |
| `recall_async` | `recall_async` (no change) |
| `recall_effort` | `recall_effort` (no change) |

### Env var names (derived from config keys)
Config key `pp_enabled` maps to env var `CLAUDE_PLUGIN_OPTION_PP_ENABLED`.
New: `pair_programmer_enabled` maps to `CLAUDE_PLUGIN_OPTION_PAIR_PROGRAMMER_ENABLED`.

Same pattern for all `pp_*` -> `pair_programmer_*` and `auto_save_*` -> `memory_save_*`.

### Status file names
| Old | New |
|-----|-----|
| `status/<sid>/recall.json` | (no change) |
| `status/<sid>/auto_save.json` | `status/<sid>/memory_save.json` |
| `status/<sid>/pp.json` | `status/<sid>/pair_programmer.json` |

### State file
| Old | New |
|-----|-----|
| `pp_state.json` | `pair_programmer_state.json` |

### Log event names
| Old | New |
|-----|-----|
| `"event": "auto_save"` | `"event": "memory_save"` |
| `"event": "pair_programmer"` | (no change, already correct) |

## Cautions

1. **Env var names are UPPERCASE with underscores**: `CLAUDE_PLUGIN_OPTION_PAIR_PROGRAMMER_ENABLED` -- make sure the uppercasing is correct in `os.environ.get()` calls.

2. **`memory_save_async` already exists**: The async config key for auto_save is ALREADY `memory_save_async` (not `auto_save_async`). Don't rename it again.

3. **`recall_async` and `recall_effort` already correct**: No rename needed for recall config keys.

4. **hooks.json command paths must match file renames**: `memory_recall.py` -> `recall.py`, `auto_save.py` -> `memory_save.py`.

5. **User's statusline.sh may have no hardcoded hook names**: The statusline reads hook names from JSON `hook` field, so it auto-adapts. No statusline.sh change needed.

6. **User's settings.json pluginConfigs**: After this change, any existing `pp_enabled`, `auto_save_enabled` etc. will be silently ignored. User must update manually.

7. **pp_state.json**: Old state file at `~/.claude/plugins/data/memory-recall-memory-recall/pp_state.json` will become orphaned. The new code will look for `pair_programmer_state.json`. Cooldown state resets (harmless).

8. **Old status files**: Existing `status/<sid>/pp.json` and `status/<sid>/auto_save.json` become orphaned. New ones will be created on next hook execution. Orphans are harmless (tiny files, auto-cleaned by session expiry).

---

### Task 1: Rename files and update hooks.json

**Files:**
- Rename: `hooks/memory_recall.py` -> `hooks/recall.py`
- Rename: `hooks/auto_save.py` -> `hooks/memory_save.py`
- Modify: `hooks/hooks.json`

- [ ] **Step 1: Rename Python files**

```bash
cd /data1/peijia/projects/memory-recall-plugin/plugins/memory-recall
git mv hooks/memory_recall.py hooks/recall.py
git mv hooks/auto_save.py hooks/memory_save.py
```

- [ ] **Step 2: Update hooks.json command paths**

In `hooks/hooks.json`, change:

```json
"command": "python3 \"${CLAUDE_PLUGIN_ROOT}/hooks/memory_recall.py\""
```
to (both UserPromptSubmit and SubagentStart entries):
```json
"command": "python3 \"${CLAUDE_PLUGIN_ROOT}/hooks/recall.py\""
```

And change:
```json
"command": "python3 \"${CLAUDE_PLUGIN_ROOT}/hooks/auto_save.py\""
```
to:
```json
"command": "python3 \"${CLAUDE_PLUGIN_ROOT}/hooks/memory_save.py\""
```

`pair_programmer.py` path stays the same.

- [ ] **Step 3: Update comment in utils.py line 4**

Change:
```python
Agent SDK calling, and JSON parsing. Used by memory_recall.py, auto_save.py,
```
to:
```python
Agent SDK calling, and JSON parsing. Used by recall.py, memory_save.py,
```

- [ ] **Step 4: Verify**

```bash
python3 -c "import ast; ast.parse(open('hooks/recall.py').read()); print('OK')"
python3 -c "import ast; ast.parse(open('hooks/memory_save.py').read()); print('OK')"
python3 -c "import json; json.load(open('hooks/hooks.json')); print('OK')"
```

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "refactor: rename hook files (memory_recall->recall, auto_save->memory_save)"
```

---

### Task 2: Rename config keys in utils.py

**Files:**
- Modify: `hooks/utils.py` (load_plugin_config function, lines ~94-168)

- [ ] **Step 1: Rename auto_save_* config keys to memory_save_***

In `load_plugin_config()`, change these keys and their env var names:

```python
# Old
"auto_save_enabled": os.environ.get("CLAUDE_PLUGIN_OPTION_AUTO_SAVE_ENABLED", "true") != "false",
"auto_save_targets": os.environ.get("CLAUDE_PLUGIN_OPTION_AUTO_SAVE_TARGETS", "native"),
"auto_save_context_turns": int(os.environ.get("CLAUDE_PLUGIN_OPTION_AUTO_SAVE_CONTEXT_TURNS", "3")),
"auto_save_effort": os.environ.get("CLAUDE_PLUGIN_OPTION_AUTO_SAVE_EFFORT", ""),

# New
"memory_save_enabled": os.environ.get("CLAUDE_PLUGIN_OPTION_MEMORY_SAVE_ENABLED", "true") != "false",
"memory_save_targets": os.environ.get("CLAUDE_PLUGIN_OPTION_MEMORY_SAVE_TARGETS", "native"),
"memory_save_context_turns": int(os.environ.get("CLAUDE_PLUGIN_OPTION_MEMORY_SAVE_CONTEXT_TURNS", "3")),
"memory_save_effort": os.environ.get("CLAUDE_PLUGIN_OPTION_MEMORY_SAVE_EFFORT", ""),
```

Note: `memory_save_async` is already correct. Don't change it.

- [ ] **Step 2: Rename pp_* config keys to pair_programmer_***

```python
# Old
"pp_enabled": os.environ.get("CLAUDE_PLUGIN_OPTION_PP_ENABLED", "false") != "false",
"pp_model": os.environ.get("CLAUDE_PLUGIN_OPTION_PP_MODEL", "haiku"),
"pp_sample_rate": float(os.environ.get("CLAUDE_PLUGIN_OPTION_PP_SAMPLE_RATE", "1.0")),
"pp_cooldown_s": float(os.environ.get("CLAUDE_PLUGIN_OPTION_PP_COOLDOWN_S", "120")),
"pp_context_messages": int(os.environ.get("CLAUDE_PLUGIN_OPTION_PP_CONTEXT_MESSAGES", "5")),
"pp_context_max_chars": int(os.environ.get("CLAUDE_PLUGIN_OPTION_PP_CONTEXT_MAX_CHARS", "3000")),
"pp_effort": os.environ.get("CLAUDE_PLUGIN_OPTION_PP_EFFORT", ""),
"pp_max_tool_input_chars": int(os.environ.get("CLAUDE_PLUGIN_OPTION_PP_MAX_TOOL_INPUT_CHARS", "2000")),
"pp_max_tool_output_chars": int(os.environ.get("CLAUDE_PLUGIN_OPTION_PP_MAX_TOOL_OUTPUT_CHARS", "1000")),
"pp_max_recall_files": int(os.environ.get("CLAUDE_PLUGIN_OPTION_PP_MAX_RECALL_FILES", "5")),
"pp_max_memory_file_chars": int(os.environ.get("CLAUDE_PLUGIN_OPTION_PP_MAX_MEMORY_FILE_CHARS", "2000")),
"pp_async": os.environ.get("CLAUDE_PLUGIN_OPTION_PP_ASYNC", "true") != "false",

# New
"pair_programmer_enabled": os.environ.get("CLAUDE_PLUGIN_OPTION_PAIR_PROGRAMMER_ENABLED", "false") != "false",
"pair_programmer_model": os.environ.get("CLAUDE_PLUGIN_OPTION_PAIR_PROGRAMMER_MODEL", "haiku"),
"pair_programmer_sample_rate": float(os.environ.get("CLAUDE_PLUGIN_OPTION_PAIR_PROGRAMMER_SAMPLE_RATE", "1.0")),
"pair_programmer_cooldown_s": float(os.environ.get("CLAUDE_PLUGIN_OPTION_PAIR_PROGRAMMER_COOLDOWN_S", "120")),
"pair_programmer_context_messages": int(os.environ.get("CLAUDE_PLUGIN_OPTION_PAIR_PROGRAMMER_CONTEXT_MESSAGES", "5")),
"pair_programmer_context_max_chars": int(os.environ.get("CLAUDE_PLUGIN_OPTION_PAIR_PROGRAMMER_CONTEXT_MAX_CHARS", "3000")),
"pair_programmer_effort": os.environ.get("CLAUDE_PLUGIN_OPTION_PAIR_PROGRAMMER_EFFORT", ""),
"pair_programmer_max_tool_input_chars": int(os.environ.get("CLAUDE_PLUGIN_OPTION_PAIR_PROGRAMMER_MAX_TOOL_INPUT_CHARS", "2000")),
"pair_programmer_max_tool_output_chars": int(os.environ.get("CLAUDE_PLUGIN_OPTION_PAIR_PROGRAMMER_MAX_TOOL_OUTPUT_CHARS", "1000")),
"pair_programmer_max_recall_files": int(os.environ.get("CLAUDE_PLUGIN_OPTION_PAIR_PROGRAMMER_MAX_RECALL_FILES", "5")),
"pair_programmer_max_memory_file_chars": int(os.environ.get("CLAUDE_PLUGIN_OPTION_PAIR_PROGRAMMER_MAX_MEMORY_FILE_CHARS", "2000")),
"pair_programmer_async": os.environ.get("CLAUDE_PLUGIN_OPTION_PAIR_PROGRAMMER_ASYNC", "true") != "false",
```

- [ ] **Step 3: Verify**

```bash
python3 -c "import ast; ast.parse(open('hooks/utils.py').read()); print('OK')"
```

- [ ] **Step 4: Commit**

```bash
git add hooks/utils.py
git commit -m "refactor: rename config keys (auto_save->memory_save, pp->pair_programmer)"
```

---

### Task 3: Update memory_save.py (was auto_save.py) to use new names

**Files:**
- Modify: `hooks/memory_save.py`

- [ ] **Step 1: Replace all `auto_save` references with `memory_save`**

Use replace-all for these strings:

| Old | New |
|-----|-----|
| `write_status("auto_save"` | `write_status("memory_save"` |
| `config["auto_save_enabled"]` | `config["memory_save_enabled"]` |
| `config["auto_save_context_turns"]` | `config["memory_save_context_turns"]` |
| `config["auto_save_targets"]` | `config["memory_save_targets"]` |
| `config["auto_save_effort"]` | `config["memory_save_effort"]` |
| `"event": "auto_save"` | `"event": "memory_save"` |

Note: `maybe_go_async("memory_save_async", config)` is already correct. Don't change it.

- [ ] **Step 2: Verify**

```bash
python3 -c "import ast; ast.parse(open('hooks/memory_save.py').read()); print('OK')"
grep -n "auto_save" hooks/memory_save.py  # should return nothing
```

- [ ] **Step 3: Commit**

```bash
git add hooks/memory_save.py
git commit -m "refactor: update memory_save.py to use memory_save_* config keys"
```

---

### Task 4: Update pair_programmer.py to use new names

**Files:**
- Modify: `hooks/pair_programmer.py`

- [ ] **Step 1: Replace all `pp_` config references with `pair_programmer_`**

Use replace-all for these config key accesses:

| Old | New |
|-----|-----|
| `config.get("pp_cooldown_s"` | `config.get("pair_programmer_cooldown_s"` |
| `config.get("pp_enabled"` | `config.get("pair_programmer_enabled"` |
| `config.get("pp_sample_rate"` | `config.get("pair_programmer_sample_rate"` |
| `config.get("pp_context_messages"` | `config.get("pair_programmer_context_messages"` |
| `config.get("pp_context_max_chars"` | `config.get("pair_programmer_context_max_chars"` |
| `config.get("pp_max_tool_input_chars"` | `config.get("pair_programmer_max_tool_input_chars"` |
| `config.get("pp_max_tool_output_chars"` | `config.get("pair_programmer_max_tool_output_chars"` |
| `config.get("pp_model"` | `config.get("pair_programmer_model"` |
| `config.get("pp_max_recall_files"` | `config.get("pair_programmer_max_recall_files"` |
| `config.get("pp_max_memory_file_chars"` | `config.get("pair_programmer_max_memory_file_chars"` |
| `config.get("pp_effort"` | `config.get("pair_programmer_effort"` |
| `maybe_go_async("pp_async"` | `maybe_go_async("pair_programmer_async"` |

- [ ] **Step 2: Replace hook display name in write_status calls**

| Old | New |
|-----|-----|
| `write_status("pp"` | `write_status("pair_programmer"` |

There are 4 occurrences (lines 297, 300, 307, 354).

- [ ] **Step 3: Rename state file constant**

Change:
```python
STATE_FILE = os.path.join(DATA_DIR, "pp_state.json")
```
to:
```python
STATE_FILE = os.path.join(DATA_DIR, "pair_programmer_state.json")
```

- [ ] **Step 4: Rename local variables (optional but consistent)**

Change local variable names for clarity:
```python
# Old
pp_cost = ...
pp_model = ...

# New (use full name for consistency)
pair_programmer_cost = ...
pair_programmer_model = ...
```

- [ ] **Step 5: Verify**

```bash
python3 -c "import ast; ast.parse(open('hooks/pair_programmer.py').read()); print('OK')"
grep -n '"pp"' hooks/pair_programmer.py       # should return nothing
grep -n '"pp_' hooks/pair_programmer.py        # should return nothing
grep -n 'pp_state' hooks/pair_programmer.py    # should return nothing
```

- [ ] **Step 6: Commit**

```bash
git add hooks/pair_programmer.py
git commit -m "refactor: update pair_programmer.py to use pair_programmer_* config keys"
```

---

### Task 5: Update plugin.json userConfig keys

**Files:**
- Modify: `.claude-plugin/plugin.json`

- [ ] **Step 1: Rename auto_save_* keys to memory_save_***

Change these userConfig key names (the JSON object keys, not the internal values):

| Old key | New key |
|---------|---------|
| `"auto_save_enabled"` | `"memory_save_enabled"` |
| `"auto_save_targets"` | `"memory_save_targets"` |
| `"auto_save_effort"` | `"memory_save_effort"` |
| `"auto_save_context_turns"` | `"memory_save_context_turns"` |

Note: `"memory_save_async"` is already correct. Don't change it.

- [ ] **Step 2: Rename pp_* keys to pair_programmer_***

| Old key | New key |
|---------|---------|
| `"pp_enabled"` | `"pair_programmer_enabled"` |
| `"pp_model"` | `"pair_programmer_model"` |
| `"pp_sample_rate"` | `"pair_programmer_sample_rate"` |
| `"pp_cooldown_s"` | `"pair_programmer_cooldown_s"` |
| `"pp_context_messages"` | `"pair_programmer_context_messages"` |
| `"pp_context_max_chars"` | `"pair_programmer_context_max_chars"` |
| `"pp_effort"` | `"pair_programmer_effort"` |
| `"pp_max_tool_input_chars"` | `"pair_programmer_max_tool_input_chars"` |
| `"pp_max_tool_output_chars"` | `"pair_programmer_max_tool_output_chars"` |
| `"pp_max_recall_files"` | `"pair_programmer_max_recall_files"` |
| `"pp_max_memory_file_chars"` | `"pair_programmer_max_memory_file_chars"` |
| `"pp_async"` | `"pair_programmer_async"` |

- [ ] **Step 3: Verify**

```bash
python3 -c "import json; json.load(open('.claude-plugin/plugin.json')); print('OK')"
grep -n '"pp_' .claude-plugin/plugin.json          # should return nothing
grep -n '"auto_save_' .claude-plugin/plugin.json    # should return nothing
```

- [ ] **Step 4: Commit**

```bash
git add .claude-plugin/plugin.json
git commit -m "refactor: rename plugin.json userConfig keys to match canonical names"
```

---

### Task 6: Update setup skill documentation

**Files:**
- Modify: `skills/setup/SKILL.md`

- [ ] **Step 1: Replace all old names with new names**

| Old | New |
|-----|-----|
| `auto_save` | `memory_save` |
| (any `pp` references if present) | `pair_programmer` |

The setup skill already uses `pair_programmer` in one place. Just ensure consistency.

- [ ] **Step 2: Commit**

```bash
git add skills/setup/SKILL.md
git commit -m "docs: update setup skill to use canonical hook names"
```

---

### Task 7: Update user's settings.json and sync

**Files:**
- Modify: `~/.claude/settings.json` (user's pluginConfigs)

- [ ] **Step 1: Update pluginConfigs keys**

In `~/.claude/settings.json`, under `pluginConfigs.memory-recall@memory-recall.options`, rename:
- `pp_enabled` -> `pair_programmer_enabled`
- Any other `pp_*` or `auto_save_*` keys the user has configured

- [ ] **Step 2: Sync to all cache locations**

```bash
SRC="/data1/peijia/projects/memory-recall-plugin/plugins/memory-recall"
rsync -av --delete --exclude='.git' --exclude='__pycache__' "$SRC/" "$HOME/.claude/plugins/cache/memory-recall/memory-recall/3.1.0/"
find "$HOME/.claude/plugins/cache/memory-recall" -name "hooks" -type d | while read d; do
    [ -f "$d/hooks.json" ] && cp "$SRC/hooks/"*.py "$SRC/hooks/hooks.json" "$d/"
done
rsync -av --delete --exclude='.git' --exclude='__pycache__' "$SRC/" "$HOME/.claude/plugins/marketplaces/memory-recall/plugins/memory-recall/"
```

- [ ] **Step 3: Push and reload**

```bash
cd /data1/peijia/projects/memory-recall-plugin
git push
```

Then `/reload-plugins` in Claude Code.

- [ ] **Step 4: Verify statusLine displays new names**

Send a message and check statusLine shows `recall`, `memory_save`, `pair_programmer` (not `auto_save` or `pp`).

- [ ] **Step 5: Commit settings.json change (if desired)**

The settings.json change is user-local, no git commit needed.
