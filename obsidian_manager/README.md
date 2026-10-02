# Hendrix v0.2

Hendrix is the Obsidian Vault management layer shared by Abraham, Abel, Jacques, and Omari.

It is not the tutor, knowledge classifier, music engine, or AI orchestrator.

## Role
- Vault integrity and structure
- Metadata ownership governance
- Link and backlink integrity
- Future embedding/index management
- Safe write boundary for Obsidian
- Integration boundary between the other systems and Obsidian

## Ownership rule
Hendrix must never silently take ownership of metadata created by another system.

### Abraham-owned
entity, entities, rating, source, source_url, source_id, readwise_id, classification, category, relations, relation

### Abel-owned
abel_subject, abel_level, abel_learning_state, abel_mastery, abel_priority, abel_wrong_count, abel_correct_count, abel_last_wrong, abel_saved

### Hendrix-owned
hendrix_id, hendrix_indexed, hendrix_embedding, hendrix_last_audit, hendrix_integrity

Unknown metadata is protected by default until ownership is explicitly declared.

## V0.2 checks
- Markdown inventory
- duplicate filename/stem detection
- frontmatter errors
- broken wikilinks
- broken relative Markdown links
- metadata ownership
- unknown metadata-owner detection
- empty Markdown files
- basic path statistics

## Safety
V0.2 remains read-only.
- No Vault writes
- No renames
- No deletes
- No LLM calls
- No automatic metadata changes
- Abraham and Abel metadata are explicitly non-writable by Hendrix
- Unknown metadata is non-writable by Hendrix

The JSON report is an external audit artifact, not a Vault mutation.

## Usage
    python -m obsidian_manager.audit /path/to/Invest\ Insight
    python -m obsidian_manager.audit /path/to/Invest\ Insight --json report.json
    python -m obsidian_manager.audit /path/to/Invest\ Insight --fail-on-issues

## Architecture
    Omari
      |
      +-- Abraham  (knowledge processing)
      +-- Abel     (personal learning)
      +-- Jacques  (music intelligence)
      |
      +-- Hendrix  (Obsidian management)
             |
             +-- Obsidian

Hendrix is infrastructure. It does not decide what the user's knowledge means and it does not decide how the user should study.