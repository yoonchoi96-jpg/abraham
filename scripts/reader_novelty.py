"""Conservative, local-only semantic novelty candidates for Abraham Reader.

Not a topic classifier and not an automatic deletion/DeepSeek suppression.
Semantic similarity proposes candidate matches; source-backed claim coverage,
new numbers/dates, and negations block unsafe skips. No model downloads or API
keys. Caller supplies local embedding function: list[str] -> list[list[float]].
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from typing import Callable

import numpy as np


MAX_BODY_CHARS = 40000
MAX_CLAIMS = 32
MIN_CLAIM_CHARS = 18
TOP_K = 5


def claims_from_text(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?。！？])\s*|\n+", text)
    return [p.strip() for p in parts if len(p.strip()) >= MIN_CLAIM_CHARS]


def critical_markers(text: str) -> set[str]:
    # Differences in numbers, times, quantitative units, explicit quotes and
    # negation cues mean "new/possibly conflicting evidence" regardless of sim.
    numbers = {m.lower() for m in re.findall(
        r"(?<!\w)\d+(?:[.,]\d+)*(?:%|％|억|만|조|bn|million|billion|년|월|일|원|달러)?", text
    )}
    quoted = {m.casefold() for m in re.findall(
        r'["“「『]([^"”」』]{3,120})["”」』]', text
    )}
    negations = {x for x in (
        "not", "no longer", "denied", "false", "never",
        "부인", "아니다", "없다", "취소", "중단", "不", "没有", "否认",
    ) if x in text.casefold()}
    return {"number:" + x for x in numbers} | {
        "quote:" + x for x in quoted
    } | {"negation:" + x for x in negations}


def normalized_embedding(value) -> np.ndarray:
    v = np.asarray(value, dtype=np.float32)
    if v.ndim != 1 or v.size < 8 or v.size > 8192 or not np.isfinite(v).all():
        raise ValueError("INVALID_LOCAL_EMBEDDING")
    mag = float(np.linalg.norm(v))
    if mag < 1e-12:
        raise ValueError("ZERO_EMBEDDING")
    return v / mag


class NoveltyIndex:
    """SQLite-backed cross-document semantic comparison. One writer.

    Every source is preserved. "redundancy_candidate" is NOT an authorization
    to drop its original, grade, quotation, translation or unique perspective.
    """

    def __init__(self, database: sqlite3.Connection, *, model_id: str):
        if not model_id or len(model_id) > 256:
            raise ValueError("MODEL_ID_REQUIRED")
        self.db = database
        self.model_id = model_id
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS novelty_v1 (
            document_key TEXT NOT NULL, model_id TEXT NOT NULL,
            body_sha256 TEXT NOT NULL, title TEXT NOT NULL,
            body_excerpt TEXT NOT NULL, markers TEXT NOT NULL,
            vector BLOB, dimensions INTEGER,
            decision TEXT NOT NULL, match_key TEXT, similarity REAL,
            coverage REAL, reason TEXT NOT NULL,
            PRIMARY KEY(document_key, model_id)
        );
        CREATE INDEX IF NOT EXISTS novelty_v1_hash
            ON novelty_v1(model_id,body_sha256);
        """)

    def _top_matches(self, vector, *, exclude):
        matches = []
        cursor = self.db.execute(
            """SELECT document_key, vector, dimensions, body_excerpt, markers
               FROM novelty_v1
               WHERE model_id=? AND vector IS NOT NULL AND document_key<>?""",
            (self.model_id, exclude)
        )
        # Disk-streaming O(N) search: no full vector library in unified RAM.
        # At the current 12.8k-doc scale this avoids requiring a new ANN
        # dependency; index acceleration can be benchmarked separately.
        for key, raw, dimensions, excerpt, markers in cursor:
            if dimensions != vector.size:
                continue
            prior = np.frombuffer(raw, dtype="<f4")
            similarity = float(np.dot(prior, vector))
            if similarity >= 0.84:
                matches.append((similarity, key, excerpt, markers))
                matches.sort(key=lambda x: -x[0])
                if len(matches) > TOP_K:
                    matches.pop()
        return matches

    def assess(self, key: str, title: str, body: str,
               embed: Callable[[list[str]], list[list[float]]]) -> dict:
        if not key or not isinstance(body, str) or not body.strip():
            raise ValueError("NONEMPTY_DOCUMENT_REQUIRED")
        already = self.db.execute(
            """SELECT decision,match_key,similarity,coverage,reason FROM novelty_v1
               WHERE document_key=? AND model_id=?""",
            (key, self.model_id)
        ).fetchone()
        if already:
            return dict(zip(("decision", "match_key", "similarity",
                             "coverage", "reason"), already))
        sha = hashlib.sha256(body.encode()).hexdigest()
        old = self.db.execute(
            """SELECT document_key FROM novelty_v1
               WHERE model_id=? AND body_sha256=? LIMIT 1""",
            (self.model_id, sha)
        ).fetchone()
        decision, match_key, score, coverage = "novel", None, None, None
        reason = "NO_EQUIVALENT_EVIDENCE"
        vector = None
        markers = critical_markers(body)
        excerpt = body if len(body) <= MAX_BODY_CHARS else body[:MAX_BODY_CHARS]
        if old:
            decision, match_key, score, coverage = "exact_duplicate", old[0], 1.0, 1.0
            reason = "IDENTICAL_EXTRACTED_BODY"
        elif len(body) > MAX_BODY_CHARS:
            decision, reason = "review_required", "LONG_DOCUMENT_NOT_COMPARED"
        else:
            whole = embed([title + "\n" + excerpt])
            if len(whole) != 1:
                raise ValueError("EMBEDDING_COUNT_MISMATCH")
            vector = normalized_embedding(whole[0])
            matches = self._top_matches(vector, exclude=key)
            if matches:
                score, match_key, other_text, other_markers = matches[0]
                if score < 0.94:
                    decision, reason = "novel", "TOPIC_SIMILAR_BUT_NOT_EQUIVALENT"
                elif markers != set(json.loads(other_markers)):
                    decision, reason = "novel", "DISTINCT_NUMBERS_QUOTES_OR_NEGATIONS"
                else:
                    current_claims, other_claims = claims_from_text(body), claims_from_text(other_text)
                    if (not current_claims or not other_claims or
                        len(current_claims) > MAX_CLAIMS or len(other_claims) > MAX_CLAIMS):
                        decision, reason = "review_required", "CLAIMS_TOO_LONG_OR_EMPTY"
                    else:
                        vectors = embed(current_claims + other_claims)
                        if len(vectors) != len(current_claims) + len(other_claims):
                            raise ValueError("EMBEDDING_COUNT_MISMATCH")
                        a = np.stack([normalized_embedding(v) for v in vectors[:len(current_claims)]])
                        b = np.stack([normalized_embedding(v) for v in vectors[len(current_claims):]])
                        if a.shape[1] != b.shape[1] or a.shape[1] != vector.size:
                            raise ValueError("EMBEDDING_DIMENSIONS_CHANGED")
                        similarities = a @ b.T
                        coverage = float(np.mean(np.max(similarities, axis=1) >= 0.96))
                        if coverage == 1.0 and score >= 0.965:
                            decision, reason = "redundancy_candidate", "LOCAL_CLAIMS_COVERED"
                        else:
                            decision, reason = "novel", "NOVEL_OR_UNCERTAIN_CLAIM"
        record = (key, self.model_id, sha, str(title)[:500], excerpt,
                  json.dumps(sorted(markers), ensure_ascii=False),
                  vector.astype("<f4").tobytes() if vector is not None else None,
                  int(vector.size) if vector is not None else None,
                  decision, match_key, score, coverage, reason)
        with self.db:
            self.db.execute(
                """INSERT OR IGNORE INTO novelty_v1 VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                record
            )
        return dict(decision=decision, match_key=match_key,
                    similarity=score, coverage=coverage, reason=reason)

    def counts(self) -> dict:
        return dict(self.db.execute(
            "SELECT decision, COUNT(*) FROM novelty_v1 WHERE model_id=? GROUP BY decision",
            (self.model_id,)
        ).fetchall())
