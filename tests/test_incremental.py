import sqlite3
import unittest

from run_v560_storage import _processing_gate, _rating_tier, _validate_rating, RATING_DIMENSIONS

from abraham_incremental import (
    content_hash,
    ensure_schema,
    select_pending_documents,
    mark_completed,
)

class IncrementalStateTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        ensure_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_content_hash_is_stable(self):
        self.assertEqual(content_hash("a", "b"), content_hash("a", "b"))
        self.assertNotEqual(content_hash("a", "b"), content_hash("a", "c"))

    def test_new_document_is_pending(self):
        docs = [{"readwise_id": "r1", "content": "hello", "canonical_text": "hello"}]
        pending, skipped = select_pending_documents(self.conn, docs, "korean_translation")
        self.assertEqual([d["readwise_id"] for d in pending], ["r1"])
        self.assertEqual(skipped, [])

    def test_completed_unchanged_document_is_skipped(self):
        docs = [{"readwise_id": "r1", "content": "hello", "canonical_text": "hello", "document_id": "r1"}]
        pending, _ = select_pending_documents(self.conn, docs, "korean_translation")
        mark_completed(self.conn, pending, "korean_translation", {"r1": {"korean_title": "안녕", "korean_text": "hello"}})
        pending2, skipped2 = select_pending_documents(self.conn, docs, "korean_translation")
        self.assertEqual(pending2, [])
        self.assertEqual([d["readwise_id"] for d in skipped2], ["r1"])

    def test_changed_content_becomes_pending(self):
        docs = [{"readwise_id": "r1", "content": "hello", "canonical_text": "hello", "document_id": "r1"}]
        pending, _ = select_pending_documents(self.conn, docs, "korean_translation")
        mark_completed(self.conn, pending, "korean_translation", {"r1": {"korean_title": "안녕", "korean_text": "hello"}})
        changed = [{"readwise_id": "r1", "content": "changed", "canonical_text": "changed", "document_id": "r1"}]
        pending2, skipped2 = select_pending_documents(self.conn, changed, "korean_translation")
        self.assertEqual([d["readwise_id"] for d in pending2], ["r1"])
        self.assertEqual(skipped2, [])

    def test_reader_color_processing_gate(self):
        cases = {
            "⚪ white": (False, False),
            "🔴 red": (False, False),
            "🟠 orange": (True, False),
            "🟡 yellow": (True, False),
            "🟢 green": (True, False),
            "🔵 blue": (True, True),
            "rating-navy": (True, True),
            "🟣 Purple": (True, True),
            "🏆 Gold": (True, True),
        }
        for tag, expected in cases.items():
            doc = {"readwise_tags": {tag: {"name": tag}}}
            _, exportable, rateable = _processing_gate(doc)
            self.assertEqual((exportable, rateable), expected)

    def test_rating_tier_matches_existing_scale(self):
        self.assertEqual(_rating_tier(59.5), "D")
        self.assertEqual(_rating_tier(60.0), "C")
        self.assertEqual(_rating_tier(69.5), "C")
        self.assertEqual(_rating_tier(70.0), "B")
        self.assertEqual(_rating_tier(79.5), "B")
        self.assertEqual(_rating_tier(80.0), "B+")

    def test_rating_validation_rejects_invalid_values(self):
        item = {"document_id": "1", "reason": "ok", **{k: 50 for k in RATING_DIMENSIONS}}
        self.assertEqual(_validate_rating(item, "1")["information_density"], 50)
        bad = dict(item)
        bad["information_density"] = 51
        with self.assertRaises(ValueError):
            _validate_rating(bad, "1")

if __name__ == "__main__":
    unittest.main()