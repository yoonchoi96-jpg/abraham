# Reader quality extensions (staging only)

The existing Reader → private Drive immutable-manifest path stays manual.
No production core workflow is dispatched, no Reader API write exists, no Drive
source is deleted/modified/shared, and no paid AI provider is introduced.
The former automatic core triggers stay paused. These additions do not promote
staging records into Abraham's production database, NAV, or Obsidian.

## HTML and identity

Install `requirements-reader.txt` in the intake environment. Trafilatura 2.3.1
extracts HTML files and HTML bodies inside JSON, favoring precision and excluding
comments. Failed/empty extraction is `HTML_EXTRACTION_FAILED`, held with a hashed
member reference. It is never replaced by menu text or guessed article text.
Markdown/plain text bodies retain their original bytes after UTF-8 decoding.
The untouched source ZIP remains the recovery source, including failed members.
HTML extraction changes the canonical text and therefore its SHA-256; a prior
ID/URL with different canonical text is quarantined, never overwritten. Legacy
state may therefore require explicit review rather than automatic replacement.

SHA-256 remains exact, with no whitespace/Unicode normalization. Every distinct
body remains stored even when near-duplicate candidates are found. Datasketch
2.0.0 MinHash uses NFKC/casefold/whitespace-normalized character 5-grams, 128
permutations, seed 1, 32 SQLite-backed bands of 4 values and candidate score 0.85.
Text shorter than 40 normalized characters is exact-only. Candidate scores are
probabilistic; false positives/negatives are expected and 0.85 is uncalibrated.
At most 20 candidates are attached to each new canonical record. No near match
causes deletion, merging, color propagation or auto-admission.

Signature version `near_v1` is explicit. Legacy records without a signature are
not silently described as covered; only bodies processed with this index are
searched. A future backfill must read the retained canonical shards and retain
this version or create a new index version. Alias conflicts still quarantine.
Identical bodies with different source tags retain original evidence in the ZIP;
canonical exact-dedup records retain the first source's metadata, as before.

The archive's index rows and LSH bands commit only after completed shard close
and fsync. An interrupted archive rolls back index changes and replays that
archive from its immutable ZIP; it does not resume halfway through a gzip file.
This avoids an older partial-checkpoint/truncated-shard failure. The existing
Drive manifest-last publication boundary stays authoritative.

## Authoritative colors

`reader_colors.py` contains the unchanged V5 tag parser and processing matrix,
shared by V5 and intake (including Korean/emoji aliases and conflict priority).
White/red/unclassified/conflicting records are held. Orange/yellow/green can
export without rating; blue/navy/purple/gold can export and rate. This is a
processing policy, **not a verified text-to-color classification rubric**.
A model's output is only an evaluation candidate. No output writes Reader tags.

## Drive bulk interface

`Bulk.run(Source, processor, max_items=1000)` in `scripts/reader_bulk.py` is the
integration boundary for the whole `Reader_Uploaded_Files` / `Library` tree.
Supply explicit folder IDs; ambiguous display names are never selected.
`DriveSource(existing_Drive)` exposes only paginated list, GET, and Docs export.
It preserves the source; Google Docs are read as an exported text snapshot.
Native source files remain in Drive. Unsupported MIME types are held.

- Recursive folder discovery uses SQLite queues, 100-file pages and atomic
  page-token + job insertion. A crash can replay a page without duplicating jobs.
- Default and maximum concurrency is **one** writer/download/processor. The
  owner-local advisory lock excludes simultaneous runs using the same workspace.
- File id + Drive version gives a SHA-256 idempotency key. Read checks version
  and sharing both before and after download. A changed source is held.
- Per-file state is pending/running/done/held. Raw snapshots precede processing
  and are retained when processing fails. The callback must durably publish
  output idempotently by the supplied key before returning. Crash after callback
  success but before SQLite commit can invoke it again with the same key.
- Transient 429/408/5xx/network failures retry at most three times by default
  (configurable 1–5), exponential backoff capped at 30 seconds. Permanent failures
  and exhausted retries are held; `retry_held()` is an explicit operator action.
- `max_items` bounds discovery pages and processed files per run. Rerun until
  discovery is complete and pending is zero. `start_scan()` explicitly discovers
  later additions and revisions; completed version keys remain immutable.
- Source reads are capped at 32 MiB; larger files are held for a separate adapter.
  SQLite/state/raw snapshots and callback outputs belong in private storage.
  Copying them into public CI logs, artifacts, Git or Releases is prohibited.
- Existing `Drive.list()` now follows every page for legacy commit/upload lookup.

The interface and read-only Drive adapter are implemented and tested with fake
paginated sources. Wiring the processor to the existing private Drive manifest
publisher, durable remote bulk checkpoints, real folder IDs/OAuth access and
full Library execution remain deployment work. A local SQLite queue alone does
not promise resume across ephemeral GitHub runners. Do not claim full ingestion
until those connections and real source runs are verified.

## Optional local MLX versus Ollama

Install `requirements-mlx.txt` **only** in a separate Apple Silicon Python >=3.11
environment. System Python does not need replacing. There is no cloud MLX job.
Weights must already exist in an explicit local directory; inference sets
`HF_HUB_OFFLINE=1`, disables HF telemetry and disallows tokenizer remote code.
The user/operator may separately download approved weights. No model pull or
remote inference fallback exists in this integration.

`scripts/reader_local.py` imports actual inspected AMANDO `command_bridge`
telemetry/local_resources from `--amando-root`. Missing/stale/bad measurements,
audio protection, nonnormal memory pressure, resident-model conflicts and actual
23.5 GiB ceiling deny the next call. The current AMANDO policy makes projected
memory and swap growth warnings rather than invented predictive rejection.
Model-specific weight/KV/runtime budgets are supplied, with context <=4096 and
output <=512 tokens. Use the **same** `--work` directory for all callers to share
the single inference lock; external AMANDO consumers do not yet acquire it.

Admission is rechecked during streamed output (at most once per second) and after
completion. A denied stream is cooperatively closed; no unrelated process/model
is killed. Blocking load/prefill/network waits cannot be preempted by this Python
adapter: native continuous GearShift lease/control integration remains required
for a production hard real-time guarantee. Ollama requests use `keep_alive=0`
and require no resident model initially. The adapter never unloads an externally
owned model. MLX resources are released when its call exits.

Example from repo root (private paths and actual model names must be supplied):

```sh
PYTHONPATH=. python -m scripts.reader_local --backend ollama --model qwen3:8b \
  --amando-root /path/to/AMANDO --work .reader-private/runtime \
  --prompt-file .reader-private/prompt.txt --output .reader-private/ollama.json
PYTHONPATH=. python -m scripts.reader_local --backend mlx --model /path/to/local/MLX/weights \
  --amando-root /path/to/AMANDO --work .reader-private/runtime \
  --prompt-file .reader-private/prompt.txt --output .reader-private/mlx.json
```

Record exact model revisions, quantization, runtime versions, context, output
cap, prompt SHA, cold/warm state and repetitions. Total latency includes load,
prefill and safety probes; first-token latency is first **visible text**, not
hidden reasoning. Backend token counts can differ. A one-prompt smoke is not a
50-case classification comparison, reliable throughput result, or speed ranking.
Peak system memory/native audio latency are not calibrated by this harness.

## Independent 50-case evaluation with Promptfoo

No human gold or content rubric is committed. Do not convert fixtures or existing
Reader tags into a supposedly independent gold set. Obtain an independently
reviewed, model-blinded 50-case set and user-confirmed content rubric first.
`validate_gold` checks declarations only, not the real identity/truth of reviewers.
All nine colors must have support; identical text and IDs are rejected.

Private JSON contract:

```json
{"schema":"reader-human-gold-v1","cases":[
  {"id":"private-stable-case-id","text":"full reviewed text",
   "text_sha256":"SHA256_OF_EXACT_UTF8_TEXT","expected":"blue",
   "reader_tags":["rating-blue"],
   "confirmation":{"method":"independent-human","reviewer":"human-id",
     "blinded_to_model_output":true,"confirmed_at":"ISO8601 timestamp"}}
]}
```

This is a schema illustration, **not** a gold case. Supply exactly 50 real cases.

```sh
PYTHONPATH=. python -m scripts.reader_eval prepare \
  --gold .reader-private/gold.json --rubric .reader-private/confirmed-rubric.txt \
  --output .reader-private/promptfooconfig.json
# Set READER_OLLAMA_MODEL, READER_MLX_MODEL, READER_AMANDO_ROOT,
# READER_LOCAL_WORK, and PROMPTFOO_PYTHON to the isolated Python executable.
PROMPTFOO_DISABLE_TELEMETRY=1 PROMPTFOO_DISABLE_UPDATE=1 \
  npx --no-install promptfoo eval --no-cache \
  -c .reader-private/promptfooconfig.json -o .reader-private/results.json
PYTHONPATH=. python -m scripts.reader_eval report \
  --results .reader-private/results.json --output .reader-private/metrics.json
```

Install pinned Promptfoo 0.124.1 locally (no global install needed). Its provider
calls only the existing tag parser and the local guarded backends. Gold labels
and Reader tags never enter the model prompt. No LLM judge/provider API is used.
Reports compare accuracy over all cases, coverage, hold rate, selective accuracy,
9×10 confusion matrix and per-color TP/FP/FN/precision/recall. A hold/invalid output
counts as a miss for the true color; abstention never improves overall accuracy.
Backend-unavailable holds remain visible. Synthetic `smoke.yaml` tests provider
plumbing only and must never be reported as classification accuracy.

The added manual validation workflow uses only synthetic fixtures, no secrets,
private datasets, artifact uploads or model inference. Existing Reader check's
PR trigger remains validation-only; existing production schedules stay paused.

## Primary references

- [Trafilatura extraction API](https://trafilatura.readthedocs.io/en/latest/corefunctions.html)
- [Datasketch MinHash/LSH](https://ekzhu.com/datasketch/lsh.html)
- [MLX-LM official repository](https://github.com/ml-explore/mlx-lm)
- [Promptfoo Python provider](https://www.promptfoo.dev/docs/providers/python/)
- [Promptfoo configuration](https://www.promptfoo.dev/docs/configuration/reference/)
