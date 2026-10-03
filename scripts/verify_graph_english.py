#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sqlite3
import unicodedata
from pathlib import Path


def is_latin(text):
    for ch in str(text or ""):
        if ch.isalpha() and "LATIN" not in unicodedata.name(ch, ""):
            return False
    return bool(str(text or "").strip())


def q(conn, sql):
    return int(conn.execute(sql).fetchone()[0])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    args = ap.parse_args()
    path = Path(args.db)
    if not path.is_file():
        raise SystemExit(f"DB not found: {path}")
    con = sqlite3.connect(path)
    try:
        bad_entities = [
            r for r in con.execute(
                "SELECT entity_id,canonical_name FROM entities "
                "WHERE COALESCE(status,'active') NOT IN ('deleted','rejected','merged')"
            ).fetchall() if not is_latin(r[1])
        ]
        bad_relations = [
            r for r in con.execute(
                "SELECT relation_id,relation_type FROM relations"
            ).fetchall() if not is_latin(r[1])
        ]
        duplicate_entities = q(con, """
            SELECT COUNT(*) FROM (
              SELECT lower(trim(canonical_name))
              FROM entities
              WHERE COALESCE(status,'active') NOT IN ('deleted','rejected','merged')
              GROUP BY lower(trim(canonical_name))
              HAVING COUNT(*)>1
            )
        """)
        orphan_de = q(con, """
            SELECT COUNT(*) FROM document_entities de
            LEFT JOIN entities e ON e.entity_id=de.entity_id
            WHERE e.entity_id IS NULL
        """)
        orphan_rel = q(con, """
            SELECT COUNT(*) FROM relations r
            LEFT JOIN entities s ON s.entity_id=r.subject_entity_id
            LEFT JOIN entities o ON o.entity_id=r.object_entity_id
            WHERE s.entity_id IS NULL OR o.entity_id IS NULL
        """)
        if bad_entities or bad_relations or duplicate_entities or orphan_de or orphan_rel:
            print(f"bad_entities={len(bad_entities)}")
            print(f"bad_relations={len(bad_relations)}")
            print(f"duplicate_entities={duplicate_entities}")
            print(f"orphan_document_entities={orphan_de}")
            print(f"orphan_relations={orphan_rel}")
            for eid, name in bad_entities[:20]:
                print(f"BAD_ENTITY {eid} {name}")
            for rid, name in bad_relations[:20]:
                print(f"BAD_RELATION {rid} {name}")
            raise SystemExit(1)
        print("ABRAHAM_GRAPH_ENGLISH_OK")
        print(
            "active_entities="
            + str(q(con, "SELECT COUNT(*) FROM entities "
                          "WHERE COALESCE(status,'active') NOT IN "
                          "('deleted','rejected','merged')"))
        )
        print(f"relations={q(con, 'SELECT COUNT(*) FROM relations')}")
        print("non_latin_entities=0")
        print("non_latin_relations=0")
        print("duplicate_entities=0")
        print("orphan_document_entities=0")
        print("orphan_relations=0")
    finally:
        con.close()


if __name__ == "__main__":
    main()
