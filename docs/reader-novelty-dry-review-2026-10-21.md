# Reader novelty: read-only real-article manual review, 2026-10-21

**Execution boundary:** The Readwise Reader connector was used **read-only**.
No DeepSeek API tool or key was invoked, no article or label was deleted,
no Reader Seen/tag was changed, and no translation was suppressed. The
sample was never placed in a public dataset or sent to a paid model.

This is an **independently read review of live Reader texts**, not a
claimed execution of the complete `NoveltyIndex` embeddings pipeline. The
connected Reader source and the local staged SQLite database were not
mounted together, and no macOS Ollama/GearShift model was accessible. A
genuine local embedding confusion matrix remains unavailable.

## Sampling and labels

From the latest 500 Reader Feed metadata rows, a conservative surface-title
overlap prefilter identified 31 *pairs*. A deliberately diverse subset of 14
pairs (28 full-body document fetches) was manually reviewed from original
Reader content. Selected pairs are **not random** and results MUST NOT be
represented as a production false-positive rate, recall, or monetary savings.

| Manual reviewed pair class | Pairs | Interpretation |
| --- | ---: | --- |
| byte-identical article bodies | 6 | eligible to reuse a *completed, same-policy* translation if content and title versions agree; original source retained |
| editorial paraphrases, same reported event and no substantive distinct claim found | 1 | semantic overlap for **manual candidate only** |
| overlapping reports with differing details, attribution, or additional commentary | 4 | preserve both, do not infer complete claim coverage |
| same headline template about a different locality with different temperatures | 1 | clear nonduplicate |
| article-title/body extraction mismatch or cross-linked unrelated body | 2 | extraction-quality review, not duplicate elimination |
| **Total** | **14** | |

Observed categories (public-news descriptions only; no Reader ID/source text exported):
- Six repeated syndicated Chinese articles had exactly equal extracted bodies,
  including pieces about old-furniture collection, a typhoon forecast, policy
  support for specialized small businesses and national railway investments.
- Two similarly titled reports about a rejected cross-strait cultural visit
  described materially the same single news event with minor editorial changes;
  no different substantive claim was identified in this review. This is NOT a
  verified equivalence for automated reuse.
- A Spanish weather template for **Culiacán** versus **Puebla de Zaragoza**
  described 37°C versus 22°C highs. Similar wording is NOT an article duplicate.
- Reports on a state-company property auction, a presidential press confrontation,
  an election controversy and a concert-ticket rumour overlapped in headline
  but differed in accompanying details or commentary. They were *not*
  certified for full-document omission.
- Two football headlines reported a team's progression, while one extracted
  body described a different earlier referee controversy and first-leg result.
  The suspect extraction must be held/reviewed, not treated as reliable
  semantic evidence.

## Observable offline proxy (NOT actual v5 embedding decisions)

All 14 hand-reviewed pairs were surfaced by the intentionally permissive
title-overlap *exploration* prefilter. If one had naively eliminated all
14 pairs, **7/14 pair eliminations** would have lacked evidence of complete
document equivalence (four partial reports, one locality counterexample, two
source-quality failures). This is an intentionally selected audit subset and
**not a statistical 50% error-rate estimate**.

The new conservative v5 headline identity screen only admits the seven
**same-headline** pairs into later semantic candidate evaluation (six exact
bodies and one editorial paraphrase). Its rule excludes the seven reviewed
changed-headline pairs, including the different-city weather documents.
This is a deterministic title-screen comparison, **not proof** that the
full `NoveltyIndex` would have produced those seven candidates. Full local
embeddings, marker checks and claim coverage may reject some.

## Code action and next quantitative gate

`scripts/reader_novelty.py` v5:
- Unicode-normalized headline identity required before suggesting semantic
  reuse; varied named locations/actors and similar topical templates stay
  distinct. Exact-body identity continues to require a matching headline.
- Only an independently indexed `novel` original can be used as a
  semantic-neighbor source: an advisory `redundancy_candidate` cannot
  recursively justify another candidate.
- Preexisting number/quotation/negation/attribution and claim-coverage guards
  are retained; every output stays advisory.

`scripts/reader_novelty_scan.py --dry-run`:
- Removes DeepSeek/Gemini/OpenAI keys from the subprocess environment,
  denies nonlocal TCP egress, and admits only `127.0.0.1:11434` /
  `::1:11434` to the existing local Ollama embedding route.
- **This guard has synthetic tests but was not used against a live Mac mini.**
  It is process-scoped defense-in-depth, not a systemwide sandbox.
- Scanner continues to use only already staged private files and local
  Ollama inference. It does not write to Reader, Drive, the translation
  pipeline, or any paid API.

Example operator command on the Mac with a **preinstalled** local embedding
model (no added cloud service):

```sh
python3 -m scripts.reader_novelty_scan \
  --work "$HOME/.abraham-reader-stage" \
  --model EXISTING_MULTILINGUAL_EMBEDDING_MODEL \
  --amando-root /Users/yoon/AMANDO \
  --limit 100 \
  --dry-run
```

Run `python3 -m scripts.reader_novelty_eval --db ... --labels ... --model ...`
with at least 50 independently reviewed *real* stage-key labels to calculate
candidate precision, recall and high-risk false-skip counts. Pair-level
provenance, model-version drift, source extraction errors and missing bodies
must all be verified before authorizing any DeepSeek suppression.

**No automatic document elimination is enabled.** This PR remains draft,
with no production rollout and no changes to central GearShift.
