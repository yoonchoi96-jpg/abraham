# Reader first-pass inventory and durable ingestion (proposed)

## Scope

- GPT stays the **only** owner of initial Reader 9-color classification.
- Abraham remains the **second-pass** scorer for **blue, navy, purple and gold**. Green is not second-pass rated.
- The new SQLite `reader_first_pass_queue` table is an **inventory of Reader-observed states**, **not** an AI classification engine and **not yet** a ChatGPT Tasks connector.
- Nav and Omari downstream behavior is unchanged.

## Why the old code could silently skip Reader data

Former ingestion fetched at most 1,000 Reader results and then selected the **maximum `updated_at` among locally stored documents** for the next `updatedAfter` query. If more than 1,000 records matched, the older matches were never persisted, and a newer watermark could exclude them permanently.

## Proposed ingestion changes

1. Use the existing SQLite file and add `reader_ingest_progress` and `reader_first_pass_queue` with `CREATE TABLE IF NOT EXISTS`. Existing stage, entity, relation, canonical text, rating, and cache tables are not recreated.
2. **Cold-start safety**: `window_start=NULL` forces one complete Reader catalog traversal on initial deployment, because legacy capped runs may have missed entries. The traversal may take **many scheduled runs**; a capped run is *not* a completed scan.
3. Persist **each page** and its **next page cursor** in the **same SQLite transaction**. A crash after any committed page resumes at that cursor without advancing the watermark.
4. Advance the watermark **only** when Reader returns an exhausted page (no `nextPageCursor`). Use the scan's **start timestamp minus lookback** rather than its finish time, so concurrent updates can be reread on the next scan.
5. If Reader rejects an expired cursor with HTTP 400/410, restart the **same** scan window **once**, without advancing the watermark or claiming completeness. Other failures leave the last committed checkpoint intact.
6. Load only the affected document rows from SQLite for each page instead of reading a full multi-gigabyte document snapshot into Python memory.
7. Reader-sourced first-pass inventory states: `pending`, `tagged_unseen`, `graded_seen`, `seen_untagged`, `conflict`, `unverified`. `seen_untagged` does **not** prove intentional GPT deferral; it can be a manually read article. Never auto-assign a color from these states.

## **Privacy blocker: do not deploy or export this queue yet**

As observed on **2026-10-17**, repository `yoonchoi96-jpg/abraham` is **PUBLIC** and the published `state-current` release exposes a `readwise_pipeline.db` asset (roughly 1.2 GB) and `abraham-vault.tar.gz`. A public release asset is not a private database, regardless of the file name or README wording.

**User preference (2026-10-17): keep the Abraham CODE repository PUBLIC.** Do not change its visibility. Instead, create a separate **PRIVATE** state repository and move canonical state releases there after validating original database/vault copies. Existing public release assets remain exposed until the owner safely retires them; this PR does not delete them. Review historical exposure and rotate any credentials inadvertently included in state assets. Do not publish queue IDs, titles, bodies or private checkpoints to public files, workflow logs, artifacts, release bodies or issues. No production data was modified by this branch or PR.

## GPT connector bridge still needed

ChatGPT Tasks can read connected Readwise documents but **cannot directly open** an SQLite file held as a GitHub release asset. The new tables alone do not make the scheduled GPT task resume by queue ID. After the data location is private, design an **authenticated private queue handoff** readable by the ChatGPT task (for example an approved private connected service); prove that permissions, serialization and acknowledgments work before redirecting the hourly GPT task. Never publish real Reader IDs to a public git file to fake the bridge.

**Do not change the GPT task prompt to claim queue consumption until that authenticated bridge is tested.** Until then, existing GPT automation continues to use Readwise directly.

A private-only queue manifest generator is now available:
`python scripts/export_reader_queue.py --db data/readwise_pipeline.db --output-dir "$RUNNER_TEMP/reader-queue"`.
It writes content-addressed shards and an index containing Reader IDs only (no document body). Those generated files are **still sensitive**. They are not uploaded automatically; publishing the private manifest and making it accessible to ChatGPT Tasks require a separately approved and tested authenticated handoff. Do not use workflow artifacts in a public repo as a substitute.

## Tests

Token-free tests in `tests/test_reader_queue_recovery.py` cover: 1,000-style partial scan checkpointing, resume after failure, cold-start recovery despite newer existing local timestamps, and inventory state separation. CI runs these plus legacy `tests/test_readwise_sync.py`. No Reader API token or private SQLite is used by CI.

## Rollout

1. Secure production repository and state assets.
2. Run PR CI, review branch diff, and test against an **isolated copy** of the real SQLite state; verify `PRAGMA integrity_check`.
3. Confirm Reader cursor longevity under real scheduled invocations. HTTP 400/410 reset is safe but repeated expiration may prevent full backfill and needs an alternative page partition strategy.
4. **Public-code/private-state deployment**: keep `abraham` public, create a separate private state repo with a `state-current` release containing validated `readwise_pipeline.db` and `abraham-vault.tar.gz`. Configure the public code repo's `vars.ABRAHAM_STATE_REPOSITORY` to `owner/private-repo` and `secrets.ABRAHAM_STATE_TOKEN` to a fine-grained token with read/write permission for the private state repo only. The code workflow explicitly checks `.private=true` before any download/upload and refuses missing vault assets. Test on a copy, then merge only when the private release state is ready. Do not put tokens in source code or output logs.
5. The new workflow runs **fast recent-update lane first**, then independent historical-backfill lane; each lane permits up to 1,000 documents per invocation and persists its own cursor. No hard daily completion guarantee.
6. Introduce and test an authenticated private queue connector for GPT. Only then report end-to-end queue consumption complete.

## Known limitations

- First complete catalog scan can take days and will temporarily delay new-entry ingestion in the same cursor lane. Separate fast-lane incremental ingestion may later reduce latency, while keeping the full backfill independently checkpointed.
- Reader pagination consistency and the meaning of `updatedAfter` under concurrent updates require live validation.
- Some Reader results may omit tags or seen fields; such items are inventoried as `unverified`, never assumed unseen or completed.
- The inventory reflects last observed source state, not a durable `GPT-claimed` / `GPT-acknowledged` transaction. That requires the private connector.
- Successful sync updates the existing canonical source rows; it does **not** mark anything Seen or apply any color in Reader.


## Diagnostics and validation

Run `python scripts/reader_queue_status.py --db data/readwise_pipeline.db` to show only aggregate counts for `pending`, `tagged_unseen`, `graded_seen`, `seen_untagged`, `conflict` and cursor-health flags for each lane. It never prints document IDs or opaque page cursors. The status represents the **last observed** Reader state; freshly GPT-tagged documents may not be reflected until the next incremental synchronization.

The proposed workflow does **not** expose Reader IDs or private DB on a public GitHub Actions artifact. The reader queue index is only a potential privately published handoff, not a completed ChatGPT Tasks connection.
