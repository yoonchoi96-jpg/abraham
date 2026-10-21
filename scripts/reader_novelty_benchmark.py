"""Reproducible 100% synthetic SQLite title-gate benchmark; no API calls.

This benchmarks candidate lookup, NOT Ollama inference or end-to-end translation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import time

import numpy as np

from scripts.reader_novelty import NoveltyIndex, POLICY_REVISION, normalized_title_key


def run(docs: int = 10000, trials: int = 5) -> dict:
    if not 100 <= docs <= 100000 or not 1 <= trials <= 100:
        raise ValueError("BOUNDED_BENCHMARK_REQUIRED")
    db = sqlite3.connect(":memory:")
    index = NoveltyIndex(db, model_id="offline-test-model")
    vector = np.zeros(16, dtype=np.float32)
    vector[0] = 1.0
    blob = vector.astype("<f4").tobytes()
    title = "Target headline for indexed retrieval"
    rows = []
    for n in range(docs):
        headline = title if n == docs // 2 else f"Unrelated headline {n}"
        rows.append((
            f"synthetic-{n}", "offline-test-model",
            hashlib.sha256(str(n).encode()).hexdigest(),
            headline, "This is a locally generated benchmark-only statement.",
            "[]", blob, 16, "novel", None, None, None,
            "NO_EQUIVALENT_EVIDENCE", POLICY_REVISION, None,
            normalized_title_key(headline),
        ))
    db.executemany("""INSERT INTO novelty_v1
        (document_key,model_id,body_sha256,title,body_excerpt,markers,
         vector,dimensions,decision,match_key,similarity,coverage,reason,
         policy_revision,match_body_sha256,title_key)
         VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", rows)
    db.commit()

    def full_scan():
        found = []
        for key, raw, dimensions, headline in db.execute(
                """SELECT document_key,vector,dimensions,title FROM novelty_v1
                WHERE model_id=? AND policy_revision=? AND decision='novel'
                AND vector IS NOT NULL AND document_key<>?""",
                ("offline-test-model", POLICY_REVISION, "none")):
            if normalized_title_key(headline) != normalized_title_key(title):
                continue
            if dimensions == vector.size and float(np.dot(
                    np.frombuffer(raw, dtype="<f4"), vector)) >= 0.84:
                found.append(key)
        return found

    def indexed():
        return [row[1] for row in index._top_matches(
            vector, exclude="none", title=title)]

    assert full_scan() == indexed() == [f"synthetic-{docs // 2}"]
    samples = []
    for _ in range(trials):
        t0 = time.perf_counter()
        full_scan()
        t1 = time.perf_counter()
        indexed()
        t2 = time.perf_counter()
        samples.append((t1 - t0, t2 - t1))
    median = lambda seq: float(np.median(seq))
    slow_ms = 1000 * median([x[0] for x in samples])
    fast_ms = 1000 * median([x[1] for x in samples])
    plan = db.execute("""EXPLAIN QUERY PLAN
        SELECT document_key,vector FROM novelty_v1
        WHERE model_id=? AND policy_revision=? AND decision='novel'
          AND title_key=? AND vector IS NOT NULL AND document_key<>?""",
        ("offline-test-model", POLICY_REVISION,
         normalized_title_key(title), "none")).fetchall()
    indexed_ok = any("novelty_v1_titlekey" in row[3] for row in plan)
    db.close()
    if not indexed_ok:
        raise AssertionError("SQLITE_TITLE_INDEX_NOT_USED")
    return {
        "synthetic_documents": docs,
        "trials": trials,
        "baseline_fullscan_median_ms": round(slow_ms, 3),
        "indexed_title_gate_median_ms": round(fast_ms, 3),
        "measured_lookup_speedup": round(slow_ms / max(fast_ms, 1e-9), 2),
        "sqlite_title_index_verified": indexed_ok,
        "deepseek_calls": 0,
        "real_corpus_model_accuracy_measured": False,
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--docs", type=int, default=10000)
    p.add_argument("--trials", type=int, default=5)
    args = p.parse_args()
    print(json.dumps(run(args.docs, args.trials), sort_keys=True))


if __name__ == "__main__":
    main()
