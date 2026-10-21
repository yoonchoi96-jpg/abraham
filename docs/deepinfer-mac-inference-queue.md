# DeepInfer-inspired local Reader queue (experimental; NOT a DeepInfer port)

## Scope and source analysis

DeepInfer official source: https://github.com/deepseek-ai/DeepInfer

- Apache-2.0, but read and honor all notices before copying code. This
  implementation copies **no DeepInfer source**; it borrows architecture ideas.
- DeepInfer's \`csrc/engine/scheduler.hpp\` and \`deepinfer/engine/llm_engine.py\`
  implement engine-level request scheduling with GPU block management and
  distributed prefill/decode scheduling.
- Official hardware: NVIDIA SM100 and Huawei Ascend 950; Linux prerequisites.
- The public release explicitly says a **complete service** still requires
  a separate KV cache system, VE/prefill/decode disaggregation, and replica load
  balancing. These are NOT provided here.
- The published benchmark uses multiple high-end GPUs, so its throughput or
  speedups are **not transferable** to an Apple Silicon 24GB Mac mini.
- Mac deployment uses the existing locally installed MLX-LM or Ollama backend.
  There is no CUDA/Metal kernel port, distributed pipeline, batching, or speculative
  decoder. Do not call this DeepInfer running on macOS.

## What was actually implemented

\`scripts/reader_infer_queue.py\` is an optional local-only **serial** FIFO with:

- Private 0700 workspace; request files and SQLite state persist across exits.
- Bounded pending jobs; SHA-256 exact request idempotency; FIFO by insertion.
- One queue consumer plus the existing \`reader_local.infer\` lock for model
  admission. Inference still obeys GearShift's actual live telemetry and resource
  constraints. A queue slot does **not** override any GearShift refusal.
- Stable-key replay of a job left \`running\` by an interrupted process.
- Exact JSON shape \`{"color":"blue","topic":"AI","reason":"...","evidence_quote":"..."}\`
  with one of the nine existing Reader colors and an evidence quote that must
  occur in the supplied source text. Unparseable/invented claims become
  \`review_required\`.
- Valid responses become \`classified_candidate\`, **never**
  \`classified_verified\`. Model output is not an independent human gold label.
- GearShift/backend denial is held, not spun on. Only the operator can retry
  a held task, once (\`attempts < 2\`). No automatic high-volume retries.
- No Reader API writes, Drive file renames, external provider/API requests,
  user source commits, public logs, automatic workflow activation or promotion
  to Abraham/NAV/Obsidian.

This is a **prototype seam** for the Reader bulk processor; it has not been
wired into the production intake, and no user Reader document was processed.

## Usage (local Mac only)

Use a local, private directory, e.g. \`~/.amando-reader-queue\`, never
inside a public Git checkout or shared file-sync folder.

Supply two **existing private local files**: the complete candidate prompt
(including any 9-color rubric) and the original article text for checking
the quoted evidence.

\`\`\`bash
python3 -m scripts.reader_infer_queue --work ~/.amando-reader-queue submit \
  --backend ollama --model qwen3:8b \
  --prompt-file /PRIVATE/path/prompt.txt --source-file /PRIVATE/path/article.txt

python3 -m scripts.reader_infer_queue --work ~/.amando-reader-queue run \
  --amando-root /Users/yoon/AMANDO --max-items 100

python3 -m scripts.reader_infer_queue --work ~/.amando-reader-queue status
\`\`\`

MLX uses \`--backend mlx --model /path/to/local/mlx/model\`. The model must
already be installed; \`reader_local.infer\` will not download weights.

Responses are in the private SQLite database, not the CLI output. Treat
all candidate grades as unverified. \`review_required\` indicates invalid
or unsupported model output; \`held\` indicates execution was blocked.

To manually retry a held task (up to two total attempts), run:

\`\`\`bash
python3 -m scripts.reader_infer_queue --work ~/.amando-reader-queue retry-held
\`\`\`

## Tests and measurements

The added \`tests/test_reader_infer_queue.py\` exercises exact deduplication,
private storage, bounded queue, valid candidate outputs, ungrounded/invalid
output rejection, GearShift hold, explicit retry, crash replay, tamper detection,
and private exception handling using synthetic inputs and fake inference.

It does **not** validate actual Reader classification accuracy.

**Mac benchmarks remain REQUIRED before selecting this scheduler.** Measure
under identical prompts and generation limits, sequential runs and warm/cold
model states for each: (a) direct Ollama, (b) direct MLX-LM, (c) queue + same
backend. Record documents/second, end-to-end p50/p95, memory pressure/peak RSS
or resident GPU footprint, schema-valid rate, holds, and post-crash resumed jobs.
The queue does not provide genuine GPU batching, so it is **not expected to
accelerate raw single-model decoding**; the potential value is reliability,
throughput governance, and avoiding duplicate work. Do not extrapolate from
synthetic tests.

## Limits / follow-up

1. The real Reader bulk processor and Drive publisher are not connected to
   this prototype. Only add that connection after testing crash and privacy
   boundaries with real local files.
2. Work queue uses local private storage only; it cannot provide cross-run
   durability between different ephemeral GitHub runners.
3. The same GearShift admission API is reused, but other AMANDO consumers do
   not necessarily share the same queue lock.
4. No human-confirmed 50-item, 9-color gold set or accuracy benchmark exists
   as part of this change. Keep human confirmation and production admission
   separate.
5. No live Mac mini inference, throughput or memory benchmark was performed
   in this change.
