#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from gemini_adapter import get_gemini_client

MIGRATION = "graph_english_v1"
BATCH_SIZE = 50


def now():
    return datetime.now(timezone.utc).isoformat()


def is_latin(text: str) -> bool:
    for ch in str(text or ""):
        if ch.isalpha() and "LATIN" not in unicodedata.name(ch, ""):
            return False
    return bool(str(text or "").strip())


def columns(conn, table):
    return {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}


def ensure_audit(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS graph_canonicalization_migrations (
        migration_id TEXT PRIMARY KEY,
        completed_at TEXT NOT NULL,
        entity_count INTEGER NOT NULL DEFAULT 0,
        relation_type_count INTEGER NOT NULL DEFAULT 0,
        merged_entity_count INTEGER NOT NULL DEFAULT 0,
        normalized_relation_count INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS graph_canonicalization_audit (
        audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
        migration_id TEXT NOT NULL,
        object_type TEXT NOT NULL,
        object_id TEXT NOT NULL,
        old_value TEXT NOT NULL,
        new_value TEXT NOT NULL,
        survivor_id TEXT,
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_graph_canon_audit_migration
        ON graph_canonicalization_audit(migration_id);
    """)


def gemini_json(client, prompt):
    response = client.models.generate_content(
        model=os.getenv("GEMINI_ENTITY_RELATION_MODEL", "gemini-3.8-flash"),
        contents=prompt,
        config={
            "response_mime_type": "application/json",
            "thinking": os.getenv("ABRAHAM_ENTITY_RELATION_THINKING", "low"),
            "max_output_tokens": int(os.getenv("GEMINI_ENTITY_RELATION_MAX_TOKENS", "16384")),
        },
    )
    data = json.loads(response.text.strip())
    if not isinstance(data, dict):
        raise ValueError("Gemini returned a non-object JSON response")
    return data


def map_entities(client, rows):
    out = {}
    for start in range(0, len(rows), BATCH_SIZE):
        batch = rows[start:start+BATCH_SIZE]
        prompt = """You are cleaning a long-term knowledge graph.
Return STRICT JSON. Convert every canonical entity name below into ONE stable English canonical name.
Use the established official English name when one exists. Otherwise use a natural English translation
or standard English romanization. Do not use Chinese/Han, Japanese kana, Korean Hangul, Cyrillic,
Arabic, Hebrew, Devanagari, or other non-Latin scripts.
Do not merge distinct real-world entities. Preserve entity identity by id.
Proper names may retain Latin diacritics. Do not add explanations.
Schema: {"entities":[{"entity_id":"...","canonical_name":"..."}]}

Entities:
""" + json.dumps(batch, ensure_ascii=False)
        data = gemini_json(client, prompt)
        items = data.get("entities") or []
        for item in items:
            if not isinstance(item, dict):
                continue
            eid = str(item.get("entity_id") or "")
            name = str(item.get("canonical_name") or "").strip()
            if eid in {str(x["entity_id"]) for x in batch} and name and is_latin(name):
                out[eid] = name
        missing = [x["entity_id"] for x in batch if x["entity_id"] not in out]
        if missing:
            raise RuntimeError(f"Entity canonicalization missing {len(missing)} ids in batch starting {start}")
        print(f"[ENTITY CANONICALIZATION] {min(start+BATCH_SIZE,len(rows))}/{len(rows)}")
    return out


def map_relations(client, relation_types):
    if not relation_types:
        return {}
    prompt = """Convert each relation label into ONE short, reusable English canonical relation label.
Use snake_case. Examples: acquired, founded, developed, headquartered_in, uses, regulates,
owns, created, employed_by, located_in, part_of, competes_with, partnered_with, caused, occurred_in,
associated_with. Do not use any non-Latin script. Preserve distinctions where they are real.
Schema: {"relations":[{"old":"...","canonical":"..."}]}
Relation labels:
""" + json.dumps(relation_types, ensure_ascii=False)
    data = gemini_json(client, prompt)
    result = {}
    allowed = set(relation_types)
    for item in data.get("relations") or []:
        if not isinstance(item, dict):
            continue
        old = str(item.get("old") or "")
        new = str(item.get("canonical") or "").strip()
        if old in allowed and new and is_latin(new):
            result[old] = new
    missing = allowed - set(result)
    if missing:
        raise RuntimeError(f"Relation canonicalization missing: {sorted(missing)}")
    return result


def remap_entity(conn, old_id, new_id):
    if old_id == new_id:
        return
    # Preserve the richest document/entity link already present.
    de_cols = columns(conn, "document_entities")
    if {"readwise_id", "entity_id"}.issubset(de_cols):
        rows = conn.execute(
            "SELECT readwise_id, entity_id, mention, confidence, evidence_quote, created_at "
            "FROM document_entities WHERE entity_id=?", (old_id,)
        ).fetchall()
        for rid, _, mention, confidence, evidence, created_at in rows:
            exists = conn.execute(
                "SELECT 1 FROM document_entities WHERE readwise_id=? AND entity_id=?",
                (rid, new_id),
            ).fetchone()
            if not exists:
                conn.execute(
                    "INSERT INTO document_entities (readwise_id,entity_id,mention,confidence,evidence_quote,created_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (rid,new_id,mention,confidence,evidence,created_at or now()),
                )
        conn.execute("DELETE FROM document_entities WHERE entity_id=?", (old_id,))

    conn.execute("UPDATE relations SET subject_entity_id=? WHERE subject_entity_id=?", (new_id, old_id))
    conn.execute("UPDATE relations SET object_entity_id=? WHERE object_entity_id=?", (new_id, old_id))
    conn.execute("UPDATE entities SET status='merged', updated_at=? WHERE entity_id=?", (now(), old_id))


def dedupe_relations(conn):
    # Keep the lowest relation_id for identical graph edges after normalization.
    conn.execute("""
        DELETE FROM relations
        WHERE relation_id NOT IN (
            SELECT MIN(relation_id)
            FROM relations
            GROUP BY readwise_id, subject_entity_id, relation_type, object_entity_id
        )
    """)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    path = Path(args.db)
    if not path.is_file():
        raise SystemExit(f"DB not found: {path}")

    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        ensure_audit(conn)
        if not args.force and conn.execute(
            "SELECT 1 FROM graph_canonicalization_migrations WHERE migration_id=?",
            (MIGRATION,),
        ).fetchone():
            print(f"{MIGRATION}=already_completed")
            return

        entities = conn.execute(
            "SELECT entity_id, canonical_name, entity_type, created_at "
            "FROM entities WHERE COALESCE(status,'active') NOT IN ('deleted','rejected','merged') "
            "ORDER BY COALESCE(created_at,''), entity_id"
        ).fetchall()
        relation_types = [
            r[0] for r in conn.execute(
                "SELECT DISTINCT relation_type FROM relations "
                "WHERE relation_type IS NOT NULL AND TRIM(relation_type)<>'' ORDER BY relation_type"
            ).fetchall()
        ]

        client = get_gemini_client()
        entity_map = map_entities(
            client,
            [{"entity_id":r[0],"canonical_name":r[1],"entity_type":r[2]} for r in entities],
        )
        relation_map = map_relations(client, relation_types)

        conn.execute("BEGIN")
        merged = 0
        changed_entities = 0
        for eid, old_name, _, _ in entities:
            new_name = entity_map[eid]
            if old_name != new_name:
                changed_entities += 1
            conn.execute(
                "INSERT INTO graph_canonicalization_audit "
                "(migration_id,object_type,object_id,old_value,new_value,survivor_id,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (MIGRATION,"entity",eid,old_name,new_name,None,now()),
            )

        # First select one survivor per normalized English canonical name.
        groups = {}
        for eid, old_name, etype, created in entities:
            groups.setdefault(entity_map[eid].casefold(), []).append((eid, old_name, etype, created))
        survivors = {}
        for key, group in groups.items():
            group.sort(key=lambda x: (x[3] or "", x[0]))
            survivor = group[0][0]
            survivors[key] = survivor
            new_name = entity_map[survivor]
            conn.execute(
                "UPDATE entities SET canonical_name=?, updated_at=? WHERE entity_id=?",
                (new_name,now(),survivor),
            )
            for eid, old_name, _, _ in group:
                if eid == survivor:
                    conn.execute(
                        "UPDATE graph_canonicalization_audit SET survivor_id=? "
                        "WHERE migration_id=? AND object_type='entity' AND object_id=?",
                        (survivor,MIGRATION,eid),
                    )
                    continue
                remap_entity(conn,eid,survivor)
                conn.execute(
                    "UPDATE graph_canonicalization_audit SET survivor_id=? "
                    "WHERE migration_id=? AND object_type='entity' AND object_id=?",
                    (survivor,MIGRATION,eid),
                )
                merged += 1

        # Normalize relation labels after entity remapping.
        for old, new in relation_map.items():
            if old != new:
                conn.execute("UPDATE relations SET relation_type=? WHERE relation_type=?", (new,old))
        dedupe_relations(conn)

        for old, new in relation_map.items():
            conn.execute(
                "INSERT INTO graph_canonicalization_audit "
                "(migration_id,object_type,object_id,old_value,new_value,survivor_id,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (MIGRATION,"relation_type",old,old,new,None,now()),
            )

        conn.execute(
            "INSERT INTO graph_canonicalization_migrations "
            "(migration_id,completed_at,entity_count,relation_type_count,merged_entity_count,normalized_relation_count) "
            "VALUES (?,?,?,?,?,?)",
            (MIGRATION,now(),len(entities),len(relation_types),merged,
             sum(1 for k,v in relation_map.items() if k != v)),
        )
        conn.commit()
        print(f"ABRAHAM_GRAPH_ENGLISH_MIGRATION_OK")
        print(f"entities_scanned={len(entities)}")
        print(f"entities_changed={changed_entities}")
        print(f"entities_merged={merged}")
        print(f"relation_types_scanned={len(relation_types)}")
        print(f"relation_types_changed={sum(1 for k,v in relation_map.items() if k != v)}")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
