"""Abraham V7 research orchestration with durable provenance."""
from __future__ import annotations
import hashlib,json,sqlite3
from datetime import datetime,timezone
from v6_semantic import search
VERSION="7.0.0"
def ensure_schema(conn):
 conn.executescript("""CREATE TABLE IF NOT EXISTS v7_research_runs(run_id TEXT PRIMARY KEY,question TEXT NOT NULL,status TEXT NOT NULL,result_json TEXT,created_at TEXT NOT NULL,completed_at TEXT);
 CREATE TABLE IF NOT EXISTS v7_evidence(run_id TEXT NOT NULL,readwise_id TEXT NOT NULL,rank INTEGER NOT NULL,score REAL,role TEXT,PRIMARY KEY(run_id,readwise_id));"""); conn.commit()
def build_plan(conn,question,max_sources=8):
 return {"question":question,"sources":search(conn,question,max_sources),"steps":["decompose","retrieve","compare evidence","detect uncertainty/conflicts","synthesize with provenance"]}
def run_research(conn,question,max_sources=8,answerer=None):
 ensure_schema(conn); rid=hashlib.sha256((question+datetime.now(timezone.utc).isoformat()).encode()).hexdigest()[:24]; now=datetime.now(timezone.utc).isoformat(); plan=build_plan(conn,question,max(1,min(int(max_sources),8)))
 conn.execute("INSERT INTO v7_research_runs VALUES(?,?,?,?,?,NULL)",(rid,question,"collecting",None,now))
 for i,s in enumerate(plan["sources"],1): conn.execute("INSERT OR REPLACE INTO v7_evidence VALUES(?,?,?,?,?)",(rid,s["readwise_id"],i,s.get("score"),"retrieved"))
 answer=answerer(plan) if answerer else None; result={"run_id":rid,"plan":plan,"answer":answer}
 conn.execute("UPDATE v7_research_runs SET status=?,result_json=?,completed_at=? WHERE run_id=?",("completed" if answer is not None else "prepared",json.dumps(result,ensure_ascii=False),datetime.now(timezone.utc).isoformat(),rid)); conn.commit(); return result
