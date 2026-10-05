from __future__ import annotations

import hashlib
import json
import os
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
CREATE INDEX IF NOT EXISTS idx_document_stages_status ON document_stages(status);
CREATE INDEX IF NOT EXISTS idx_document_stages_stage ON document_stages(stage);
'''

MAX_TRANSIENT_ATTEMPTS = int(os.getenv("ABRAHAM_MAX_TRANSIENT_ATTEMPTS", "5"))

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

def content_hash(*parts: object) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update(('' if part is None else str(part)).encode('utf-8', errors='replace'))
        h.update(b'\x00')
    return h.hexdigest()

def result_hash(*parts: object) -> str:
    return content_hash(*parts)

def _failure_status(error: Exception) -> str:
    text = str(error).lower()
    permanent_markers = (
        "content exists risk", "content_exists_risk", "invalid translation index",
        "translation index out of range", "missing korean_title", "missing korean_text",
        "invalid rating value", "rating must be", "document_id mismatch",
        "entity/relation extraction produced no valid result",
    )
    return "permanent_failed" if any(x in text for x in permanent_markers) else "transient_failed"

def failure_category(error: Exception | str) -> str:
    text = str(error).lower()
    if any(marker in text for marker in ("content exists risk", "content_exists_risk")):
        return "content_risk"
    if any(marker in text for marker in ("429", "rate limit", "rate_limit", "too many requests")):
        return "rate_limit"
    if any(marker in text for marker in ("invalid", "malformed", "missing", "mismatch", "json")):
        return "data"
    return "api"

def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(document_stages)").fetchall()}
    for column, column_type in {"content_hash": "TEXT", "result_hash": "TEXT", "result_json": "TEXT"}.items():
        if column not in columns:
            conn.execute(f"ALTER TABLE document_stages ADD COLUMN {column} {column_type}")
            print(f"[SCHEMA MIGRATION] document_stages.{column} added")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_document_stages_status ON document_stages(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_document_stages_stage ON document_stages(stage)")
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
    result = {}
    for start in range(0, len(ids), 900):
        chunk = ids[start:start + 900]
        placeholders = ','.join('?' for _ in chunk)
        rows = conn.execute(
            f'''SELECT * FROM document_stages
                WHERE stage = ? AND readwise_id IN ({placeholders})''',
            (stage, *chunk),
        ).fetchall()
        result.update({str(row['readwise_id']): row for row in rows})
    return result

def select_pending_documents(
    conn: sqlite3.Connection,
    documents: list[dict],
    stage: str,
    *,
    force_ids: Iterable[str] | None = None,
    retry_ids: Iterable[str] | None = None,
    ensure_rows: bool = True,
) -> tuple[list[dict], list[dict]]:
    """Return (pending, skipped), suppressing permanent failures and exhausted transient failures."""
    ids = [str(d.get('readwise_id') or d.get('document_id') or '').strip() for d in documents]
    if ensure_rows:
        ensure_stage_rows(conn, ids, stage)
    rows = get_stage_rows(conn, ids, stage)
    forced = {str(x).strip() for x in (force_ids or [])}
    retries = {str(x).strip() for x in (retry_ids or [])}
    pending, skipped = [], []
    for document in documents:
        rid = str(document.get('readwise_id') or document.get('document_id') or '').strip()
        current_hash = content_hash(document.get('content', ''), document.get('canonical_text', ''))
        row = rows.get(rid)
        status = str(row['status']) if row else 'pending'
        attempts = int(row['attempt_count'] or 0) if row else 0
        done = row is not None and status == 'completed' and row['content_hash'] == current_hash and rid not in forced
        failure_hash_matches = row is not None and row['content_hash'] == current_hash
        exhausted = (
            rid not in forced and rid not in retries and failure_hash_matches and
            (status == 'permanent_failed' or (status == 'transient_failed' and attempts >= MAX_TRANSIENT_ATTEMPTS))
        )
        document['_stage_content_hash'] = current_hash
        if done or exhausted:
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
        result = result_by_id.get(d.get('document_id'), {})
        rh = result_hash(*(result.get(k, '') for k in result_fields))
        metadata = {k: result[k] for k in ('output_path','title','old_filename','new_filename','korean_title','korean_text','translation_notes','prompt_version','model','cache_key','graph_stats') if k in result}
        conn.execute(
            '''UPDATE document_stages
               SET status='completed', completed_at=?, last_error=NULL,
                   content_hash=?, result_hash=?, result_json=?, updated_at=?
               WHERE readwise_id=? AND stage=?''',
            (now, d.get('_stage_content_hash'), rh, json.dumps(metadata, ensure_ascii=False, sort_keys=True), now, rid, stage),
        )
    conn.commit()

def mark_failed(conn: sqlite3.Connection, documents: list[dict], stage: str, error: Exception) -> None:
    now = utc_now()
    message = str(error)[:4000]
    status = _failure_status(error)
    category = failure_category(error)
    for d in documents:
        rid = str(d.get('readwise_id') or d.get('document_id') or '').strip()
        conn.execute(
            '''UPDATE document_stages
               SET status=?, last_error=?, updated_at=?
               WHERE readwise_id=? AND stage=?''',
            (status, message, now, rid, stage),
        )
        row = conn.execute(
            '''SELECT attempt_count FROM document_stages
               WHERE readwise_id=? AND stage=?''',
            (rid, stage),
        ).fetchone()
        attempts = int(row["attempt_count"] or 0) if row else 0
        print(json.dumps({
            "event": "stage_failure",
            "document_id": str(d.get("document_id") or rid),
            "readwise_id": rid,
            "stage": stage,
            "error_type": type(error).__name__,
            "category": category,
            "reason": message,
            "status": status,
            "retry_eligible": (
                status == "transient_failed" and attempts < MAX_TRANSIENT_ATTEMPTS
            ),
        }, ensure_ascii=False, sort_keys=True))
    conn.commit()
