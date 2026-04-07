# Memory Recall Plugin

Forces Claude Code to check memory files before responding, and write new memories after responding.

## Problem

Claude Code's built-in memory recall (Sonnet prefetch) is unreliable:
- Gated by a remote GrowthBook feature flag (`tengu_moth_copse`) that may not be enabled for you
- Async and non-blocking, so fast responses can miss the prefetch entirely
- MEMORY.md index is always in context, but only contains one-line descriptions -- not enough for Claude to know when topic files are relevant

## Features

- **Per-turn memory hook**: injects a reminder on every user message to read relevant memories before responding and write new memories after responding
- **`/dream` skill**: manual memory consolidation across project memory, global memory, and `~/.claude/CLAUDE.md`
- **Global memory**: cross-project memory directory at `~/.claude/plugins/data/memory-recall/global-memory/`

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

## Updating

The plugin is cached locally after installation. To pull the latest version from GitHub:

```bash
claude plugin install memory-recall@memory-recall
```

This re-fetches from the marketplace and overwrites the local cache at `~/.claude/plugins/cache/memory-recall/memory-recall/<version>/`.

If the update doesn't take effect in the current session, restart Claude Code or run `/plugins` to reload.

## How It Works

### Hook (every user message)

On every `UserPromptSubmit` event, the hook:

1. Computes the project memory directory path from `cwd`
2. Outputs a reminder as `additionalContext`, which Claude sees as a `<system-reminder>`

The reminder tells Claude to:
- Read MEMORY.md indexes and relevant topic files **before** responding
- Write new memories **after** responding if the conversation produced anything worth remembering

Three sources are listed:
- Project memory: `~/.claude/projects/<sanitized-cwd>/memory/`
- Global memory: `~/.claude/plugins/data/memory-recall/global-memory/`
- Global instructions: `~/.claude/CLAUDE.md`

### /dream skill (manual)

Runs a 5-phase memory consolidation:

1. **Orient** -- read both memory directories and `~/.claude/CLAUDE.md`
2. **Gather** -- search transcripts for new signal from the current project
3. **Consolidate** -- write to project or global memory based on content (project-specific vs cross-project)
4. **Tidy CLAUDE.md** -- propose moving memory-like entries from CLAUDE.md to global memory (requires user approval via AskUserQuestion)
5. **Prune** -- update both MEMORY.md indexes

## Cost

- Hook: zero API cost (pure shell script), < 100ms, ~60 tokens per injection
- /dream: runs in main agent context, cost depends on how many files need updating
