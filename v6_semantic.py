"""Abraham V6 semantic intelligence: incremental local semantic index/search."""
from __future__ import annotations
import hashlib,json,math,re,sqlite3
from collections import Counter
VERSION="6.0.0"
TOKEN_RE=re.compile(r"[A-Za-z0-9_\-]{2,}|[가-힣]{2,}|[一-龯]{2,}|[ぁ-ゔァ-ヴー]{2,}")
def tokenize(text): return [x.lower() for x in TOKEN_RE.findall(text or "")]
def _hash(text): return hashlib.sha256((text or "").encode("utf-8","replace")).hexdigest()
def vector(text,dims=384):
 c=Counter(tokenize(text)); total=sum(c.values()) or 1; out={}
 for t,n in c.items():
  k=str(int(hashlib.sha256(t.encode()).hexdigest()[:16],16)%dims); out[k]=out.get(k,0)+n/total
 norm=math.sqrt(sum(v*v for v in out.values())) or 1
 return {k:v/norm for k,v in out.items()}
def cosine(a,b): return sum(v*b.get(k,0) for k,v in a.items())
def ensure_schema(conn):
 conn.executescript("""CREATE TABLE IF NOT EXISTS v6_vectors(readwise_id TEXT PRIMARY KEY,content_hash TEXT NOT NULL,vector_json TEXT NOT NULL,dims INTEGER NOT NULL,updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
 CREATE INDEX IF NOT EXISTS idx_v6_vectors_hash ON v6_vectors(content_hash);"""); conn.commit()
def index_documents(conn,docs):
 ensure_schema(conn); a=u=s=0
 for d in docs:
  rid=str(d.get("readwise_id") or d.get("document_id") or "").strip()
  if not rid: continue
  text=(d.get("title","")+"\n"+(d.get("canonical_text") or d.get("content") or "")).strip(); h=_hash(text)
  old=conn.execute("SELECT content_hash FROM v6_vectors WHERE readwise_id=?",(rid,)).fetchone()
  if old and old[0]==h: s+=1; continue
  conn.execute("""INSERT INTO v6_vectors VALUES(?,?,?,?,CURRENT_TIMESTAMP)
  ON CONFLICT(readwise_id) DO UPDATE SET content_hash=excluded.content_hash,vector_json=excluded.vector_json,dims=excluded.dims,updated_at=CURRENT_TIMESTAMP""",(rid,h,json.dumps(vector(text),separators=(",",":")),384))
  (u:=u+1) if old else (a:=a+1)
 conn.commit(); return {"added":a,"updated":u,"skipped":s}
def _docs(conn,rids):
 if not rids:return []
 q=",".join("?" for _ in rids); rows=conn.execute(f"SELECT * FROM documents WHERE readwise_id IN ({q})",rids).fetchall()
 return [dict(r) for r in rows]
def search(conn,query,limit=10):
 ensure_schema(conn)
 limit=max(1,min(int(limit),50))
 qv=vector(query); scored=[]
 for r in conn.execute("SELECT readwise_id,vector_json FROM v6_vectors"):
  try: scored.append((cosine(qv,json.loads(r[1])),r[0]))
  except: pass
 scored.sort(reverse=True); out=[]
 for score,rid in scored[:max(1,limit)]:
  ds=_docs(conn,[rid]); d=ds[0] if ds else {}
  out.append({"readwise_id":rid,"score":round(score,6),"title":d.get("title","")})
 return out
def related(conn,rid,limit=10):
 row=conn.execute("SELECT vector_json FROM v6_vectors WHERE readwise_id=?",(rid,)).fetchone()
 if not row:return []
 target=json.loads(row[0]); vals=[]
 for r in conn.execute("SELECT readwise_id,vector_json FROM v6_vectors WHERE readwise_id<>?",(rid,)):
  try: vals.append((cosine(target,json.loads(r[1])),r[0]))
  except: pass
 vals.sort(reverse=True); return [{"readwise_id":x,"score":round(s,6)} for s,x in vals[:limit]]
