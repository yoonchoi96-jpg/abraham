from pathlib import Path
from obsidian_manager.audit import audit, parse_frontmatter

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
    assert any(i["kind"] == "broken_wikilink" for i in report["issues"])
    assert report["protected_metadata_observed"]["rating"] == 1

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
