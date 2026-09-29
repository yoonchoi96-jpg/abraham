from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Iterable


SCHEMA_SQL = '''
CREATE TABLE IF NOT EXISTS document_stages (
    readwise_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    started_at TEXT,
    completed_at TEXT,
    last_error TEXT,
    worker_id TEXT,
    content_hash TEXT,
    result_hash TEXT,
    result_json TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (readwise_id, stage)
);
CREATE INDEX IF NOT EXISTS idx_document_stages_status
    ON document_stages(status);
CREATE INDEX IF NOT EXISTS idx_document_stages_stage
    ON document_stages(stage);
'''


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def content_hash(*parts: object) -> str:
    h = hashlib.sha256()
    for part in parts:
        text = '' if part is None else str(part)
        h.update(text.encode('utf-8', errors='replace'))
        h.update(b'\x00')
    return h.hexdigest()


def result_hash(*parts: object) -> str:
    return content_hash(*parts)


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)

    # CREATE TABLE IF NOT EXISTS does not upgrade an existing table.
    # V5.6 therefore performs additive schema migration here.
    columns = {
        row["name"]
        for row in conn.execute(
            "PRAGMA table_info(document_stages)"
        ).fetchall()
    }

    required_columns = {
        "content_hash": "TEXT",
        "result_hash": "TEXT",
        "result_json": "TEXT",
    }

    for column, column_type in required_columns.items():
        if column not in columns:
            conn.execute(
                f"ALTER TABLE document_stages "
                f"ADD COLUMN {column} {column_type}"
            )
            print(
                f"[SCHEMA MIGRATION] "
                f"document_stages.{column} added"
            )

    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_document_stages_status
        ON document_stages(status)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_document_stages_stage
        ON document_stages(stage)
        """
    )

    conn.commit()


def ensure_stage_rows(conn: sqlite3.Connection, readwise_ids: Iterable[str], stage: str) -> None:
    now = utc_now()
    conn.executemany(
        '''INSERT OR IGNORE INTO document_stages
           (readwise_id, stage, status, updated_at)
           VALUES (?, ?, 'pending', ?)''',
        [(str(rid), stage, now) for rid in readwise_ids if str(rid).strip()],
    )
    conn.commit()


def get_stage_rows(conn: sqlite3.Connection, readwise_ids: Iterable[str], stage: str) -> dict[str, sqlite3.Row]:
    ids = [str(x) for x in readwise_ids if str(x).strip()]
    if not ids:
        return {}
    placeholders = ','.join('?' for _ in ids)
    rows = conn.execute(
        f'''SELECT * FROM document_stages
            WHERE stage = ? AND readwise_id IN ({placeholders})''',
        (stage, *ids),
    ).fetchall()
    return {str(row['readwise_id']): row for row in rows}


def select_pending_documents(
    conn: sqlite3.Connection,
    documents: list[dict],
    stage: str,
    *,
    force_ids: Iterable[str] | None = None,
) -> tuple[list[dict], list[dict]]:
    '''Return (pending, skipped) using content hashes.

    A document is pending when its stage is missing, failed, incomplete,
    or its source content hash differs from the last completed hash.
    Explicit --readwise-id values bypass the skip decision.
    '''
    ids = [str(d.get('readwise_id') or d.get('document_id') or '').strip() for d in documents]
    ensure_stage_rows(conn, ids, stage)
    rows = get_stage_rows(conn, ids, stage)
    forced = {str(x).strip() for x in (force_ids or [])}
    pending, skipped = [], []

    for document in documents:
        rid = str(document.get('readwise_id') or document.get('document_id') or '').strip()
        current_hash = content_hash(
            document.get('content', ''),
            document.get('canonical_text', ''),
        )
        row = rows.get(rid)
        done = (
            row is not None
            and row['status'] == 'completed'
            and row['content_hash'] == current_hash
            and rid not in forced
        )
        document['_stage_content_hash'] = current_hash
        if done:
            skipped.append(document)
        else:
            pending.append(document)
    return pending, skipped


def mark_started(conn: sqlite3.Connection, documents: list[dict], stage: str, worker_id: str = '') -> None:
    now = utc_now()
    for d in documents:
        rid = str(d.get('readwise_id') or d.get('document_id') or '').strip()
        conn.execute(
            '''UPDATE document_stages
               SET status='running', attempt_count=attempt_count+1,
                   started_at=?, completed_at=NULL, last_error=NULL,
                   worker_id=?, content_hash=?, updated_at=?
               WHERE readwise_id=? AND stage=?''',
            (now, worker_id, d.get('_stage_content_hash') or content_hash(d.get('content', ''), d.get('canonical_text', '')), now, rid, stage),
        )
    conn.commit()


def mark_completed(conn: sqlite3.Connection, documents: list[dict], stage: str, result_by_id: dict, result_fields: tuple[str, ...] = ('korean_title', 'korean_text')) -> None:
    now = utc_now()
    for d in documents:
        rid = str(d.get('readwise_id') or d.get('document_id') or '').strip()
        did = d.get('document_id')
        result = result_by_id.get(did, {})
        rh = result_hash(*(result.get(k, '') for k in result_fields))
        metadata = {
            k: result[k]
            for k in ('output_path', 'title', 'old_filename', 'new_filename')
            if k in result
        }
        conn.execute(
            '''UPDATE document_stages
               SET status='completed', completed_at=?, last_error=NULL,
                   content_hash=?, result_hash=?, result_json=?, updated_at=?
               WHERE readwise_id=? AND stage=?''',
            (
                now,
                d.get('_stage_content_hash'),
                rh,
                json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                now,
                rid,
                stage,
            ),
        )
    conn.commit()

def mark_failed(conn: sqlite3.Connection, documents: list[dict], stage: str, error: Exception) -> None:
    now = utc_now()
    message = str(error)[:4000]
    for d in documents:
        rid = str(d.get('readwise_id') or d.get('document_id') or '').strip()
        conn.execute(
            '''UPDATE document_stages
               SET status='failed', last_error=?, updated_at=?
               WHERE readwise_id=? AND stage=?''',
            (message, now, rid, stage),
        )
    conn.commit()
