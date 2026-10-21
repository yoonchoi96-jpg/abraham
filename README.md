# Abraham

Abraham is the Readwise Reader → SQLite → Obsidian knowledge pipeline.

## Current production line

- V4.61 Freeze: cleaned compatibility baseline.
- V5.6.1: cloud-ready incremental pipeline with SQLite stage state, SQLite-backed Korean result caching, safe file writes, backups, storage maintenance, and an execution lock.
- Readwise: connected through the Reader integration for current document retrieval.
- Obsidian: GitHub Actions uses Obsidian Headless to sync the production vault; the runner keeps only an ephemeral working copy and does not require the Mac or iPhone to hold the full vault.

## Production schedule

KST: 00:00, 09:00, 17:00.

The schedule is configured by ABRAHAM_SCHEDULE_KST and mirrored in the systemd timer under abraham_v56_deploy/.

## Source of truth

SQLite is the canonical pipeline state and source snapshot. Rendered Markdown is an output, not an input source for article content.

## Incremental behavior

document_stages tracks stage status, attempts, content hashes, result hashes, errors, and worker IDs. Unchanged completed documents are skipped; changed content is reprocessed. Explicit --readwise-id runs bypass the skip decision for targeted reruns.

Use `python run_v560_storage.py --status` for read-only pipeline health, including stale running checkpoints (24 hours by default; configure with `ABRAHAM_STALE_STAGE_MINUTES`). Resume a bounded stage with `python run_v560_storage.py --resume --from-stage korean_translation --limit 100`. Resume persists database results but defers Obsidian writes; `--readwise-id ID` can target a specific document.

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

The first production state must be the validated V4.61 SQLite database from the freeze baseline. After that, each successful production run restores the state, incrementally syncs Readwise, processes changes, commits Vault/report output, and refreshes the rolling SQLite Release Asset. The existing release asset is replaced in-place with `gh release upload --clobber`, so a failed upload does not require deleting the previous state first.

## Readwise ingestion

`scripts/sync_readwise.py` incrementally imports new/updated Reader documents into the canonical SQLite database before V5.6.1 processing. It uses the Reader API's `updatedAfter` window with a small overlap and paginates up to the configured maximum, while preserving existing V4.61 canonical text on already-known documents.

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


## Reader color processing policy

Reader rating/color tags are the source of truth. Abraham does not ask Gemini to classify the color.

| Reader color | Korean translation | Rating | Obsidian export |
|---|---:|---:|---:|
| White | No | No | No |
| Red | No | No | No |
| Orange | Yes | No | Yes |
| Yellow | Yes | No | Yes |
| Green | Yes | No | Yes |
| Blue | Yes | Yes | Yes |
| Navy | Yes | Yes | Yes |
| Purple | Yes | Yes | Yes |
| Gold | Yes | Yes | Yes |

Conflicting or unclassified color tags are quarantined: no rating, translation, or Obsidian export.

Existing SQLite ratings are adopted into the V5 rating stage without another Gemini call. Only rateable documents that lack a valid completed rating stage require the rating AI stage.


## Cloud architecture

- Readwise Reader is the document source.
- GitHub Actions is the compute/orchestration layer.
- SQLite state is persisted in the private `state-current` GitHub Release asset.
- The Obsidian vault is **not** stored in the Git repository. The runner uses an ephemeral working copy and syncs it through official Obsidian Headless/Sync.
- Mac/iPhone access is provided by native Obsidian Sync; the GitHub runner is a separate Headless Sync device.
- Generated vault files are therefore not accumulated on the phone merely because Abraham processed more documents. Device-level Obsidian Sync selective-sync settings remain available for storage control.

Obsidian Headless is currently an open-beta official client and requires Node.js 22+ and an active Obsidian Sync subscription. It supports one-shot CI synchronization and uses the same Sync encryption/privacy model as the desktop client. The GitHub Actions workflow uses a dedicated device name and serialized concurrency.

## Reader quality and local evaluation

See [Reader quality extensions](docs/reader-quality-local-eval.md) for offline HTML
extraction, advisory near-duplicate candidates, resumable Drive bulk interfaces,
optional GearShift-admitted MLX/Ollama inference, and independent human-gold
Promptfoo evaluation. These remain staging/evaluation paths; no Reader tag write
or production promotion is added.
