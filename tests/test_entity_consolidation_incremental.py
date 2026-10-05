import sqlite3
import sys

from scripts import consolidate_entities


def test_completed_consolidation_is_incremental_for_new_non_latin_entity(tmp_path, monkeypatch):
    db = tmp_path / "pipeline.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE entities (
            entity_id TEXT PRIMARY KEY,
            canonical_name TEXT NOT NULL,
            entity_type TEXT,
            created_at TEXT,
            status TEXT,
            updated_at TEXT
        );
        CREATE TABLE document_entities (
            readwise_id TEXT,
            entity_id TEXT,
            mention TEXT,
            confidence REAL,
            evidence_quote TEXT,
            created_at TEXT
        );
        CREATE TABLE relations (
            relation_id TEXT PRIMARY KEY,
            subject_entity_id TEXT,
            object_entity_id TEXT,
            relation_type TEXT
        );
        INSERT INTO entities VALUES
            ('ent-old', 'Jane Doe', 'Person', '2026-10-01', 'active', '2026-10-01'),
            ('ent-new', '林于凱', 'Person', '2026-10-06', 'active', '2026-10-06');
        INSERT INTO document_entities VALUES
            ('rw-1', 'ent-new', '林于凱', 0.95, 'mention', '2026-10-06');
        """
    )
    con.commit()
    con.close()

    # Simulate a production DB where the one-time v1 migration is already recorded.
    con = sqlite3.connect(db)
    consolidate_entities.ensure(con)
    con.execute(
        "INSERT INTO entity_consolidation_runs "
        "(migration_id, completed_at, scanned, merged, rejected, ai_names) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (consolidate_entities.MIGRATION, "2026-10-05T00:00:00+00:00", 1, 0, 0, 0),
    )
    con.commit()
    con.close()

    calls = []

    def fake_ai_names(rows):
        calls.append(rows)
        return {"ent-new": "Lin Yu-kai"}

    monkeypatch.setattr(consolidate_entities, "ai_names", fake_ai_names)
    monkeypatch.setattr(sys, "argv", ["consolidate_entities.py", "--db", str(db)])

    consolidate_entities.main()

    con = sqlite3.connect(db)
    name = con.execute(
        "SELECT canonical_name FROM entities WHERE entity_id='ent-new'"
    ).fetchone()[0]
    run = con.execute(
        "SELECT scanned, ai_names FROM entity_consolidation_runs WHERE migration_id=?",
        (consolidate_entities.MIGRATION,),
    ).fetchone()
    con.close()

    assert name == "Lin Yu-kai"
    assert calls == [[
        {"entity_id": "ent-new", "canonical_name": "林于凱", "entity_type": "Person"}
    ]]
    assert run == (2, 1)
