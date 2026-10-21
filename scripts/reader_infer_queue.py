"""Opt-in, crash-resumable, serial Reader inference queue for Mac/Apple Silicon.

Inspired by DeepInfer's bounded request scheduler, not a DeepInfer port.
This does not implement GPU batching, KV caches, or speculative decoding.
Existing reader_local.infer exclusively controls GearShift and MLX/Ollama.

Private local-only candidates; NEVER an authoritative rating or a Drive write.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import sqlite3
import tempfile
from pathlib import Path

from reader_colors import RATING_COLORS
from scripts.reader_local import Held, infer


class QueueError(RuntimeError):
    pass


class LocalInferenceQueue:
    """Single-consumer queue with stable identities and explicit held retries.

    Files and SQLite live in a private local work directory. Never point work
    at a public GitHub checkout, synced directory, or Actions artifact/cache.
    """

    def __init__(self, work: str | Path, *, max_pending: int = 512):
        if not 1 <= max_pending <= 10000:
            raise QueueError("INVALID_QUEUE_LIMIT")
        self.work = Path(work).expanduser().resolve()
        self.work.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.requests = self.work / "private-requests"
        self.requests.mkdir(parents=True, exist_ok=True, mode=0o700)
        for directory in (self.work, self.requests):
            if directory.stat().st_mode & 0o077:
                raise QueueError("PRIVATE_DIRECTORY_REQUIRED")
        self.max_pending = max_pending
        self.db = sqlite3.connect(
            self.work / "reader-inference-queue.sqlite", timeout=10
        )
        os.chmod(self.work / "reader-inference-queue.sqlite", 0o600)
        self.db.execute("PRAGMA busy_timeout=10000")
        self.db.execute("""CREATE TABLE IF NOT EXISTS jobs (
            key TEXT PRIMARY KEY, backend TEXT NOT NULL, model TEXT NOT NULL,
            context INTEGER NOT NULL, max_tokens INTEGER NOT NULL,
            state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
            outcome TEXT, reason TEXT, submitted_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
        self.db.commit()

    def _exclusive(self):
        lock = (self.work / "reader-inference-queue.lock").open("a")
        fcntl.flock(lock, fcntl.LOCK_EX)
        return lock

    def submit(self, *, backend: str, model: str, prompt: str, source_text: str,
               context: int = 4096, max_tokens: int = 256) -> str:
        if (backend not in {"ollama", "mlx"} or not isinstance(model, str)
            or not model or len(model) > 512 or not isinstance(prompt, str)
            or not isinstance(source_text, str) or not source_text.strip()
            or not 512 <= context <= 4096 or not 1 <= max_tokens <= 512):
            raise QueueError("INVALID_REQUEST")
        if not prompt.strip() or len(prompt.encode("utf-8")) > context - max_tokens:
            raise QueueError("PROMPT_EXCEEDS_CONTEXT")
        if len(source_text.encode("utf-8")) > 32 * 1024:
            raise QueueError("SOURCE_TOO_LARGE")
        request = {"backend": backend, "model": model, "prompt": prompt,
                   "source_text": source_text, "context": context,
                   "max_tokens": max_tokens}
        payload = json.dumps(
            request, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
        key = hashlib.sha256(payload).hexdigest()
        with self._exclusive():
            if self.db.execute(
                "SELECT 1 FROM jobs WHERE key=?", (key,)
            ).fetchone():
                return key
            pending = self.db.execute(
                "SELECT COUNT(*) FROM jobs WHERE state IN ('pending','running')"
            ).fetchone()[0]
            if pending >= self.max_pending:
                raise QueueError("QUEUE_FULL")
            with tempfile.NamedTemporaryFile(
                dir=self.requests, prefix=".staging-", delete=False
            ) as temp:
                os.fchmod(temp.fileno(), 0o600)
                temp.write(payload)
                temp.flush()
                os.fsync(temp.fileno())
                temporary = Path(temp.name)
            os.replace(temporary, self.requests / (key + ".json"))
            with self.db:
                self.db.execute(
                    """INSERT INTO jobs(key,backend,model,context,max_tokens,state)
                       VALUES(?,?,?,?,?,'pending')""",
                    (key, backend, model, context, max_tokens)
                )
        return key

    @staticmethod
    def _candidate(raw: str, source: str) -> tuple[str, str, str | None]:
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return "review_required", "INVALID_JSON", None
        if (not isinstance(parsed, dict)
            or set(parsed) != {"color", "topic", "reason", "evidence_quote"}):
            return "review_required", "INVALID_SCHEMA", None
        color, topic, reason, quote = (
            parsed[k] for k in ("color", "topic", "reason", "evidence_quote")
        )
        if (color not in RATING_COLORS
            or not isinstance(topic, str) or not topic.strip()
            or len(topic) > 80
            or not isinstance(reason, str) or not reason.strip()
            or len(reason) > 800
            or not isinstance(quote, str) or not quote.strip()
            or quote not in source or len(quote) > 500):
            return "review_required", "UNSUPPORTED_CLAIM", None
        result = json.dumps(
            {"color": color, "topic": topic.strip(), "reason": reason,
             "evidence_quote": quote, "status": "classified_candidate"},
            ensure_ascii=False
        )
        return "candidate", "MODEL_CANDIDATE_ONLY", result

    def run(self, *, amando_root: str | Path, infer_fn=None,
            max_items: int = 100) -> dict:
        if not 1 <= max_items <= 10000:
            raise QueueError("INVALID_RUN_LIMIT")
        executor = infer if infer_fn is None else infer_fn
        lock = (self.work / "reader-inference-consumer.lock").open("a")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            raise QueueError("CONSUMER_BUSY") from None
        stats = {"candidate": 0, "review_required": 0, "held": 0}
        try:
            # A crashed process may leave running jobs; re-execute same key.
            with self.db:
                self.db.execute(
                    "UPDATE jobs SET state='pending' WHERE state='running'"
                )
            for _ in range(max_items):
                job = self.db.execute(
                    """SELECT key FROM jobs WHERE state='pending'
                       ORDER BY submitted_at, rowid LIMIT 1"""
                ).fetchone()
                if not job:
                    break
                key = job[0]
                with self.db:
                    self.db.execute(
                        "UPDATE jobs SET state='running',attempts=attempts+1 WHERE key=?",
                        (key,)
                    )
                try:
                    data = json.loads(
                        (self.requests / (key + ".json")).read_text()
                    )
                    packed = json.dumps(
                        data, ensure_ascii=False, sort_keys=True,
                        separators=(",", ":")
                    ).encode()
                    if hashlib.sha256(packed).hexdigest() != key:
                        raise QueueError("REQUEST_HASH_MISMATCH")
                    result = executor(
                        data["backend"], data["model"], data["prompt"],
                        amando_root=amando_root, work=self.work,
                        context=data["context"], max_tokens=data["max_tokens"]
                    )
                    state, reason, outcome = self._candidate(
                        result["output"], data["source_text"]
                    )
                except Held as exc:
                    code = str(exc)
                    if not (3 <= len(code) <= 80 and
                            all(c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_"
                                for c in code)):
                        code = "LOCAL_HELD"
                    state, reason, outcome = "held", code, None
                except Exception:
                    # Exceptions may contain private source; do not persist.
                    state, reason, outcome = "held", "LOCAL_EXECUTION_FAILED", None
                with self.db:
                    self.db.execute(
                        "UPDATE jobs SET state=?,reason=?,outcome=? WHERE key=?",
                        (state, reason, outcome, key)
                    )
                stats[state] += 1
            stats["pending"] = self.db.execute(
                "SELECT COUNT(*) FROM jobs WHERE state='pending'"
            ).fetchone()[0]
            return stats
        finally:
            lock.close()

    def status(self) -> dict:
        return dict(self.db.execute(
            "SELECT state,COUNT(*) FROM jobs GROUP BY state"
        ).fetchall())

    def retry_held(self) -> int:
        # Explicit retry only. Never spin on GearShift denial.
        with self.db:
            cursor = self.db.execute(
                """UPDATE jobs SET state='pending',reason=NULL
                   WHERE state='held' AND attempts<2"""
            )
        return cursor.rowcount

    def close(self):
        self.db.close()


def main():
    parser = argparse.ArgumentParser(
        description="Private, optional Reader local candidate inference queue"
    )
    parser.add_argument("--work", required=True)
    subs = parser.add_subparsers(dest="command", required=True)
    enqueue = subs.add_parser("submit")
    enqueue.add_argument("--backend", choices=["mlx", "ollama"], required=True)
    enqueue.add_argument("--model", required=True)
    enqueue.add_argument("--prompt-file", type=Path, required=True)
    enqueue.add_argument("--source-file", type=Path, required=True)
    process = subs.add_parser("run")
    process.add_argument("--amando-root", required=True)
    process.add_argument("--max-items", type=int, default=100)
    subs.add_parser("status")
    subs.add_parser("retry-held")
    args = parser.parse_args()
    queue = LocalInferenceQueue(args.work)
    try:
        if args.command == "submit":
            result = {"key": queue.submit(
                backend=args.backend, model=args.model,
                prompt=args.prompt_file.read_text(),
                source_text=args.source_file.read_text()
            )}
        elif args.command == "run":
            result = queue.run(
                amando_root=args.amando_root, max_items=args.max_items
            )
        elif args.command == "retry-held":
            result = {"requeued": queue.retry_held()}
        else:
            result = queue.status()
        # Never print article text, prompts, credentials or model outputs.
        print(json.dumps(result, ensure_ascii=False))
    finally:
        queue.close()


if __name__ == "__main__":
    main()
