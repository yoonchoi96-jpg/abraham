#!/usr/bin/env python3
"""Abraham V6→V9 runner with bounded-memory local processing."""
from __future__ import annotations
import argparse
import json

from abraham_sqlite import connect
from v6_semantic import ensure_schema as v6_schema, index_documents, search
from v7_research import ensure_schema as v7_schema
from v8_autonomous import ensure_schema as v8_schema, detect
from v9_intelligence import ensure_schema as v9_schema, process as v9_process, status as v9_status


def _columns(conn, table):
    return {str(r["name"]) for r in conn.execute(f'PRAGMA table_info("{table}")').fetchall()}


def _iter_semantic_documents(conn, batch_size=1000):
    # Use Readwise updated_at as a cheap first-pass filter. index_documents()
    # still performs the authoritative content-hash check.
    cols = _columns(conn, "documents")
    order = [f"d.{x}" for x in ("updated_at", "last_processed_at", "first_seen_at") if x in cols]
    order_sql = "COALESCE(" + ",".join(order + ["d.rowid"]) + ") DESC" if order else "d.rowid DESC"

    if "updated_at" in cols:
        sql = f"""
            SELECT d.readwise_id,d.title,d.content,d.canonical_text
            FROM documents d
            LEFT JOIN v6_vectors v ON v.readwise_id=d.readwise_id
            WHERE v.readwise_id IS NULL
               OR v.updated_at IS NULL
               OR d.updated_at > v.updated_at
            ORDER BY {order_sql}
            LIMIT ? OFFSET ?
        """
    else:
        sql = f"""
            SELECT d.readwise_id,d.title,d.content,d.canonical_text
            FROM documents d
            LEFT JOIN v6_vectors v ON v.readwise_id=d.readwise_id
            WHERE v.readwise_id IS NULL
            ORDER BY {order_sql}
            LIMIT ? OFFSET ?
        """

    offset = 0
    while True:
        rows = conn.execute(sql, (batch_size, offset)).fetchall()
        if not rows:
            return
        yield [dict(r) for r in rows]
        offset += len(rows)


def _latest_stage_documents(conn, limit):
    rows = conn.execute(
        """SELECT DISTINCT readwise_id
           FROM document_stages
           WHERE status='completed'
           ORDER BY updated_at DESC
           LIMIT ?""",
        (max(1, min(int(limit), 5000)),),
    ).fetchall()
    ids = [str(r["readwise_id"]) for r in rows]
    if not ids:
        return []
    ph = ",".join("?" for _ in ids)
    return [
        dict(r)
        for r in conn.execute(
            f"SELECT readwise_id,title,content,canonical_text FROM documents WHERE readwise_id IN ({ph})",
            ids,
        ).fetchall()
    ]


def _index_all(conn):
    total = {"added": 0, "updated": 0, "skipped": 0}
    for batch in _iter_semantic_documents(conn):
        result = index_documents(conn, batch)
        for key in total:
            total[key] += result.get(key, 0)
    return total


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=("index", "search", "research", "autonomous", "status"), default="autonomous")
    p.add_argument("--query")
    p.add_argument("--limit", type=int, default=250)
    p.add_argument("--question")
    a = p.parse_args()

    c = connect()
    # Each subsystem owns its schema. The previous runner only initialized
    # document_stages here, leaving V6 tables absent on restored production DBs.
    v6_schema(c)
    v7_schema(c)
    v8_schema(c)
    v9_schema(c)

    if a.mode == "index":
        out = {"v6": _index_all(c)}
    elif a.mode == "search":
        if not a.query:
            raise SystemExit("--query required")
        out = {"query": a.query, "results": search(c, a.query, a.limit)}
    elif a.mode == "research":
        if not a.question:
            raise SystemExit("--question required")
        from v7_research import run_research
        out = {"v7": run_research(c, a.question, a.limit)}
    elif a.mode == "autonomous":
        out = {
            "v6": _index_all(c),
            "v8": detect(c, _latest_stage_documents(c, a.limit), a.limit),
            "v9": v9_process(c, a.limit),
        }
    else:
        out = {
            "v6_vectors": c.execute("SELECT COUNT(*) FROM v6_vectors").fetchone()[0],
            "v7_research_runs": c.execute("SELECT COUNT(*) FROM v7_research_runs").fetchone()[0],
            "v8_events": c.execute("SELECT COUNT(*) FROM v8_events").fetchone()[0],
            "v8_insights": c.execute("SELECT COUNT(*) FROM v8_insights").fetchone()[0],
            "v9": v9_status(c),
        }

    print(json.dumps(out, ensure_ascii=False))
    c.close()


if __name__ == "__main__":
    main()
