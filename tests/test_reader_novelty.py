"""Offline synthetic novelty and adaptive allocation tests; no live AI calls."""
import json
import sqlite3
from unittest.mock import Mock, patch

import numpy as np
import pytest

from scripts.reader_local import Held, required_context
from scripts.reader_novelty import NoveltyIndex, critical_markers
from scripts.reader_novelty_scan import GuardedLocalEmbedder, scan
from scripts.reader_drive_stage import DriveLibraryStager


FIRST = ("Researchers documented the energy consumption and thermal reliability "
         "of the new industrial computing system.")
REPHRASE = ("The new industrial computing system was documented by researchers "
            "for its energy consumption and thermal reliability.")
ADDITIONAL = ("New laboratory evidence challenges the projected reliability "
              "and indicates previously unreported technical limitations.")


class FakeEmbed:
    def __init__(self):
        self.count = 0

    def __call__(self, texts):
        self.count += 1
        out = []
        for text in texts:
            v = np.zeros(16, dtype=float)
            if "New laboratory evidence" in text and "\n" not in text:
                v[1] = 1
            elif "unrelated fashion" in text:
                v[2] = 1
            else:
                v[0] = 1
            out.append(v.tolist())
        return out


def test_exact_repeat_avoids_embedding_and_persists(tmp_path):
    db = sqlite3.connect(tmp_path/"n.db")
    idx = NoveltyIndex(db, model_id="local-embed-v1")
    embed = FakeEmbed()
    first = idx.assess("a", "Energy systems", FIRST, embed)
    again = idx.assess("a", "Energy systems", FIRST, embed)
    duplicate = idx.assess("b", "Energy systems", FIRST, embed)
    assert first["decision"] == "novel"
    assert again == first
    assert duplicate["decision"] == "exact_duplicate"
    assert duplicate["match_key"] == "a"
    assert embed.count == 1
    db.close()


def test_similar_topic_without_new_claim_is_only_candidate(tmp_path):
    db = sqlite3.connect(tmp_path/"n.db")
    idx = NoveltyIndex(db, model_id="local-embed-v1")
    e = FakeEmbed()
    idx.assess("a", "Computing", FIRST, e)
    result = idx.assess("b", "Computing", REPHRASE, e)
    assert result["decision"] == "redundancy_candidate"
    assert result["coverage"] == 1.0
    # Not a fully approved skip or a completed translation.
    assert "verified" not in result
    db.close()


def test_same_topic_new_fact_is_not_suppressed(tmp_path):
    db = sqlite3.connect(tmp_path/"n.db")
    idx = NoveltyIndex(db, model_id="local-embed-v1")
    e = FakeEmbed()
    idx.assess("a", "Computing", FIRST, e)
    b = idx.assess("b", "Computing", REPHRASE + " " + ADDITIONAL, e)
    assert b["decision"] == "novel"
    assert b["coverage"] < 1
    db.close()


def test_changed_numbers_or_quote_prevents_omission(tmp_path):
    db = sqlite3.connect(tmp_path/"n.db")
    idx = NoveltyIndex(db, model_id="local-embed-v1")
    e = FakeEmbed()
    idx.assess("a", "Forecast", FIRST + " Measured at 300 watts.", e)
    b = idx.assess("b", "Forecast", REPHRASE + " Measured at 350 watts.", e)
    assert b["decision"] == "novel"
    assert b["reason"] == "DISTINCT_NUMBERS_QUOTES_OR_NEGATIONS"
    assert critical_markers('Firm "raises earnings"') != critical_markers('Firm "cuts earnings"')
    db.close()


def test_model_revisions_are_separate_and_failed_inference_not_stored(tmp_path):
    db = sqlite3.connect(tmp_path/"n.db")
    a = NoveltyIndex(db, model_id="v1")
    a.assess("x", "Company", FIRST, FakeEmbed())
    other = NoveltyIndex(db, model_id="v2")
    assert other.counts() == {}
    with pytest.raises(ValueError):
        other.assess("y", "Company", FIRST, lambda x: [[0] * 16])
    assert other.counts() == {}
    db.close()


def test_long_document_review_instead_of_unsafe_skip(tmp_path):
    db = sqlite3.connect(tmp_path/"n.db")
    ix = NoveltyIndex(db, model_id="v1")
    r = ix.assess("a", "Long report", "A" * 40001, FakeEmbed())
    assert r["decision"] == "review_required"
    assert r["reason"] == "LONG_DOCUMENT_NOT_COMPARED"
    db.close()


def test_dynamic_context_releases_unneeded_kv_budget():
    assert required_context("Short Reader item", 128, 4096) == 512
    assert required_context("x" * 1300, 128, 4096) < 4096
    assert required_context("x" * 3700, 256, 4096) == 4096
    with pytest.raises(Held, match="PROMPT_EXCEEDS_CONTEXT"):
        required_context("x" * 4000, 256, 4096)


def test_local_provider_checks_gearshift_and_never_downloads(tmp_path):
    class Http:
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def read(self): return json.dumps({"embeddings": [[1.0] * 16]}).encode()
    gear = Mock()
    with patch("scripts.reader_novelty_scan.GearShift", return_value=gear), \
         patch("scripts.reader_novelty_scan.budget_for", return_value={"required_increment_bytes": 5}) as budget, \
         patch("scripts.reader_novelty_scan.urllib.request.urlopen", return_value=Http()) as req:
        e = GuardedLocalEmbedder("installed-multilingual", str(tmp_path))
        assert len(e(["test document"])[0]) == 16
        assert budget.call_args.args[2] == 512
        assert gear.check.call_count == 2
        payload = json.loads(req.call_args.args[0].data)
        assert payload["keep_alive"] == 0 and payload["truncate"] is False


def test_scan_works_on_private_staged_raw_without_mutating_source(tmp_path):
    work = tmp_path/"private"
    stager = DriveLibraryStager(work)
    source = work/"fixed.source"
    content = ("<html><body><article><p>" + FIRST * 5 + "</p></article></body></html>").encode()
    source.write_bytes(content)
    stager.process("fixed", {"id": "drive1", "name": "article.html", "version": "1"}, source)
    stager.close()
    bulk = sqlite3.connect(work/"bulk.sqlite")
    bulk.execute("CREATE TABLE jobs(key TEXT PRIMARY KEY, metadata TEXT)")
    bulk.execute("INSERT INTO jobs VALUES(?,?)", ("fixed", json.dumps(
        {"id": "drive1", "name": "article.html", "version": "1"})))
    bulk.commit()
    bulk.close()
    result = scan(work, model="test-local", embed=FakeEmbed(), max_items=5)
    assert result["evaluated"] == 1 and result["deepseek_api_calls"] == 0
    assert result["automatic_api_suppression"] is False
    assert source.read_bytes() == content
    rerun = scan(work, model="test-local", embed=FakeEmbed(), max_items=5)
    assert rerun["evaluated"] == 0
