# Memory Recall Plugin

Forces Claude Code to check memory files before responding to every user message.

## Problem

Claude Code's built-in memory recall (Sonnet prefetch) is unreliable:
- Gated by a remote GrowthBook feature flag (`tengu_moth_copse`) that may not be enabled for you
- Async and non-blocking, so fast responses can miss the prefetch entirely
- MEMORY.md index is always in context, but only contains one-line descriptions -- not enough for Claude to know when topic files are relevant

## Solution

This plugin registers a `UserPromptSubmit` hook that injects a reminder on every user message, listing all available topic files with their full paths. This nudges Claude to proactively Read relevant memory files before responding.

## Installation

```bash
claude plugin marketplace add t2ance/memory-recall-plugin
claude plugin install memory-recall@memory-recall
```

Or add to `~/.claude/settings.json`:

```json
{
  "extraKnownMarketplaces": {
    "memory-recall": {
      "source": {
        "source": "github",
        "repo": "t2ance/memory-recall-plugin"
      }
    }
  },
  "enabledPlugins": {
    "memory-recall@memory-recall": true
  }
}
```

## How It Works

On every `UserPromptSubmit` event, the hook:

1. Reads `cwd` from the hook input JSON
2. Computes the project memory directory path (`~/.claude/projects/<sanitized-cwd>/memory/`)
3. Lists all `.md` topic files (excluding `MEMORY.md` itself)
4. Outputs a `CRITICAL` reminder as `additionalContext`, which Claude sees as a `<system-reminder>`

The injected reminder looks like:

```
CRITICAL: Before responding, check your memory directory for relevant context.
You MUST scan the MEMORY.md index and Read any topic files that might be
relevant to the user's query. Available topic files in
/home/user/.claude/projects/.../memory/: project_foo.md, feedback_bar.md, ...
Also review ~/.claude/CLAUDE.md for global instructions.
```

## Cost

- Zero API cost (pure shell script, no LLM calls)
- < 100ms execution time
- ~100 tokens per injection
