from v9_intelligence import ensure_schema, process
import sqlite3

def test_v9_builds_local_intelligence_tables():
    c=sqlite3.connect(":memory:"); c.row_factory=sqlite3.Row
    c.execute("CREATE TABLE documents(readwise_id TEXT PRIMARY KEY,title TEXT,content TEXT,canonical_text TEXT)")
    for i in range(6):
        c.execute("INSERT INTO documents VALUES(?,?,?,?)",(f"d{i}",f"AI music {i}","AI music technology market history. "+("not confirmed " if i==0 else "" )*3,""))
    c.commit()
    stats=process(c,6)
    assert stats["documents"]==6
    assert c.execute("SELECT COUNT(*) FROM v9_fingerprints").fetchone()[0]==6
    assert c.execute("SELECT COUNT(*) FROM v9_questions").fetchone()[0]>0
    assert c.execute("SELECT COUNT(*) FROM v9_telemetry").fetchone()[0]==1

def test_adaptive_batches():
    from adaptive_batch import plan_adaptive_batches
    docs=[{"title":"x","canonical_text":"a"*10000} for _ in range(10)]
    batches=plan_adaptive_batches(docs,target_chars=25000,min_batch=1,max_batch=80)
    assert all(sum(len(d["canonical_text"]) for d in b)<=25000 or len(b)==1 for b in batches)
    assert sum(map(len,batches))==10
