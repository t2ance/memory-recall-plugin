"""Hardcoded resource lists for built-in CC resources not discoverable from file system.

These are compiled into cli.js and cannot be scanned at runtime.
Update when CC adds new built-in skills/tools/agents (infrequent).
Last updated: 2026-04-07, CC v2.1.92.
"""

BUILTIN_SKILLS = [
    {"name": "update-config", "description": "Configure Claude Code harness via settings.json. For hooks, automated behaviors."},
    {"name": "keybindings-help", "description": "Customize keyboard shortcuts, rebind keys, add chord bindings."},
    {"name": "simplify", "description": "Review changed code for reuse, quality, efficiency, then fix issues found."},
    {"name": "loop", "description": "Run a prompt or slash command on a recurring interval."},
    {"name": "schedule", "description": "Create, update, list scheduled remote agents (triggers) on a cron schedule."},
    {"name": "claude-api", "description": "Build apps with Claude API or Anthropic SDK. Trigger when code imports anthropic SDK."},
    {"name": "remember", "description": "Quick save what you learned in this conversation to memory."},
    {"name": "dream", "description": "Run memory consolidation. Synthesize session context into durable memory files."},
    {"name": "verify", "description": "Verify code changes, run tests, check for regressions."},
    {"name": "debug", "description": "Debug issues with Claude Code itself."},
    {"name": "skillify", "description": "Create a new skill from the current conversation."},
    {"name": "batch", "description": "Run a command on multiple files or inputs in batch."},
    {"name": "stuck", "description": "Help when you are stuck on a problem."},
]

DEFERRED_TOOLS = [
    {"name": "WebFetch", "description": "Fetch content from a URL. Use for reading web pages, API responses, documentation."},
    {"name": "WebSearch", "description": "Search the web for information. Returns search results with snippets."},
    {"name": "TaskCreate", "description": "Create a task to track work progress."},
    {"name": "TaskGet", "description": "Get details of a specific task."},
    {"name": "TaskUpdate", "description": "Update task status (in_progress, completed, etc.)."},
    {"name": "TaskStop", "description": "Stop a running task."},
    {"name": "TaskOutput", "description": "Get output from a task."},
    {"name": "TaskList", "description": "List all tasks."},
    {"name": "TeamCreate", "description": "Create a team of agents (swarm) for collaborative work."},
    {"name": "TeamDelete", "description": "Delete a team."},
    {"name": "EnterWorktree", "description": "Enter a git worktree for isolated work."},
    {"name": "ExitWorktree", "description": "Exit a git worktree."},
    {"name": "EnterPlanMode", "description": "Enter plan mode for designing implementation strategy."},
    {"name": "ExitPlanMode", "description": "Exit plan mode and begin implementation."},
    {"name": "CronCreate", "description": "Create a cron job for recurring tasks."},
    {"name": "CronDelete", "description": "Delete a cron job."},
    {"name": "CronList", "description": "List all cron jobs."},
    {"name": "RemoteTrigger", "description": "Trigger a remote agent."},
    {"name": "AskUserQuestion", "description": "Ask the user a question with multiple choice options."},
    {"name": "SendMessage", "description": "Send a message to a running agent/teammate."},
    {"name": "NotebookEdit", "description": "Edit Jupyter notebook cells."},
    {"name": "ListMcpResourcesTool", "description": "List available MCP resources."},
    {"name": "ReadMcpResourceTool", "description": "Read a specific MCP resource."},
]

BUILTIN_AGENTS = [
    {"name": "general-purpose", "description": "General-purpose agent for research, code search, multi-step tasks. Has access to all tools."},
    {"name": "Explore", "description": "Fast read-only agent for codebase exploration. Find files, search code, answer questions about structure. Uses Haiku."},
    {"name": "Plan", "description": "Software architect agent for designing implementation plans. Read-only, returns step-by-step plans."},
    {"name": "statusline-setup", "description": "Configure Claude Code status line setting."},
    {"name": "claude-code-guide", "description": "Answer questions about Claude Code features, hooks, slash commands, MCP servers, settings, IDE integrations."},
]
