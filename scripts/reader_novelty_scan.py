"""Read-only, resumable offline semantic screening of already staged Reader bodies.

No Reader/Drive writes and no DeepSeek calls. The model is an *already locally
installed* Ollama embedding model, governed by the same GearShift admission.
Semantic redundancy remains a candidate only until independent review.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import urllib.request
from pathlib import Path

from scripts.reader_drive_stage import _private_work
from scripts.reader_intake import parse_document
from scripts.reader_local import GearShift, Held, budget_for, required_context
from scripts.reader_novelty import NoveltyIndex


class GuardedLocalEmbedder:
    def __init__(self, model: str, amando_root: str):
        if not model or len(model) > 256:
            raise ValueError("LOCAL_MODEL_REQUIRED")
        self.model = model
        self.gear = GearShift(amando_root)

    def __call__(self, texts: list[str]) -> list[list[float]]:
        if not texts or len(texts) > 65:
            raise ValueError("BOUNDED_EMBEDDING_BATCH_REQUIRED")
        # The same per-batch live GearShift check as other local inference.
        # Do not reserve 4096 context for a short embedding prompt.
        context = max(required_context(t[:3700], 1, 4096) for t in texts)
        budget = budget_for("ollama", self.model, context, self.gear)
        self.gear.check(budget)
        inputs = [t[:3700] for t in texts]
        req = urllib.request.Request(
            "http://127.0.0.1:11434/api/embed",
            data=json.dumps({"model": self.model, "input": inputs,
                             "truncate": False, "keep_alive": 0}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=240) as resp:
                data = json.load(resp)
        except Exception:
            raise Held("LOCAL_EMBEDDING_UNAVAILABLE") from None
        vectors = data.get("embeddings", [])
        if not isinstance(vectors, list) or len(vectors) != len(inputs):
            raise Held("LOCAL_EMBEDDING_INVALID_RESPONSE")
        self.gear.check(budget, owned=self.model)
        return vectors


def scan(work: Path, *, model: str, embed, max_items: int = 100) -> dict:
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
    novelty_db = sqlite3.connect(work / "reader-novelty.sqlite")
    try:
        index = NoveltyIndex(novelty_db, model_id=model)
        novelty_db.execute("""CREATE TABLE IF NOT EXISTS novelty_held (
            document_key TEXT NOT NULL, model_id TEXT NOT NULL,
            reason TEXT NOT NULL,
            PRIMARY KEY(document_key,model_id)
        )""")
        completed = blocked = 0
        rows = stage_db.execute("""SELECT job_key,drive_file_id FROM stage
            WHERE state IN ('review_required','existing_tag_unverified','duplicate')
            ORDER BY rowid""")
        for key, drive_id in rows:
            if completed + blocked >= max_items:
                break
            if novelty_db.execute(
                "SELECT 1 FROM novelty_v1 WHERE document_key=? AND model_id=?",
                (key, model)
            ).fetchone():
                continue
            if novelty_db.execute(
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
                doc = parse_document(name, raw.read_bytes())
                index.assess(key, doc["title"], doc["content"], embed)
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
                "deepseek_api_calls": 0, "drive_writes": 0,
                "automatic_api_suppression": False}
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
    args = p.parse_args()
    embedder = GuardedLocalEmbedder(args.model, args.amando_root)
    result = scan(args.work, model=args.model, embed=embedder, max_items=args.limit)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
