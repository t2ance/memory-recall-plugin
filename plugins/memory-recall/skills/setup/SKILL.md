---
name: setup
description: "Interactive setup for memory-recall plugin. Choose backend (reminder/agentic/embedding), configure options, and set up environment. Use when the user says /setup or wants to configure the memory-recall plugin."
user_invocable: true
---

# Memory-Recall Plugin Setup

Guide the user through configuring the memory-recall plugin interactively. Use AskUserQuestion to collect preferences, then apply changes.

## Configuration location

Plugin options are stored in `~/.claude/settings.json` under:

```json
{
  "pluginConfigs": {
    "memory-recall@memory-recall": {
      "options": {
        "backend": "reminder",
        ...
      }
    }
  }
}
```

The hook reads these as `CLAUDE_PLUGIN_OPTION_*` env vars. Changes take effect on the next session (or next message if CC reloads settings live).

## Step 1: Read current config

Read `~/.claude/settings.json` and extract the current `pluginConfigs.memory-recall@memory-recall.options` values. Show the user their current settings. If no pluginConfigs exist yet, all options are at defaults.

## Step 2: Choose backend

Use AskUserQuestion to ask which backend to use:

| Backend | Description | Requirements |
|---------|-------------|--------------|
| `reminder` | Injects memory directory paths. Agent reads files on demand. Zero cost, zero latency. | None |
| `agentic` | Calls Haiku via claude-agent-sdk to select 0-3 relevant files, injects their content directly. ~$0.003/query, ~5s latency. | `claude-agent-sdk` Python package |
| `embedding` | Local RAG daemon with sentence-transformers. Encodes query, cosine similarity search, injects top results. Zero cost after setup, <1s latency. | conda env `memory-recall` with `sentence-transformers`, `numpy`, `torch` |

## Step 3: Backend-specific options

Based on the chosen backend, ask about relevant options using AskUserQuestion:

**If agentic:**
- `model`: Which model for file selection? Options: `haiku` (fast/cheap, recommended), `sonnet` (smarter but slower/costlier). Default: `haiku`.

**If embedding:**
- `embedding_model`: HuggingFace model name. Default: `intfloat/multilingual-e5-small`.
- `embedding_python`: Path to Python with sentence-transformers. Default: `~/miniconda3/envs/memory-recall/bin/python`.
- `embedding_threshold`: Cosine similarity threshold (0-1). Default: `0.85`. Lower = more results but less precise.
- `embedding_top_k`: Max files to inject (1-10). Default: `3`.
- `embedding_device`: `cpu` or `cuda`. Default: `cpu`.

**Shared options (ask for all backends except reminder):**
- `context_messages`: Number of recent conversation messages for search context (0-20). Default: `5`.
- `context_max_chars`: Max chars of conversation context (0-10000). Default: `2000`.
- `max_content_chars`: Max total chars of injected memory content (1000-10000). Default: `9000`.

**If reminder:** no additional options needed.

Only ask about options the user is likely to care about. For most users, defaults are fine -- offer to use defaults and only drill into specifics if the user wants to customize.

## Step 4: Apply config

Read `~/.claude/settings.json`, merge the new options into `pluginConfigs.memory-recall@memory-recall.options`, and write back. Preserve all other settings.

If `pluginConfigs` or `memory-recall@memory-recall` key doesn't exist yet, create it.

## Step 5: Environment setup (embedding only)

If the user chose `embedding`, run the full environment setup:

1. **Create conda env** (skip if already exists):
   ```bash
   conda create -n memory-recall python=3.11 -y
   ```

2. **Install dependencies**:
   ```bash
   conda run -n memory-recall pip install torch --index-url https://download.pytorch.org/whl/cpu
   conda run -n memory-recall pip install sentence-transformers numpy
   ```
   If the user chose `embedding_device: cuda`, install CUDA torch instead:
   ```bash
   conda run -n memory-recall pip install torch
   ```

3. **Pre-download model**:
   ```bash
   conda run -n memory-recall python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('MODEL_NAME')"
   ```
   Replace MODEL_NAME with the configured `embedding_model`.

4. **Start daemon**:
   ```bash
   PLUGIN_DATA="${CLAUDE_PLUGIN_DATA:-$HOME/.claude/plugins/data/memory-recall-memory-recall}"
   EMBEDDING_MODEL="MODEL_NAME" EMBEDDING_DEVICE="DEVICE" nohup ~/miniconda3/envs/memory-recall/bin/python "$(find ~/.claude -path '*/memory-recall/hooks/embedding_daemon.py' | head -1)" >> "${PLUGIN_DATA}/daemon.log" 2>&1 &
   ```

## Step 6: Verify

Based on the chosen backend, verify it works:

**agentic:** Check that `claude-agent-sdk` is importable:
```bash
python3 -c "from claude_agent_sdk import query; print('OK')"
```
If it fails, tell the user to install it: `pip install claude-agent-sdk`.

**embedding:** Test the daemon with a sample query:
```bash
PLUGIN_DATA="${CLAUDE_PLUGIN_DATA:-$HOME/.claude/plugins/data/memory-recall-memory-recall}"
echo '{"query":"test","memory_dirs":["'$HOME'/.claude/projects/test/memory"],"top_k":1}' | python3 -c "
import socket, sys, json, os
sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
sock.connect(os.path.expanduser('${PLUGIN_DATA}/daemon.sock'))
sock.sendall(sys.stdin.buffer.read())
sock.shutdown(socket.SHUT_WR)
data = b''
while True:
    chunk = sock.recv(4096)
    if not chunk: break
    data += chunk
sock.close()
print(json.loads(data)['status'])
"
```

**reminder:** No verification needed. It always works.

## Step 7: Summary

Show the user a summary of what was configured and any actions they need to take (e.g., restart session for settings to take effect).
