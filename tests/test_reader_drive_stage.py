"""Synthetic staging integration tests. They are not user Reader classifications."""
import json
import os
import sqlite3
from pathlib import Path

import pytest

from scripts.reader_bulk import Bulk
from scripts.reader_drive_stage import DriveLibraryStager, _existing_label, _private_work


def html(body):
    return ("<html><body><article><h1>Test article</h1><p>" +
            body + "</p></article></body></html>").encode()


def item(fid, name, version="1"):
    return {"id": fid, "name": name, "mimeType": "text/html",
            "version": version, "shared": False}


SENTENCE = (
    "AI semiconductor production requires careful power budgeting and "
    "durable supply-chain planning, including independently checked metrics. "
) * 5


def test_existing_filename_colors_are_metadata_not_human_truth():
    assert _existing_label("【🔵파랑·반도체】 Article (01m123).html") == (
        "blue", "EXISTING_FILE_NAME_LABEL"
    )
    assert _existing_label("【보류·본문부족】 Article.html") == (
        None, "EXISTING_DEFERRED_MARKER"
    )
    assert _existing_label("Plain article.html") == (None, None)


def test_persists_reader_stage_and_exact_dedup_without_mutation(tmp_path):
    work = tmp_path / "private"
    stage = DriveLibraryStager(work)
    a, b = item("a", "【🔵파랑·산업】 A.html"), item("b", "B.html")
    src_a, src_b = work / "a.html", work / "b.html"
    src_a.write_bytes(html(SENTENCE))
    src_b.write_bytes(html(SENTENCE))
    stage.process("one", a, src_a)
    stage.process("two", b, src_b)
    assert stage.status() == {"duplicate": 1, "existing_tag_unverified": 1}
    assert stage.db.execute(
        "SELECT filename_color,state FROM stage WHERE job_key='one'"
    ).fetchone() == ("blue", "existing_tag_unverified")
    assert stage.db.execute(
        "SELECT COUNT(DISTINCT body_sha256) FROM stage"
    ).fetchone()[0] == 1
    assert src_a.exists() and src_b.exists()
    stage.process("one", a, src_a)
    assert stage.db.execute("SELECT COUNT(*) FROM stage").fetchone()[0] == 2
    stage.close()


def test_html_only_noise_deferred(tmp_path):
    stage = DriveLibraryStager(tmp_path / "private")
    source = stage.work / "noise.html"
    source.write_bytes(b"<html><head><script>ads()</script></head><body></body></html>")
    stage.process("x", item("x", "Noise.html"), source)
    assert stage.status() == {"deferred_no_fulltext": 1}
    assert stage.db.execute("SELECT reason FROM stage").fetchone()[0] == "EXTRACTION_FAILED"
    assert source.exists()
    stage.close()


class FakeDrive:
    """Pagination, no mutation methods."""
    def __init__(self):
        self.downloads = 0

    def page(self, folder, token):
        if token is None:
            return {"files": [item("a", "A.html")], "nextPageToken": "page2"}
        return {"files": [item("b", "B.html")]}

    def download(self, metadata, destination):
        self.downloads += 1
        destination.write_bytes(html(SENTENCE + metadata["id"]))


def test_bulk_end_to_end_resume_staging_read_only(tmp_path):
    stage = DriveLibraryStager(tmp_path / "private")
    source = FakeDrive()
    bulk = Bulk(stage.work, ["readerroot"])
    first = bulk.run(source, stage.process, max_items=1)
    assert first["discovery_complete"] is False
    assert first["completed"] == 1
    bulk.close()
    stage.close()

    stage = DriveLibraryStager(tmp_path / "private")
    bulk = Bulk(stage.work, ["readerroot"])
    second = bulk.run(source, stage.process, max_items=100)
    assert second["discovery_complete"] is True
    assert second["completed"] == 1
    assert source.downloads == 2
    assert stage.status() == {"review_required": 2}
    assert bulk.run(source, stage.process)["completed"] == 0
    bulk.close()
    stage.close()


def test_public_checkout_and_shared_workspace_rejected(tmp_path):
    checkout = tmp_path/"project"
    checkout.mkdir()
    (checkout/".git").mkdir()
    with pytest.raises(RuntimeError, match="PUBLIC_REPOSITORY_WORKSPACE_FORBIDDEN"):
        _private_work(checkout/"private")
    unsafe = tmp_path/"shared"
    unsafe.mkdir(mode=0o777)
    os.chmod(unsafe, 0o755)
    with pytest.raises(RuntimeError, match="PRIVATE_WORKSPACE_REQUIRED"):
        _private_work(unsafe)
