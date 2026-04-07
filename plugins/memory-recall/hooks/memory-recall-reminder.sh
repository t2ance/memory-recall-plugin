#!/usr/bin/env bash
set -euo pipefail

PYTHON="$HOME/miniconda3/envs/memory-recall/bin/python"
PLUGIN_DIR="${CLAUDE_PLUGIN_ROOT:-$(dirname "$0")/..}"
HOOKS_DIR="${PLUGIN_DIR}/hooks"

input=$(cat)

# Tier 1: try RAG client
result=$( echo "$input" | "$PYTHON" "${HOOKS_DIR}/client.py" 2>/dev/null ) && rc=$? || rc=$?

if [ $rc -eq 0 ] && [ -n "$result" ]; then
  echo "$result"
  exit 0
fi

# Daemon not running or no relevant results -- start daemon in background if needed
PLUGIN_DATA="${CLAUDE_PLUGIN_DATA:-$HOME/.claude/plugins/data/memory-recall-memory-recall}"
SOCK="${PLUGIN_DATA}/daemon.sock"
if [ ! -S "$SOCK" ]; then
  nohup "$PYTHON" "${HOOKS_DIR}/daemon.py" >> "${PLUGIN_DATA}/daemon.log" 2>&1 &
fi

# Tier 2: fall back to path-based reminder
cwd=$(echo "$input" | python3 -c "import sys,json; print(json.load(sys.stdin).get('cwd',''))" 2>/dev/null || echo "")
if [ -z "$cwd" ]; then
  exit 0
fi

sanitized=$(echo "$cwd" | sed 's|/|-|g; s|^-||')
proj_mem_dir="$HOME/.claude/projects/-${sanitized}/memory"
if [ ! -d "$proj_mem_dir" ]; then
  proj_mem_dir="$HOME/.claude/projects/${sanitized}/memory"
fi

global_mem_dir="${PLUGIN_DATA}/global-memory"

reminder="MANDATORY: Before responding, check your memory directories for relevant context. Read the MEMORY.md index in each directory and Read any topic files relevant to the user's query."
reminder="$reminder Project memory: $proj_mem_dir"
reminder="$reminder Global memory: $global_mem_dir"
reminder="$reminder Global instructions: $HOME/.claude/CLAUDE.md"

printf '{"hookSpecificOutput":{"hookEventName":"UserPromptSubmit","additionalContext":"%s"}}' "$(echo "$reminder" | sed 's/"/\\"/g')"
