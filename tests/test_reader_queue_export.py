import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.export_reader_queue import build_manifest
from scripts.reader_queue_status import summarize


def make_db(path):
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE reader_first_pass_queue(
            readwise_id TEXT PRIMARY KEY, state TEXT NOT NULL,
            grade_tag TEXT, first_opened_at TEXT,
            source_location TEXT, source_updated_at TEXT,
            observed_at TEXT NOT NULL
        );
        CREATE TABLE reader_ingest_progress(
            name TEXT PRIMARY KEY, window_start TEXT, window_end TEXT,
            page_cursor TEXT, pages INTEGER NOT NULL, updated_at TEXT NOT NULL
        );
        INSERT INTO reader_ingest_progress
          VALUES('reader',NULL,'2026-10-17T05:00:00Z','next',1,'2026-10-17T05:00:00Z');
        INSERT INTO reader_first_pass_queue VALUES
          ('a','pending',NULL,NULL,'feed','2026-10-15T00:00:00Z','2026-10-17T00:00:00Z'),
          ('b','tagged_unseen','등급/🔵파랑',NULL,'feed','2026-10-16T00:00:00Z','2026-10-17T00:00:00Z'),
          ('c','graded_seen','등급/🔵파랑','2026-10-17T00:00:00Z','feed','2026-10-17T00:00:00Z','2026-10-17T00:00:00Z'),
          ('d','pending',NULL,NULL,'archive','2026-10-16T00:00:00Z','2026-10-17T00:00:00Z');
    """)
    conn.commit()
    conn.close()


class QueueExportTests(unittest.TestCase):
    def test_export_only_feed_pending_and_tagged_unseen(self):
        with tempfile.TemporaryDirectory() as td:
            db, out = Path(td)/"db.sqlite", Path(td)/"output"
            make_db(db)
            manifest = build_manifest(db, out, shard_size=1)
            self.assertEqual(manifest["total_candidates"], 2)
            self.assertEqual(len(manifest["shards"]), 2)
            ids = set()
            for part in manifest["shards"]:
                payload = json.loads((out/part["filename"]).read_text())
                ids.update(v["id"] for v in payload["items"])
            self.assertEqual(ids, {"a", "b"})
            self.assertNotIn("c", ids)
            self.assertNotIn("d", ids)

    def test_content_addressed_shards_are_stable(self):
        with tempfile.TemporaryDirectory() as td:
            db, out1, out2 = Path(td)/"db.sqlite", Path(td)/"a", Path(td)/"b"
            make_db(db)
            first = build_manifest(db, out1)
            second = build_manifest(db, out2)
            self.assertEqual(first["shards"], second["shards"])

    def test_status_never_emits_document_ids(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td)/"db.sqlite"
            make_db(db)
            status = summarize(db)
            self.assertEqual(status["candidate_pending"], 2)
            self.assertEqual(status["tagged_but_unseen"], 1)
            self.assertEqual(status["graded_seen"], 1)
            self.assertEqual(status["lanes"]["reader"]["pages_in_window"], 1)
            self.assertNotIn("readwise_id", json.dumps(status))
            self.assertNotIn("page_cursor", json.dumps(status).replace("resumable_page_cursor", ""))


if __name__ == "__main__":
    unittest.main()
