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

# List project memory files
proj_files=""
if [ -d "$proj_mem_dir" ]; then
  proj_files=$(ls "$proj_mem_dir"/*.md 2>/dev/null | grep -v MEMORY.md | xargs -I{} basename {} 2>/dev/null | tr '\n' ', ' || echo "")
fi

# List global memory files
global_files=""
if [ -d "$global_mem_dir" ]; then
  global_files=$(ls "$global_mem_dir"/*.md 2>/dev/null | grep -v MEMORY.md | xargs -I{} basename {} 2>/dev/null | tr '\n' ', ' || echo "")
fi

if [ -z "$proj_files" ] && [ -z "$global_files" ]; then
  exit 0
fi

reminder="CRITICAL: Before responding, check your memory directories for relevant context. You MUST scan the MEMORY.md index and Read any topic files that might be relevant to the user's query."
if [ -n "$proj_files" ]; then
  reminder="$reminder Project memory ($proj_mem_dir): $proj_files"
fi
if [ -n "$global_files" ]; then
  reminder="$reminder Global memory ($global_mem_dir): $global_files"
fi
reminder="$reminder Also review ~/.claude/CLAUDE.md for global instructions."

printf '{"hookSpecificOutput":{"hookEventName":"UserPromptSubmit","additionalContext":"%s"}}' "$(echo "$reminder" | sed 's/"/\\"/g')"
