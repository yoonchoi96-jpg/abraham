# Abraham DeepSeek API efficiency layer — PR prototype

**Goal:** retain complete, faithful Korean translation and justified downstream
AI results while reducing **paid tokens per validated document**. This is not a
Mac port of DeepInfer, and DeepInfer cannot speed up DeepSeek's remote GPU.

## Existing optimizations that were preserved

- `run_v560_storage.py`: persisted SQLite per-document Korean translation cache,
  stage resume, salvage of valid document JSON, recursive isolation of failures
  and adaptive character-based batching (`adaptive_batch.py`).
- `build_korean_prompt()`: stable common rules at the start of requests, document
  content only at the end. This layout is appropriate for DeepSeek's automatic
  prefix-based context cache. Do not reorder the immutable rules randomly for
  each batch or change prompt versions without need.
- The same `deepseek-flash` API model, JSON output, thinking setting, and
  authoritative 9-color Reader processing gates remain unchanged.
- **API use is permitted** for large translation and useful analysis. There
  is NO hard-coded per-day/per-hour agent or DeepSeek call count quota.

## Added code

1. `abraham_api_usage.py` extracts **provider-reported** `prompt_cache_hit_tokens`,
   `prompt_cache_miss_tokens`, output tokens, nested reasoning tokens, and
   response finish status. If the provider returns no cache fields, the values
   remain unknown: NEVER treat unobserved cache hits as zero.
2. `deepseek_adapter.py` logs only token counts, model, UTC timestamp,
   response model, termination status and known task type. No prompt, document
   body, key, request URL or model response content is logged. Log appends
   are locked and file permission is 0600.
3. Call sites now attribute provider usage to `korean_translation`, `rating`,
   `entity_classification`, `entity_relation`, and
   `entity_canonicalization`. If a different caller uses the adapter, the
   task is labeled `unattributed`.
4. **Single-flight** for only **simultaneous byte-identical calls**, matched
   by hash of provider account, endpoint, task, request model and generation
   parameters. Concurrent identical callers receive the same response.
   It does not store paid responses or replay them in later sessions, and
   distinct requests, model settings, tasks, and accounts never coalesce.
   This can save duplicate requests only if the workload contains them.
   Set `DEEPSEEK_SINGLEFLIGHT=0` to turn this off.
5. `scripts/deepseek_usage_report.py` reads provider usage metadata and
   reports task/model token totals, cache hit rates, and number of
   length-truncated responses; it never calls the API.

### Examples: read-only report

```sh
python3 -m scripts.deepseek_usage_report \\
  --log 90_Dashboard/auto_reports/ai_usage.jsonl
```

For a price **scenario**, supply per-million-token prices for exactly ONE
model; this is not a provider invoice:

```sh
python3 -m scripts.deepseek_usage_report \\
  --log 90_Dashboard/auto_reports/ai_usage.jsonl \\
  --model deepseek-flash \\
  --cache-hit-rate 0.003 --cache-miss-rate 0.15 --output-rate 0.60
```

The sample values above correspond to the published OFF-PEAK USD per million
token `deepseek-flash` prices checked 2026-10-21, NOT guaranteed future prices
or peak-hour prices. The peak-hour rates, other models, and changing provider
prices require separate scenarios. When any request lacks provider cache
breakdown, the tool refuses to assert a full cost estimate for that task.
Because DeepSeek Cache is best-effort, hit rates and savings require actual
paid requests to observe. The counterfactual "cache savings vs all miss"
is a pricing scenario, not evidence that all uncached prompts would be billed
at the same rate or the user actually saved that amount.

Official source:
- https://api-docs.deepseek.com/api/create-chat-completion/
- https://api-docs.deepseek.com/guides/kv_cache/
- https://api-docs.deepseek.com/quick_start/pricing/

## Operational boundaries

- API calls are **NOT** run by the tests or this implementation session.
- Existing production workflows remain as configured (manual/paused).
  No batch dispatch, DeepSeek API invocation, public output or new scheduled
  job is triggered by this PR.
- This PR is stacked on #17 → #16 → #15 and is **not merged** to main.
- Existing user Reader data are not modified or used as synthetic gold.
- Existing usage logs already lack cache hit/miss and task fields: report
  them as *unknown/unattributed*, not reconstructed values.
- The logging path is configured through existing `ABRAHAM_AI_USAGE_LOG`;
  this change does not start new credential access.
- DeepSeek context caching is performed by DeepSeek's service, not by
  DeepInfer locally; GPU-side MTP, KV cache manipulation and speculative
  decoding cannot be applied to remote API calls.

## Next cost/quality benchmarks before production changes

1. Record a real, user-authorized, bounded translation slice of Reader docs.
   Compare its *complete* translation fidelity and named entity/number/date
   preservation to existing reference; never translate only excerpts.
2. Compare real provider `prompt_cache_hit_tokens`/miss tokens, paid
   completion tokens, billed price time window, invalid JSON/partial rate,
   and successful output count. Preserve representative long documents.
3. Quantify exact duplicate requests actually coalesced; do not claim
   a speedup from the presence of a single-flight implementation alone.
4. Only after measuring, consider changing batch size, optional off-peak
   scheduling, local preprocessing or retry budgets. Avoid token savings
   purchased by worse translation quality, excessive latency, or invented grades.
