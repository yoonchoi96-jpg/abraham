# Abraham Reader Drive Library: manual read-only staging

This optional **real-source connector** uses the existing Abraham PR #15
`reader_bulk.py` and `reader_intake.Drive` user OAuth. It reads, extracts
and indexes documents from the user's Drive Reader Library, and does **not**
modify their Drive or Reader metadata.

Source root (explicit):
- `Reader_Uploaded_Files/Library` Drive folder ID:
  `1KIsyDd_EUJuDxMIvrnIvi2VWriaxaB2U`.

## First manual run (Mac/private persistent environment)

The `DRIVE_OAUTH_JSON` credential must be provided by the user in their
private environment (or the three existing `GOOGLE_DRIVE_OAUTH_*` variables).
Neither this connector nor ChatGPT exposes an OAuth token to GitHub. The
OAuth client needs actual permission to the folder. Do not copy secrets into
CLI arguments, code, PRs, chat or public logs.

Run from the project checkout with an existing supported Python environment:

```sh
python3 -m scripts.reader_drive_stage \
  --work "$HOME/.abraham-reader-stage" --max-items 1000
python3 -m scripts.reader_drive_stage \
  --work "$HOME/.abraham-reader-stage" --status-only
```

Run again until `discovery_complete=true` and `pending=0`.
To detect newly edited documents later, use `--rescan` once.
The work directory must be private, local, persistent and not in Git.

The scanner uses paginated Drive API access, per-file ID+version
idempotency, bounded retries, source SHA-256, exact body hash and SQLite
MinHash candidate indexing. The original downloaded raw files stay in
private storage for crash replay and are not deleted by this command.

The local `stage` table stores only hashes, Reader IDs, source colors,
Drive filename color hints, duplicate status and processing reasons.
In particular, already colored Drive filenames are treated as
`existing_tag_unverified`, not blindly promoted to authoritative human
grading. A new color is **never** assigned by this stage.

### Status vocabulary

- `review_required`: successfully extracted original body, color not
  independently established
- `existing_tag_unverified`: source/filename has a color; retained only
  as an existing hint until validated
- `duplicate`: exact body match with previous entry; both originals kept
- `deferred_no_fulltext`: extraction failed or source has no usable body
- `held` in the bulk job table: error/authorization/version changed;
  needs operator action, not endless automatic retry

## Explicitly not completed by this implementation

- No actual user Reader document has been processed by this PR's authorship.
- Neither Writer nor remote cross-run Drive/ZIP manifest checkpoint is added.
- No colored grade is generated or written back to Drive.
- No production Abraham SQLite import, DeepSeek translation, NAV handoff or
  Obsidian export happens automatically.
- No hourly schedules, GitHub production workflow, local model daemon,
  Secrets, cloud resources, paid API calls, or user files are mutated.

Before production: run a small manual **read-only** private smoke, check
Drive source permission, compare actual body extraction, authorize a
controlled output publisher with immutable write+readback, verify existing
9-color rubric with human-confirmed gold, and only then deploy full pipeline.

This PR is stacked on #16 (itself stacked on #15). Merge/rebase deliberately
so no changes are lost; Argon has no approval gate here.
