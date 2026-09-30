"""Abraham V6 semantic intelligence: incremental local semantic index/search.

No external embedding/API is required. The default engine combines:
1) hashed sparse vectors for semantic-ish cosine similarity,
2) a SQLite inverted token index for cheap candidate generation,
3) vector reranking over only the candidate set.

The design is intentionally provider-free so a true embedding provider can be
added later without changing the V6 storage contract.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from collections import Counter

VERSION = "6.1.0"
DIMS = 384
TOKEN_RE = re.compile(
    r"[A-Za-z0-9_\-]{2,}|[가-힣]{2,}|[一-龯]{2,}|[ぁ-ゔァ-ヴー]{2,}"
)


def tokenize(text):
    return [x.lower() for x in TOKEN_RE.findall(text or "")]


def _hash(text):
    return hashlib.sha256((text or "").encode("utf-8", "replace")).hexdigest()


def vector(text, dims=DIMS):
    counts = Counter(tokenize(text))
    total = sum(counts.values()) or 1
    out = {}
    for token, n in counts.items():
        key = str(int(hashlib.sha256(token.encode()).hexdigest()[:16], 16) % dims)
        out[key] = out.get(key, 0.0) + n / total
    norm = math.sqrt(sum(v * v for v in out.values())) or 1.0
    return {k: v / norm for k, v in out.items()}


def cosine(a, b):
    # Iterate through the smaller dict for cheap sparse dot products.
    if len(a) > len(b):
        a, b = b, a
    return sum(v * b.get(k, 0.0) for k, v in a.items())


def ensure_schema(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS v6_vectors(
            readwise_id TEXT PRIMARY KEY,
            content_hash TEXT NOT NULL,
            vector_json TEXT NOT NULL,
            dims INTEGER NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_v6_vectors_hash
            ON v6_vectors(content_hash);

        CREATE TABLE IF NOT EXISTS v6_tokens(
            token TEXT NOT NULL,
            readwise_id TEXT NOT NULL,
            tf REAL NOT NULL,
            PRIMARY KEY(token, readwise_id)
        );
        CREATE INDEX IF NOT EXISTS idx_v6_tokens_token
            ON v6_tokens(token);
        CREATE INDEX IF NOT EXISTS idx_v6_tokens_doc
            ON v6_tokens(readwise_id);
        """
    )
    conn.commit()


def _doc_id(d):
    return str(d.get("readwise_id") or d.get("document_id") or "").strip()


def _text(d):
    return (d.get("title", "") + "\n" + (d.get("canonical_text") or d.get("content") or "")).strip()


def index_documents(conn, docs):
    ensure_schema(conn)
    added = updated = skipped = 0

    for d in docs:
        rid = _doc_id(d)
        if not rid:
            continue
        text = _text(d)
        h = _hash(text)
        old = conn.execute(
            "SELECT content_hash FROM v6_vectors WHERE readwise_id=?", (rid,)
        ).fetchone()
        if old and old[0] == h:
            skipped += 1
            continue

        vec = vector(text)
        counts = Counter(tokenize(text))
        total = sum(counts.values()) or 1

        conn.execute(
            """
            INSERT INTO v6_vectors
                (readwise_id,content_hash,vector_json,dims,updated_at)
            VALUES(?,?,?,?,CURRENT_TIMESTAMP)
            ON CONFLICT(readwise_id) DO UPDATE SET
                content_hash=excluded.content_hash,
                vector_json=excluded.vector_json,
                dims=excluded.dims,
                updated_at=CURRENT_TIMESTAMP
            """,
            (rid, h, json.dumps(vec, separators=(",", ":")), DIMS),
        )

        # Rebuild this document's inverted-index rows only when its content changed.
        conn.execute("DELETE FROM v6_tokens WHERE readwise_id=?", (rid,))
        conn.executemany(
            "INSERT OR REPLACE INTO v6_tokens(token,readwise_id,tf) VALUES(?,?,?)",
            [(token, rid, n / total) for token, n in counts.items()],
        )

        if old:
            updated += 1
        else:
            added += 1

    conn.commit()
    return {"added": added, "updated": updated, "skipped": skipped}


def _docs(conn, rids):
    if not rids:
        return []
    q = ",".join("?" for _ in rids)
    rows = conn.execute(
        f"SELECT * FROM documents WHERE readwise_id IN ({q})", rids
    ).fetchall()
    return [dict(r) for r in rows]


def _candidate_ids(conn, query, candidate_limit):
    tokens = list(dict.fromkeys(tokenize(query)))
    if not tokens:
        return []

    placeholders = ",".join("?" for _ in tokens)
    rows = conn.execute(
        f"""
        SELECT readwise_id, SUM(tf) AS lexical_score
        FROM v6_tokens
        WHERE token IN ({placeholders})
        GROUP BY readwise_id
        ORDER BY lexical_score DESC
        LIMIT ?
        """,
        (*tokens, candidate_limit),
    ).fetchall()
    return [str(r["readwise_id"]) for r in rows]


def search(conn, query, limit=10):
    ensure_schema(conn)
    limit = max(1, min(int(limit), 50))

    # Generate a broad lexical candidate pool, then rerank semantically.
    candidate_limit = min(max(limit * 12, 40), 500)
    ids = _candidate_ids(conn, query, candidate_limit)

    # For an out-of-vocabulary query, fall back to a bounded vector scan.
    if not ids:
        rows = conn.execute(
            "SELECT readwise_id, vector_json FROM v6_vectors LIMIT 5000"
        ).fetchall()
    else:
        placeholders = ",".join("?" for _ in ids)
        rows = conn.execute(
            f"SELECT readwise_id, vector_json FROM v6_vectors WHERE readwise_id IN ({placeholders})",
            ids,
        ).fetchall()

    qv = vector(query)
    scored = []
    for row in rows:
        try:
            scored.append(
                (cosine(qv, json.loads(row["vector_json"])), str(row["readwise_id"]))
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            continue

    scored.sort(reverse=True)
    out = []
    for score, rid in scored[:limit]:
        ds = _docs(conn, [rid])
        d = ds[0] if ds else {}
        out.append(
            {
                "readwise_id": rid,
                "score": round(score, 6),
                "title": d.get("title", ""),
            }
        )
    return out


def related(conn, rid, limit=10):
    row = conn.execute(
        "SELECT vector_json FROM v6_vectors WHERE readwise_id=?", (rid,)
    ).fetchone()
    if not row:
        return []

    # Use the source document's strongest tokens as candidates. This avoids a
    # full-corpus pairwise scan for normal documents.
    source = conn.execute(
        "SELECT token FROM v6_tokens WHERE readwise_id=? ORDER BY tf DESC LIMIT 12",
        (rid,),
    ).fetchall()
    tokens = [r["token"] for r in source]
    if not tokens:
        return []

    placeholders = ",".join("?" for _ in tokens)
    candidates = conn.execute(
        f"""
        SELECT readwise_id, SUM(tf) AS lexical_score
        FROM v6_tokens
        WHERE token IN ({placeholders}) AND readwise_id<>?
        GROUP BY readwise_id
        ORDER BY lexical_score DESC
        LIMIT ?
        """,
        (*tokens, rid, max(40, min(limit * 15, 500))),
    ).fetchall()
    ids = [str(r["readwise_id"]) for r in candidates]
    if not ids:
        return []

    ph = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT readwise_id, vector_json FROM v6_vectors WHERE readwise_id IN ({ph})",
        ids,
    ).fetchall()
    target = json.loads(row["vector_json"])
    vals = []
    for r in rows:
        try:
            vals.append((cosine(target, json.loads(r["vector_json"])), str(r["readwise_id"])))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    vals.sort(reverse=True)
    return [
        {"readwise_id": rid2, "score": round(score, 6)}
        for score, rid2 in vals[:limit]
    ]
