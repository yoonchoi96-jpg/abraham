"""Abraham V8 event-driven proactive insight detector."""
from __future__ import annotations
import hashlib,json,sqlite3
from datetime import datetime,timezone
from v6_semantic import related
VERSION="8.0.0"
def ensure_schema(conn):
 conn.executescript("""CREATE TABLE IF NOT EXISTS v8_events(event_id TEXT PRIMARY KEY,event_type TEXT NOT NULL,entity_key TEXT,payload_json TEXT NOT NULL,created_at TEXT NOT NULL);
 CREATE TABLE IF NOT EXISTS v8_insights(insight_id TEXT PRIMARY KEY,kind TEXT NOT NULL,title TEXT NOT NULL,body TEXT NOT NULL,evidence_json TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'candidate',created_at TEXT NOT NULL);
 CREATE INDEX IF NOT EXISTS idx_v8_insights_status ON v8_insights(status);"""); conn.commit()
def detect(conn,documents,max_documents=250):
 ensure_schema(conn); byid={str(d.get("readwise_id")):d for d in documents}; checked=created=0
 rows=conn.execute("""SELECT DISTINCT readwise_id FROM document_stages WHERE status='completed' ORDER BY updated_at DESC LIMIT ?""",(max(1,min(int(max_documents),250)),)).fetchall()
 for row in rows:
  rid=str(row[0]); d=byid.get(rid)
  if not d: continue
  checked+=1; rel=related(conn,rid,3)
  if not rel: continue
  payload={"readwise_id":rid,"title":d.get("title",""),"related":rel}
  eid=hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=False).encode()).hexdigest()[:32]
  if conn.execute("SELECT 1 FROM v8_events WHERE event_id=?",(eid,)).fetchone(): continue
  now=datetime.now(timezone.utc).isoformat()
  conn.execute("INSERT INTO v8_events VALUES(?,?,?,?,?)",(eid,"new_related_cluster",rid,json.dumps(payload,ensure_ascii=False),now))
  iid=hashlib.sha256((eid+"insight").encode()).hexdigest()[:32]
  conn.execute("INSERT OR IGNORE INTO v8_insights VALUES(?,?,?,?,?,?,?)",(iid,"knowledge_connection","새로운 지식 연결 후보","기존 지식과 유사도가 높은 문서 연결이 감지되었습니다. 근거를 검토하세요.",json.dumps(payload,ensure_ascii=False),"candidate",now)); created+=1
 conn.commit(); return {"checked":checked,"events_created":created}
def pending_insights(conn,limit=20):
 ensure_schema(conn); return [dict(r) for r in conn.execute("SELECT * FROM v8_insights WHERE status='candidate' ORDER BY created_at DESC LIMIT ?",(limit,))]
