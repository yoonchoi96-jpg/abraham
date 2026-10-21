# Abraham v9: multilingual candidate discovery and bounded local embedding cache

Date: 2026-10-21

## Two independent lanes

**Lane A — existing safe dedup:** Whole-body SHA and exact normalized headline
are sufficient only for *identical complete extracted bodies*. Same headline
but different bodies may become `redundancy_candidate` only after the existing
numbers, negation, attribution, short-claim and local-claim coverage gates. All
candidates are advisory; never authorize paid DeepSeek suppression or deletion.

**Lane B — cross-language discovery:** Deterministic 32-bit random-hyperplane
LSH (4 × 8-bit bands) stored in the private existing `reader-novelty.sqlite`.
For each **novel** local embedding, four indexed buckets are stored atomically
with the novelty record. New `NoveltyIndex.cross_language_review(key, limit=5)`
returns near neighbors with actual cosine similarity, title, Unicode script
hints, explicit `semantic_equivalence_proven=False`, `retrieval_is_approximate
=True`, and `automatic_api_suppression=False`. Spanish, French, English,
Portuguese, Chinese, Japanese, Korean, Russian and Arabic headlines can be
shown in the **same candidate list** when their embeddings are aligned.

LSH introduces **false negatives** and may surface **false positives**. It
never adjudicates factual equivalence, nor does it permit article skipping.
Candidate pools are bounded to 1,024 records and truncation is reported. Since
this uses the same local embedding model, the cross-lingual quality depends on
that model actually supporting aligned multilingual embeddings. No download,
external API or GPU-specific DeepInfer code is required. Existing v8 novelty assessments remain valid because v9 changes only
advisory retrieval and caching, not safe-skip eligibility. The scanner
backfills missing LSH buckets directly from previously stored current-policy
vectors (default at most 5,000 each run). It makes **zero new Ollama calls**
for backfilled rows. `--review-backfill-limit 25000` provides a larger explicit
one-shot bound. This avoids paid inference or repeated local embeddings on
upgrades.

## Local embeddings: lazy deduplication and RAM-capped cache

`GuardedLocalEmbedder` now hashes each exact UTF-8 chunk with SHA-256, batches
only *unique cache misses*, and reassembles chunk embeddings in original
document order. The 32 MiB / 4,096-entry in-process LRU holds only numeric
embedding vectors; no article text or vectors are sent to a paid API. Repeated
claims/paragraphs avoid redundant local Ollama requests. On restarts the cache
is intentionally empty to avoid stale-model matches across model upgrades.
`ABRAHAM_EMBED_CACHE=0` disables inter-call reuse. Tracking exposes
`cache_hits` and `embedding_misses`; it preserves no truncation and GearShift
memory-admission checks for every actual local request.

## Reproducible validation

`python3 -m pytest -q tests/test_reader_*.py tests/test_color_rules.py`
`python3 -m scripts.reader_crosslingual_benchmark --docs 10000 --trials 5`
`python3 -m scripts.reader_novelty_benchmark --docs 10000 --trials 7`

CI tests model/row isolation, source mutation, candidate safety, cache hits
and opt-out, and compares bounded LSH lookups to a full-scan on 10,000
synthetic vectors. Benchmark reports **retrieval latency only**, not end-to-end
Ollama cost or real multilingual recall. Real Reader/translation quality still
requires an independently annotated diverse-language gold corpus and an actual
local Ollama model; no measured real-data precision/recall claims.

No private articles, Reader identifiers, or secrets committed. No DeepSeek
API usage, tagging, Seen state, Drive mutations, or auto article deletion.
