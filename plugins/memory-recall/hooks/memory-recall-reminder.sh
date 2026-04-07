#!/usr/bin/env bash
set -euo pipefail

input=$(cat)
cwd=$(echo "$input" | python3 -c "import sys,json; print(json.load(sys.stdin).get('cwd',''))" 2>/dev/null || echo "")

if [ -z "$cwd" ]; then
  exit 0
fi

sanitized=$(echo "$cwd" | sed 's|/|-|g; s|^-||')
mem_dir="$HOME/.claude/projects/-${sanitized}/memory"

if [ ! -d "$mem_dir" ]; then
  mem_dir="$HOME/.claude/projects/${sanitized}/memory"
fi

if [ ! -d "$mem_dir" ]; then
  exit 0
fi

mem_files=$(ls "$mem_dir"/*.md 2>/dev/null | grep -v MEMORY.md | xargs -I{} basename {} 2>/dev/null | tr '\n' ', ' || echo "")

if [ -z "$mem_files" ]; then
  exit 0
fi

reminder="CRITICAL: Before responding, check your memory directory for relevant context."
reminder="$reminder You MUST scan the MEMORY.md index and Read any topic files that might be relevant to the user's query."
reminder="$reminder Available topic files in $mem_dir: $mem_files"
reminder="$reminder Also review ~/.claude/CLAUDE.md for global instructions."

printf '{"hookSpecificOutput":{"hookEventName":"UserPromptSubmit","additionalContext":"%s"}}' "$(echo "$reminder" | sed 's/"/\\"/g')"
