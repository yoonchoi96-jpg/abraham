#!/usr/bin/env python3
"""Abraham V6→V8 runner."""
import argparse,json
from abraham_sqlite import connect,load_documents
from abraham_incremental import ensure_schema
from v6_semantic import index_documents,search
from v7_research import ensure_schema as v7_schema
from v8_autonomous import ensure_schema as v8_schema,detect
def main():
 p=argparse.ArgumentParser(); p.add_argument("--mode",choices=("index","search","research","autonomous","status"),default="autonomous"); p.add_argument("--query"); p.add_argument("--limit",type=int,default=1000); p.add_argument("--question"); a=p.parse_args()
 c=connect(); ensure_schema(c); v7_schema(c); v8_schema(c); docs,_=load_documents()
 if a.mode=="index": out={"v6":index_documents(c,docs)}
 elif a.mode=="search":
  if not a.query: raise SystemExit("--query required")
  out={"query":a.query,"results":search(c,a.query,a.limit)}
 elif a.mode=="research":
  if not a.question: raise SystemExit("--question required")
  from v7_research import run_research
  out={"v7":run_research(c,a.question,a.limit)}
 elif a.mode=="autonomous": out={"v6":index_documents(c,docs),"v8":detect(c,docs,a.limit)}
 else: out={t:c.execute("SELECT COUNT(*) FROM "+t).fetchone()[0] for t in ("v6_vectors","v7_research_runs","v8_events","v8_insights")}
 print(json.dumps(out,ensure_ascii=False)); c.close()
if __name__=="__main__": main()
