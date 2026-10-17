#!/usr/bin/env python3
"""Export opaque Reader document IDs for a *private* GitHub queue handoff.

Output MUST NEVER be committed to the public Abraham repository. It contains
personal document IDs. GPT uses Reader as the source of truth for document
content, grade and seen-state; this manifest is only a discovery hint.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SHARD_SIZE = 1000


def build_manifest(db: Path, dest: Path, shard_size: int = SHARD_SIZE) -> dict:
    if not db.is_file():
        raise FileNotFoundError(f"Missing canonical SQLite: {db}")
    if shard_size < 1 or shard_size > 1000:
        raise ValueError("shard_size must be in 1..1000")
    conn = sqlite3.connect(f"file:{db.resolve()}?mode=ro", uri=True)
    try:
        existing = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        if "reader_first_pass_queue" not in existing:
            raise RuntimeError("Reader queue state unavailable; do not publish")
        rows = conn.execute(
            """SELECT readwise_id, state FROM reader_first_pass_queue
               WHERE state IN ('tagged_unseen','pending')
                 AND source_location='feed'
               ORDER BY CASE state WHEN 'tagged_unseen' THEN 0 ELSE 1 END,
                        source_updated_at ASC, readwise_id ASC"""
        ).fetchall()
    finally:
        conn.close()
    # Do not allow a path anywhere within the tracked git workspace by
    # default; the caller must choose an ephemeral output directory.
    dest.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    manifest = {
        "schema": "abraham-reader-queue-v1",
        "generated_at": now,
        "source": "reader-observation-snapshot",
        "source_of_truth": "Readwise Reader API (revalidate before mutation)",
        "total_candidates": len(rows),
        "shards": [],
    }
    for start in range(0, len(rows), shard_size):
        entries = [{"id": rid, "state": state}
                   for rid, state in rows[start:start+shard_size]]
        name = f"pending-{start//shard_size+1:04d}.json"
        payload = {
            "schema": "abraham-reader-queue-v1",
            "generated_at": now,
            "items": entries,
        }
        body = json.dumps(payload, ensure_ascii=False,
                          separators=(",", ":"), sort_keys=True) + "\n"
        (dest / name).write_text(body, encoding="utf-8")
        manifest["shards"].append({
            "filename": name,
            "count": len(entries),
            "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        })
    (dest / "index.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--shard-size", type=int, default=SHARD_SIZE)
    args = parser.parse_args()
    result = build_manifest(args.db, args.output_dir, args.shard_size)
    # Safe diagnostics only: never print Reader IDs or private file contents.
    print(f"READER_QUEUE_EXPORTED candidates={result['total_candidates']} "
          f"shards={len(result['shards'])}")


if __name__ == "__main__":
    main()
