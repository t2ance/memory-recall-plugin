#!/usr/bin/env bash
set -euo pipefail

input=$(cat)
cwd=$(echo "$input" | python3 -c "import sys,json; print(json.load(sys.stdin).get('cwd',''))" 2>/dev/null || echo "")

if [ -z "$cwd" ]; then
  exit 0
fi

# Project memory directory
sanitized=$(echo "$cwd" | sed 's|/|-|g; s|^-||')
proj_mem_dir="$HOME/.claude/projects/-${sanitized}/memory"
if [ ! -d "$proj_mem_dir" ]; then
  proj_mem_dir="$HOME/.claude/projects/${sanitized}/memory"
fi

# Global memory directory (plugin data)
global_mem_dir="${CLAUDE_PLUGIN_DATA:-$HOME/.claude/plugins/data/memory-recall}/global-memory"

reminder="CRITICAL: Before responding, check your memory directories for relevant context. Read the MEMORY.md index in each directory and Read any topic files relevant to the user's query."
reminder="$reminder Project memory: $proj_mem_dir"
reminder="$reminder Global memory: $global_mem_dir"

printf '{"hookSpecificOutput":{"hookEventName":"UserPromptSubmit","additionalContext":"%s"}}' "$(echo "$reminder" | sed 's/"/\\"/g')"
