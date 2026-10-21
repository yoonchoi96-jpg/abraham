"""Read-only, resumable offline semantic screening of already staged Reader bodies.

No Reader/Drive writes and no DeepSeek calls. The model is an *already locally
installed* Ollama embedding model, governed by the same GearShift admission.
Semantic redundancy remains a candidate only until independent review.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import urllib.request

import numpy as np
from pathlib import Path

from scripts.reader_drive_stage import _private_work
from scripts.reader_intake import parse_document
from scripts.reader_local import GearShift, Held, budget_for, required_context
from scripts.reader_novelty import NoveltyIndex, normalized_embedding


# DeepInfer's chunked-prefill scheduler preserves the entire token sequence
# while admitting bounded work. This is the analogous *API-side* batching
# policy for Ollama /api/embed; it is NOT DeepInfer's GPU KV-cache algorithm.
# We deliberately retain every UTF-8 codepoint: silently embedding only the
# opening 3,700 characters can miss late-breaking evidence in an article.
EMBED_CHUNK_BYTES = 3000
EMBED_BATCH_BYTES = 12000
EMBED_MAX_INPUTS = 65
EMBED_MAX_CHUNKS = 128


def _utf8_chunks(value: str, max_bytes: int = EMBED_CHUNK_BYTES) -> list[str]:
    """Bound local model context without truncating any evidence or Unicode."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("NONEMPTY_EMBEDDING_TEXT_REQUIRED")
    result, current, size = [], [], 0
    for char in value:
        encoded = len(char.encode("utf-8"))
        if current and size + encoded > max_bytes:
            result.append("".join(current))
            current, size = [], 0
        current.append(char)
        size += encoded
    if current:
        result.append("".join(current))
    return result


def _embedding_batches(chunks: list[str]) -> list[list[str]]:
    """FIFO admission bounded by request bytes and count, no dropped chunks."""
    batches, current, size = [], [], 0
    for part in chunks:
        part_bytes = len(part.encode("utf-8"))
        if part_bytes > EMBED_BATCH_BYTES:
            raise ValueError("EMBEDDING_CHUNK_OVER_BUDGET")
        if current and (len(current) >= EMBED_MAX_INPUTS or
                        size + part_bytes > EMBED_BATCH_BYTES):
            batches.append(current)
            current, size = [], 0
        current.append(part)
        size += part_bytes
    if current:
        batches.append(current)
    return batches


class GuardedLocalEmbedder:
    def __init__(self, model: str, amando_root: str):
        if not model or len(model) > 256:
            raise ValueError("LOCAL_MODEL_REQUIRED")
        self.model = model
        self.gear = GearShift(amando_root)
        self.last_call_stats = {}
        self.total_stats = {"texts": 0, "chunks": 0, "local_requests": 0,
                            "input_bytes": 0, "truncated_bytes": 0}

    def __call__(self, texts: list[str]) -> list[list[float]]:
        if not texts or len(texts) > EMBED_MAX_INPUTS:
            raise ValueError("BOUNDED_EMBEDDING_BATCH_REQUIRED")
        chunks, owner = [], []
        for index, content in enumerate(texts):
            parts = _utf8_chunks(content)
            chunks.extend(parts)
            owner.extend([index] * len(parts))
        if len(chunks) > EMBED_MAX_CHUNKS:
            # Never silently use a prefix for a semantic skip decision.
            raise Held("EMBEDDING_CHUNK_BUDGET_EXCEEDED")
        # Rollback mode disables multi-chunk pooling without returning to
        # unsafe head-only 3,700-character truncation.
        if os.getenv("ABRAHAM_NOVELTY_CHUNKED_EMBEDDINGS", "1") == "0" and (
                any(owner.count(i) > 1 for i in range(len(texts)))):
            raise Held("CHUNKED_EMBEDDINGS_DISABLED")
        vectors, requests = [], 0
        for batch in _embedding_batches(chunks):
            context = max(required_context(t, 1, 4096) for t in batch)
            budget = budget_for("ollama", self.model, context, self.gear)
            self.gear.check(budget)
            req = urllib.request.Request(
                "http://127.0.0.1:11434/api/embed",
                data=json.dumps({"model": self.model, "input": batch,
                                 "truncate": False, "keep_alive": 0}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=240) as resp:
                    data = json.load(resp)
            except Exception:
                raise Held("LOCAL_EMBEDDING_UNAVAILABLE") from None
            batch_vectors = data.get("embeddings", [])
            if not isinstance(batch_vectors, list) or len(batch_vectors) != len(batch):
                raise Held("LOCAL_EMBEDDING_INVALID_RESPONSE")
            try:
                vectors.extend(normalized_embedding(v) for v in batch_vectors)
            except ValueError:
                raise Held("LOCAL_EMBEDDING_INVALID_RESPONSE") from None
            self.gear.check(budget, owned=self.model)
            requests += 1
            self.total_stats["local_requests"] += 1

        # Each document is represented by a byte-weighted, unit-normalized
        # mean of ALL its chunks. No article suffix is discarded. This is
        # lossy semantic retrieval only; claim-level evidence verification
        # still decides whether a candidate can be marked redundant.
        pooled = [[] for _ in texts]
        weights = [[] for _ in texts]
        for index, part, vector in zip(owner, chunks, vectors):
            pooled[index].append(vector)
            weights[index].append(len(part.encode("utf-8")))
        result = []
        try:
            for group, sizes in zip(pooled, weights):
                if len({v.size for v in group}) != 1:
                    raise ValueError("EMBEDDING_DIMENSIONS_CHANGED")
                mean = np.average(np.stack(group), axis=0, weights=sizes)
                result.append(normalized_embedding(mean).tolist())
        except ValueError:
            raise Held("LOCAL_EMBEDDING_INVALID_RESPONSE") from None
        self.last_call_stats = {
            "texts": len(texts), "chunks": len(chunks), "local_requests": requests,
            "input_bytes": sum(len(t.encode("utf-8")) for t in texts),
            "truncated_bytes": 0,
        }
        for field in ("texts", "chunks", "input_bytes"):
            self.total_stats[field] += self.last_call_stats[field]
        return result

def scan(work: Path, *, model: str, embed, max_items: int = 100, retry_held: bool = False) -> dict:
    if not 1 <= max_items <= 100000:
        raise ValueError("BOUNDED_SCAN_REQUIRED")
    work = _private_work(work)
    stage_path = work / "reader-library-stage.sqlite"
    bulk_path = work / "bulk.sqlite"
    if not stage_path.is_file() or not bulk_path.is_file():
        raise RuntimeError("DRIVE_STAGE_MISSING")
    import fcntl
    lock = (work / "semantic-novelty.lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise RuntimeError("SEMANTIC_SCAN_BUSY") from None
    stage_db = sqlite3.connect(stage_path)
    bulk_db = sqlite3.connect(bulk_path)
    novelty_path = work / "reader-novelty.sqlite"
    novelty_db = sqlite3.connect(novelty_path)
    os.chmod(novelty_path, 0o600)
    try:
        index = NoveltyIndex(novelty_db, model_id=model)
        novelty_db.execute("""CREATE TABLE IF NOT EXISTS novelty_held (
            document_key TEXT NOT NULL, model_id TEXT NOT NULL,
            reason TEXT NOT NULL,
            PRIMARY KEY(document_key,model_id)
        )""")
        # Idempotent resume checks the original source fingerprint rather
        # than blindly trusting the last decision for a stable Drive ID.
        # Existing databases migrate automatically, without deleting data.
        novelty_db.execute("""CREATE TABLE IF NOT EXISTS novelty_source_v1 (
            document_key TEXT NOT NULL, model_id TEXT NOT NULL,
            raw_sha256 TEXT NOT NULL, source_name TEXT NOT NULL,
            PRIMARY KEY(document_key,model_id)
        )""")
        completed = blocked = 0
        rows = stage_db.execute("""SELECT job_key,drive_file_id FROM stage
            WHERE state IN ('review_required','existing_tag_unverified','duplicate')
            ORDER BY rowid""")
        for key, drive_id in rows:
            if completed + blocked >= max_items:
                break
            if not retry_held and novelty_db.execute(
                "SELECT 1 FROM novelty_held WHERE document_key=? AND model_id=?",
                (key, model)
            ).fetchone():
                continue
            raw = work / (key + ".source")
            try:
                if not raw.is_file():
                    raise ValueError("RAW_SOURCE_NOT_FOUND")
                meta = bulk_db.execute(
                    "SELECT metadata FROM jobs WHERE key=?", (key,)
                ).fetchone()
                if not meta:
                    raise ValueError("SOURCE_METADATA_NOT_FOUND")
                obj = json.loads(meta[0])
                if obj["id"] != drive_id:
                    raise ValueError("SOURCE_METADATA_MISMATCH")
                name = obj.get("name") or obj["id"] + ".html"
                original_bytes = raw.read_bytes()
                source_sha = hashlib.sha256(original_bytes).hexdigest()
                prior_source = novelty_db.execute(
                    """SELECT raw_sha256,source_name FROM novelty_source_v1
                       WHERE document_key=? AND model_id=?""", (key, model)
                ).fetchone()
                if prior_source == (source_sha, name) and index.is_current(key):
                    continue
                doc = parse_document(name, original_bytes)
                index.assess(key, doc["title"], doc["content"], embed)
                with novelty_db:
                    novelty_db.execute(
                        """INSERT OR REPLACE INTO novelty_source_v1 VALUES(?,?,?,?)""",
                        (key, model, source_sha, name)
                    )
                    # Explicit retrial succeeded; clear the quarantine marker.
                    novelty_db.execute(
                        "DELETE FROM novelty_held WHERE document_key=? AND model_id=?",
                        (key, model)
                    )
                completed += 1
            except (Held, ValueError, OSError, RuntimeError):
                # No source document or exception is put into status/logs.
                with novelty_db:
                    novelty_db.execute(
                        "INSERT OR REPLACE INTO novelty_held VALUES(?,?,?)",
                        (key, model, "LOCAL_SCREEN_UNAVAILABLE")
                    )
                blocked += 1
        return {"evaluated": completed, "held": blocked,
                "decisions": index.counts(),
                "local_embedding_metrics": getattr(embed, "total_stats", None),
                "deepseek_api_calls": 0, "drive_writes": 0,
                "automatic_api_suppression": False, "retried_held": retry_held}
    finally:
        novelty_db.close()
        bulk_db.close()
        stage_db.close()
        lock.close()


def main():
    p = argparse.ArgumentParser(description="Local Reader novelty candidate scan")
    p.add_argument("--work", type=Path, required=True)
    p.add_argument("--model", required=True, help="Existing local Ollama embedding model")
    p.add_argument("--amando-root", required=True)
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--retry-held", action="store_true",
                   help="Explicitly retry previously held local-only jobs, once per scan")
    args = p.parse_args()
    embedder = GuardedLocalEmbedder(args.model, args.amando_root)
    result = scan(args.work, model=args.model, embed=embedder,
                  max_items=args.limit, retry_held=args.retry_held)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
