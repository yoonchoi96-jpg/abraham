#!/usr/bin/env python3
"""Validate and prepare an existing V4.61 SQLite state for Abraham V5.6.

This script never mutates the source DB. It copies it to the runtime path,
runs SQLite integrity checks, reports the V4.61 baseline tables/counts, and
leaves the resulting file ready for the production Release Asset upload.
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
from pathlib import Path

def scalar(conn, sql):
    row = conn.execute(sql).fetchone()
    return row[0] if row else None

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--source", required=True)
    p.add_argument("--output", required=True)
    args = p.parse_args()

    src = Path(args.source).expanduser().resolve()
    out = Path(args.output).expanduser().resolve()

    if not src.is_file():
        raise SystemExit(f"Source DB not found: {src}")
    if src.stat().st_size == 0:
        raise SystemExit("Source DB is empty.")

    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, out)

    conn = sqlite3.connect(f"file:{out}?mode=ro", uri=True)
    integrity = scalar(conn, "PRAGMA integrity_check")
    if integrity != "ok":
        raise SystemExit(f"SQLite integrity_check failed: {integrity}")

    tables = [
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
    ]
    print(f"Source size      : {src.stat().st_size:,} bytes")
    print(f"Prepared size    : {out.stat().st_size:,} bytes")
    print(f"Integrity        : {integrity}")
    print(f"Tables           : {', '.join(tables)}")

    for table in ("documents", "entities", "document_entities", "entity_relations",
                  "document_stages", "ratings"):
        if table in tables:
            print(f"{table:18}: {scalar(conn, f'SELECT COUNT(*) FROM {table}')}")
    conn.close()
    print(f"READY: {out}")

if __name__ == "__main__":
    main()
