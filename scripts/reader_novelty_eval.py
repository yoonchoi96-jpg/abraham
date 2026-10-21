"""Read-only, source-agnostic evaluation of Abraham novelty *candidates*.

Gold labels must come from independently reviewed original articles, not from
this embedding model. Only keys, expected novelty and risk are read; article
bodies and quotes are never exported. No DeepSeek API, Ollama, or Drive writes.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from urllib.parse import quote

from scripts.reader_novelty import NoveltyIndex, POLICY_REVISION


def evaluate(database: Path, labels: Path, *, model_id: str) -> dict:
    """Count potential false-skip cases; does NOT authorize automatic skips."""
    gold = []
    keys = set()
    with Path(labels).open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"INVALID_GOLD_JSON_LINE_{line_number}") from exc
            if not isinstance(item, dict) or set(item) != {
                "document_key", "expected", "risk"
            }:
                raise ValueError(f"INVALID_GOLD_FIELDS_LINE_{line_number}")
            key = item["document_key"]
            if (not isinstance(key, str) or not key or len(key) > 512
                or key in keys or item["expected"] not in ("novel", "redundant")
                or item["risk"] not in ("normal", "high")):
                raise ValueError(f"INVALID_GOLD_ITEM_LINE_{line_number}")
            keys.add(key)
            gold.append(item)
            if len(gold) > 100000:
                raise ValueError("GOLD_SET_TOO_LARGE")
    if not gold:
        raise ValueError("NONEMPTY_GOLD_SET_REQUIRED")
    if not isinstance(model_id, str) or not model_id or len(model_id) > 256:
        raise ValueError("MODEL_ID_REQUIRED")

    # Restrict all queries to the local SQLite artifact. No schema creation,
    # cache mutation, model inference, API invocation or data export.
    uri = "file:" + quote(str(Path(database).resolve()), safe="/") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    count = {"novel_gold": 0, "redundant_gold": 0,
             "scoreable": 0, "missing_or_stale": 0,
             "held_or_review": 0, "candidate_positive": 0,
             "true_redundancy_candidates": 0,
             "false_skip_candidates": 0,
             "high_risk_false_skip_candidates": 0}
    try:
        for item in gold:
            expected, risk = item["expected"], item["risk"]
            count[expected + "_gold"] += 1
            try:
                row = conn.execute(
                    """SELECT policy_revision,decision,match_key,match_body_sha256
                       FROM novelty_v1 WHERE document_key=? AND model_id=?""",
                    (item["document_key"], model_id)
                ).fetchone()
            except sqlite3.OperationalError as exc:
                raise ValueError("NOVELTY_V3_SCHEMA_REQUIRED") from exc
            if not row or row[0] != POLICY_REVISION:
                count["missing_or_stale"] += 1
                continue
            if row[2]:
                match = conn.execute(
                    """SELECT body_sha256,title FROM novelty_v1
                       WHERE document_key=? AND model_id=?""",
                    (row[2], model_id)
                ).fetchone()
                if (not match or not row[3] or
                    NoveltyIndex._source_signature(*match) != row[3]):
                    count["missing_or_stale"] += 1
                    continue
            count["scoreable"] += 1
            if row[1] in ("review_required",):
                count["held_or_review"] += 1
                continue
            if row[1] in ("redundancy_candidate", "exact_duplicate"):
                count["candidate_positive"] += 1
                if expected == "novel":
                    count["false_skip_candidates"] += 1
                    if risk == "high":
                        count["high_risk_false_skip_candidates"] += 1
                else:
                    count["true_redundancy_candidates"] += 1
    finally:
        conn.close()
    labels_total = len(gold)
    count["gold_items"] = labels_total
    count["coverage_fraction"] = round(count["scoreable"] / labels_total, 4)
    count["candidate_precision"] = (
        round(count["true_redundancy_candidates"] /
              count["candidate_positive"], 4)
        if count["candidate_positive"] else None
    )
    count["redundancy_candidate_recall"] = (
        round(count["true_redundancy_candidates"] /
              count["redundant_gold"], 4)
        if count["redundant_gold"] else None
    )
    count["independent_gold_minimum_met"] = labels_total >= 50
    count["zero_false_skip_candidates"] = count["false_skip_candidates"] == 0
    # Even a perfect small sample cannot prove that semantic skipping is safe.
    count["automatic_deepseek_skip_authorized"] = False
    count["warning"] = "Embedding similarity is not factual entailment; audit source quotes."
    return count


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline novelty false-skip audit")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--labels", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--require-zero-false-skip", action="store_true")
    args = parser.parse_args()
    result = evaluate(args.db, args.labels, model_id=args.model)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    if args.require_zero_false_skip and (
        result["false_skip_candidates"] or
        result["high_risk_false_skip_candidates"] or
        not result["independent_gold_minimum_met"] or
        result["missing_or_stale"]
    ):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
