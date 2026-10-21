"""Synthetic-only tests of the independent human-gold audit tool."""
import json
import sqlite3

import pytest

from scripts.reader_novelty import NoveltyIndex
from scripts.reader_novelty_eval import evaluate
import numpy as np


FIRST = ("Researchers documented the energy consumption and thermal reliability "
         "of the new industrial computing system.")
REPHRASE = ("The new industrial computing system was documented by researchers "
            "for its energy consumption and thermal reliability.")


class FakeEmbed:
    def __call__(self, texts):
        result = []
        for text in texts:
            vector = np.zeros(16, dtype=float)
            vector[0] = 1
            result.append(vector.tolist())
        return result


def _make(tmp_path):
    db_path = tmp_path / "novelty.sqlite"
    db = sqlite3.connect(db_path)
    idx = NoveltyIndex(db, model_id="test-embedding")
    e = FakeEmbed()
    idx.assess("source", "Computing", FIRST, e)
    idx.assess("candidate", "Computing", REPHRASE, e)
    db.close()
    return db_path


def _labels(tmp_path, rows):
    path = tmp_path / "gold.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


def test_false_skip_is_visible_but_never_authorizes_paid_suppression(tmp_path):
    path = _make(tmp_path)
    gold = _labels(tmp_path, [
        {"document_key": "candidate", "expected": "novel", "risk": "high"},
        {"document_key": "source", "expected": "novel", "risk": "normal"},
    ])
    result = evaluate(path, gold, model_id="test-embedding")
    assert result["gold_items"] == 2
    assert result["candidate_positive"] == 1
    assert result["false_skip_candidates"] == 1
    assert result["high_risk_false_skip_candidates"] == 1
    assert result["automatic_deepseek_skip_authorized"] is False
    assert result["independent_gold_minimum_met"] is False


def test_redundancy_candidate_is_measured_without_assuming_full_recall(tmp_path):
    path = _make(tmp_path)
    gold = _labels(tmp_path, [
        {"document_key": "candidate", "expected": "redundant", "risk": "normal"},
        {"document_key": "source", "expected": "redundant", "risk": "normal"},
    ])
    result = evaluate(path, gold, model_id="test-embedding")
    assert result["true_redundancy_candidates"] == 1
    assert result["redundancy_candidate_recall"] == 0.5
    assert result["candidate_precision"] == 1.0
    assert result["automatic_deepseek_skip_authorized"] is False


def test_stale_dependency_is_excluded_from_quality_denominator(tmp_path):
    path = _make(tmp_path)
    db = sqlite3.connect(path)
    idx = NoveltyIndex(db, model_id="test-embedding")
    idx.assess("source", "Computing revised", FIRST, FakeEmbed())
    db.close()
    gold = _labels(tmp_path, [
        {"document_key": "candidate", "expected": "novel", "risk": "high"},
    ])
    result = evaluate(path, gold, model_id="test-embedding")
    assert result["missing_or_stale"] == 1
    assert result["candidate_positive"] == 0
    assert result["scoreable"] == 0


@pytest.mark.parametrize("rows", [
    [{"document_key": "a", "expected": "maybe", "risk": "normal"}],
    [{"document_key": "a", "expected": "novel", "risk": "normal"},
     {"document_key": "a", "expected": "novel", "risk": "normal"}],
    [{"document_key": "a", "expected": "novel", "risk": "normal", "body": "private"}],
])
def test_invalid_or_duplicate_labels_are_rejected(tmp_path, rows):
    path = _make(tmp_path)
    gold = _labels(tmp_path, rows)
    with pytest.raises(ValueError, match="INVALID_GOLD_ITEM|INVALID_GOLD_FIELDS"):
        evaluate(path, gold, model_id="test-embedding")



def test_independent_gold_excludes_candidates_with_reclassified_origin(tmp_path):
    db_path = _make(tmp_path)
    db = sqlite3.connect(db_path)
    db.execute("UPDATE novelty_v1 SET decision='review_required' WHERE document_key='source'")
    db.commit()
    db.close()
    labels = _labels(tmp_path, [
        {"document_key": "candidate", "expected": "novel", "risk": "high"},
    ])
    result = evaluate(db_path, labels, model_id="test-embedding")
    assert result["missing_or_stale"] == 1
    assert result["candidate_positive"] == 0
    assert result["scoreable"] == 0
