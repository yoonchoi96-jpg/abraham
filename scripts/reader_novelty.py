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
MAX_CLAIM_CHARS = 3400
MIN_CLAIM_CHARS = 18
TOP_K = 5
# Old similarity decisions were based on head-only embedding and a single
# nearest neighbor. Re-evaluate them before they may become review candidates.
POLICY_REVISION = "v3-headline-guards-dependent-signature-20261021"


def claims_from_text(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?。！？])\s*|\n+", text)
    return [p.strip() for p in parts if len(p.strip()) >= MIN_CLAIM_CHARS]


def critical_markers(text: str) -> set[str]:
    # Differences in numbers, times, quantitative units, explicit quotes and
    # negation cues mean "new/possibly conflicting evidence" regardless of sim.
    numbers = {m.lower() for m in re.findall(
        r"\d+(?:[.,]\d+)*(?:%|％|억|만|조|bn|million|billion|년|월|일|원|달러)?", text
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
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(novelty_v1)")}
        if "policy_revision" not in columns:
            self.db.execute("ALTER TABLE novelty_v1 ADD COLUMN policy_revision TEXT")
        if "match_body_sha256" not in columns:
            self.db.execute("ALTER TABLE novelty_v1 ADD COLUMN match_body_sha256 TEXT")
        self.db.commit()

    def is_current(self, key: str) -> bool:
        """Old policies or mutated matching originals invalidate dependents."""
        row = self.db.execute(
            """SELECT policy_revision,match_key,match_body_sha256
               FROM novelty_v1 WHERE document_key=? AND model_id=?""",
            (key, self.model_id)
        ).fetchone()
        if not row or row[0] != POLICY_REVISION:
            return False
        if not row[1]:
            return True
        match = self.db.execute(
            """SELECT body_sha256,title FROM novelty_v1
               WHERE document_key=? AND model_id=?""",
            (row[1], self.model_id)
        ).fetchone()
        return bool(match and row[2] and self._source_signature(*match) == row[2])

    @staticmethod
    def _source_signature(body_sha256: str, title: str) -> str:
        return hashlib.sha256(
            json.dumps([body_sha256, title], ensure_ascii=False).encode("utf-8")
        ).hexdigest()

    def _top_matches(self, vector, *, exclude):
        matches = []
        cursor = self.db.execute(
            """SELECT document_key, vector, dimensions, body_excerpt, markers, title
               FROM novelty_v1
               WHERE model_id=? AND policy_revision=?
                 AND vector IS NOT NULL AND document_key<>?""",
            (self.model_id, POLICY_REVISION, exclude)
        )
        # Disk-streaming O(N) search: no full vector library in unified RAM.
        # At the current 12.8k-doc scale this avoids requiring a new ANN
        # dependency; index acceleration can be benchmarked separately.
        for key, raw, dimensions, excerpt, markers, headline in cursor:
            if dimensions != vector.size:
                continue
            prior = np.frombuffer(raw, dtype="<f4")
            similarity = float(np.dot(prior, vector))
            if similarity >= 0.84:
                matches.append((similarity, key, excerpt, markers, headline))
                matches.sort(key=lambda x: (-x[0], x[1]))
                if len(matches) > TOP_K:
                    matches.pop()
        return matches

    def assess(self, key: str, title: str, body: str,
               embed: Callable[[list[str]], list[list[float]]]) -> dict:
        if not key or not isinstance(body, str) or not body.strip():
            raise ValueError("NONEMPTY_DOCUMENT_REQUIRED")
        # A Reader item may gain new paragraphs or change its title in place.
        # Cached decisions are valid only for the exact assessed input, never
        # merely for a stable Drive/document ID. Preserve the previous record
        # until the *new* inference finishes and can commit atomically.
        sha = hashlib.sha256(body.encode()).hexdigest()
        stored_title = str(title)[:500]
        already = self.db.execute(
            """SELECT body_sha256,title,decision,match_key,similarity,coverage,reason
               FROM novelty_v1 WHERE document_key=? AND model_id=?""",
            (key, self.model_id)
        ).fetchone()
        if (already and already[0] == sha and already[1] == stored_title
                and self.is_current(key)):
            return dict(zip(("decision", "match_key", "similarity",
                             "coverage", "reason"), already[2:]))
        old = self.db.execute(
            """SELECT document_key FROM novelty_v1
               WHERE model_id=? AND body_sha256=? AND title=?
                 AND document_key<>? LIMIT 1""",
            (self.model_id, sha, stored_title, key)
        ).fetchone()
        decision, match_key, score, coverage = "novel", None, None, None
        reason = "NO_EQUIVALENT_EVIDENCE"
        match_sha = None
        vector = None
        markers = critical_markers(title + "\n" + body)
        excerpt = body if len(body) <= MAX_BODY_CHARS else body[:MAX_BODY_CHARS]
        if old:
            decision, match_key, score, coverage = "exact_duplicate", old[0], 1.0, 1.0
            reason = "IDENTICAL_EXTRACTED_BODY"
            matched = self.db.execute(
                """SELECT title FROM novelty_v1
                   WHERE document_key=? AND model_id=?""",
                (match_key, self.model_id)
            ).fetchone()
            if not matched:
                raise ValueError("MATCH_DISAPPEARED")
            match_sha = self._source_signature(sha, matched[0])
        elif len(body) > MAX_BODY_CHARS:
            decision, reason = "review_required", "LONG_DOCUMENT_NOT_COMPARED"
        else:
            whole = embed([title + "\n" + excerpt])
            if len(whole) != 1:
                raise ValueError("EMBEDDING_COUNT_MISMATCH")
            vector = normalized_embedding(whole[0])
            matches = self._top_matches(vector, exclude=key)
            if matches:
                score, match_key = matches[0][0], matches[0][1]
                reason = "TOPIC_SIMILAR_BUT_NOT_EQUIVALENT"
                # A topically similar first hit need not be the best evidence
                # match. Try the bounded top-k; never combine DIFFERENT
                # articles' claims into a fabricated single complete source.
                current_claims = claims_from_text(title + "\n" + body)
                review_needed = False
                current_vectors = None
                for candidate_score, candidate_key, other_text, other_markers, other_title in matches:
                    if candidate_score < 0.94:
                        continue
                    if markers != set(json.loads(other_markers)):
                        reason = "DISTINCT_NUMBERS_QUOTES_OR_NEGATIONS"
                        continue
                    other_claims = claims_from_text(other_title + "\n" + other_text)
                    if (not current_claims or not other_claims or
                        len(current_claims) > MAX_CLAIMS or len(other_claims) > MAX_CLAIMS or
                        any(len(c) > MAX_CLAIM_CHARS for c in current_claims + other_claims)):
                        review_needed = True
                        continue
                    if current_vectors is None:
                        embedded = embed(current_claims)
                        if len(embedded) != len(current_claims):
                            raise ValueError("EMBEDDING_COUNT_MISMATCH")
                        current_vectors = np.stack([normalized_embedding(v) for v in embedded])
                        if current_vectors.shape[1] != vector.size:
                            raise ValueError("EMBEDDING_DIMENSIONS_CHANGED")
                    embedded = embed(other_claims)
                    if len(embedded) != len(other_claims):
                        raise ValueError("EMBEDDING_COUNT_MISMATCH")
                    other_vectors = np.stack([normalized_embedding(v) for v in embedded])
                    if other_vectors.shape[1] != vector.size:
                        raise ValueError("EMBEDDING_DIMENSIONS_CHANGED")
                    similarities = current_vectors @ other_vectors.T
                    candidate_coverage = float(np.mean(np.max(similarities, axis=1) >= 0.96))
                    if candidate_coverage == 1.0 and candidate_score >= 0.965:
                        decision, reason = "redundancy_candidate", "LOCAL_CLAIMS_COVERED"
                        score, match_key, coverage = candidate_score, candidate_key, candidate_coverage
                        break
                    if candidate_key == match_key:
                        coverage = candidate_coverage
                        reason = "NOVEL_OR_UNCERTAIN_CLAIM"
                else:
                    if review_needed:
                        decision, reason = "review_required", "CLAIMS_TOO_LONG_OR_EMPTY"
                if decision == "redundancy_candidate":
                    matched = self.db.execute(
                        """SELECT body_sha256,title FROM novelty_v1
                           WHERE document_key=? AND model_id=?""",
                        (match_key, self.model_id)
                    ).fetchone()
                    if not matched:
                        raise ValueError("MATCH_DISAPPEARED")
                    match_sha = self._source_signature(*matched)
                else:
                    # A novel/review decision does not depend on a particular
                    # neighbor. Avoid storing a dangling match ID as evidence.
                    match_key = None
        record = (key, self.model_id, sha, stored_title, excerpt,
                  json.dumps(sorted(markers), ensure_ascii=False),
                  vector.astype("<f4").tobytes() if vector is not None else None,
                  int(vector.size) if vector is not None else None,
                  decision, match_key, score, coverage, reason,
                  POLICY_REVISION, match_sha)
        with self.db:
            self.db.execute(
                """INSERT OR REPLACE INTO novelty_v1
                   (document_key,model_id,body_sha256,title,body_excerpt,markers,
                    vector,dimensions,decision,match_key,similarity,coverage,
                    reason,policy_revision,match_body_sha256)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                record
            )
        return dict(decision=decision, match_key=match_key,
                    similarity=score, coverage=coverage, reason=reason)

    def evidence_cards(self, key: str,
                       embed: Callable[[list[str]], list[list[float]]]) -> dict:
        """Read-only claim→source comparison for human review.

        Similarity is a retrieval clue, NOT textual entailment or authorization
        to skip translation. Quotes are literal excerpts from the persisted
        parsed source. Any stale origin makes this view unavailable.
        """
        if not self.is_current(key):
            return {"state": "stale_or_missing", "cards": []}
        row = self.db.execute(
            """SELECT title,body_excerpt,decision,match_key FROM novelty_v1
               WHERE document_key=? AND model_id=?""", (key, self.model_id)
        ).fetchone()
        if not row or row[2] != "redundancy_candidate" or not row[3]:
            return {"state": "not_redundancy_candidate", "cards": []}
        source = self.db.execute(
            """SELECT title,body_excerpt FROM novelty_v1
               WHERE document_key=? AND model_id=?""", (row[3], self.model_id)
        ).fetchone()
        if not source:
            return {"state": "stale_or_missing", "cards": []}
        current_text = row[0] + "\n" + row[1]
        source_text = source[0] + "\n" + source[1]
        current_claims = claims_from_text(current_text)
        source_claims = claims_from_text(source_text)
        if (not current_claims or not source_claims or
            len(current_claims) > MAX_CLAIMS or len(source_claims) > MAX_CLAIMS or
            any(len(c) > MAX_CLAIM_CHARS for c in current_claims + source_claims)):
            return {"state": "review_required", "cards": []}
        vectors = embed(current_claims + source_claims)
        if len(vectors) != len(current_claims) + len(source_claims):
            raise ValueError("EMBEDDING_COUNT_MISMATCH")
        norm = [normalized_embedding(v) for v in vectors]
        sizes = {x.size for x in norm}
        if len(sizes) != 1:
            raise ValueError("EMBEDDING_DIMENSIONS_CHANGED")
        query_vectors = np.stack(norm[:len(current_claims)])
        source_vectors = np.stack(norm[len(current_claims):])
        comparisons = query_vectors @ source_vectors.T
        cards = []
        for position, claim in enumerate(current_claims):
            match_index = int(np.argmax(comparisons[position]))
            quote = source_claims[match_index]
            if quote not in source_text:
                raise ValueError("SOURCE_QUOTE_NOT_FOUND")
            cards.append({
                "claim": claim,
                "source_quote": quote,
                "cosine_similarity": round(float(comparisons[position, match_index]), 6),
                "candidate_match": bool(comparisons[position, match_index] >= 0.96),
                "source_document_key": row[3],
            })
        return {"state": "review_candidate_only", "document_key": key,
                "match_key": row[3], "cards": cards,
                "inference": "local_embedding_similarity_not_factual_entailment"}

    def counts(self) -> dict:
        return dict(self.db.execute(
            "SELECT decision, COUNT(*) FROM novelty_v1 WHERE model_id=? GROUP BY decision",
            (self.model_id,)
        ).fetchall())
