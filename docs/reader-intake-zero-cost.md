# Reader intake: zero additional infrastructure cost

This is a separate, manual cloud intake path. Existing Abraham production state,
workflow files, release assets and deployed GCP services are preserved.

## Execution and storage

- Only standard `ubuntu-24.04` GitHub-hosted runners in the existing **public**
  code repository. A private repository fails closed. No self-hosted/local runner.
- Public-repository standard runners are free; do not change repository visibility,
  runner label, billing budgets, or introduce paid runners.
- Account Actions budget was observed at $0 with **Stop usage when budget limit
  is reached** checked on 2026-10-20. No payment method was configured in that view.
  The account hard budget is the overage control. The workflow does not claim to
  enforce a private-repository minute allowance from inside an already started job.
- No Actions artifacts/caches, Release assets, dataset commits, GCP jobs, buckets,
  model calls, new subscriptions, or hourly schedule are created by this path.
- Source ZIP, canonical gzip JSONL shards, SQLite identity index, hashed issue
  records, and immutable commit manifests are persisted in private Google Drive.
- Full source ZIP is preserved unchanged. Binary attachments are streamed through
  ZIP CRC validation and retained in the original ZIP. They are not sent to AI.
- Working copies exist only on the ephemeral GitHub Runner.

## Authentication

Existing repository secret `READWISE_TOKEN` is used by the official pinned
`@readwise/cli@0.5.10` to create/poll the Reader Markdown ZIP export. No input ZIP
path or manually uploaded public Release asset is required.

One Google **user OAuth** credential must be installed as repository secret
`DRIVE_OAUTH_JSON`:

```json
{"client_id":"...","client_secret":"...","refresh_token":"..."}
```

Use a Google OAuth client with Drive `drive.file` access and offline consent.
When no `READER_DRIVE_FOLDER_ID` variable is configured, the application creates
its own private folder in the user's existing Drive. This avoids granting access
to all Drive files. If selecting an existing folder, the OAuth application must
already have permission for that folder (for example through Google Picker);
a folder created by a different OAuth client is not automatically accessible
under `drive.file`. A service account cannot substitute for the user's personal
Drive storage quota. Do not put credentials or signed export URLs in code, PRs,
workflow dispatch inputs, public logs, or this document.

The Codex Drive connector does not expose its OAuth credential to GitHub.
This code never scrapes browser/cloud credentials or repurposes Cloud Shell
tokens. Google user consent and the GitHub secret are required for production.

## Semantics

- Exact extracted UTF-8 body SHA-256 is canonical. Raw source stays in ZIP.
- Reader ID and conservative normalized URL are aliases of a body.
- Same body is stored once; aliases are retained across archives/reruns.
- Same ID/URL with a different body is quarantined, never overwritten.
- Empty body is deferred as `DEFERRED_NO_FULLTEXT`.
- Invalid metadata, malformed JSON, oversized text, unsafe ZIP members and CRC
  failures are isolated using hashed member references. Full filenames and body
  text do not reach public logs.
- At most one text document (32 MiB limit) is materialized at once. Shards rotate
  at about 32 MiB uncompressed. Archive/member/expanded-size ceilings are explicit.
- Completed immutable uploads are verified with Drive size and MD5, deduplicated
  by SHA-256, and reused on rerun. Source, shards, issues and index are uploaded
  first; a new manifest is uploaded last as the commit marker. Failed runs leave
  the previous committed index authoritative. No existing Drive data is deleted.

## Abraham → NAV → Obsidian admission boundary

The canonical JSONL uses the documented Nayvadius Abraham input fields:
`id`, `content`, `source`, `metadata.readwise_id`, `metadata.url`, `tags`,
`rating`, `translation_ko`, `entities`, and `relations`.

This intake is a **staging producer**, not permission to replace Abraham's
existing V4.61-derived database or admit an unreviewed full Reader export to NAV.
Reader color tags are retained and ambiguous/unclassified/white/red records are
held. Entities/relations are empty instead of being invented or re-extracted
using paid providers. Translation is not run in this intake workflow and no AI
key is exposed to it. DeepSeek is the only permitted future paid provider, for
translation only; user-managed funding is outside this workflow.

Production Abraham import, DeepSeek translation, NAV QA and Obsidian promotion
must be separately validated before claiming end-to-end integration complete.
The old core workflow still contains nontranslation DeepSeek/Gemini operations;
this new intake never dispatches it. Existing automatic triggers were already
paused in commit 493d41ed6a69b1709102b182a93bb6f4bc63091c.

## Validation and honest reporting

`reader-intake-check.yml` runs deterministic isolation/dedup/restore tests and a
real, nonsparse ZIP larger than **3 GiB compressed** on a standard hosted Linux
runner. The large ZIP contains synthetic binary payload plus tiny synthetic
documents. It verifies sequential CRC reading and a second idempotent run.

This is **not** evidence that the user's actual 59k-document Reader export has
been processed or that actual Drive persistence/Abraham/NAV/Obsidian succeeded.
Until Drive authorization is installed and an actual manual run finishes:
actual Reader documents processed = 0; Drive end-to-end = unverified;
production 3GB ZIP test = not performed.

Public Actions logs contain only operational codes and aggregate counts.
No new hourly automation exists.
