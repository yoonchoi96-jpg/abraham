import sqlite3

from scripts.consolidate_relations import consolidate, ensure_schema


def test_consolidate_relations_normalizes_and_deduplicates(tmp_path):
    db = tmp_path / "graph.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
    CREATE TABLE entities (
        entity_id TEXT PRIMARY KEY,
        canonical_name TEXT,
        entity_type TEXT,
        status TEXT,
        created_at TEXT,
        updated_at TEXT
    );
    CREATE TABLE relations (
        relation_id INTEGER PRIMARY KEY AUTOINCREMENT,
        readwise_id TEXT NOT NULL,
        subject_entity_id TEXT NOT NULL,
        relation_type TEXT NOT NULL,
        object_entity_id TEXT NOT NULL,
        confidence REAL,
        evidence_quote TEXT,
        evidence_start_hint TEXT,
        status TEXT,
        created_at TEXT
    );
    """)
    conn.execute(
        "INSERT INTO relations(readwise_id,subject_entity_id,relation_type,object_entity_id,confidence,evidence_quote,created_at) "
        "VALUES ('rw-1','a','purchased','b',0.8,'A purchased B.','now')"
    )
    conn.execute(
        "INSERT INTO relations(readwise_id,subject_entity_id,relation_type,object_entity_id,confidence,evidence_quote,created_at) "
        "VALUES ('rw-2','a','acquired','b',0.9,'A acquired B.','now')"
    )
    result = consolidate(conn)
    assert result["scanned"] == 2
    assert result["normalized"] == 1
    assert result["merged"] == 1
    assert conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0] == 1
    assert conn.execute("SELECT relation_type FROM relations").fetchone()[0] == "acquired"
    assert conn.execute("SELECT COUNT(*) FROM relation_evidence").fetchone()[0] == 2
    conn.close()
