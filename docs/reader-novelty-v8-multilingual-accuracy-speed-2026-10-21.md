# Reader novelty v8: multilingual safety + indexed retrieval (2026-10-21)

## Scope and guarantees

Built for independent Reader items in Chinese (Simplified/Traditional), Japanese,
Korean, English, Spanish, French, Russian, Arabic, Portuguese and scripts beyond.
Language/script routing is a heuristic; shared Han does not identify Chinese
versus Japanese; Latin does not distinguish English, Spanish, French, Portuguese.
The engine still **does not claim multilingual entailment**. Cross-language or
differently titled reports remain **novel**, rather than automatic translation skips.

Critical guards now recognize language-specific negations and speculative
attribution with word boundaries (space-delimited scripts) and conservative
substring checks (East Asian scripts). Arabic-Indic/Persian/fullwidth decimal
digits are canonicalized; French/Arabic guillemets in quoted claims are
preserved. Distinct numbers, uncertainty, denial or literal short claims
bar automatic semantic redundancy candidates. This is lexical safety, not a
logical theorem or a full morphology engine.

## Performance implementation

Existing algorithm: for each new document, read **all current-policy stored
vectors** and calculate cosine similarity, despite enforcing identical
normalized titles before permitting a candidate (worst case O(N) vector loads).

v8: store `title_key` normalized with Unicode NFKC and `casefold`; create
SQLite indexed key `(model_id, policy_revision, decision, title_key)`, and a
matching body-sha exact-lookup index. Candidate retrieval compares vectors only
for same-title originals (indexed O(log N + M), where M is candidates with
the headline; absent extreme same-title repetition M ≪ N). Raw title+body
fingerprints and source preservation remain unchanged. Exact body duplicates
also accept punctuation-equivalent normalized headlines. Policy revision
invalidates prior advisory decisions, so old rows never become new evidence
without reassessment. Cross-language candidate search is intentionally **not**
enabled as an automatic suppressor.

Synthetic benchmark: `python3 -m scripts.reader_novelty_benchmark --docs 10000
--trials 7`. CI runs both legacy full scan and indexed lookup on the same
deterministic corpus and checks identical results and SQLite index usage.
Numbers from this benchmark reflect **candidate lookup only**, not local
embedding execution, overall Reader time or DeepSeek cost.

## Validation and isolation

`tests/test_reader_multilingual.py` covers 9 languages for denial cues,
hedged claims and changed-negation adversarial near-duplicates; Unicode script
hints and three digit systems; guillemets; and cross-language same-topic but
distinct-title articles. Additional current-policy migration, normalized-title
exact-body and index-plan tests live in `tests/test_reader_novelty.py`.
Existing Reader unit test suite and >3 GiB synthetic archive CI remain gating.

All samples in this PR are synthetic, with no private Reader raw body or IDs.
No DeepSeek API use, Reader Seen/tag mutation, document deletion, production
suppression, or extra paid cloud service. Real cross-language model precision,
recall and end-to-end speed on user's Mac mini require an Ollama-backed local
Reader staged corpus run and independent human gold labels.
