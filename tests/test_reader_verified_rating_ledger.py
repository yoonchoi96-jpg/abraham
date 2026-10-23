"""Fail-closed tests for verified Reader rating-ledger join (no live Reader/Drive writes)."""
import hashlib
import importlib.util
import json
import zipfile
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1] / "scripts" / "reader_intake.py"
spec = importlib.util.spec_from_file_location("reader_intake", MODULE)
intake = importlib.util.module_from_spec(spec)
spec.loader.exec_module(intake)

BODY = "An article with verified substantive content."
DOC_ID = "01mtestverifieddocument00000001"

def row(body=BODY, grade="등급/🔵파랑", **overrides):
    record = {
        "reader_id": DOC_ID,
        "body_sha256": hashlib.sha256(body.strip().encode()).hexdigest(),
        "grade": grade,
        "run_id": "reader-20261023-test",
        "hash_scope": "trimmed_utf8_body_v1",
        "reader_tag_verified": True,
        "reader_drive_body_exact_match": True,
    }
    record.update(overrides)
    return record

def load(tmp_path, records):
    p = tmp_path / "ledger.jsonl"
    p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")
    return intake.load_verified_rating_ledger(p)

def doc(tags="[]", body=BODY):
    return intake.parse_document(
        "article.md",
        (f"---\nid: {DOC_ID}\ntitle: Verified article\ntags: {tags}\n---\n" + body).encode(),
    )

def test_verified_blue_recovers_stale_empty_raw_tags(tmp_path):
    ledger = load(tmp_path, [row()])
    result = intake.apply_verified_rating(doc(), ledger)
    assert result["rating"] == "blue"
    assert result["admission"] == "selected"
    assert result["rating_verification"] == "VERIFIED_LEDGER_MATCH"
    assert result["rating_run_id"] == "reader-20261023-test"

def test_green_remains_held_even_when_verified(tmp_path):
    ledger = load(tmp_path, [row(grade="등급/🟢초록")])
    result = intake.apply_verified_rating(doc(), ledger)
    assert result["rating"] == "green"
    assert result["admission"] == "held"

def test_changed_body_fails_closed(tmp_path):
    ledger = load(tmp_path, [row()])
    result = intake.apply_verified_rating(doc(body="A modified article body"), ledger)
    assert result["rating"] is None
    assert result["admission"] == "held"
    assert result["rating_verification"] == "VERIFIED_LEDGER_BODY_HASH_MISMATCH"

def test_raw_rating_conflict_fails_closed(tmp_path):
    ledger = load(tmp_path, [row()])
    result = intake.apply_verified_rating(doc(tags="['등급/🟨금색']"), ledger)
    assert result["admission"] == "held"
    assert result["rating_verification"] == "VERIFIED_LEDGER_RAW_RATING_CONFLICT"

def test_missing_id_fails_closed(tmp_path):
    ledger = load(tmp_path, [row()])
    result = intake.apply_verified_rating(
        intake.parse_document("article.md", b"---\nid: different\n---\nAn article with verified substantive content."),
        ledger,
    )
    assert result["admission"] == "held"
    assert result["rating_verification"] == "MISSING_VERIFIED_LEDGER_ENTRY"

@pytest.mark.parametrize("invalid", [
    {"hash_scope": "unknown normalization scope"},
    {"reader_tag_verified": False},
    {"reader_drive_body_exact_match": False},
    {"body_sha256": "bad"},
    {"grade": "주제/🔵파랑"},
    {"run_id": ""},
])
def test_invalid_ledger_is_rejected(tmp_path, invalid):
    with pytest.raises(ValueError, match="INVALID_VERIFIED_RATING_LEDGER"):
        load(tmp_path, [row(**invalid)])

def test_conflicting_duplicate_ids_rejected(tmp_path):
    with pytest.raises(ValueError, match="CONFLICTING_VERIFIED_RATING_LEDGER"):
        load(tmp_path, [row(), row(grade="등급/🟨금색")])

def test_empty_ledger_rejected(tmp_path):
    with pytest.raises(ValueError, match="EMPTY_VERIFIED_RATING_LEDGER"):
        load(tmp_path, [])

def test_partial_verified_archive_replays_without_overwriting_shards(tmp_path):
    import gzip

    other_id = "01mtestverifieddocument00000002"
    other_body = "A second distinct article with its own verified body."
    archive = tmp_path / "source.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("one.md", (
            f"---\\nid: {DOC_ID}\\ntitle: First\\ntags: []\\n---\\n" + BODY
        ).replace("\\n", "\n"))
        z.writestr("two.md", (
            f"---\\nid: {other_id}\\ntitle: Second\\ntags: []\\n---\\n" + other_body
        ).replace("\\n", "\n"))

    partial_ledger = load(tmp_path, [row()])
    engine = intake.Intake(tmp_path / "work")
    try:
        first = engine.run(archive, rating_ledger=partial_ledger)
        assert first["archive_complete"] is False
        assert first["stats"]["canonical_new"] == 1
        assert first["stats"]["pending_verified_ledger"] == 1
        assert engine.db.execute("SELECT COUNT(*) FROM docs").fetchone()[0] == 1
        assert engine.db.execute("SELECT COUNT(*) FROM archives").fetchone()[0] == 0
        first_shard = engine.work / first["shards"][0]
        assert first_shard.exists()
        first_bytes = first_shard.read_bytes()
        with gzip.open(engine.work / first["issues_file"], "rt") as f:
            issues = [json.loads(line) for line in f]
        assert [i["reason"] for i in issues] == ["MISSING_VERIFIED_LEDGER_ENTRY"]

        complete_ledger = load(tmp_path, [
            row(), row(body=other_body, reader_id=other_id, grade="등급/🟦남색"),
        ])
        second = engine.run(archive, rating_ledger=complete_ledger)
        assert second["archive_complete"] is True
        assert second["stats"]["canonical_new"] == 1
        assert second["stats"]["duplicate_body"] == 1
        assert second["stats"]["pending_verified_ledger"] == 0
        assert first_shard.read_bytes() == first_bytes
        assert first["shards"][0] != second["shards"][0]
        assert engine.db.execute("SELECT COUNT(*) FROM docs").fetchone()[0] == 2
        assert engine.db.execute("SELECT COUNT(*) FROM archives").fetchone()[0] == 1
        third = engine.run(archive, rating_ledger=complete_ledger)
        assert third["archive_already_complete"] is True
    finally:
        engine.close()
