#!/usr/bin/env python3
"""Deterministically canonicalize and deduplicate the existing relation graph."""
from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timezone

from entity_relation_ai import _canonical_relation
from abraham_sqlite import connect as connect_db

MIGRATION_ID = "relation_canonicalization_v1"

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS relation_canonicalization_audit (
        audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
        migration_id TEXT NOT NULL,
        relation_id INTEGER NOT NULL,
        old_relation_type TEXT NOT NULL,
        new_relation_type TEXT NOT NULL,
        survivor_id INTEGER,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS relation_evidence (
        evidence_id INTEGER PRIMARY KEY AUTOINCREMENT,
        relation_id INTEGER NOT NULL,
        readwise_id TEXT NOT NULL,
        confidence REAL,
        evidence_quote TEXT,
        evidence_start_hint TEXT,
        created_at TEXT NOT NULL,
        UNIQUE(relation_id, readwise_id, evidence_quote)
    );
    CREATE INDEX IF NOT EXISTS idx_relation_evidence_relation
        ON relation_evidence(relation_id);
    """)

def consolidate(conn: sqlite3.Connection) -> dict:
    ensure_schema(conn)
    rows = conn.execute(
        "SELECT relation_id,readwise_id,subject_entity_id,relation_type,"
        "object_entity_id,confidence,evidence_quote,evidence_start_hint,created_at "
        "FROM relations ORDER BY relation_id"
    ).fetchall()
    seen = {}
    normalized = 0
    merged = 0
    evidence = 0
    for row in rows:
        rid, readwise_id, subject, old_type, object_, confidence, quote, hint, created_at = row
        new_type = _canonical_relation(old_type)
        key = (str(subject), new_type, str(object_))
        if new_type != old_type:
            conn.execute("UPDATE relations SET relation_type=? WHERE relation_id=?", (new_type, rid))
            conn.execute(
                "INSERT INTO relation_canonicalization_audit "
                "(migration_id,relation_id,old_relation_type,new_relation_type,survivor_id,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (MIGRATION_ID, rid, old_type, new_type, rid, _now()),
            )
            normalized += 1
        survivor = seen.get(key)
        if survivor is None:
            seen[key] = rid
            survivor = rid
        else:
            conn.execute(
                "UPDATE relations SET confidence=MAX(COALESCE(confidence,0), ?) WHERE relation_id=?",
                (confidence, survivor),
            )
            conn.execute(
                "UPDATE relation_canonicalization_audit SET survivor_id=? "
                "WHERE migration_id=? AND relation_id=?",
                (survivor, MIGRATION_ID, rid),
            )
            conn.execute("DELETE FROM relations WHERE relation_id=?", (rid,))
            merged += 1
        if quote:
            cur = conn.execute(
                "SELECT 1 FROM relation_evidence WHERE relation_id=? AND readwise_id=? AND evidence_quote=?",
                (survivor, str(readwise_id), str(quote)),
            ).fetchone()
            if not cur:
                conn.execute(
                    "INSERT INTO relation_evidence "
                    "(relation_id,readwise_id,confidence,evidence_quote,evidence_start_hint,created_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (survivor, str(readwise_id), confidence, str(quote), str(hint or ""), created_at or _now()),
                )
                evidence += 1
    conn.commit()
    return {"scanned": len(rows), "normalized": normalized, "merged": merged, "evidence_added": evidence}

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=None)
    args = parser.parse_args()
    conn = sqlite3.connect(args.db) if args.db else connect_db()
    try:
        result = consolidate(conn)
        print("RELATION_CONSOLIDATION_OK")
        for key, value in result.items():
            print(f"{key}={value}")
    finally:
        conn.close()

if __name__ == "__main__":
    main()
