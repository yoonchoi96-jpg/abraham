# DeepInfer source audit → Abraham evidence-safe embedding

Status: **experimental; not in production; no automatic paid API suppression**.
Source snapshot inspected 2026-10-21:
[DeepInfer 9bd9cf243fc53ef9afbf8f04d391d989117d6ab9](https://github.com/deepseek-ai/DeepInfer/tree/9bd9cf243fc53ef9afbf8f04d391d989117d6ab9).
License: Apache-2.0 (no original DeepInfer code copied into Abraham).
This is a Python/Ollama application-level adaptation, not an Apple Silicon port of
DeepInfer's CUDA/CANN runtime.

## What upstream actually implements

| Upstream file/function | Observed mechanics | Appropriate Abraham analogue |
| --- | --- | --- |
| `csrc/engine/scheduler.hpp`, `_schedule`, `_schedule_prefill` | Waiting/running queues, prefill/decode decisions, max batch tokens, available KV blocks, optional chunked prefill, indivisible multimodal boundaries | FIFO/local batch byte budgets, whole-document chunk admission, preserving original boundaries |
| `deepinfer/engine/llm_engine.py`, `LLMEngine.__init__` | Loads workers/model, profiles real device usage, initializes cache and block managers, then creates scheduler | Gather measured model/backend demand before changing GearShift admission. **Not implemented in this branch** |
| `deepinfer/worker/cache_engine.py` | Device KV/state layout, bounded replay, GPU-specific token and block accounting | No direct re-use on DeepSeek's hosted API; Abraham already uses independent SQLite translation caching and provider usage accounting |
| `deepinfer/engine/profile_server.py`, `ProfileManager` | Optional runtime profiler with bounded sampling, concurrent trace rejection, deferred disk export | Counter-only local embedding request metrics; no heavy trace profiler installed |
| `README.md` | Offline serving for V4.1-Flash on NVIDIA SM100/Huawei Ascend 950; DSpark/MTP confidence-scheduled verification, model-specific optimizations; external components needed for full serving service | **Cannot** enable these mechanisms in hosted DeepSeek API from Abraham, nor assume they run on 24 GiB Apple Silicon |

Links:
- [Upstream scheduler](https://github.com/deepseek-ai/DeepInfer/blob/9bd9cf243fc53ef9afbf8f04d391d989117d6ab9/csrc/engine/scheduler.hpp)
- [Upstream engine](https://github.com/deepseek-ai/DeepInfer/blob/9bd9cf243fc53ef9afbf8f04d391d989117d6ab9/deepinfer/engine/llm_engine.py)
- [Upstream cache implementation](https://github.com/deepseek-ai/DeepInfer/blob/9bd9cf243fc53ef9afbf8f04d391d989117d6ab9/deepinfer/worker/cache_engine.py)
- [Upstream runtime profiler](https://github.com/deepseek-ai/DeepInfer/blob/9bd9cf243fc53ef9afbf8f04d391d989117d6ab9/deepinfer/engine/profile_server.py)

## Actual Abraham changes

1. `scripts/reader_novelty_scan.py`: replaces the old *first 3,700 characters only* embedding
   with UTF-8-safe chunks (<=3,000 input bytes each), submitted in batches
   with <=12,000 input bytes and <=65 input records. At most 128 chunks per
   `GuardedLocalEmbedder` call. Above that budget: hold; NEVER silently truncate.
   Each sub-request is checked by the existing GearShift integration. Pool all
   returned chunk vectors by original byte length, normalize once per original
   article/claim. Retain per-call and aggregate `texts/chunks/local_requests/input_bytes/truncated_bytes`
   metrics. No new paid API, no model downloads, no real queue parallelism.
2. `scripts/reader_novelty.py`: previously, a stable document ID returned its
   old decision even when the title or body changed. Cache identity now includes
   SHA-256 of assessed body and title. On revision, exclude self from exact-body
   matching; commit a replacement *after* successful assessment, leaving the
   previous record intact if inference fails.
3. `scripts/reader_novelty_scan.py`: adds `novelty_source_v1`, a private
   versioned (model, document, source SHA-256, filename) index; unchanged source
   files are skipped efficiently, changed source files are reassessed even if
   they have the same Drive ID. Existing databases gain this table lazily.
4. `ABRAHAM_NOVELTY_CHUNKED_EMBEDDINGS=0` safely disables multi-chunk
   embedding for rollback: long inputs are *held*, never embedded as
   misleading partial prefixes. The whole novelty scanner is optional
   and not called by the production Core workflow.

## Data correctness boundaries

- **No claim that same-topic articles are equivalent**: embedding similarity
  only nominates a match, and a claim-level pass checks sentence coverage,
  numbers, quotes and negations. Candidate thresholds remain uncalibrated.
- `redundancy_candidate` is NOT a translated-content cache hit, source
  deletion authorization, Reader Seen/tag action, or permission to skip DeepSeek.
- A changed source can introduce new evidence even if its ID stays constant.
- Byte-weighted pooling can lose details; it is retrieval only. Full evidence
  comparison and independent human-labeled false-skip tests are still needed.
- Existing AMANDO/GearShift repository and live Mac mini processes are not
  accessible through the connected GitHub account. This branch does not
  modify the central GearShift daemon or remove active lease/memory protection.

## Verification and rollout

Synthetic source-regression tests have been added to
`tests/test_reader_novelty.py` for Unicode chunk preservation, byte budgets,
source revisions, failure rollback, safe disable and zero automatic API suppression.
A separate *independent* 7-case smoke check of these invariants passed on 2026-10-21.
**That check did not run the checked-out GitHub code.** Full repository pytest,
live Ollama checks, 24 GiB Mac resource tests, false-skip measurements and
cost/quality before-after benchmark **have not been executed** as part of this PR.
Do not present synthetic tests as operational verification.

Production gate:
- Obtain and review at least 50 labeled article-pair cases, including late
  evidence and contradictory numbers/attribution; measure high-risk false-skip.
- Run repository pytest and local Mac model tests with real model ID and
  GearShift telemetry. Record total source bytes, local Ollama requests,
  actual DeepSeek prompt cache hit/miss tokens and completion usage.
- Only after validated source-linked translated documents exist can an
  independent feature flag authorize translation reuse (NOT in this PR).
- Keep no-new-cost and no-auto-GitHub-run policies; no workflow schedules changed.
