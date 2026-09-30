import sqlite3

from v6_semantic import index_documents, search
from v8_autonomous import detect


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
