# Obsidian Manager v0.1

Read-only Vault audit engine for the Invest Insight Obsidian vault.

## Design rule

Obsidian Manager is not an alternative classifier. It never decides or rewrites Abraham-owned classification, entity, rating, source, or relation metadata.

V0.1 only scans and reports.

## Checks

- Markdown inventory
- duplicate filename/stem detection
- frontmatter errors
- broken wikilinks
- broken relative Markdown links
- Abraham-owned metadata observed
- empty Markdown files
- basic path statistics

## Safety

- Read-only by construction.
- No file writes, renames, or deletes.
- No API calls.
- No LLM calls.
- Exit code is non-zero only with --fail-on-issues.

## Usage

    python -m obsidian_manager.audit /path/to/Invest\ Insight
    python -m obsidian_manager.audit /path/to/Invest\ Insight --json report.json

The report becomes the input contract for later safe repair proposals, embedding indexing, and learning/article linking.
