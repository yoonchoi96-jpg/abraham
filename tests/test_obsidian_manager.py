from pathlib import Path

from obsidian_manager.audit import audit, parse_frontmatter
from obsidian_manager.governance import audit_field_ownership, is_protected_from_hendrix, owner_for


def test_parse_frontmatter():
    data, error = parse_frontmatter("---\ntype: article\nrating: 4\n---\nBody")
    assert error is None
    assert data["type"] == "article"
    assert data["rating"] == "4"


def test_audit_is_read_only_and_detects_broken_links(tmp_path: Path):
    p = tmp_path / "A.md"
    p.write_text("---\nrating: 4\n---\nSee [[Missing]].", encoding="utf-8")
    before = p.read_bytes()
    report = audit(tmp_path)
    assert p.read_bytes() == before
    assert report["mode"] == "read_only"
    assert report["manager_name"] == "Hendrix"
    assert any(i["kind"] == "broken_wikilink" for i in report["issues"])


def test_metadata_ownership_is_explicit():
    assert owner_for("rating") == "abraham"
    assert owner_for("abel_mastery") == "abel"
    assert owner_for("hendrix_indexed") == "hendrix"
    assert owner_for("future_field") == "unknown"

    assert is_protected_from_hendrix("rating")
    assert is_protected_from_hendrix("abel_mastery")
    assert is_protected_from_hendrix("future_field")
    assert not is_protected_from_hendrix("hendrix_indexed")


def test_audit_reports_ownership_and_unknown_fields(tmp_path: Path):
    (tmp_path / "A.md").write_text(
        "---\nrating: 4\nabel_mastery: 0.5\nhendrix_indexed: true\nfuture_field: x\n---\n# A",
        encoding="utf-8",
    )
    report = audit(tmp_path)
    assert report["metadata_ownership"]["abraham"] == 1
    assert report["metadata_ownership"]["abel"] == 1
    assert report["metadata_ownership"]["hendrix"] == 1
    assert report["metadata_ownership"]["unknown"] == 1
    assert any(i["kind"] == "unknown_metadata_owner" for i in report["issues"])


def test_audit_resolves_existing_wikilinks(tmp_path: Path):
    (tmp_path / "A.md").write_text("See [[B]].", encoding="utf-8")
    (tmp_path / "B.md").write_text("# B", encoding="utf-8")
    report = audit(tmp_path)
    assert not any(i["kind"] == "broken_wikilink" for i in report["issues"])


def test_duplicate_stems_are_reported(tmp_path: Path):
    (tmp_path / "A.md").write_text("# A", encoding="utf-8")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "A.md").write_text("# A2", encoding="utf-8")
    report = audit(tmp_path)
    assert sum(i["kind"] == "duplicate_stem" for i in report["issues"]) == 2
