#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

REQUIRED_TABLES = {
    "documents",
    "entities",
    "document_entities",
    "relations",
    "document_stages",
    "ratings",
    "pipeline_state",
}


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
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise SystemExit(f"SQLite integrity_check failed: {integrity}")

        tables = {
            row[0]
            for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        missing = REQUIRED_TABLES - tables
        if missing:
            raise SystemExit(f"Missing required tables: {sorted(missing)}")

        checks = {
            "documents": q(con, "SELECT COUNT(*) FROM documents"),
            "entities": q(con, "SELECT COUNT(*) FROM entities"),
            "document_entities": q(con, "SELECT COUNT(*) FROM document_entities"),
            "relations": q(con, "SELECT COUNT(*) FROM relations"),
            "document_stages": q(con, "SELECT COUNT(*) FROM document_stages"),
            "ratings": q(con, "SELECT COUNT(*) FROM ratings"),
        }

        duplicate_documents = q(
            con,
            """
            SELECT COUNT(*) FROM (
                SELECT readwise_id
                FROM documents
                WHERE readwise_id IS NOT NULL AND TRIM(readwise_id) <> ''
                GROUP BY readwise_id
                HAVING COUNT(*) > 1
            )
            """,
        )

        orphan_document_entities = q(
            con,
            """
            SELECT COUNT(*)
            FROM document_entities de
            LEFT JOIN documents d ON d.readwise_id = de.readwise_id
            LEFT JOIN entities e ON e.entity_id = de.entity_id
            WHERE d.readwise_id IS NULL OR e.entity_id IS NULL
            """,
        )

        orphan_relations = q(
            con,
            """
            SELECT COUNT(*)
            FROM relations r
            LEFT JOIN documents d ON d.readwise_id = r.readwise_id
            LEFT JOIN entities s ON s.entity_id = r.subject_entity_id
            LEFT JOIN entities o ON o.entity_id = r.object_entity_id
            WHERE d.readwise_id IS NULL
               OR s.entity_id IS NULL
               OR o.entity_id IS NULL
            """,
        )

        orphan_ratings = q(
            con,
            """
            SELECT COUNT(*)
            FROM ratings r
            LEFT JOIN documents d ON d.readwise_id = r.readwise_id
            WHERE d.readwise_id IS NULL
            """,
        )

        orphan_stages = q(
            con,
            """
            SELECT COUNT(*)
            FROM document_stages s
            LEFT JOIN documents d ON d.readwise_id = s.readwise_id
            WHERE d.readwise_id IS NULL
            """,
        )

        failures = {
            "duplicate_documents": duplicate_documents,
            "orphan_document_entities": orphan_document_entities,
            "orphan_relations": orphan_relations,
            "orphan_ratings": orphan_ratings,
            "orphan_document_stages": orphan_stages,
        }

        print("ABRAHAM_STATE_OK")
        print(f"db={path}")
        print(f"documents={checks['documents']}")
        print(f"entities={checks['entities']}")
        print(f"document_entities={checks['document_entities']}")
        print(f"relations={checks['relations']}")
        print(f"document_stages={checks['document_stages']}")
        print(f"ratings={checks['ratings']}")
        print("integrity_check=ok")

        bad = {k: v for k, v in failures.items() if v}
        if bad:
            raise SystemExit(f"State consistency checks failed: {bad}")
    finally:
        con.close()


if __name__ == "__main__":
    main()
