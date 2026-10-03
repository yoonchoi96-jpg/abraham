from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any


DEFAULT_DB_PATH = Path("readwise_pipeline.db")


def get_db_path() -> Path:
    return Path(os.getenv("ABRAHAM_DB_PATH", str(DEFAULT_DB_PATH))).expanduser()


def connect() -> sqlite3.Connection:
    path = get_db_path()
    if not path.exists():
        raise FileNotFoundError(f"Abraham SQLite database not found: {path}")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _parse_json(value: Any) -> Any:
    if value is None or value == "":
        return value
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return value


def _normalize_document(row: sqlite3.Row) -> dict:
    d = dict(row)
    for key in ("topics", "region", "classification_notes", "readwise_tags"):
        if key in d:
            d[key] = _parse_json(d[key])

    rid = d.get("readwise_id") or d.get("id")
    if rid is not None:
        d["readwise_id"] = str(rid)

    # V5 uses SQLite as the canonical source snapshot.
    # canonical_text may be NULL in legacy/V4 rows, so fall back to content.
    content = d.get("content")
    canonical_text = d.get("canonical_text")

    if content is None:
        content = ""
    else:
        content = str(content)

    if canonical_text is None or not str(canonical_text).strip():
        canonical_text = content
    else:
        canonical_text = str(canonical_text)

    d["content"] = content
    d["canonical_text"] = canonical_text

    return d


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    return {str(r["name"]) for r in rows}


def _first(row: sqlite3.Row, names: tuple[str, ...]) -> Any:
    keys = set(row.keys())
    for name in names:
        if name in keys:
            return row[name]
    return None


def _load_relations(conn: sqlite3.Connection) -> list[dict]:
    """Load validated entity-to-entity relations from the V4/V5 SQLite schema."""

    tables = {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }

    if "relations" not in tables:
        return []

    rows = conn.execute("""
        SELECT
            r.relation_id,
            r.readwise_id,
            r.subject_entity_id,
            r.relation_type,
            r.object_entity_id,
            r.confidence,
            r.evidence_quote,
            r.evidence_start_hint,
            r.status,
            se.canonical_name AS subject_name,
            oe.canonical_name AS object_name
        FROM relations r
        LEFT JOIN entities se ON se.entity_id = r.subject_entity_id
        LEFT JOIN entities oe ON oe.entity_id = r.object_entity_id
        WHERE LOWER(COALESCE(r.status, '')) IN (
            'validated',
            'valid',
            'accepted',
            'confirmed'
        )
        ORDER BY r.relation_id
    """).fetchall()

    result = []

    for row in rows:
        subject_id = row["subject_entity_id"]
        object_id = row["object_entity_id"]
        relation_type = row["relation_type"]

        if not subject_id or not object_id or not relation_type:
            continue

        item = {
            "relation_id": row["relation_id"],
            "readwise_id": str(row["readwise_id"] or ""),
            "subject": {
                "entity_id": str(subject_id),
                "canonical_name": str(row["subject_name"] or ""),
            },
            "relation": str(relation_type),
            "object": {
                "entity_id": str(object_id),
                "canonical_name": str(row["object_name"] or ""),
            },
        }

        if row["confidence"] is not None:
            item["confidence"] = row["confidence"]

        if row["evidence_quote"]:
            item["evidence_quote"] = row["evidence_quote"]

        if row["evidence_start_hint"]:
            item["evidence_start_hint"] = row["evidence_start_hint"]

        item["status"] = str(row["status"] or "validated")

        result.append(item)

    return result

def _load_document_entities(
    conn: sqlite3.Connection,
    readwise_ids: list[str],
) -> dict[str, list[dict]]:
    """Load existing V4 entity links and resolve them from the entities table.

    This reuses the existing SQLite knowledge graph. No AI/entity extraction
    is performed here.
    """
    if not readwise_ids:
        return {}

    tables = {
        str(r["name"])
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }

    if "document_entities" not in tables or "entities" not in tables:
        return {}

    ids = [str(x).strip() for x in readwise_ids if str(x).strip()]
    if not ids:
        return {}

    placeholders = ",".join("?" for _ in ids)

    rows = conn.execute(
        f"""
        SELECT
            de.readwise_id,
            de.entity_id,
            de.mention,
            e.canonical_name,
            e.entity_type,
            e.status
        FROM document_entities de
        LEFT JOIN entities e
            ON e.entity_id = de.entity_id
        WHERE de.readwise_id IN ({placeholders})
        ORDER BY de.readwise_id, e.canonical_name, de.entity_id
        """,
        tuple(ids),
    ).fetchall()

    result: dict[str, list[dict]] = {}

    for row in rows:
        canonical_name = row["canonical_name"]
        entity_id = row["entity_id"]

        if not canonical_name or not entity_id:
            continue

        item = {
            "entity_id": str(entity_id),
            "canonical_name": str(canonical_name),
            "entity_type": str(row["entity_type"] or "Other"),
            "mention": str(row["mention"] or canonical_name),
        }

        readwise_id = str(row["readwise_id"])
        result.setdefault(readwise_id, []).append(item)

    return result


def _attach_resolved_entities(
    conn: sqlite3.Connection,
    documents: list[dict],
) -> list[dict]:
    """Attach existing SQLite entity links to each document."""
    if not documents:
        return documents

    readwise_ids = [
        str(d.get("readwise_id") or "").strip()
        for d in documents
        if str(d.get("readwise_id") or "").strip()
    ]

    entity_map = _load_document_entities(conn, readwise_ids)

    for document in documents:
        readwise_id = str(document.get("readwise_id") or "").strip()
        document["resolved_entities"] = entity_map.get(readwise_id, [])

    return documents


def _load_documents(
    conn: sqlite3.Connection,
    where: str = "",
    params: tuple = (),
    limit: int | None = None,
) -> list[dict]:
    sql = "SELECT * FROM documents"
    if where:
        sql += " WHERE " + where

    # V4/V5 databases do not necessarily expose the same timestamp columns.
    # Build the ordering expression from columns that actually exist.
    document_columns = _table_columns(conn, "documents")
    order_columns = [
        name for name in ("updated_at", "last_processed_at", "first_seen_at")
        if name in document_columns
    ]
    if order_columns:
        sql += " ORDER BY COALESCE(" + ", ".join(order_columns + ["rowid"]) + ") DESC"
    else:
        sql += " ORDER BY rowid DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params = (*params, limit)

    rows = conn.execute(sql, params).fetchall()
    documents = [_normalize_document(row) for row in rows]

    return _attach_resolved_entities(conn, documents)


def load_documents(limit: int | None = None) -> tuple[list[dict], list[dict]]:
    """V5-neutral replacement for run_sqlite_to_v461.load_documents."""
    with connect() as conn:
        documents = _load_documents(conn, limit=limit)
        relations = _load_relations(conn)
    return documents, relations


def load_documents_by_ids(readwise_ids: list[str]) -> tuple[list[dict], list[dict]]:
    """Load exact Readwise IDs for debugging and targeted reruns."""
    ids = [str(x).strip() for x in readwise_ids if str(x).strip()]
    if not ids:
        return [], []

    placeholders = ",".join("?" for _ in ids)
    with connect() as conn:
        documents = _load_documents(
            conn,
            where=f"readwise_id IN ({placeholders})",
            params=tuple(ids),
        )
        relations = _load_relations(conn)
    return documents, relations
