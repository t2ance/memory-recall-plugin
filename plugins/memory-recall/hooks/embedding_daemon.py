#!/usr/bin/env python3
"""Embedding daemon for memory-recall plugin.

Loads multilingual-e5-small on CPU, listens on a unix socket for embedding
search queries against memory files. Auto-exits after 30 min idle.
"""

import json
import logging
import os
import signal
import socket
import sys
import threading
import time

import numpy as np
from sentence_transformers import SentenceTransformer

DATA_DIR = os.environ.get(
    "CLAUDE_PLUGIN_DATA",
    os.path.expanduser("~/.claude/plugins/data/memory-recall-memory-recall"),
)
SOCKET_PATH = os.path.join(DATA_DIR, "daemon.sock")
CACHE_DIR = os.path.join(DATA_DIR, "cache")
PID_FILE = os.path.join(DATA_DIR, "daemon.pid")
LOG_FILE = os.path.join(DATA_DIR, "daemon.log")

MODEL_NAME = os.environ.get("EMBEDDING_MODEL", "intfloat/multilingual-e5-small")
DEVICE = os.environ.get("EMBEDDING_DEVICE", "cpu")
DEFAULT_TOP_K = int(os.environ.get("EMBEDDING_TOP_K", "3"))
DEFAULT_THRESHOLD = float(os.environ.get("EMBEDDING_THRESHOLD", "0.85"))
IDLE_TIMEOUT = 0  # 0 = no idle timeout, daemon runs until killed
MAX_MSG_SIZE = 2 * 1024 * 1024  # 2MB

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("daemon")


class EmbeddingDaemon:
    def __init__(self):
        self.model = None
        self.embeddings = {}  # {filepath: np.ndarray}
        self.manifest = {}  # {filepath: mtime_float}
        self.last_activity = time.time()
        self.running = True

    # ---- model & cache ----

    def load_model(self):
        log.info("Loading model %s on %s", MODEL_NAME, DEVICE)
        self.model = SentenceTransformer(MODEL_NAME, device=DEVICE)
        log.info("Model loaded")

    def load_cache(self):
        manifest_path = os.path.join(CACHE_DIR, "manifest.json")
        embeddings_path = os.path.join(CACHE_DIR, "embeddings.npz")
        if os.path.exists(manifest_path) and os.path.exists(embeddings_path):
            with open(manifest_path) as f:
                self.manifest = json.load(f)
            data = np.load(embeddings_path)
            self.embeddings = {k: data[k] for k in data.files}
            log.info("Cache loaded: %d entries", len(self.embeddings))

    def save_cache(self):
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(os.path.join(CACHE_DIR, "manifest.json"), "w") as f:
            json.dump(self.manifest, f)
        if self.embeddings:
            np.savez(os.path.join(CACHE_DIR, "embeddings.npz"), **self.embeddings)
        log.info("Cache saved: %d entries", len(self.embeddings))

    # ---- cache update ----

    def update_cache(self, memory_dirs):
        """Scan memory dirs, re-embed changed/new files, remove deleted."""
        current_files = {}
        for d in memory_dirs:
            if not os.path.isdir(d):
                continue
            for fname in os.listdir(d):
                if not fname.endswith(".md"):
                    continue
                path = os.path.join(d, fname)
                current_files[path] = os.path.getmtime(path)

        to_embed = []
        for path, mtime in current_files.items():
            cached_mtime = self.manifest.get(path)
            if cached_mtime is None or cached_mtime != mtime:
                to_embed.append(path)

        deleted = [p for p in list(self.manifest) if p not in current_files]
        for p in deleted:
            del self.manifest[p]
            self.embeddings.pop(p, None)

        if to_embed:
            texts = []
            for path in to_embed:
                with open(path) as f:
                    content = f.read()
                texts.append(f"passage: {content}")

            vectors = self.model.encode(texts, normalize_embeddings=True)
            for i, path in enumerate(to_embed):
                self.embeddings[path] = vectors[i]
                self.manifest[path] = current_files[path]

            log.info("Embedded %d files", len(to_embed))

        if to_embed or deleted:
            self.save_cache()

    # ---- search ----

    def search(self, query, memory_dirs, top_k=DEFAULT_TOP_K, threshold=DEFAULT_THRESHOLD):
        self.update_cache(memory_dirs)

        if not self.embeddings:
            return []

        query_vec = self.model.encode(
            [f"query: {query}"], normalize_embeddings=True
        )[0]

        scores = []
        for path, vec in self.embeddings.items():
            score = float(np.dot(query_vec, vec))
            scores.append((path, score))
        scores.sort(key=lambda x: x[1], reverse=True)

        if not scores or scores[0][1] < threshold:
            return []

        results = []
        for path, score in scores[:top_k]:
            with open(path) as f:
                content = f.read()
            results.append({"path": path, "score": round(score, 4), "content": content})
        return results

    # ---- connection handling ----

    def handle_connection(self, conn):
        self.last_activity = time.time()
        try:
            data = b""
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
                if len(data) > MAX_MSG_SIZE:
                    conn.sendall(json.dumps({"status": "error", "error": "message too large"}).encode())
                    return

            request = json.loads(data.decode())
            results = self.search(
                request["query"],
                request["memory_dirs"],
                request.get("top_k", DEFAULT_TOP_K),
                request.get("threshold", DEFAULT_THRESHOLD),
            )
            response = {"status": "ok", "results": results}
            conn.sendall(json.dumps(response).encode())
        except BrokenPipeError:
            log.warning("Client disconnected before response was sent")
        except Exception as e:
            log.exception("Error handling request")
            response = {"status": "error", "error": str(e)}
            conn.sendall(json.dumps(response).encode())

    # ---- lifecycle ----

    def _idle_checker(self):
        if IDLE_TIMEOUT <= 0:
            return
        while self.running:
            time.sleep(60)
            if time.time() - self.last_activity > IDLE_TIMEOUT:
                log.info("Idle timeout reached, shutting down")
                self.running = False
                break

    def _check_stale(self):
        """Remove stale socket/PID from a previous crashed daemon."""
        if os.path.exists(PID_FILE):
            with open(PID_FILE) as f:
                old_pid = int(f.read().strip())
            try:
                os.kill(old_pid, 0)  # check if alive
                log.error("Another daemon is running (PID %d), exiting", old_pid)
                sys.exit(1)
            except OSError:
                log.info("Cleaning up stale PID %d", old_pid)
                if os.path.exists(SOCKET_PATH):
                    os.unlink(SOCKET_PATH)
                os.unlink(PID_FILE)
        elif os.path.exists(SOCKET_PATH):
            os.unlink(SOCKET_PATH)

    def _cleanup(self):
        if os.path.exists(SOCKET_PATH):
            os.unlink(SOCKET_PATH)
        if os.path.exists(PID_FILE):
            os.unlink(PID_FILE)

    def run(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        self._check_stale()

        with open(PID_FILE, "w") as f:
            f.write(str(os.getpid()))

        self.load_model()
        self.load_cache()

        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(SOCKET_PATH)
        server.listen(5)
        server.settimeout(1.0)

        if IDLE_TIMEOUT > 0:
            threading.Thread(target=self._idle_checker, daemon=True).start()

        def shutdown(signum, frame):
            self.running = False

        signal.signal(signal.SIGTERM, shutdown)
        signal.signal(signal.SIGINT, shutdown)

        log.info("Daemon started, listening on %s", SOCKET_PATH)

        try:
            while self.running:
                try:
                    conn, _ = server.accept()
                    try:
                        self.handle_connection(conn)
                    finally:
                        conn.close()
                except socket.timeout:
                    continue
                except OSError as e:
                    log.warning("Connection error (daemon continues): %s", e)
        finally:
            server.close()
            self._cleanup()
            log.info("Daemon stopped")


if __name__ == "__main__":
    EmbeddingDaemon().run()
