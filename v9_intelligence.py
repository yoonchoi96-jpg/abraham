"""Abraham V9 knowledge intelligence layer.
Local-first, deterministic and API-free. It turns existing SQLite evidence into
durable fingerprints, deltas, questions, conflicts, blind spots, source profiles,
temporal hints, cross-domain candidates, feedback, and analyst disagreement.
It deliberately emits candidates/signals rather than asserting truth.
"""
from __future__ import annotations
import hashlib,json,re,sqlite3
from collections import Counter,defaultdict
from datetime import datetime,timezone

TOKEN_RE=re.compile(r"[A-Za-z0-9_\-]{2,}|[가-힣]{2,}|[一-龯]{2,}|[ぁ-ゔァ-ヴー]{2,}")
STOP={"the","and","that","this","with","from","for","are","was","were","have","has","into","about","their","there","which","will","can","not","but","또","그리고","있는","하는","대한","통해"}

def now(): return datetime.now(timezone.utc).isoformat()
def digest(*parts):
    h=hashlib.sha256()
    for p in parts:
        h.update(("" if p is None else str(p)).encode("utf-8","replace")); h.update(b"\0")
    return h.hexdigest()
def toks(s):
    return [x.lower() for x in TOKEN_RE.findall(s or "") if x.lower() not in STOP]
def text(d): return (str(d.get("title") or "")+"\n"+str(d.get("canonical_text") or d.get("content") or "")).strip()
def js(x): return json.dumps(x,ensure_ascii=False,sort_keys=True)

def ensure_schema(c):
    c.executescript("""
    CREATE TABLE IF NOT EXISTS v9_fingerprints(readwise_id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,content_hash TEXT NOT NULL,duplicate_of TEXT,updated_at TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS idx_v9_fp_hash ON v9_fingerprints(fingerprint);
    CREATE TABLE IF NOT EXISTS v9_knowledge_deltas(delta_id TEXT PRIMARY KEY,readwise_id TEXT NOT NULL,delta_type TEXT NOT NULL,payload_json TEXT NOT NULL,created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS v9_questions(question_id TEXT PRIMARY KEY,question TEXT NOT NULL,source_readwise_id TEXT,status TEXT NOT NULL DEFAULT 'open',evidence_json TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS v9_conflicts(conflict_id TEXT PRIMARY KEY,left_id TEXT NOT NULL,right_id TEXT NOT NULL,signal REAL NOT NULL,evidence_json TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'candidate',created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS v9_surprises(surprise_id TEXT PRIMARY KEY,readwise_id TEXT NOT NULL,kind TEXT NOT NULL,score REAL NOT NULL,payload_json TEXT NOT NULL,created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS v9_blindspots(blindspot_id TEXT PRIMARY KEY,topic TEXT NOT NULL,score REAL NOT NULL,payload_json TEXT NOT NULL,created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS v9_source_profiles(source_key TEXT PRIMARY KEY,documents INTEGER NOT NULL,avg_length REAL NOT NULL,updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS v9_temporal_hints(hint_id TEXT PRIMARY KEY,readwise_id TEXT NOT NULL,event_date TEXT NOT NULL,precision TEXT NOT NULL,payload_json TEXT NOT NULL,created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS v9_cross_domain(candidate_id TEXT PRIMARY KEY,left_id TEXT NOT NULL,right_id TEXT NOT NULL,score REAL NOT NULL,payload_json TEXT NOT NULL,created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS v9_feedback(readwise_id TEXT PRIMARY KEY,useful INTEGER,important INTEGER,wrong INTEGER,updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS v9_duel(duel_id TEXT PRIMARY KEY,readwise_id TEXT NOT NULL,analyst_a_json TEXT NOT NULL,analyst_b_json TEXT NOT NULL,disagreement_json TEXT NOT NULL,created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS v9_telemetry(run_id TEXT PRIMARY KEY,payload_json TEXT NOT NULL,created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS v9_memory(entity_key TEXT PRIMARY KEY,state TEXT NOT NULL,first_seen TEXT,last_seen TEXT,version INTEGER NOT NULL,payload_json TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS idx_v9_q_status ON v9_questions(status);
    CREATE INDEX IF NOT EXISTS idx_v9_conflicts_status ON v9_conflicts(status);
    """)
    c.commit()

def _documents(c,limit=1000):
    rows=c.execute("SELECT readwise_id,title,content,canonical_text FROM documents ORDER BY rowid DESC LIMIT ?",(max(1,min(int(limit),5000)),)).fetchall()
    return [dict(r) for r in rows]

def fingerprint(c,d):
    rid=str(d.get("readwise_id") or "").strip()
    h=digest(text(d))
    old=c.execute("SELECT readwise_id FROM v9_fingerprints WHERE fingerprint=? AND readwise_id<>? ORDER BY updated_at LIMIT 1",(h,rid)).fetchone()
    dup=str(old[0]) if old else None
    c.execute("""INSERT INTO v9_fingerprints VALUES(?,?,?,?,?)
                 ON CONFLICT(readwise_id) DO UPDATE SET fingerprint=excluded.fingerprint,content_hash=excluded.content_hash,duplicate_of=excluded.duplicate_of,updated_at=excluded.updated_at""",(rid,h,h,dup,now()))
    return dup

def knowledge_delta(c,d):
    rid=str(d["readwise_id"]); words=Counter(toks(text(d)))
    top=[x for x,n in words.most_common(20)]
    # Compare against semantic neighbours if available.
    neighbours=[]
    try:
        from v6_semantic import related
        neighbours=related(c,rid,5)
    except Exception: pass
    payload={"top_terms":top,"related":neighbours}
    did=digest("delta",rid,h:=digest(text(d)))[:32]
    c.execute("INSERT OR REPLACE INTO v9_knowledge_deltas VALUES(?,?,?,?,?)",(did,rid,"knowledge_delta",js(payload),now()))
    return payload

def question_generation(c,d):
    rid=str(d["readwise_id"]); ts=toks(text(d))
    if len(ts)<8:return []
    counts=Counter(ts); terms=[x for x,n in counts.most_common(8) if len(x)>3][:4]
    qs=[]
    for term in terms[:3]:
        q=f"이 자료에서 {term}은 기존 지식과 어떻게 연결되거나 달라지는가?"
        qid=digest("question",rid,term)[:32]
        exists=c.execute("SELECT 1 FROM v9_questions WHERE question_id=? LIMIT 1",(qid,)).fetchone()
        if not exists:
            c.execute("INSERT INTO v9_questions VALUES(?,?,?,?,?,?,?)",(qid,q,rid,"open","[]",now(),now()))
            qs.append(q)
    return qs

def contradiction_candidates(c,docs):
    out=[]
    for i,a in enumerate(docs):
        ta=set(toks(text(a)))
        if len(ta)<8: continue
        for b in docs[i+1:min(i+80,len(docs))]:
            tb=set(toks(text(b))); inter=len(ta&tb); union=len(ta|tb)
            if inter<6: continue
            neg_a=bool(ta & {"not","no","never","없","아니","않","못"})
            neg_b=bool(tb & {"not","no","never","없","아니","않","못"})
            if neg_a==neg_b: continue
            score=min(1.0,(inter/max(1,union))*2.5)
            cid=digest("conflict",a["readwise_id"],b["readwise_id"])[:32]
            payload={"overlap":inter/max(1,union),"reason":"negation disagreement","left_terms":list(ta&tb)[:20]}
            c.execute("INSERT OR IGNORE INTO v9_conflicts VALUES(?,?,?,?,?,?,?)",(cid,a["readwise_id"],b["readwise_id"],score,js(payload),"candidate",now()))
            out.append(cid)
    return out

def surprise_and_blindspots(c,docs):
    if not docs:return {"surprises":0,"blindspots":0}
    freq=Counter()
    for d in docs: freq.update(set(toks(text(d))))
    total=len(docs); surprises=blind=0
    for d in docs:
        ts=set(toks(text(d)))
        rare=sum(1 for x in ts if freq[x]==1)/max(1,len(ts))
        if rare>=0.55:
            sid=digest("surprise",d["readwise_id"])[:32]
            payload={"rare_term_ratio":round(rare,4),"rare_terms":[x for x in ts if freq[x]==1][:20]}
            c.execute("INSERT OR IGNORE INTO v9_surprises VALUES(?,?,?,?,?,?)",(sid,d["readwise_id"],"novel_terms",rare,js(payload),now())); surprises+=1
    # Topics with broad presence but missing subterms become coverage candidates.
    for term,n in freq.most_common(100):
        if n<3: continue
        related=[x for x in freq if x!=term and x.startswith(term[:3])]
        if not related:
            bid=digest("blindspot",term)[:32]
            payload={"topic":term,"document_count":n,"reason":"high-frequency topic with low lexical subcoverage"}
            c.execute("INSERT OR IGNORE INTO v9_blindspots VALUES(?,?,?,?,?)",(bid,term,min(1,n/total),js(payload),now())); blind+=1
    return {"surprises":surprises,"blindspots":blind}

def source_profiles(c,docs):
    groups=defaultdict(list)
    for d in docs:
        title=str(d.get("title") or "").strip()
        source=title.split("|")[0].split(" — ")[0].strip() or "unknown"
        groups[source].append(len(text(d)))
    for k,v in groups.items():
        c.execute("INSERT INTO v9_source_profiles VALUES(?,?,?,?) ON CONFLICT(source_key) DO UPDATE SET documents=excluded.documents,avg_length=excluded.avg_length,updated_at=excluded.updated_at",(k,len(v),sum(v)/len(v),now()))
    return len(groups)

def temporal_hints(c,docs):
    n=0
    pat=re.compile(r"\b(19\d{2}|20\d{2})[-/.](0?[1-9]|1[0-2])(?:[-/.](0?[1-9]|[12]\d|3[01]))?\b")
    for d in docs:
        for m in pat.finditer(text(d)):
            date=m.group(0).replace("/","-").replace(".","-")
            hid=digest("time",d["readwise_id"],date)[:32]
            precision="day" if len(m.group(0))==10 else "month"
            c.execute("INSERT OR IGNORE INTO v9_temporal_hints VALUES(?,?,?,?,?,?)",(hid,d["readwise_id"],date,precision,js({"match":m.group(0)}),now())); n+=1
    return n

def cross_domain(c,docs):
    # Domains are intentionally coarse and configurable by keyword evidence.
    domains={"music":{"music","song","album","artist","label","genre","음악","노래"},
             "technology":{"ai","software","technology","model","computer","기술","인공지능"},
             "finance":{"stock","market","revenue","profit","investment","주식","시장","투자"},
             "culture":{"culture","history","art","film","fashion","문화","역사","예술"}}
    memberships=[]
    for d in docs:
        s=set(toks(text(d))); ds={k for k,v in domains.items() if len(s&v)>=2}
        for x in ds: memberships.append((x,d))
    out=0
    for i,(da,a) in enumerate(memberships):
        for db,b in memberships[i+1:]:
            if da==db or a["readwise_id"]==b["readwise_id"]: continue
            aid,bid=sorted((a["readwise_id"],b["readwise_id"]))
            score=min(1.0,0.2+len(set(toks(text(a)))&set(toks(text(b))))/50)
            cid=digest("cross",aid,bid,da,db)[:32]
            payload={"left_domain":da,"right_domain":db,"note":"cross-domain connection candidate; review source evidence"}
            c.execute("INSERT OR IGNORE INTO v9_cross_domain VALUES(?,?,?,?,?,?)",(cid,aid,bid,score,js(payload),now())); out+=1
    return out

def telemetry(c,run_id,stats):
    c.execute("INSERT OR REPLACE INTO v9_telemetry VALUES(?,?,?)",(run_id,js(stats),now()))

def process(c,limit=250,run_id=None):
    ensure_schema(c); docs=_documents(c,limit)
    stats={"documents":len(docs),"duplicates":0,"questions":0,"conflicts":0,"surprises":0,"blindspots":0,"source_profiles":0,"temporal_hints":0,"cross_domain":0}
    for d in docs:
        if not d.get("readwise_id"): continue
        if fingerprint(c,d): stats["duplicates"]+=1
        knowledge_delta(c,d)
        stats["questions"]+=len(question_generation(c,d))
    stats["conflicts"]=len(contradiction_candidates(c,docs))
    sb=surprise_and_blindspots(c,docs); stats.update(sb)
    stats["source_profiles"]=source_profiles(c,docs)
    stats["temporal_hints"]=temporal_hints(c,docs)
    stats["cross_domain"]=cross_domain(c,docs)
    # Minimal disagreement engine: two deterministic analyst views.
    for d in docs[:min(100,len(docs))]:
        ts=toks(text(d)); a={"terms":ts[:20],"length":len(ts)}
        b={"terms":list(reversed(ts[-20:])),"length":len(ts)}
        dis=sorted(set(a["terms"])^set(b["terms"]))
        did=digest("duel",d["readwise_id"])[:32]
        c.execute("INSERT OR REPLACE INTO v9_duel VALUES(?,?,?,?,?)",(did,d["readwise_id"],js(a),js(b),js(dis),now()))
    stats["duels"]=min(100,len(docs))
    rid=run_id or digest("v9run",now())[:24]; telemetry(c,rid,stats)
    c.commit()
    return stats

def status(c):
    ensure_schema(c)
    tables=["v9_fingerprints","v9_knowledge_deltas","v9_questions","v9_conflicts","v9_surprises","v9_blindspots","v9_source_profiles","v9_temporal_hints","v9_cross_domain","v9_feedback","v9_duel","v9_telemetry","v9_memory"]
    return {t:c.execute("SELECT COUNT(*) FROM "+t).fetchone()[0] for t in tables}
