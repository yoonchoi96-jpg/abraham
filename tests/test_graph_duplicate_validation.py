import sqlite3

from scripts import verify_graph_english


def test_same_canonical_name_is_only_duplicate_within_entity_type(tmp_path, monkeypatch):
    db = tmp_path / "graph.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE entities (
            entity_id TEXT PRIMARY KEY,
            canonical_name TEXT NOT NULL,
            entity_type TEXT,
            status TEXT
        );
        CREATE TABLE relations (
            relation_id TEXT PRIMARY KEY,
            relation_type TEXT
        );
        CREATE TABLE document_entities (
            readwise_id TEXT,
            entity_id TEXT
        );
        INSERT INTO entities VALUES
            ('company-apple', 'Apple', 'Company', 'active'),
            ('product-apple', 'Apple', 'Product', 'active'),
            ('company-apple-2', 'Apple', 'Company', 'active');
        """
    )
    con.commit()
    con.close()

    monkeypatch.setattr(
        verify_graph_english,
        "q",
        verify_graph_english.q,
    )

    con = sqlite3.connect(db)
    duplicate_entities = verify_graph_english.q(con, """
        SELECT COUNT(*) FROM (
          SELECT lower(trim(canonical_name)), COALESCE(entity_type,'Other')
          FROM entities
          WHERE COALESCE(status,'active') NOT IN ('deleted','rejected','merged')
          GROUP BY lower(trim(canonical_name)), COALESCE(entity_type,'Other')
          HAVING COUNT(*)>1
        )
    """)
    con.close()

    assert duplicate_entities == 1
