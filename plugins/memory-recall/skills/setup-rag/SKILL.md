---
name: setup-rag
description: One-time setup for memory-recall RAG (conda env, model download, daemon start)
user_invocable: true
---

# Setup Memory-Recall RAG

Run these commands in sequence to set up the embedding-based RAG system for memory recall.

## Step 1: Create conda environment

```bash
conda create -n memory-recall python=3.11 -y
```

## Step 2: Install dependencies (CPU-only torch)

```bash
conda run -n memory-recall pip install torch --index-url https://download.pytorch.org/whl/cpu && conda run -n memory-recall pip install sentence-transformers numpy
```

## Step 3: Pre-download embedding model

```bash
conda run -n memory-recall python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('intfloat/multilingual-e5-small')"
```

## Step 4: Start the daemon

```bash
PLUGIN_DATA="${CLAUDE_PLUGIN_DATA:-$HOME/.claude/plugins/data/memory-recall-memory-recall}"
nohup ~/miniconda3/envs/memory-recall/bin/python "$(find ~/.claude -path '*/memory-recall/hooks/daemon.py' | head -1)" >> "${PLUGIN_DATA}/daemon.log" 2>&1 &
```

## Step 5: Verify

Wait a few seconds for the model to load, then test with a sample query:

```bash
PLUGIN_DATA="${CLAUDE_PLUGIN_DATA:-$HOME/.claude/plugins/data/memory-recall-memory-recall}"
echo '{"query":"test","memory_dirs":["'"$HOME"'/.claude/projects/'$(pwd | sed 's|/|-|g; s|^-||')'/memory"],"top_k":1}' | ~/miniconda3/envs/memory-recall/bin/python -c "
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
resp = json.loads(data)
print('Status:', resp['status'])
print('Results:', len(resp.get('results', [])))
"
```

If you see `Status: ok`, the RAG system is working. The hook will now inject relevant memory content directly on every user message.
