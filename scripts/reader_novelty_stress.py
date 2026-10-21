"""Deterministic 1,000-document multilingual Reader novelty integration stress.

All 1,000 bodies are SYNTHETIC (nine languages); no private Reader documents
or DeepSeek requests. A fake embedding backend intentionally treats all facts
as semantically identical to stress fail-closed guards, source lineage,
local SQLite indexing and cross-language review retrieval.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from unittest.mock import Mock, patch

import numpy as np

from scripts.reader_novelty import NoveltyIndex
from scripts.reader_novelty_scan import GuardedLocalEmbedder


LANGUAGES = (
    ("ko", "산업 시스템 신뢰성 분석", "연구진이 산업 설비의 전력과 신뢰성을 검토했다."),
    ("zh", "工业系统可靠性报告", "研究人员评估了工业系统的能源消耗和稳定性。"),
    ("ja", "産業設備の信頼性調査", "研究者たちは産業設備の電力と安定性を検証した。"),
    ("en", "Industrial reliability report", "Researchers studied the reliability of industrial systems."),
    ("es", "Informe de fiabilidad industrial", "Investigadores evaluaron la estabilidad de los equipos."),
    ("fr", "Rapport de fiabilité industrielle", "Des chercheurs ont examiné la stabilité des équipements."),
    ("ru", "Отчет о надежности оборудования", "Исследователи оценили надежность промышленного оборудования."),
    ("ar", "تقرير موثوقية النظام الصناعي", "درس الباحثون استقرار المعدات الصناعية واستهلاك الطاقة."),
    ("pt", "Relatório de confiabilidade industrial", "Pesquisadores analisaram a estabilidade dos equipamentos."),
)
FIRST = ("Researchers documented the energy consumption and thermal "
         "reliability of the new industrial computing system.")
REPHRASE = ("The new industrial computing system was documented by "
            "researchers for its energy consumption and thermal reliability.")


class AdversarialFakeEmbedder:
    """Deliberately overconfident: makes semantically distinct items collide."""

    def __init__(self):
        self.requests = 0
        self.texts = 0

    def __call__(self, texts):
        self.requests += 1
        self.texts += len(texts)
        vector = np.zeros(16, dtype=np.float32)
        vector[0] = 1.0
        return [vector.tolist() for _ in texts]


def _cache_stress(operations: int) -> dict:
    """Mock Ollama, not a real model. Test the production cache/GearShift path."""
    class Response:
        def __init__(self, n):
            self.n = n

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return json.dumps(
                {"embeddings": [[1.0] * 16 for _ in range(self.n)]}
            ).encode("utf-8")

    http_calls = []
    def local_urlopen(request, timeout):
        payload = json.loads(request.data)
        assert payload["truncate"] is False
        assert payload["keep_alive"] == 0
        http_calls.append(len(payload["input"]))
        return Response(len(payload["input"]))

    gear = Mock()
    with patch("scripts.reader_novelty_scan.GearShift", return_value=gear), \
         patch("scripts.reader_novelty_scan.budget_for",
               return_value={"required_increment_bytes": 1}), \
         patch("scripts.reader_novelty_scan.urllib.request.urlopen",
               side_effect=local_urlopen):
        emb = GuardedLocalEmbedder("synthetic-embedding-backend", "/ignored")
        for start in range(0, operations, 25):
            group = [
                f"Repeated multilingual article chunk {i % 10} "
                "about generic industrial research and reliability."
                for i in range(start, min(start + 25, operations))
            ]
            vectors = emb(group)
            assert len(vectors) == len(group)
            assert all(len(v) == 16 for v in vectors)
        stats = emb.total_stats
        assert stats["texts"] == operations
        assert stats["embedding_misses"] == 10
        assert stats["cache_hits"] == operations - 10
        assert sum(http_calls) == 10
        assert gear.check.call_count == len(http_calls) * 2
        assert emb._cache_bytes <= 32 * 1024 * 1024
        assert len(emb._chunk_cache) <= 4096
    return {
        "mock_ollama_requests": len(http_calls),
        "mock_unique_chunk_embeddings": sum(http_calls),
        "cache_hits": stats["cache_hits"],
        "requests_avoided_by_cache": operations - sum(http_calls),
        "actual_ollama_calls": 0,
    }


def run(documents: int = 1000) -> dict:
    if not 100 <= documents <= 10000 or documents % 20 != 0:
        raise ValueError("DOCUMENT_COUNT_MUST_BE_MULTIPLE_OF_20_IN_100_TO_10000")
    repeat = documents // 20
    counts = {
        "novel_origins": 8 * repeat,
        "exact_duplicates": 4 * repeat,
        "same_title_rephrasings": 3 * repeat,
        "changed_number_new_facts": 2 * repeat,
        "different_title_same_body": 2 * repeat,
        "incomplete_previews": repeat,
    }
    embed = AdversarialFakeEmbedder()
    con = sqlite3.connect(":memory:")
    idx = NoveltyIndex(con, model_id="synthetic-multilingual-1000")
    originals = []
    began = time.perf_counter()

    for n in range(counts["novel_origins"]):
        lang, prefix, native = LANGUAGES[n % len(LANGUAGES)]
        title = f"{prefix} {n:04d}"
        body = native + "\n" + FIRST
        result = idx.assess(f"source-{n:04d}", title, body, embed)
        assert result["decision"] == "novel", ("original", n, result)
        originals.append((title, body, native, lang))

    for n in range(counts["exact_duplicates"]):
        title, body, _, _ = originals[n]
        result = idx.assess(f"exact-{n:04d}", title, body, embed)
        assert result["decision"] == "exact_duplicate", (n, result)
        assert result["match_key"] == f"source-{n:04d}"

    for n in range(counts["same_title_rephrasings"]):
        title, _, native, _ = originals[n]
        result = idx.assess(
            f"paraphrase-{n:04d}", title, native + "\n" + REPHRASE, embed)
        assert result["decision"] == "redundancy_candidate", (n, result)
        assert result["match_key"] == f"source-{n:04d}"

    for n in range(counts["changed_number_new_facts"]):
        title, body, _, _ = originals[n]
        result = idx.assess(
            f"new-fact-{n:04d}", title,
            body + f"\nAdditional confirmed output is {70001 + n} watts.", embed)
        assert result["decision"] == "novel", (n, result)
        assert result["match_key"] is None

    for n in range(counts["different_title_same_body"]):
        _, body, _, _ = originals[n]
        result = idx.assess(
            f"other-title-{n:04d}",
            f"Different corporate event {n:04d}: fresh evidence",
            body, embed)
        assert result["decision"] == "novel", (n, result)
        assert result["match_key"] is None

    for n in range(counts["incomplete_previews"]):
        result = idx.assess(
            f"held-{n:04d}", f"Paywall headline {n:04d}",
            "Some content could not be imported from the original document.",
            embed)
        assert result["decision"] == "review_required", (n, result)
        assert result["match_key"] is None

    observed = idx.counts()
    expected = {
        "novel": sum(counts[k] for k in (
            "novel_origins", "changed_number_new_facts",
            "different_title_same_body")),
        "exact_duplicate": counts["exact_duplicates"],
        "redundancy_candidate": counts["same_title_rephrasings"],
        "review_required": counts["incomplete_previews"],
    }
    assert observed == expected, {"observed": observed, "expected": expected}
    assert sum(observed.values()) == documents
    first_assessment_s = time.perf_counter() - began
    first_embedding_requests = embed.requests

    began_rerun = time.perf_counter()
    for n, (title, body, _, _) in enumerate(originals):
        assert idx.assess(f"source-{n:04d}", title, body, embed)["decision"] == "novel"
    for n in range(counts["exact_duplicates"]):
        title, body, _, _ = originals[n]
        assert idx.assess(f"exact-{n:04d}", title, body, embed)["decision"] == "exact_duplicate"
    for n in range(counts["same_title_rephrasings"]):
        title, _, native, _ = originals[n]
        assert idx.assess(f"paraphrase-{n:04d}", title,
                          native + "\n" + REPHRASE, embed)["decision"] == "redundancy_candidate"
    assert embed.requests == first_embedding_requests, "SECOND_PASS_CAUSED_REEMBEDDING"
    assert idx.counts() == expected
    recheck_s = time.perf_counter() - began_rerun

    advisory = idx.cross_language_review("source-0000", limit=5)
    assert advisory["state"] == "review_only"
    assert advisory["automatic_api_suppression"] is False
    assert advisory["semantic_equivalence_proven"] is False
    assert advisory["retrieval_is_approximate"] is True
    assert len(advisory["candidates"]) == 5

    # Rebuild the new review index from 100% pre-existing cached vectors.
    con.execute("DELETE FROM novelty_review_bands")
    con.commit()
    calls_before_backfill = embed.requests
    backfill = idx.review.backfill(max_rows=documents)
    assert backfill["review_buckets_backfilled"] == expected["novel"]
    assert backfill["new_ollama_embeddings"] == 0
    assert embed.requests == calls_before_backfill
    assert idx.review.backfill(max_rows=documents)["review_buckets_backfilled"] == 0

    title, body, _, _ = originals[0]
    assert idx.is_current("exact-0000") is True
    new_source = idx.assess(
        "source-0000", title,
        body + "\nA later test uncovered a new observation of 987654 watts.",
        embed)
    assert new_source["decision"] == "novel"
    assert idx.is_current("exact-0000") is False
    assert idx.is_current("paraphrase-0000") is False

    # A failed inference must not persist a partial or unsafe source.
    try:
        idx.assess("failed-test-unique", "Unrelated technical safety update",
                   FIRST, lambda texts: [[0.0] * 16 for _ in texts])
    except ValueError as exc:
        assert "ZERO_EMBEDDING" in str(exc)
    else:
        raise AssertionError("FAILED_EMBEDDING_NOT_REJECTED")
    assert con.execute(
        "SELECT 1 FROM novelty_v1 WHERE document_key='failed-test-unique'"
    ).fetchone() is None

    page_count = con.execute("PRAGMA page_count").fetchone()[0]
    page_size = con.execute("PRAGMA page_size").fetchone()[0]
    con.close()
    cache = _cache_stress(documents)
    return {
        "synthetic_documents_ingested": documents,
        "language_title_and_body_fixtures": len(LANGUAGES),
        "source_decision_distribution": observed,
        "verified_same_pass_expectations": expected == observed,
        "first_pass_seconds": round(first_assessment_s, 3),
        "idempotent_second_pass_seconds": round(recheck_s, 3),
        "second_pass_additional_fake_embedding_requests": 0,
        "approximate_review_neighbor_sample": len(advisory["candidates"]),
        "source_mutation_invalidated_dependent_decisions": True,
        "failed_embedding_atomicity_verified": True,
        "review_buckets_backfilled_without_inference": backfill["review_buckets_backfilled"],
        "synthetic_sqlite_page_bytes": page_count * page_size,
        "mocked_local_cache": cache,
        "deepseek_api_calls": 0,
        "reader_writes": 0,
        "automatic_translation_skip_enabled": False,
        "real_ollama_model_evaluated": False,
        "real_reader_full_contents_evaluated": False,
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--documents", type=int, default=1000)
    args = p.parse_args()
    print(json.dumps(run(args.documents), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
