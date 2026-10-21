"""Offline synthetic novelty and adaptive allocation tests; no live AI calls."""
import json
import sqlite3
from unittest.mock import Mock, patch

import numpy as np
import pytest

from scripts.reader_local import Held, required_context
from scripts.reader_novelty import NoveltyIndex, critical_markers, normalized_embedding
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


def test_revised_document_invalidates_stale_novelty_cache(tmp_path):
    db = sqlite3.connect(tmp_path / "revision.sqlite")
    idx = NoveltyIndex(db, model_id="local-embed-v1")
    embed = FakeEmbed()
    original = idx.assess("stable-drive-id", "Original title", FIRST, embed)
    assert original["decision"] == "novel"
    # Same Drive identity, newly appended evidence: old advisory judgment
    # MUST NOT be reused, even when the semantic model still clusters it.
    amended = FIRST + " " + ADDITIONAL
    revised = idx.assess("stable-drive-id", "Original title", amended, embed)
    assert revised["decision"] == "novel"
    assert embed.count == 2
    # Exact-body matching must not return the old version of the same ID.
    title_changed = idx.assess("stable-drive-id", "Revised title", amended, embed)
    assert title_changed["decision"] == "novel"
    assert embed.count == 3
    # Other IDs can now safely discover the updated complete body.
    duplicate = idx.assess("other-drive-id", "Revised title", amended, embed)
    assert duplicate["decision"] == "exact_duplicate"
    assert duplicate["match_key"] == "stable-drive-id"
    db.close()


def test_failed_revision_does_not_discard_previous_index(tmp_path):
    db = sqlite3.connect(tmp_path / "rollback.sqlite")
    idx = NoveltyIndex(db, model_id="local-embed-v1")
    embed = FakeEmbed()
    first = idx.assess("doc", "Original", FIRST, embed)
    with pytest.raises(ValueError, match="ZERO_EMBEDDING"):
        idx.assess("doc", "Revised", FIRST + " " + ADDITIONAL,
                   lambda texts: [[0.0] * 16 for _ in texts])
    assert idx.assess("doc", "Original", FIRST, embed) == first
    db.close()


def test_long_multilingual_embedding_preserves_every_byte_in_bounded_batches(tmp_path):
    from scripts.reader_novelty_scan import (
        EMBED_BATCH_BYTES, EMBED_CHUNK_BYTES, GuardedLocalEmbedder,
    )
    article = "한국어 evidence 文书. " * 480
    payloads = []

    class Http:
        def __init__(self, count):
            self.count = count
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return False
        def read(self):
            return json.dumps({"embeddings": [[1.0] * 16] * self.count}).encode()

    def fake_urlopen(request, timeout):
        payload = json.loads(request.data)
        payloads.append(payload)
        return Http(len(payload["input"]))

    gear = Mock()
    with patch("scripts.reader_novelty_scan.GearShift", return_value=gear), \
         patch("scripts.reader_novelty_scan.budget_for", return_value={"required_increment_bytes": 5}), \
         patch("scripts.reader_novelty_scan.urllib.request.urlopen", side_effect=fake_urlopen):
        e = GuardedLocalEmbedder("existing-model", str(tmp_path))
        vectors = e([article])
        assert len(vectors) == 1 and len(vectors[0]) == 16
        assert e.last_call_stats["truncated_bytes"] == 0
        assert e.last_call_stats["input_bytes"] == len(article.encode("utf-8"))
        assert e.last_call_stats["chunks"] > 1
        assert e.last_call_stats["local_requests"] == len(payloads)
        assert gear.check.call_count == len(payloads) * 2
    parts = [part for p in payloads for part in p["input"]]
    assert "".join(parts) == article
    assert max(len(part.encode("utf-8")) for part in parts) <= EMBED_CHUNK_BYTES
    assert all(sum(len(part.encode("utf-8")) for part in p["input"]) <= EMBED_BATCH_BYTES
               for p in payloads)
    assert all(p["truncate"] is False and p["keep_alive"] == 0 for p in payloads)


def test_unbounded_embedding_request_is_held_not_truncated(tmp_path):
    from scripts.reader_novelty_scan import GuardedLocalEmbedder
    with patch("scripts.reader_novelty_scan.GearShift", return_value=Mock()), \
         patch("scripts.reader_novelty_scan.urllib.request.urlopen") as urlopen:
        embed = GuardedLocalEmbedder("existing-model", str(tmp_path))
        with pytest.raises(Held, match="EMBEDDING_CHUNK_BUDGET_EXCEEDED"):
            embed(["a" * 400000])
        urlopen.assert_not_called()


def test_scanner_revisits_mutated_source_without_paid_api(tmp_path):
    work = tmp_path / "private"
    stager = DriveLibraryStager(work)
    source = work / "stable.source"
    first_html = ("<html><body><article><p>" + FIRST * 5 +
                  "</p></article></body></html>").encode()
    source.write_bytes(first_html)
    stager.process("stable", {"id": "drive1", "name": "article.html", "version": "1"}, source)
    stager.close()
    bulk = sqlite3.connect(work / "bulk.sqlite")
    bulk.execute("CREATE TABLE jobs(key TEXT PRIMARY KEY, metadata TEXT)")
    bulk.execute("INSERT INTO jobs VALUES(?,?)",
                 ("stable", json.dumps({"id": "drive1", "name": "article.html", "version": "1"})))
    bulk.commit()
    bulk.close()
    assert scan(work, model="test-local", embed=FakeEmbed(), max_items=5)["evaluated"] == 1
    assert scan(work, model="test-local", embed=FakeEmbed(), max_items=5)["evaluated"] == 0
    # Same key, amended body, even though staging metadata still has version 1.
    source.write_bytes(("<html><body><article><p>" + FIRST * 5 + " " +
                        ADDITIONAL + "</p></article></body></html>").encode())
    rerun = scan(work, model="test-local", embed=FakeEmbed(), max_items=5)
    assert rerun["evaluated"] == 1
    assert rerun["automatic_api_suppression"] is False
    db = sqlite3.connect(work / "reader-novelty.sqlite")
    row = db.execute("SELECT body_sha256 FROM novelty_v1 WHERE document_key='stable'").fetchone()
    assert row and len(row[0]) == 64
    db.close()


def test_rollback_disables_chunked_embeddings_without_truncating(tmp_path, monkeypatch):
    from scripts.reader_novelty_scan import GuardedLocalEmbedder
    monkeypatch.setenv("ABRAHAM_NOVELTY_CHUNKED_EMBEDDINGS", "0")
    with patch("scripts.reader_novelty_scan.GearShift", return_value=Mock()), \
         patch("scripts.reader_novelty_scan.urllib.request.urlopen") as urlopen:
        embed = GuardedLocalEmbedder("existing-model", str(tmp_path))
        with pytest.raises(Held, match="CHUNKED_EMBEDDINGS_DISABLED"):
            embed(["한국어 " * 2000])
        urlopen.assert_not_called()


def test_secondary_match_can_prove_candidate_without_using_bad_first_hit(tmp_path):
    db = sqlite3.connect(tmp_path / "topk.sqlite")
    idx = NoveltyIndex(db, model_id="local-embed-v1")
    embed = FakeEmbed()
    idx.assess("a", "Computing", FIRST + " Measured at 300 watts.", embed)
    idx.assess("c", "Computing", FIRST, embed)
    found = idx.assess("d", "Computing", REPHRASE, embed)
    assert found["decision"] == "redundancy_candidate"
    assert found["match_key"] == "c"
    assert idx.is_current("d")
    db.close()


def test_changed_headline_fact_prevents_exact_body_duplicate(tmp_path):
    db = sqlite3.connect(tmp_path / "headline.sqlite")
    idx = NoveltyIndex(db, model_id="local-embed-v1")
    embed = FakeEmbed()
    idx.assess("a", "Forecast revised to 2025", FIRST, embed)
    found = idx.assess("b", "Forecast revised to 2026", FIRST, embed)
    assert found["decision"] == "novel"
    # Title-indexed lookup never compares different-headline bodies.
    assert found["reason"] == "NO_EQUIVALENT_EVIDENCE"
    assert found["match_key"] is None
    db.close()


def test_dependent_candidate_is_stale_when_original_title_changes(tmp_path):
    db = sqlite3.connect(tmp_path / "dependent.sqlite")
    idx = NoveltyIndex(db, model_id="local-embed-v1")
    embed = FakeEmbed()
    idx.assess("a", "Computing", FIRST, embed)
    candidate = idx.assess("b", "Computing", REPHRASE, embed)
    assert candidate["decision"] == "redundancy_candidate"
    assert candidate["match_key"] == "a"
    assert idx.is_current("b") is True
    idx.assess("a", "Computing updated headline", FIRST, embed)
    assert idx.is_current("b") is False
    old_count = embed.count
    idx.assess("b", "Computing", REPHRASE, embed)
    assert embed.count > old_count
    assert idx.is_current("b") is True
    db.close()


def test_legacy_policy_row_requires_reassessment(tmp_path):
    db = sqlite3.connect(tmp_path / "legacy.sqlite")
    idx = NoveltyIndex(db, model_id="local-embed-v1")
    embed = FakeEmbed()
    idx.assess("a", "Computing", FIRST, embed)
    with db:
        db.execute("UPDATE novelty_v1 SET policy_revision='old' WHERE document_key='a'")
    assert idx.is_current("a") is False
    before = embed.count
    result = idx.assess("a", "Computing", FIRST, embed)
    assert result["decision"] == "novel"
    assert embed.count > before
    db.close()


def test_explicit_retry_clears_held_marker_after_success(tmp_path):
    work = tmp_path / "private"
    stager = DriveLibraryStager(work)
    source = work / "fixed.source"
    source.write_bytes(("<html><body><article><p>" + FIRST * 4 +
                        "</p></article></body></html>").encode())
    stager.process("fixed", {"id": "drive1", "name": "article.html", "version": "1"}, source)
    stager.close()
    bulk = sqlite3.connect(work / "bulk.sqlite")
    bulk.execute("CREATE TABLE jobs(key TEXT PRIMARY KEY, metadata TEXT)")
    bulk.execute("INSERT INTO jobs VALUES(?,?)", ("fixed", json.dumps(
        {"id": "drive1", "name": "article.html", "version": "1"})))
    bulk.commit()
    bulk.close()
    failed = scan(work, model="installed-local", embed=lambda texts: (_ for _ in ()).throw(Held("NO_MODEL")), max_items=1)
    assert failed["held"] == 1
    assert scan(work, model="installed-local", embed=FakeEmbed(), max_items=1)["evaluated"] == 0
    success = scan(work, model="installed-local", embed=FakeEmbed(), max_items=1, retry_held=True)
    assert success["evaluated"] == 1 and success["retried_held"] is True
    assert scan(work, model="installed-local", embed=FakeEmbed(), max_items=1)["evaluated"] == 0


def test_evidence_cards_quote_real_source_and_detect_staleness(tmp_path):
    db = sqlite3.connect(tmp_path / "cards.sqlite")
    idx = NoveltyIndex(db, model_id="local-embed-v1")
    embed = FakeEmbed()
    idx.assess("origin", "Computing", FIRST, embed)
    choice = idx.assess("candidate", "Computing", REPHRASE, embed)
    assert choice["decision"] == "redundancy_candidate"
    cards = idx.evidence_cards("candidate", embed)
    assert cards["state"] == "review_candidate_only"
    assert cards["inference"] == "local_embedding_similarity_not_factual_entailment"
    assert cards["cards"]
    assert all(c["source_quote"] in FIRST and c["source_document_key"] == "origin"
               for c in cards["cards"])
    assert cards["cards"][0]["candidate_match"]
    idx.assess("origin", "Computing", FIRST + " New detail at 2026.", embed)
    assert idx.evidence_cards("candidate", embed) == {
        "state": "stale_or_missing", "cards": [],
    }
    db.close()


def test_changed_company_name_blocks_semantic_false_skip(tmp_path):
    db = sqlite3.connect(tmp_path / "entities.sqlite")
    idx = NoveltyIndex(db, model_id="local-embed-v1")
    emb = FakeEmbed()
    original = ("Apple announced a new enterprise infrastructure agreement "
                "after an independent review of regulatory filings.")
    altered = ("Microsoft announced a new enterprise infrastructure agreement "
               "after an independent review of regulatory filings.")
    idx.assess("apple", "Technology agreement", original, emb)
    result = idx.assess("microsoft", "Technology agreement", altered, emb)
    assert result["decision"] == "novel"
    assert result["reason"] == "DISTINCT_NUMBERS_QUOTES_OR_NEGATIONS"
    assert "entity:apple" in critical_markers(original)
    assert "entity:microsoft" in critical_markers(altered)
    db.close()


def test_uncertainty_cues_are_preserved_in_semantic_candidates(tmp_path):
    db = sqlite3.connect(tmp_path / "attribution.sqlite")
    idx = NoveltyIndex(db, model_id="local-embed-v1")
    emb = FakeEmbed()
    first = ("Officials confirmed the factory will close after the next "
             "operational inspection in the region.")
    uncertain = ("Officials reportedly confirmed the factory will close after "
                 "the next operational inspection in the region.")
    idx.assess("certain", "Factory decision", first, emb)
    result = idx.assess("uncertain", "Factory decision", uncertain, emb)
    assert result["decision"] == "novel"
    assert "attribution:reportedly" in critical_markers(uncertain)
    assert "negation:not" not in critical_markers("The notable findings were published.")
    db.close()


def test_oversized_headline_is_held_for_review_not_exact_duplicate(tmp_path):
    db = sqlite3.connect(tmp_path / "longtitle.sqlite")
    idx = NoveltyIndex(db, model_id="local-embed-v1")
    embed = FakeEmbed()
    first = idx.assess("a", "A" * 600, FIRST, embed)
    repeated = idx.assess("b", "A" * 599 + "B", FIRST, embed)
    assert first["decision"] == "review_required"
    assert repeated["decision"] == "review_required"
    assert repeated["reason"] == "LONG_HEADLINE_NOT_COMPARED"
    assert repeated["match_key"] is None
    db.close()


def test_same_template_different_city_is_not_semantic_duplicate(tmp_path):
    db = sqlite3.connect(tmp_path / "weather.sqlite")
    idx = NoveltyIndex(db, model_id="local-embed-v1")
    embed = FakeEmbed()
    boilerplate = ("The local forecast reports cloudy periods and mild "
                   "winds throughout the day in this region.")
    first = idx.assess(
        "city-a", "Clima hoy en Culiacán México miércoles",
        boilerplate, embed,
    )
    second = idx.assess(
        "city-b", "Clima hoy en Puebla de Zaragoza México miércoles",
        boilerplate, embed,
    )
    assert first["decision"] == "novel"
    assert second["decision"] == "novel"
    assert second["match_key"] is None
    db.close()


def test_renamed_headline_does_not_authorize_new_fact_collapse(tmp_path):
    db = sqlite3.connect(tmp_path / "actors.sqlite")
    idx = NoveltyIndex(db, model_id="local-embed-v1")
    embed = FakeEmbed()
    idx.assess("first", "Company confirms new review",
               FIRST, embed)
    second = idx.assess("second", "Company denies new review",
                        REPHRASE, embed)
    assert second["decision"] == "novel"
    assert second["match_key"] is None
    db.close()


def test_semantic_chains_do_not_use_unverified_candidate_as_reference(tmp_path):
    db = sqlite3.connect(tmp_path / "chains.sqlite")
    idx = NoveltyIndex(db, model_id="local-embed-v1")
    e = FakeEmbed()
    idx.assess("source", "Computing", FIRST, e)
    idx.assess("candidate", "Computing", REPHRASE, e)
    assert idx._top_matches(normalized_embedding(e([FIRST])[0]), exclude="source") == []
    matches = idx._top_matches(normalized_embedding(e([FIRST])[0]), exclude="different")
    assert [row[1] for row in matches] == ["source"]
    db.close()


def test_dry_run_denies_remote_egress_and_erases_provider_secrets(monkeypatch):
    import os
    import socket
    from scripts.reader_novelty_scan import enforce_offline_dry_run

    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-do-not-use")
    monkeypatch.setenv("GEMINI_API_KEY", "synthetic-do-not-use")
    def local_stub(self, address):
        return ("allowed", address)
    monkeypatch.setattr(socket.socket, "connect", local_stub)
    enforce_offline_dry_run()
    assert "DEEPSEEK_API_KEY" not in os.environ
    assert "GEMINI_API_KEY" not in os.environ
    sock = socket.socket()
    try:
        assert sock.connect(("127.0.0.1", 11434)) == (
            "allowed", ("127.0.0.1", 11434)
        )
        with pytest.raises(Held, match="DRY_RUN_REMOTE_EGRESS_FORBIDDEN"):
            sock.connect(("api.deepseek.com", 443))
        with pytest.raises(Held, match="DRY_RUN_REMOTE_EGRESS_FORBIDDEN"):
            sock.connect(("127.0.0.1", 443))
    finally:
        sock.close()


def test_reader_partial_import_is_never_exact_duplicate(tmp_path):
    """An identical extracted fragment does not prove complete article identity."""
    db = sqlite3.connect(tmp_path / "partial-import.sqlite")
    idx = NoveltyIndex(db, model_id="local-embed-v1")
    fragment = (
        "A publisher's substantial article paragraph about advanced "
        "industrial manufacturing processes and current procurement."
        "\n\nSome content could not be imported from the original document."
    )
    first = idx.assess("origin", "Manufacturing", fragment, FakeEmbed())
    second = idx.assess("copy", "Manufacturing", fragment, FakeEmbed())
    assert first["decision"] == "review_required"
    assert second["decision"] == "review_required"
    assert first["reason"] == second["reason"] == "INCOMPLETE_SOURCE_CONTENT"
    assert second["match_key"] is None
    assert idx.evidence_cards("copy", FakeEmbed())["state"] == "not_redundancy_candidate"
    db.close()


def test_identical_paywall_previews_remain_review_only(tmp_path):
    db = sqlite3.connect(tmp_path / "preview.sqlite")
    idx = NoveltyIndex(db, model_id="local-embed-v1")
    teaser = ("The article examines five difficult choices facing the leader "
              "after a year of economic uncertainty. "
              "Subscribe to continue reading.")
    a = idx.assess("a", "Five challenges for the leader", teaser, FakeEmbed())
    b = idx.assess("b", "Five challenges for the leader", teaser, FakeEmbed())
    assert a["decision"] == "review_required"
    assert b["decision"] == "review_required"
    assert b["match_key"] is None
    db.close()


def test_valid_duplicate_does_not_chain_via_duplicate_source(tmp_path):
    db = sqlite3.connect(tmp_path / "chain.sqlite")
    idx = NoveltyIndex(db, model_id="local-embed-v1")
    embed = FakeEmbed()
    idx.assess("original", "Manufacturing", FIRST, embed)
    d2 = idx.assess("duplicate", "Manufacturing", FIRST, embed)
    d3 = idx.assess("third", "Manufacturing", FIRST, embed)
    assert d2["decision"] == d3["decision"] == "exact_duplicate"
    assert d3["match_key"] == "original"
    db.close()



def test_title_only_reader_excerpts_never_authorize_reuse(tmp_path):
    db = sqlite3.connect(tmp_path / "tiny-excerpt.sqlite")
    ix = NoveltyIndex(db, model_id="fake-embed")
    emb = FakeEmbed()
    first = ix.assess("t1", "Company announces change", "Company announces change", emb)
    repeat = ix.assess("t2", "Company announces change", "Company announces change", emb)
    assert first["decision"] == repeat["decision"] == "review_required"
    assert repeat["reason"] == "INSUFFICIENT_EXTRACTED_BODY"
    assert repeat["match_key"] is None
    assert emb.count == 0
    db.close()


def test_image_link_only_article_not_a_confirmed_duplicate(tmp_path):
    db = sqlite3.connect(tmp_path / "image-shell.sqlite")
    ix = NoveltyIndex(db, model_id="fake-embed")
    body = "![Figure](https://example.invalid/img.jpg)" + (
        "[X](https://example.invalid/link)" * 16)
    first = ix.assess("a", "Industrial technologies", body, FakeEmbed())
    second = ix.assess("b", "Industrial technologies", body, FakeEmbed())
    assert first["reason"] == second["reason"] == "INSUFFICIENT_EXTRACTED_BODY"
    assert second["decision"] == "review_required"
    db.close()


def test_four_character_chinese_profit_reversal_not_ignored(tmp_path):
    db = sqlite3.connect(tmp_path / "short-claim.sqlite")
    ix = NoveltyIndex(db, model_id="fake-embed")
    emb = FakeEmbed()
    prior = ix.assess("prior", "Manufacturing", FIRST + "\n利润上涨", emb)
    later = ix.assess("later", "Manufacturing", REPHRASE + "\n利润下跌", emb)
    assert prior["decision"] == later["decision"] == "novel"
    assert later["reason"] == "DISTINCT_SHORT_CLAIM"
    assert later["match_key"] is None
    db.close()


def test_matching_headline_survives_crowded_similar_topic(tmp_path):
    db = sqlite3.connect(tmp_path / "crowded.sqlite")
    ix = NoveltyIndex(db, model_id="fake-embed")
    emb = FakeEmbed()
    ix.assess("z-original", "Computing", FIRST, emb)
    for n in range(7):
        ix.assess(f"a-distractor-{n}", f"Forecast for City {n}", FIRST, emb)
    candidate = ix.assess("new-copy", "Computing", REPHRASE, emb)
    assert candidate["decision"] == "redundancy_candidate"
    assert candidate["match_key"] == "z-original"
    db.close()


def test_source_status_change_invalidates_dependent_even_if_hash_same(tmp_path):
    db = sqlite3.connect(tmp_path / "stale-source-status.sqlite")
    ix = NoveltyIndex(db, model_id="fake-embed")
    ix.assess("source", "Computing", FIRST, FakeEmbed())
    ix.assess("copy", "Computing", FIRST, FakeEmbed())
    assert ix.is_current("copy")
    db.execute("UPDATE novelty_v1 SET decision='review_required' WHERE document_key='source'")
    db.commit()
    assert not ix.is_current("copy")
    assert ix.evidence_cards("copy", FakeEmbed())["state"] == "stale_or_missing"
    db.close()


def test_explicit_no_negation_is_not_missed():
    assert "negation:no" in critical_markers("There is no signed contract.")
    assert "negation:no" not in critical_markers("Researchers noted the contract.")


def test_dry_run_connect_ex_cannot_bypass_paid_provider_guard(monkeypatch):
    import socket
    from scripts.reader_novelty_scan import enforce_offline_dry_run
    monkeypatch.setattr(socket.socket, "connect", lambda self, a: "ok")
    monkeypatch.setattr(socket.socket, "connect_ex", lambda self, a: 0)
    enforce_offline_dry_run()
    sock = socket.socket()
    try:
        assert sock.connect_ex(("127.0.0.1", 11434)) == 0
        with pytest.raises(Held, match="DRY_RUN_REMOTE_EGRESS_FORBIDDEN"):
            sock.connect_ex(("api.deepseek.com", 443))
    finally:
        sock.close()



def test_normalized_punctuation_headline_exact_body_keeps_dedup_fast(tmp_path):
    db = sqlite3.connect(tmp_path / "titles-index.sqlite")
    idx = NoveltyIndex(db, model_id="local-embed-v1")
    embed = FakeEmbed()
    first = idx.assess("one", "Paris—night curfew", FIRST, embed)
    second = idx.assess("two", "Paris - night curfew", FIRST, embed)
    assert first["decision"] == "novel"
    assert second["decision"] == "exact_duplicate"
    assert second["match_key"] == "one"
    assert embed.count == 1
    db.close()


def test_empty_title_never_becomes_an_equivalent_evidence_source(tmp_path):
    db = sqlite3.connect(tmp_path / "notitle.sqlite")
    idx = NoveltyIndex(db, model_id="local-embed-v1")
    a = idx.assess("a", "", FIRST, FakeEmbed())
    b = idx.assess("b", "", FIRST, FakeEmbed())
    assert a["reason"] == b["reason"] == "MISSING_HEADLINE"
    assert b["match_key"] is None
    db.close()


def test_v8_title_index_excludes_other_language_same_topic(tmp_path):
    db = sqlite3.connect(tmp_path / "multilang-retrieval.sqlite")
    idx = NoveltyIndex(db, model_id="local-embed-v1")
    embed = FakeEmbed()
    first = idx.assess("es", "El acuerdo fue confirmado", FIRST, embed)
    second = idx.assess("pt", "O acordo foi confirmado", REPHRASE, embed)
    assert first["decision"] == "novel"
    assert second["decision"] == "novel"
    assert second["match_key"] is None
    assert idx._top_matches(normalized_embedding(embed([FIRST])[0]),
                            exclude="pt", title="O acordo foi confirmado") == []
    plan = db.execute("""EXPLAIN QUERY PLAN
        SELECT document_key,vector FROM novelty_v1
        WHERE model_id=? AND policy_revision=? AND decision='novel'
          AND title_key=? AND vector IS NOT NULL AND document_key<>?""",
        ("local-embed-v1", __import__("scripts.reader_novelty",
                                     fromlist=["POLICY_REVISION"]).POLICY_REVISION,
         "o acordo foi confirmado", "none")).fetchall()
    assert any("novelty_v1_titlekey" in row[3] for row in plan)
    db.close()



def test_review_lsh_recovers_same_event_with_different_language_title(tmp_path):
    db=sqlite3.connect(tmp_path / "cross-lang.sqlite")
    idx=NoveltyIndex(db, model_id="local-review-model")
    e=FakeEmbed()
    first=idx.assess("es", "La empresa informó resultados nuevos", FIRST, e)
    second=idx.assess("ko", "회사가 새로운 실적을 발표했습니다", REPHRASE, e)
    assert first["decision"] == second["decision"] == "novel"
    review=idx.cross_language_review("ko")
    assert review["state"]=="review_only"
    assert review["automatic_api_suppression"] is False
    assert review["semantic_equivalence_proven"] is False
    assert any(row["document_key"] == "es" for row in review["candidates"])
    assert idx.is_current("ko")
    db.close()


def test_review_index_does_not_skip_changed_facts_or_source_mutations(tmp_path):
    db=sqlite3.connect(tmp_path / "review-facts.sqlite")
    idx=NoveltyIndex(db, model_id="local-review-model")
    e=FakeEmbed()
    idx.assess("origin", "Acuerdo de 2026 confirmado", FIRST, e)
    idx.assess("copy", "2026년 계약은 취소됨", REPHRASE, e)
    review=idx.cross_language_review("copy")
    assert any(row["document_key"]=="origin" for row in review["candidates"])
    assert review["automatic_api_suppression"] is False
    idx.assess("origin", "Acuerdo de 2026 confirmado",
               FIRST + " " + ADDITIONAL, e)
    assert idx.cross_language_review("copy")["state"] == "review_only"
    # Source changed but remains a human-review-only candidate.
    idx.assess("origin", "Acuerdo de 2026 confirmado",
               "Subscribe to continue reading.", e)
    assert all(x["document_key"]!="origin" for x in
               idx.cross_language_review("copy")["candidates"])
    db.close()


def test_review_lsh_has_no_paid_embedding_calls_and_is_bounded(tmp_path):
    db=sqlite3.connect(tmp_path / "review-bounded.sqlite")
    idx=NoveltyIndex(db, model_id="local-review-model")
    e=FakeEmbed()
    for n in range(42):
        idx.assess(str(n), "Different headline " + str(n), FIRST, e)
    calls=e.count
    results=idx.cross_language_review("41", limit=3)
    assert results["state"]=="review_only"
    assert len(results["candidates"]) == 3
    assert e.count == calls
    assert results["reviewed_vector_candidates"] <= 1024
    with pytest.raises(ValueError, match="BOUNDED_REVIEW_SEARCH_REQUIRED"):
        idx.review.neighbors("41",limit=20000)
    db.close()


def test_review_lsh_current_model_and_policy_isolation(tmp_path):
    db=sqlite3.connect(tmp_path / "isolated-review.sqlite")
    a=NoveltyIndex(db,model_id="model-a")
    b=NoveltyIndex(db,model_id="model-b")
    a.assess("a", "French source report", FIRST, FakeEmbed())
    b.assess("b", "Arabic related report", FIRST, FakeEmbed())
    assert a.cross_language_review("a")["candidates"] == []
    db.close()


def test_local_embedding_chunk_cache_reuses_exact_text_without_network(tmp_path):
    class Http:
        def __init__(self, n): self.n=n
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def read(self): return json.dumps(
            {"embeddings": [[1.0]*16 for _ in range(self.n)]}).encode()
    calls=[]
    def network(req, timeout):
        count=len(json.loads(req.data)["input"])
        calls.append(count)
        return Http(count)
    with patch("scripts.reader_novelty_scan.GearShift",return_value=Mock()), \
         patch("scripts.reader_novelty_scan.budget_for",
               return_value={"required_increment_bytes":5}), \
         patch("scripts.reader_novelty_scan.urllib.request.urlopen",
               side_effect=network):
        emb=GuardedLocalEmbedder("local-digest-model",str(tmp_path))
        a=emb(["La empresa confirmó los resultados y el acuerdo comercial."])
        b=emb(["La empresa confirmó los resultados y el acuerdo comercial."])
        assert a == b
        assert len(calls)==1
        assert emb.last_call_stats["local_requests"] == 0
        assert emb.last_call_stats["embedding_misses"] == 0
        assert emb.last_call_stats["cache_hits"] == 1


def test_local_embedding_cache_deduplicates_chunks_within_batch(tmp_path):
    class Http:
        def __init__(self, n): self.n=n
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def read(self): return json.dumps(
            {"embeddings": [[1.0]*16 for _ in range(self.n)]}).encode()
    calls=[]
    def network(req, timeout):
        batch=json.loads(req.data)["input"]
        calls.append(batch)
        return Http(len(batch))
    with patch("scripts.reader_novelty_scan.GearShift",return_value=Mock()), \
         patch("scripts.reader_novelty_scan.budget_for",
               return_value={"required_increment_bytes":5}), \
         patch("scripts.reader_novelty_scan.urllib.request.urlopen",
               side_effect=network):
        emb=GuardedLocalEmbedder("test-cache-model",str(tmp_path))
        phrase="重复的中文句子用于测试快速本地向量计算。"
        rows=emb([phrase,phrase,"Additional independent content for contrast."])
        assert rows[0]==rows[1]
        assert len(calls)==1
        assert len(calls[0])==2
        assert emb.last_call_stats["cache_hits"]==1


def test_local_embedding_cache_respects_opt_out(tmp_path, monkeypatch):
    class Http:
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def read(self): return json.dumps({"embeddings":[[1.0]*16]}).encode()
    calls=[]
    def network(req,timeout):
        calls.append(req)
        return Http()
    monkeypatch.setenv("ABRAHAM_EMBED_CACHE","0")
    with patch("scripts.reader_novelty_scan.GearShift",return_value=Mock()), \
         patch("scripts.reader_novelty_scan.budget_for",
               return_value={"required_increment_bytes":5}), \
         patch("scripts.reader_novelty_scan.urllib.request.urlopen",
               side_effect=network):
        emb=GuardedLocalEmbedder("test-opt-out",str(tmp_path))
        emb(["No inter-call cache from this model."])
        emb(["No inter-call cache from this model."])
        assert len(calls)==2
