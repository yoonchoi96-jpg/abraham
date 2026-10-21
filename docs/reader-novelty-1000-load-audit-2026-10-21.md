# Abraham v9: 1,000-document load test (2026-10-21)

## Independent live Reader metadata sample (read-only)
Through connected Readwise Reader, ten consecutive feed pages of 100 RSS
records were inspected **without modifying any Reader document**.

- Actual Reader RSS metadata read: **1,000 items**.
- Normalized unique titles: **929**.
- Same-title clusters: **70**, accounting for **71 extra title occurrences**.
- Reader metadata with word_count <= 15: **276**; this is **not** evidence
  that those articles had insufficient bodies. Earlier full-body spot checks
  found Reader metadata word_count can be inconsistent with real content.
- Full 1,000 bodies were not downloaded or embedded. This is NOT a
  1,000-article end-to-end Ollama/DeepSeek test, nor a measured model
  precision/recall experiment. The 1,000 private IDs, titles, URLs and bodies
  are intentionally absent from this public GitHub file.

## CI 1,000-document synthetic data integration stress

Command:
\`python3 -m scripts.reader_novelty_stress --documents 1000\`

9-script/language fixtures (Chinese, Japanese, Korean, English, Spanish,
French, Russian, Arabic, Portuguese) with adversarial overconfident
16-dimensional fake embeddings. This ensures similar embeddings CANNOT
override changed facts, altered titles or incomplete documents.

1000-case ground truth built **synthetically and deterministically**:
- 400 unique original full-body publications.
- 200 exact same-headline and byte-identical duplicate copies.
- 150 rephrasings with same headlines, which may be flagged
  \`redundancy_candidate\` **but never authorize paid API suppression**.
- 100 same-title articles with changed numeric facts (must stay novel).
- 100 differently titled but identical-body articles (must stay novel).
- 50 explicitly incomplete imported previews (must be held for review).

Additional independent invariants:
- Exact count of all six outcome classes equals 1,000.
- 750-case second-pass cache/idempotence check (400 original, 200 exact,
  150 same-title paraphrases) adds **zero fake embedding calls**.
- Source mutation invalidates existing dependent exact/semantic advisories.
- Invalid/zero embedding fails atomically without persisting a source.
- 1,000 repeated requests through the *production* local embedding wrapper
  use mock Ollama and GearShift: just 10 unique chunks reach the mocked HTTP
  endpoint, 990 duplicate calculations are avoided; no real network calls.
- LSH returns 5 cross-language, different-title *review only* nearest
  neighbors; never authorizes suppression or source deletion.
- Drop and reconstruct advisory LSH buckets from stored vectors, without
  additional inference. Confirm second backfill is idempotent.
- Report first-pass/second-pass wall time and in-memory SQLite allocation.
- CI also runs legacy 10,000-document title-index benchmark, 10,000-vector
  advisory LSH benchmark and >3GiB synthetic ZIP end-to-end source guards.

All content used for the CI stress is generated synthetic data. No user
article text or IDs, DeepSeek API key, translation requests or paid external
AI calls are sent to GitHub. Zero Reader Seen changes, tag writes, deletions,
or production suppression. A **real** 1,000-body stress on user's Mac mini
still requires the staged sources and the actual installed Ollama model.
