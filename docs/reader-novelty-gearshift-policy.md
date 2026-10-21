# Reader Semantic Novelty Gate + GearShift utilization correction

## Scope and user goal

The user authorizes DeepSeek API for large-scale faithful translation,
rating and entity analysis. The target is **paid tokens per useful, validated
piece of new information**, not a cap on request count. Suppressing articles
merely because they discuss the same company/topic is unsafe.

The candidate stage implemented here is independent of the previous exact
hash + character MinHash duplicate checker. It uses **locally generated
multilingual text embeddings** and then compares substantive sentence-level
evidence. All originals, labels, and past translations survive.

### What the new code actually does

- `scripts/reader_novelty.py`: persistent SQLite index, model-versioned
  embeddings, exact-body deduplication, top-5 semantic candidates, sentence
  coverage, and safeguards for different numbers, quotes or negations.
- `scripts/reader_novelty_scan.py`: read-only scanner for the private
  `reader_drive_stage.py` downloaded source collection, using one
  **previously installed** local Ollama embedding model.
- Local content/embeddings remain in a private 0700 directory, DB 0600;
  requests go to `127.0.0.1:11434` only.
- It asks the **existing GearShift** to admit each local embedding operation;
  a denied or failed local inference is held, not counted as a novelty result.
- Candidate statuses: `exact_duplicate`, `redundancy_candidate`,
  `novel`, `review_required`. Similar **topic** alone cannot produce
  `redundancy_candidate`; the current article's meaningful sentences must
  be covered at high semantic similarity, with no changed quantitative or
  quoted evidence. All confidence thresholds are provisional and uncalibrated.
- Outputs are **advisory only**. No automatic DeepSeek skip, translation
  substitution, Reader tag change, Drive write, source deletion or production
  promotion. Human-vetted false-negative studies and API-result linkage are
  prerequisites to making skip decisions authoritative.

Run only after private Drive staging and after installing a compatible
multilingual Ollama embedding model through user-authorized steps:

```sh
python3 -m scripts.reader_novelty_scan \
  --work "$HOME/.abraham-reader-stage" \
  --model EXISTING_MULTILINGUAL_EMBEDDING_MODEL \
  --amando-root /Users/yoon/AMANDO \
  --limit 100
```

Repeat the bounded run to resume. Held items remain separated and are not
silently retried forever. This is a real local model invocation only **when
the operator executes it**; CI uses fake embeddings and zero real inference.

Performance note: the v1 index searches previous SQLite embeddings with a
bounded streaming O(N) candidate scan. For the measured 12,804-document
collection, benchmark throughput and adopt a local HNSW/vector index only
if required. There is no claimed real speedup yet. The next phase should
select the highest-evidence representative per event, preserve contrasting
accounts, link to existing *completed* translations, and only then safely
reuse canonical Korean translations without a new paid API call.

## GearShift: user-approved policy is NOT max-estimate rejection

Verified owner-approved decision:
[AMANDO GearShift Manual Control Policy v1.1 (2026-10-19)](https://docs.google.com/document/d/1A2fsum0wgdBgXejPbbyuCqrMEnz2SCSNG8F4amJQytc/edit).

It **supersedes** the 13 GiB Devstral fixed start gate and any memory
prediction-only denial. The approved **23.5 GiB** figure is a **utilization
target** (25,232,932,864 bytes), *not* an OS physical cap or predicted
model-specific admission rule. Older "projected RAM >23.5 GB -> reject"
reports are superseded for this purpose.

### Implementation status and what this PR changes

- `scripts/reader_local.py` now calculates the Ollama `num_ctx`, MLX
  model budget and KV estimate from **actual prompt byte length + bounded
  response**, rather than unconditionally reserving 4096 context tokens.
  The requested upper bound and actual tokenizer capacity checks remain.
  This can reduce speculative allocation for short Reader requests.
- **Central AMANDO GearShift policy itself has NOT been changed.** The
  actual Mac mini `/Users/yoon/AMANDO/gearshift` and
  `command_bridge/local_resources.py` files were not accessible through
  the linked GitHub repositories. Never claim this PR removed GearShift
  predicted-memory rejection from the Mac.
- The existing GearShift admission callback is kept intact: we do not
  bypass model leases, conflicting external owners, duplicate execution
  locks, device role checks, or user music activity.

### Exact central AMANDO change packet (for the Mac checkout)

1. Inspect the actual current `gearshift/`, `command_bridge/`,
   `amando/workflow/gearshift_broker.py`, and tests; compare with
   current owner-approved manual-control policy v1.1.
2. Separate `estimate_model_bytes` (**advisory**) from
   `admission_reject_reasons` (**actual independent conflict only**).
   Estimated model footprint, theoretical KV at maximum context, and
   `measured_used + estimated_model + loading_peak > target` may emit
   **warnings and telemetry**, not standalone rejections.
3. Measure actual physical + compressed active memory, pressure, swap
   delta, Ollama `/api/ps` resident footprint, and other active leases.
   Avoid counting a resident Ollama model twice as both OS used memory
   and an estimated model lease; distinguish booked intent from resident
   footprint.
4. Keep explicit ownership, duplicate-execution, audio-priority and
   overlapping model admission protections. Never kill Ableton, Neural DSP,
   other user apps or unowned Ollama sessions to make space.
5. On active critical pressure, use the policy's independent real-state
   safeguards, warnings/controlled stop, not speculative forecasts. Record
   actual failure/swap escalation and any safe, cooperative cancellation.
6. Regression: a normal-pressure short request with a pessimistic 24GB
   predicted model must not be rejected **solely** on the prediction;
   true active lease conflict and duplicate task must still be rejected.
   A requested unattended OMEGA profile must keep its explicit owner
   authorization requirement.
7. Do not change system LaunchAgents, task schedules, workflow production
   state, model installations, authentication, or live resident models
   during policy-only tests. There is NO Argon approval dependency in
   Abraham development; Gemini desktop Argon task remains independent.

The historical memory report
`AMANDO-GEARSHIFT-MEMORY-23500-REPORT.md` describes a different,
older projected-denial implementation and says its production merge was
deferred. This report is not evidence that the approved v1.1 policy is
running. Some prior policy-tests-first artifacts contained conflicting
failures, so do not report "tests passed" without fresh tests.

### Acceptance criteria

- Safety: no unapproved model owner takeover, no forced audio shutdown,
  no duplicate tasks, no ignored critical memory state.
- Utilization: more legitimate requests admitted when actual memory pressure
  permits; no arbitrary fixed 13 GiB startup stop or 4096-token reservation
  for a short prompt.
- Quality: no new factual evidence skipped as "same topic" without
  documented positive agreement; quote and numeric disagreements must trigger
  novel/review.
- Economy: measure **paid tokens per validated new document** and
  high-risk false-skip rate, not mere invocation count.
