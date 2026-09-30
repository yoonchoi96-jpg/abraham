import sqlite3

from v6_semantic import index_documents, search
from v8_autonomous import detect
from v7_research import build_plan, run_research
import v8_autonomous


def db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute(
        """CREATE TABLE documents(
            readwise_id TEXT PRIMARY KEY,
            title TEXT,
            content TEXT,
            canonical_text TEXT
        )"""
    )
    c.execute(
        """CREATE TABLE document_stages(
            readwise_id TEXT,
            stage TEXT,
            status TEXT,
            attempt_count INTEGER DEFAULT 0,
            started_at TEXT,
            completed_at TEXT,
            last_error TEXT,
            worker_id TEXT,
            content_hash TEXT,
            result_hash TEXT,
            result_json TEXT,
            updated_at TEXT
        )"""
    )
    return c


def test_v6():
    c = db()
    d = [
        {
            "readwise_id": "1",
            "title": "AI chips",
            "canonical_text": "GPU semiconductor memory",
        },
        {
            "readwise_id": "2",
            "title": "Food",
            "canonical_text": "pasta tomato olive oil",
        },
    ]
    assert index_documents(c, d)["added"] == 2
    assert search(c, "AI semiconductor", 1)[0]["readwise_id"] == "1"
    assert index_documents(c, d)["skipped"] == 2


def test_v8_watermark_and_change_detection():
    c = db()
    d = [
        {"readwise_id": "1", "title": "AI", "canonical_text": "AI chips"},
        {"readwise_id": "2", "title": "GPU", "canonical_text": "AI GPU chips"},
    ]
    index_documents(c, d)

    c.execute(
        """INSERT INTO document_stages(
            readwise_id, stage, status, content_hash, result_hash, result_json, updated_at
        ) VALUES('1','rating','completed','c1','r1','{}','2026-01-01')"""
    )
    c.commit()

    first = detect(c, d)
    assert first["checked"] == 1
    assert first["skipped"] == 0

    second = detect(c, d)
    assert second["checked"] == 0
    assert second["skipped"] == 1
    assert second["events_created"] == 0

    c.execute(
        """UPDATE document_stages
           SET result_hash='r2', result_json='{"changed":true}', updated_at='2026-01-02'
           WHERE readwise_id='1' AND stage='rating'"""
    )
    c.commit()

    third = detect(c, d)
    assert third["checked"] == 1
    assert third["events_created"] == 1



def test_v6_provider_metadata():
    c = db()
    d = [{"readwise_id": "1", "title": "AI", "canonical_text": "GPU semiconductor"}]
    index_documents(c, d)
    row = c.execute(
        "SELECT provider, model FROM v6_vectors WHERE readwise_id='1'"
    ).fetchone()
    assert row["provider"] == "local_hash"
    assert row["model"] == "hash-v1"


def test_v8_weak_related_is_filtered():
    c = db()
    docs = [
        {"readwise_id": "1", "title": "AI", "canonical_text": "AI chips"},
        {"readwise_id": "2", "title": "Random", "canonical_text": "AI chips"},
    ]
    index_documents(c, docs)
    c.execute(
        """INSERT INTO document_stages(
            readwise_id, stage, status, content_hash, result_hash, result_json, updated_at
        ) VALUES('1','rating','completed','c1','r1','{}','2026-01-01')"""
    )
    c.commit()
    result = detect(c, docs, 10)
    assert result["checked"] == 1



def test_v6_migrates_legacy_vector_schema():
    c = db()
    c.execute("""CREATE TABLE v6_vectors(
        readwise_id TEXT PRIMARY KEY,
        content_hash TEXT NOT NULL,
        vector_json TEXT NOT NULL,
        dims INTEGER NOT NULL,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("INSERT INTO v6_vectors(readwise_id,content_hash,vector_json,dims) VALUES('old','h','{}',384)")
    c.commit()
    from v6_semantic import ensure_schema
    ensure_schema(c)
    cols = {r["name"] for r in c.execute("PRAGMA table_info(v6_vectors)").fetchall()}
    assert {"provider", "model"} <= cols
    row = c.execute("SELECT provider,model FROM v6_vectors WHERE readwise_id='old'").fetchone()
    assert row["provider"] == "local_hash"
    assert row["model"] == "hash-v1"


def test_v8_weak_related_is_rejected(monkeypatch):
    c = db()
    docs = [{"readwise_id": "1", "title": "AI", "canonical_text": "AI chips"}]
    index_documents(c, docs)
    c.execute("""INSERT INTO document_stages(
        readwise_id, stage, status, content_hash, result_hash, result_json, updated_at
    ) VALUES('1','rating','completed','c1','r1','{}','2026-01-01')""")
    c.commit()
    monkeypatch.setattr(v8_autonomous, "related", lambda conn, rid, limit: [
        {"readwise_id": "2", "score": 0.149}
    ])
    result = detect(c, docs, 10)
    assert result["checked"] == 1
    assert result["events_created"] == 0



def test_v7_persists_disagreement_signals():
    c = db()
    docs = [
        {"readwise_id": "1", "title": "Claim A", "canonical_text": "AI chips improve performance but no evidence"},
        {"readwise_id": "2", "title": "Claim B", "canonical_text": "AI chips improve efficiency with evidence"},
    ]
    index_documents(c, docs)
    result = run_research(c, "AI chips performance", 2)
    assert "confidence" in result["plan"]
    assert result["plan"]["evidence_comparison"]["items"] >= 1
    assert c.execute("SELECT COUNT(*) FROM v7_claims").fetchone()[0] >= 1
