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
