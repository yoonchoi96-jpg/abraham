import sqlite3

from entity_relation_ai import (
    _catalog_maps,
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
