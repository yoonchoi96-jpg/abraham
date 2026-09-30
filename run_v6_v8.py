#!/usr/bin/env python3
"""Abraham V6→V8 runner with bounded-memory local processing."""
from __future__ import annotations
import argparse,json
from abraham_sqlite import connect
from abraham_incremental import ensure_schema
from v6_semantic import index_documents,search
from v7_research import ensure_schema as v7_schema
from v8_autonomous import ensure_schema as v8_schema,detect

def _columns(conn, table):
    return {str(r["name"]) for r in conn.execute(f'PRAGMA table_info("{table}")').fetchall()}

def _iter_semantic_documents(conn,batch_size=1000):
    cols=_columns(conn,"documents")
    order=[x for x in ("updated_at","last_processed_at","first_seen_at") if x in cols]
    order_sql="COALESCE("+",".join(order+["rowid"])+") DESC" if order else "rowid DESC"
    offset=0
    while True:
        rows=conn.execute(f"SELECT readwise_id,title,content,canonical_text FROM documents ORDER BY {order_sql} LIMIT ? OFFSET ?",(batch_size,offset)).fetchall()
        if not rows: return
        yield [dict(r) for r in rows]
        offset += len(rows)

def _latest_stage_documents(conn,limit):
    rows=conn.execute("""SELECT DISTINCT readwise_id FROM document_stages WHERE status='completed' ORDER BY updated_at DESC LIMIT ?""",(max(1,min(int(limit),250)),)).fetchall()
    ids=[str(r["readwise_id"]) for r in rows]
    if not ids:return []
    ph=",".join("?" for _ in ids)
    return [dict(r) for r in conn.execute(f"SELECT readwise_id,title,content,canonical_text FROM documents WHERE readwise_id IN ({ph})",ids).fetchall()]

def _index_all(conn):
    total={"added":0,"updated":0,"skipped":0}
    for batch in _iter_semantic_documents(conn):
        result=index_documents(conn,batch)
        for key in total: total[key]+=result.get(key,0)
    return total

def main():
    p=argparse.ArgumentParser(); p.add_argument("--mode",choices=("index","search","research","autonomous","status"),default="autonomous"); p.add_argument("--query"); p.add_argument("--limit",type=int,default=250); p.add_argument("--question"); a=p.parse_args()
    c=connect(); ensure_schema(c); v7_schema(c); v8_schema(c)
    if a.mode=="index": out={"v6":_index_all(c)}
    elif a.mode=="search":
        if not a.query: raise SystemExit("--query required")
        out={"query":a.query,"results":search(c,a.query,a.limit)}
    elif a.mode=="research":
        if not a.question: raise SystemExit("--question required")
        from v7_research import run_research
        out={"v7":run_research(c,a.question,a.limit)}
    elif a.mode=="autonomous":
        out={"v6":_index_all(c),"v8":detect(c,_latest_stage_documents(c,a.limit),a.limit)}
    else:
        out={t:c.execute("SELECT COUNT(*) FROM "+t).fetchone()[0] for t in ("v6_vectors","v7_research_runs","v8_events","v8_insights")}
    print(json.dumps(out,ensure_ascii=False)); c.close()
if __name__=="__main__": main()
