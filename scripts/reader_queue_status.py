#!/usr/bin/env python3
"""Read-only, aggregate-only health report for Reader ingestion.

Does NOT emit document IDs, titles, cursors, content or secrets.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


def summarize(db: Path) -> dict:
    conn = sqlite3.connect(f"file:{db.resolve()}?mode=ro", uri=True)
    try:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        if "reader_first_pass_queue" not in tables or "reader_ingest_progress" not in tables:
            return {"ready": False, "reason": "queue tables not installed"}
        states = dict(conn.execute(
            "SELECT state, COUNT(*) FROM reader_first_pass_queue GROUP BY state"
        ).fetchall())
        lanes = {}
        for name, window_start, window_end, page_cursor, pages in conn.execute(
            "SELECT name,window_start,window_end,page_cursor,pages "
            "FROM reader_ingest_progress"
        ):
            lanes[name] = {
                "scan_in_progress": window_end is not None,
                "resumable_page_cursor": page_cursor is not None,
                "pages_in_window": pages,
                "watermark_established": window_start is not None,
            }
        return {
            "ready": True,
            "candidate_pending": states.get("pending", 0),
            "tagged_but_unseen": states.get("tagged_unseen", 0),
            "graded_seen": states.get("graded_seen", 0),
            "seen_without_grade": states.get("seen_untagged", 0),
            "conflict": states.get("conflict", 0),
            "unverified": states.get("unverified", 0),
            "lanes": lanes,
            "warning": "Last observed Reader snapshot, not a live GPT completion ledger",
        }
    finally:
        conn.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--db", type=Path, required=True)
    args = p.parse_args()
    print("READER_QUEUE_STATUS " + json.dumps(
        summarize(args.db), ensure_ascii=False, sort_keys=True
    ))
