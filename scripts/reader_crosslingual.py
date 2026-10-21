"""Bounded, local embedding-neighbor search for *human review only*.

LSH is a recall-imperfect retrieval hint, NEVER a source of factual entailment,
redundant-translation approval, deletion or DeepSeek API suppression. Original
Reader data remains in the existing private SQLite database.
"""
from __future__ import annotations

from functools import lru_cache
import sqlite3

import numpy as np

from scripts.reader_multilingual import script_families

BANDS = 4
BITS_PER_BAND = 8
REVIEW_LSH_REVISION = "simhash-4x8-v1"


@lru_cache(maxsize=12)
def _hyperplanes(dimensions: int) -> np.ndarray:
    if not 8 <= dimensions <= 8192:
        raise ValueError("INVALID_EMBEDDING_DIMENSIONS")
    return np.random.default_rng(0xA81).standard_normal(
        (BANDS * BITS_PER_BAND, dimensions), dtype=np.float32
    )


def band_keys(vector: np.ndarray) -> tuple[tuple[int, int], ...]:
    """Deterministic 32-bit random-hyperplane signature, split in four bands."""
    v = np.asarray(vector, dtype=np.float32)
    if v.ndim != 1 or not np.isfinite(v).all():
        raise ValueError("INVALID_REVIEW_VECTOR")
    projection = _hyperplanes(v.size) @ v
    return tuple((band, sum(
        (int(projection[band * BITS_PER_BAND + bit] >= 0.0) << bit)
        for bit in range(BITS_PER_BAND)))
        for band in range(BANDS))


class CrossLanguageReviewIndex:
    """A strictly advisory, dimension-checked embedding candidate retrieval."""

    def __init__(self, db: sqlite3.Connection, *, model_id: str, policy_revision: str):
        self.db, self.model_id, self.policy_revision = db, model_id, policy_revision
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS novelty_review_bands (
                document_key TEXT NOT NULL,
                model_id TEXT NOT NULL,
                policy_revision TEXT NOT NULL,
                band INTEGER NOT NULL,
                bucket INTEGER NOT NULL,
                PRIMARY KEY (document_key,model_id,policy_revision,band)
            );
            CREATE INDEX IF NOT EXISTS novelty_review_lookup
                ON novelty_review_bands(model_id,policy_revision,band,bucket);
        """)

    def replace(self, key: str, vector: np.ndarray | None):
        """Call in the SAME transaction as the author's novelty_v1 update."""
        self.db.execute(
            "DELETE FROM novelty_review_bands WHERE document_key=? AND model_id=?",
            (key, self.model_id)
        )
        if vector is not None:
            self.db.executemany(
                """INSERT INTO novelty_review_bands
                   (document_key,model_id,policy_revision,band,bucket)
                   VALUES(?,?,?,?,?)""",
                [(key,self.model_id,self.policy_revision,band,bucket)
                 for band,bucket in band_keys(vector)]
            )

    def neighbors(self, key: str, *, limit: int = 5,
                  min_cosine: float = 0.80,
                  max_candidates: int = 1024) -> dict:
        """Retrieval with **no** new inference and no paid/Reader writes.

        Single-band match is intentionally approximate and may miss matches.
        Every candidate is re-ranked with true cosine, not bucket proximity.
        """
        if not (1 <= limit <= 25 and 0.0 <= min_cosine <= 1.0
                and 16 <= max_candidates <= 10000):
            raise ValueError("BOUNDED_REVIEW_SEARCH_REQUIRED")
        source = self.db.execute(
            """SELECT vector,dimensions,title,decision,policy_revision
               FROM novelty_v1 WHERE document_key=? AND model_id=?""",
            (key,self.model_id)).fetchone()
        if (not source or source[3] != "novel"
                or source[4] != self.policy_revision or source[0] is None):
            return {"state":"not_indexed", "candidates": [],
                    "automatic_api_suppression":False}
        target=np.frombuffer(source[0], dtype="<f4")
        if target.size != source[1] or not np.isfinite(target).all():
            return {"state":"invalid_local_vector", "candidates": [],
                    "automatic_api_suppression":False}
        buckets=band_keys(target)
        ids=set()
        truncated=False
        # Indexed probes; no whole-corpus vector scan. Max bound prevents
        # pathological giant topical clusters from exhausting Mac-mini RAM.
        for band,bucket in buckets:
            if truncated:
                break
            found=self.db.execute(
                """SELECT document_key FROM novelty_review_bands
                   WHERE model_id=? AND policy_revision=?
                     AND band=? AND bucket=? AND document_key<>?
                   LIMIT ?""",
                (self.model_id,self.policy_revision,band,bucket,key,
                 max_candidates+1)).fetchall()
            for (candidate_key,) in found:
                ids.add(candidate_key)
                if len(ids)>max_candidates:
                    truncated=True
                    break
        if truncated:
            ids=set(sorted(ids)[:max_candidates])
        ranked=[]
        for candidate_key in sorted(ids):
            row=self.db.execute(
                """SELECT vector,dimensions,title,decision,policy_revision
                   FROM novelty_v1 WHERE document_key=? AND model_id=?""",
                (candidate_key,self.model_id)).fetchone()
            if (not row or row[3] != "novel"
                    or row[4] != self.policy_revision or row[0] is None
                    or row[1] != target.size):
                continue
            vec=np.frombuffer(row[0], dtype="<f4")
            cosine=float(np.dot(vec,target))
            if np.isfinite(cosine) and cosine>=min_cosine:
                ranked.append({
                    "document_key":candidate_key,
                    "title":row[2],
                    "cosine_similarity":round(cosine,6),
                    "script_hints":sorted(script_families(row[2])),
                })
        ranked.sort(key=lambda x:(-x["cosine_similarity"],x["document_key"]))
        return {
            "state":"review_only",
            "source_script_hints":sorted(script_families(source[2])),
            "candidates":ranked[:limit],
            "reviewed_vector_candidates":len(ids),
            "candidate_pool_truncated":truncated,
            "retrieval_is_approximate":True,
            "semantic_equivalence_proven":False,
            "automatic_api_suppression":False,
        }
