import sqlite3
import unittest

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

if __name__ == "__main__":
    unittest.main()