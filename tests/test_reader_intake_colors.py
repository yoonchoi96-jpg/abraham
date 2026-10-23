"""Regression tests for Reader nine-color parsing and four-color admission."""
import importlib.util
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1] / "scripts" / "reader_intake.py"
spec = importlib.util.spec_from_file_location("reader_intake", MODULE)
reader_intake = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reader_intake)


@pytest.mark.parametrize("tag,expected,selected", [
    ("등급/⚪하양", "white", False),
    ("등급/🔴빨강", "red", False),
    ("등급/🟠주황", "orange", False),
    ("등급/🟡노랑", "yellow", False),
    ("등급/🟢초록", "green", False),
    ("등급/🔵파랑", "blue", True),
    ("등급/🟦남색", "navy", True),
    ("등급/🟣보라", "purple", True),
    ("등급/🟨금색", "gold", True),
    ("등급/🔵 파랑", "blue", True),
    ("등급/🟦 남색", "navy", True),
    ("등급/🔵️파랑", "blue", True),
    ("🟠 orange", "orange", False),
    ("🟡 yellow", "yellow", False),
    ("🟢 green", "green", False),
    ("🟣 purple", "purple", True),
    ("등급/Gold", "gold", True),
    ("🟨 골드", "gold", True),
])
def test_nine_color_rating_and_four_color_admission(tag, expected, selected):
    assert reader_intake.normalize_rating_tag(tag) == expected
    doc = reader_intake.parse_document(
        "article.md",
        ("---\nid: reader-123\ntitle: Test article\ntags:\n"
         f"  - {tag!r}\n---\nA substantive article body.\n").encode("utf-8"),
    )
    assert doc["rating"] == expected
    assert doc["admission"] == ("selected" if selected else "held")


@pytest.mark.parametrize("tag", [
    "주제/🔵파랑", "topic/blue", "등급/blueprint",
    "등급/🔵초록", "foo/navy", "등급/🟦남색추가",
])
def test_unrelated_or_invalid_tags_do_not_promote(tag):
    assert reader_intake.normalize_rating_tag(tag) is None


def test_conflicting_grades_are_held_without_arbitrary_promotion():
    payload = (
        "---\nid: reader-123\ntitle: Test article\n"
        "tags:\n  - 등급/🔵파랑\n  - 등급/🟢초록\n"
        "---\nA substantive article body.\n"
    ).encode("utf-8")
    doc = reader_intake.parse_document("article.md", payload)
    assert doc["rating"] is None
    assert doc["admission"] == "held"


def test_other_tags_preserved():
    payload = (
        "---\nid: reader-123\ntitle: Test article\n"
        "tags:\n  - 주제/반도체\n  - 등급/🟦남색\n"
        "---\nA substantive article body.\n"
    ).encode("utf-8")
    doc = reader_intake.parse_document("article.md", payload)
    assert doc["tags"] == ["주제/반도체", "등급/🟦남색"]
    assert doc["rating"] == "navy"
    assert doc["admission"] == "selected"
