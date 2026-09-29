# Abraham

Abraham is the Readwise Reader → SQLite → Obsidian knowledge pipeline.

## Current production line

- V4.61 Freeze: cleaned compatibility baseline.
- V5.6: cloud-ready incremental pipeline with SQLite stage state, Korean translation caching, safe file writes, backups, storage maintenance, and an execution lock.
- Readwise: connected through the Reader integration for current document retrieval.
- Obsidian: the production vault remains an external/local filesystem synced separately; this repository does not pretend GitHub has direct access to the user's Mac vault.

## Production schedule

KST: 00:00, 09:00, 17:00.

The schedule is configured by ABRAHAM_SCHEDULE_KST and mirrored in the systemd timer under abraham_v56_deploy/.

## Source of truth

SQLite is the canonical pipeline state and source snapshot. Rendered Markdown is an output, not an input source for article content.

## Incremental behavior

document_stages tracks stage status, attempts, content hashes, result hashes, errors, and worker IDs. Unchanged completed documents are skipped; changed content is reprocessed. Explicit --readwise-id runs bypass the skip decision for targeted reruns.

## Safety

- Dry-run is the default.
- --apply is required for filesystem mutation.
- Existing files are backed up before writes/renames.
- Storage maintenance never deletes Vault Markdown.
- No API keys or SQLite databases are committed to Git.

## Validation

CI compiles the production modules, runs unit tests for incremental state, and validates the configured schedule.

## Deployment

See abraham_v56_deploy/README.md for the Linux/systemd deployment model, including the separate Obsidian headless-sync service.


## Production state guard

Production does not bootstrap a replacement SQLite database. If the rolling `state-current` Release Asset is missing, the production workflow fails closed. This prevents a test/smoke database from ever replacing the preserved V4.61 state.

The first production state must be the validated V4.61 SQLite database from the freeze baseline. After that, each successful production run restores the state, incrementally syncs Readwise, processes changes, commits Vault/report output, and refreshes the rolling SQLite Release Asset.

## Readwise ingestion

`scripts/sync_readwise.py` incrementally imports new/updated Reader documents into the canonical SQLite database before V5.6 processing. It uses the Reader API's `updatedAfter` window with a small overlap and paginates up to the configured maximum, while preserving existing V4.61 canonical text on already-known documents.

## V4.61 state migration

The canonical V4.61 SQLite database must be preserved as-is. Do not rebuild the 8,802-document state from Readwise API because that would lose the existing entity, relation, stage, and cache state.

Use the migration validator:

```bash
python scripts/prepare_v461_state.py \
  --source /Users/w/obsidian_auto/obsidian_auto_v1/v4/readwise_pipeline.db \
  --output /tmp/readwise_pipeline.db
```

It copies the source without modifying it, runs `PRAGMA integrity_check`, and reports the baseline table counts.

The prepared DB should then be uploaded as the initial `readwise_pipeline.db` asset of the private `state-current` GitHub Release. The production workflow subsequently restores and replaces this rolling state asset after each successful run.
