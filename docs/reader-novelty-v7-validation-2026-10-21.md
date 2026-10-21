# v7 false-skip hardening and adversarial tests — 2026-10-21

Previous actual v6 CI: GitHub Actions run 39563019229 **passed**. That workflow validates synthetic cases (no private Reader source/model execution).

Changes:
- Insufficient extracted source text (<40 visible characters after removing linked images/URL markup) is held in review_required. Two identical title-only snippets are NOT verified complete articles.
- Short independent source claims of 4–30 normalized characters (e.g., 利润上涨 versus 利润下跌) require literal matching; global embedding cosine cannot smooth away a fact reversal.
- Within top-K semantic matches, identical normalized headlines are prioritized over different-city/weather-topic matches.
- Cached matches and gold evaluation become stale when a referenced original changes from novel to review_required despite unchanged body hash/title.
- Only the currently active policy is included in novelty index decision totals.
- Explicit 'no' and contracted English negatives are critical markers.
- Offline --dry-run rejects nonlocal TCP via connect_ex as well as connect. This is a process guard, NOT a system-level network sandbox.

No DeepSeek calls or API token usage; no Reader Seen/tag, Drive writes, file deletions or translation suppression. All test additions are synthetic; a scored real-corpus Ollama evaluation and independently adjudicated 50+ gold labels are still needed. No private Reader text/identifiers are committed.

This patch is deliberately conservative: it may mark more sources novel/review_required rather than lose a new fact.
