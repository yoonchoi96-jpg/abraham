"""Abraham V8 event-driven proactive insight detector.

Default behavior is fully local: no LLM/API call is required.
Only changed completed document stages are considered, using a durable
stage signature so repeated scheduled runs are cheap and idempotent.
"""
from __future__ import annotations
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from v6_semantic import related

VERSION = "8.3.0"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash(*parts: object) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update(("" if part is None else str(part)).encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()


def ensure_schema(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS v8_events(
        event_id TEXT PRIMARY KEY,
        event_type TEXT NOT NULL,
        entity_key TEXT,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS v8_insights(
        insight_id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        title TEXT NOT NULL,
        body TEXT NOT NULL,
        evidence_json TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'candidate',
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_v8_insights_status ON v8_insights(status);

    CREATE TABLE IF NOT EXISTS v8_processed_stages(
        readwise_id TEXT NOT NULL,
        stage TEXT NOT NULL,
        stage_signature TEXT NOT NULL,
        processed_at TEXT NOT NULL,
        PRIMARY KEY(readwise_id, stage)
    );
    CREATE INDEX IF NOT EXISTS idx_v8_processed_signature
        ON v8_processed_stages(stage_signature);
    CREATE INDEX IF NOT EXISTS idx_v8_events_entity
        ON v8_events(entity_key,created_at);
    """)
    conn.commit()


def _stage_signature(row) -> str:
    # updated_at is intentionally excluded: a harmless timestamp-only update
    # must not force another expensive similarity scan.
    return _hash(
        row["readwise_id"],
        row["stage"],
        row["status"],
        row["content_hash"],
        row["result_hash"],
        row["result_json"],
    )


def detect(conn, documents, max_documents=250):
    ensure_schema(conn)
    byid = {
        str(d.get("readwise_id") or d.get("document_id")): d
        for d in documents
        if str(d.get("readwise_id") or d.get("document_id") or "").strip()
    }
    checked = created = skipped = 0
    cap = max(1, min(int(max_documents), 250))

    rows = conn.execute(
        """SELECT readwise_id, stage, status, content_hash, result_hash, result_json
           FROM document_stages
           WHERE status='completed'
           ORDER BY updated_at DESC
           LIMIT ?""",
        (cap,),
    ).fetchall()

    for row in rows:
        rid = str(row["readwise_id"])
        d = byid.get(rid)
        if not d:
            continue

        signature = _stage_signature(row)
        old = conn.execute(
            """SELECT stage_signature
               FROM v8_processed_stages
               WHERE readwise_id=? AND stage=?""",
            (rid, row["stage"]),
        ).fetchone()
        if old and old[0] == signature:
            skipped += 1
            continue

        checked += 1
        rel = related(conn, rid, 3)
        # Persist the watermark even when there are no related documents:

        # that case is also a fully processed state.
        now = _now()
        conn.execute(
            """INSERT INTO v8_processed_stages
               (readwise_id, stage, stage_signature, processed_at)
               VALUES (?,?,?,?)
               ON CONFLICT(readwise_id, stage) DO UPDATE SET
                 stage_signature=excluded.stage_signature,
                 processed_at=excluded.processed_at""",
            (rid, row["stage"], signature, now),
        )

        if not rel:
            continue

        # Ignore weak connections; V8 should produce candidates, not noise.
        rel = [x for x in rel if float(x.get("score", 0.0)) >= 0.15]
        if not rel:
            continue

        related_titles = []
        for item in rel:
            rr = conn.execute("SELECT title FROM documents WHERE readwise_id=?", (item["readwise_id"],)).fetchone()
            related_titles.append({
                "readwise_id": item["readwise_id"],
                "title": (rr["title"] if rr else "") or "",
                "score": item["score"],
            })
        payload = {
            "readwise_id": rid,
            "stage": row["stage"],
            "title": d.get("title", ""),
            "related": related_titles,
            "stage_signature": signature,
        }

        eid = _hash("new_related_cluster", rid, row["stage"], signature)[:32]
        if conn.execute(
            "SELECT 1 FROM v8_events WHERE event_id=?", (eid,)
        ).fetchone():
            continue

        conn.execute(
            "INSERT INTO v8_events VALUES(?,?,?,?,?)",
            (
                eid,
                "new_related_cluster",
                rid,
                json.dumps(payload, ensure_ascii=False),
                now,
            ),
        )
        iid = _hash(eid, "insight")[:32]
        conn.execute(
            """INSERT OR IGNORE INTO v8_insights
               VALUES(?,?,?,?,?,?,?)""",
            (
                iid,
                "knowledge_connection",
                "새로운 지식 연결 후보",
                "기존 지식과의 강한 연결이 감지되었습니다: " + ", ".join(
                    f'{x["title"] or x["readwise_id"]} ({float(x["score"]):.2f})' for x in related_titles
                ) + ". 원문 근거를 검토하세요.",
                json.dumps(payload, ensure_ascii=False),
                "candidate",
                now,
            ),
        )
        created += 1

    conn.commit()
    return {"checked": checked, "skipped": skipped, "events_created": created}


def pending_insights(conn, limit=20):
    ensure_schema(conn)
    limit = max(1, min(int(limit), 100))
    return [
        dict(r)
        for r in conn.execute(
            """SELECT * FROM v8_insights
               WHERE status='candidate'
               ORDER BY created_at DESC LIMIT ?""",
            (limit,),
        )
    ]
