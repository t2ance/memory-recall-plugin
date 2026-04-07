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

reminder="MANDATORY — DO NOT SKIP: (1) BEFORE responding, read MEMORY.md in each memory directory below and Read any topic files relevant to the user's query. Also review ~/.claude/CLAUDE.md for global instructions. (2) AFTER responding, if this conversation produced any new information worth remembering (user preferences, corrections, decisions, lessons learned), write it to the appropriate memory directory."
reminder="$reminder Project memory: $proj_mem_dir"
reminder="$reminder Global memory: $global_mem_dir"
reminder="$reminder Global instructions: $HOME/.claude/CLAUDE.md"

printf '{"hookSpecificOutput":{"hookEventName":"UserPromptSubmit","additionalContext":"%s"}}' "$(echo "$reminder" | sed 's/"/\\"/g')"
