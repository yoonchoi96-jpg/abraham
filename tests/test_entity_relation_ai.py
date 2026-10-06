import sqlite3

from entity_relation_ai import (
    _catalog_maps,
    _canonical_relation,
    _insert_relation,
    _resolve_entity,
    _validate_item,
    ensure_graph_schema,
)


def test_entity_resolution_reuses_exact_canonical_name():
    catalog = [
        {"entity_id": "ent_1", "canonical_name": "Apple", "entity_type": "Company"}
    ]
    exact, by_id = _catalog_maps(catalog)
    resolved = _resolve_entity("apple", "Company", exact, by_id)
    assert resolved["entity_id"] == "ent_1"
    assert resolved["canonical_name"] == "Apple"


def test_validate_graph_payload_is_bounded_and_strict():
    entities, relations = _validate_item(
        {
            "readwise_id": "rw-1",
            "entities": [
                {"mention": "Apple", "canonical_name": "Apple", "entity_type": "Company"},
                {"mention": "Apple", "canonical_name": "Apple", "entity_type": "Company"},
                {"mention": "iPhone", "canonical_name": "iPhone", "entity_type": "Product"},
            ],
            "relations": [
                {
                    "subject": "Apple",
                    "relation": "developed",
                    "object": "iPhone",
                    "confidence": 0.95,
                    "evidence_quote": "Apple developed the iPhone.",
                }
            ],
        },
        "rw-1",
    )
    assert len(entities) == 2
    assert len(relations) == 1
    assert relations[0]["confidence"] == 0.95


def test_graph_schema_supports_idempotent_core_tables():
    conn = sqlite3.connect(":memory:")
    ensure_graph_schema(conn)
    ensure_graph_schema(conn)
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {"entities", "document_entities", "relations"} <= tables


def test_relation_aliases_are_canonicalized():
    assert _canonical_relation("purchased") == "acquired"
    assert _canonical_relation("headquartered in") == "headquartered_in"
    assert _canonical_relation("works for") == "employed_by"


def test_relation_edge_is_global_and_evidence_is_accumulated():
    conn = sqlite3.connect(":memory:")
    ensure_graph_schema(conn)
    now = "2026-10-06T00:00:00+00:00"
    conn.execute(
        "INSERT INTO entities(entity_id,canonical_name,entity_type,status,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?)",
        ("a", "Apple", "Company", "active", now, now),
    )
    conn.execute(
        "INSERT INTO entities(entity_id,canonical_name,entity_type,status,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?)",
        ("b", "Beats", "Company", "active", now, now),
    )
    first = _insert_relation(
        conn, "rw-1",
        {"entity_id": "a"},
        {"relation": "acquired", "confidence": 0.90, "evidence_quote": "Apple acquired Beats."},
        {"entity_id": "b"},
    )
    second = _insert_relation(
        conn, "rw-2",
        {"entity_id": "a"},
        {"relation": "purchased", "confidence": 0.95, "evidence_quote": "Apple purchased Beats."},
        {"entity_id": "b"},
    )
    assert first is True
    assert second is False
    assert conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM relation_evidence").fetchone()[0] == 2
    assert conn.execute("SELECT confidence FROM relations").fetchone()[0] == 0.95
