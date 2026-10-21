# Reader 9-color + topic candidate rubric (reconstructed from user directions)

Status: working human-readable candidate policy, **NOT** an independently
human-labelled ground-truth dataset. Adopt only where consistent with
historically verified labels. Do not claim a 50-case human gold set exists.

## Existing canonical parser and downstream gate

`reader_colors.py` defines colors in ascending information/retention order:

| Color key | Korean | Candidate meaning |
|---|---|---|
| white | 하양/흰색 | noise or no substantive informational content |
| red | 빨강 | low-reuse ephemeral updates |
| orange | 주황 | narrow, short-lived information |
| yellow | 노랑 | useful context, examples, basic background |
| green | 초록 | reusable explanations, evidence or concrete cases |
| blue | 파랑 | important structural mechanisms or durable changes |
| navy | 남색 | deep evidence-driven insight useful for consequential decisions |
| purple | 보라 | cornerstone principles, primary research or reusable methodology |
| gold | 금색 | exceptionally rare, high-value original knowledge or foundational work |

Existing Abraham *processing* gate differs from grading: white/red do not
translate/export; orange/yellow/green translate/export without extra rating;
blue/navy/purple/gold translate/export and qualify for rating. Preserve the
exact gates in code.

### Evidence-based decision procedure

1. Read the extracted article body, not merely title, filename, URL, or tag.
2. Identify source, key claims, measurements, mechanisms, time horizon, and
   uncertainty. Separate reporting, analyst opinion, advertising and rumor.
3. Ask whether the content remains useful beyond the next news cycle.
4. Grade conservatively by sustained practical/research value, NOT word count,
   named entities, emotional headlines, or personal interest alone.
5. Favor higher levels only for substantive mechanisms, structural effects,
   original evidence, reusable methods, or rare foundational references.
6. Never force equal distribution of grades. Purple and gold should be rare.
7. Supply a short exact `evidence_quote` from the extracted content with
   a Korean reason; ungrounded quote or unsupported assertion is a hold.
8. A duplicate preserves all source identities. Exact or near-duplicate
   status is separate from grade. Never destroy the originals.
9. Existing canonical user-applied tags take precedence as *existing tags*;
   Drive filename labels and prior model outputs are not independent gold
   labels and must not silently overwrite a confirmed grade.
10. Missing paywalled fulltext, video without transcript, HTML containing
    only ads/navigation, broken encoding, and unreadable/uncertain content
    are **deferred/review_required**, not automatically rated white or red.

Use content relevance only to select topic, not to inflate the grade.

## Initial topic taxonomy (provisional; preserve existing canonical names)

Primary topic: exactly one. Secondary topics: 0–2 when genuinely supported.

- `economics_investing`: equities/financial results, interest rates,
  macroeconomics, FX, bonds, ETFs, commodities and risk.
- `industry_supply_chains`: manufacturing, infrastructure, companies,
  logistics, energy and global supply chains.
- `ai_software`: artificial intelligence, LLMs, agents, software
  development, automation, computing.
- `semiconductors_hardware`: chips, fabs, packaging, memory, electronics,
  data centers and hardware.
- `science_health`: science, original research, medicine and space.
- `cybersecurity`: security vulnerabilities, incidents, privacy, internet
  infrastructure, cryptography.
- `policy_geopolitics`: laws, regulation, foreign affairs, national security,
  political structures and defense.
- `society_environment`: education, social trends, disaster, environment
  and climate.
- `music_audio`: instruments, songwriting, recording, audio tools,
  live music and performance.
- `language_learning`: foreign languages, learning theory, study materials.
- `history_culture`: history, philosophy, humanities, arts, film.
- `travel_lifestyle`: geography, travel, culinary or everyday reference.
- `other`: usable article with no fitting category.

These keys are a **new proposed interoperability taxonomy**, not an
authenticated historical list. If an existing Reader/Obsidian taxonomy is
found, map to its established names instead of introducing duplicates.

## Strict status separation

- `classified_candidate`: locally inferred value with source-backed quote.
- `review_required`: no supported/consistent candidate or conflicting tags.
- `deferred_no_fulltext`: cannot obtain usable body.
- `existing_tag_unverified`: color label observed in filename or metadata;
  retained without falsely asserting independent human review.
- `classified_verified`: only after explicit agreed validation process,
  evidence/grounding and durable readback verification.
- `duplicate`: independent exact/near source association, never deletion.

The 50-case evaluation dataset must use genuinely independent,
blinded, **human-confirmed** labels and hashes; historical AI-produced labels
are examples only. No hardcoded score thresholds and no invented probabilities.

## Historical example references (examples, NOT evaluation gold)

Google Drive: `Reader-9color-ledger-20261020-1719-KST`

`https://docs.google.com/document/d/1AfhId1qcbD6jxJmUjBqe5DOPFERZD_OUq8iC6xEkQdQ/edit`

- National security structural change, AI packaging bottlenecks, French bond
  spreads under political unrest: previously tagged **blue**.
- AI data-center cooling business plan: previously tagged **green**.
- Maternity leave / returning to work context: previously tagged **yellow**.
- Unsubstantiated console price rumor: previously tagged **orange**.
- Paywalled excerpts without complete body: **deferred without color**.

Do not feed example decisions as both prompts and ground-truth test answers.
