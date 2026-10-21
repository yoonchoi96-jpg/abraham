# Reader novelty retest: independent real-source validation, 2026-10-21

## What actually ran

1. Verified the existing draft PR #20 at head `f55e3d9155185831efe76106401d0e580f533047`.
2. Read the actual GitHub Actions job log for run 39560466310:
   - 11 baseline unit tests passed.
   - 91 Reader/color regression tests passed.
   - >3GiB compressed synthetic ZIP validation passed, archive_bytes=3225070197.
   - These are real repository tests on synthetic data, not actual Ollama
     evaluations of private Reader originals.
3. Retrieved a separate older Reader RSS metadata window of 600 entries,
   downstream of the earlier 500-sample window (boundary overlap of one
   item). A permissive normalized-title token-overlap prefilter surfaced
   78 candidate pairs, 49 of which had equal normalized titles. That
   exploration is neither production recall nor model accuracy.
4. Independently fetched and directly compared full Reader Markdown for
   12 **same-title** pairs (24 Reader records), across both repeated RSS
   items and cross-site republications. In this deliberately selected set:
   - **7/12** extracted bodies were byte-identical.
   - **5/12** bodies differed in page banners, attribution, images,
     category labels, source navigation or republisher metadata. These
     reported substantially overlapping events; do not mistake textual
     difference for an independent fact-check or certified equivalence.
   - Named examples included the oil-freight story, SpaceX quarterly
     results, Spotify synthetic-music removal, an Ebola report (identical
     extracts) and a curfew in Paris, Iran nuclear talks, a ceasefire
     statement, BBC cost reductions and a paywall-limited Japan-politics
     article (varying extracts).
5. No human-labeled `NoveltyIndex` scores were collected. The v5/v6
   embedding path requires an installed local Ollama multilingual model
   and the staged private SQLite corpus; neither is reachable from
   this chat's connector-only Reader access.

## Actual risk uncovered in source review

A financial-market Reader article's repeated extracted body contained
`Some content could not be imported from the original document.`
It is byte-identical across copies, but that does NOT prove that the
unimported data and linked charts are themselves identical. A Japan
politics article was also represented only by a short paywall-type
excerpt. Treat those as *incomplete evidence*, not as authorizations
to omit a complete document.

The v5 `exact_duplicate` check originally matched the extracted-body SHA
and title without first checking incomplete-import indicators, and it
could point to a previously held/review-required origin. For v6:
- `incomplete_source_reason` handles explicit strong Reader import
  errors or obvious paywall markers and returns `review_required`.
- Exact-body matching only reuses a **current-policy `novel`** origin,
  blocking chains through unverified or held copies.
- The policy revision invalidates older advisory cache decisions.
- New synthetic regression cases cover identical incomplete fragments,
  paywall previews and a 3-copy verified exact match.

These checks deliberately favor a false negative (translation still
happens) over an accidental deletion or a skipped new fact.

## What did NOT run / operational safeguards

- No DeepSeek API invocation was made. No paid provider was called.
- Reader operations were only `reader_list_documents` and
  `reader_get_document_details`. No deletions, Seen changes or retagging.
- No documents or user-private IDs/body text are committed to GitHub.
- No central AMANDO GearShift code, local Mac Ollama installation,
  production translator, or auto-suppression configuration was modified.
- Reader sample figures **cannot** be reported as v6 precision/recall,
  which remain unknown until a true local indexed corpus run is compared
  to independent human labels.
- A site banner/preview detector is intentionally not comprehensive;
  absence of a marker does not prove a source was fully imported.

Next release gate: run `reader_novelty_scan --dry-run` against
a fully staged local corpus, capture all predicted redundancy candidates
and a stratified sample of noncandidates, independently adjudicate their
full original source bodies, and evaluate false skip and missed redundant
cases with `reader_novelty_eval`. Continue to prohibit actual content
deletion, 9-color tag change or paid API suppression even if the offline
audit happens to pass a small gold set.
