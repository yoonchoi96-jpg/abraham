import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.reader_queue_state import queue_state
from scripts.sync_readwise import sync


def make_db(db):
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY,
            readwise_id TEXT NOT NULL UNIQUE,
            title TEXT, updated_at TEXT, content TEXT,
            canonical_text TEXT, status TEXT, path TEXT,
            readwise_tags TEXT
        );
    """)
    conn.commit()
    conn.close()


def doc(rid, t="2026-10-16T01:00:00+00:00", tags=None, opened=None):
    return {
        "id": rid, "title": rid, "updated_at": t,
        "content": "Real article text", "tags": tags or {},
        "first_opened_at": opened, "location": "feed",
    }


class DurableReaderQueueTests(unittest.TestCase):
    def test_partial_scan_resume_never_advances_watermark_early(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "abraham.db"
            make_db(path)
            calls = []

            def reader(_token, params):
                calls.append(dict(params))
                if params.get("pageCursor") == "next_100":
                    return {"results": [doc("B")], "nextPageCursor": None}
                return {"results": [doc("A")], "nextPageCursor": "next_100"}

            with patch("scripts.sync_readwise.api_get", side_effect=reader):
                with patch.dict("os.environ", {"READWISE_TOKEN": "test"}):
                    sync(path, max_documents=1, lookback_minutes=10)
                    conn = sqlite3.connect(path)
                    row = conn.execute(
                        "SELECT window_start,window_end,page_cursor FROM reader_ingest_progress"
                    ).fetchone()
                    conn.close()
                    self.assertEqual(row[0], None)  # full backfill not completed
                    self.assertIsNotNone(row[1])
                    self.assertEqual(row[2], "next_100")
                    sync(path, max_documents=1, lookback_minutes=10)

            self.assertEqual(calls[1]["pageCursor"], "next_100")
            self.assertNotIn("updatedAfter", calls[1])
            conn = sqlite3.connect(path)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 2)
            row = conn.execute(
                "SELECT window_start,window_end,page_cursor FROM reader_ingest_progress"
            ).fetchone()
            self.assertIsNotNone(row[0])  # only after full exhaustion
            self.assertIsNone(row[1])
            self.assertIsNone(row[2])
            counts = dict(conn.execute(
                "SELECT state,COUNT(*) FROM reader_first_pass_queue GROUP BY state"
            ).fetchall())
            conn.close()
            self.assertEqual(counts, {"pending": 2})

    def test_failure_commits_previous_page_and_restarts_there(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "abraham.db"
            make_db(path)

            def failing(_token, params):
                if params.get("pageCursor"):
                    raise ConnectionError("transient server failure")
                return {"results": [doc("A")], "nextPageCursor": "page2"}

            with patch("scripts.sync_readwise.api_get", side_effect=failing):
                with patch.dict("os.environ", {"READWISE_TOKEN": "test"}):
                    with self.assertRaises(ConnectionError):
                        sync(path, max_documents=2, lookback_minutes=10)

            conn = sqlite3.connect(path)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 1)
            self.assertEqual(conn.execute(
                "SELECT page_cursor FROM reader_ingest_progress"
            ).fetchone()[0], "page2")
            conn.close()

            with patch("scripts.sync_readwise.api_get", return_value={
                "results": [doc("B")], "nextPageCursor": None,
            }) as mock:
                with patch.dict("os.environ", {"READWISE_TOKEN": "test"}):
                    sync(path, max_documents=2, lookback_minutes=10)
            self.assertEqual(mock.call_args.args[1]["pageCursor"], "page2")
            conn = sqlite3.connect(path)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 2)
            conn.close()

    def test_legacy_max_timestamp_does_not_skip_unsynced_backlog(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "abraham.db"
            make_db(path)
            conn = sqlite3.connect(path)
            conn.execute(
                "INSERT INTO documents(readwise_id,updated_at,content) VALUES (?,?,?)",
                ("latest", "2026-10-17T18:00:00+00:00", "old"),
            )
            conn.commit()
            conn.close()
            with patch("scripts.sync_readwise.api_get", return_value={
                "results": [doc("missed", "2026-09-01T00:00:00+00:00")],
                "nextPageCursor": None,
            }) as mock:
                with patch.dict("os.environ", {"READWISE_TOKEN": "test"}):
                    sync(path, max_documents=100, lookback_minutes=10)
            self.assertNotIn("updatedAfter", mock.call_args.args[1])
            conn = sqlite3.connect(path)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM documents WHERE readwise_id='missed'"
            ).fetchone()[0], 1)
            conn.close()

    def test_fast_lane_does_not_overwrite_backfill_cursor(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "abraham.db"
            make_db(path)
            def api(_token, params):
                if "updatedAfter" not in params:
                    return {"results": [doc("OLD")], "nextPageCursor": "old-next"}
                return {"results": [doc("NEW")], "nextPageCursor": None}
            with patch("scripts.sync_readwise.api_get", side_effect=api):
                with patch.dict("os.environ", {"READWISE_TOKEN": "test"}):
                    sync(path, 1, 10, lane="backfill")
                    sync(path, 1, 10, lane="fast")
            conn = sqlite3.connect(path)
            rows = dict(conn.execute(
                "SELECT name,page_cursor FROM reader_ingest_progress"
            ).fetchall())
            self.assertEqual(rows["reader"], "old-next")
            self.assertIsNone(rows["reader_fast"])
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM documents"
            ).fetchone()[0], 2)
            conn.close()

    def test_fast_lane_failure_keeps_its_own_resume_cursor(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "abraham.db"
            make_db(path)
            def initial(_token, params):
                return {"results": [doc("FAST1")], "nextPageCursor": "f-next"}
            with patch("scripts.sync_readwise.api_get", side_effect=initial):
                with patch.dict("os.environ", {"READWISE_TOKEN": "test"}):
                    sync(path, 1, 10, lane="fast")
            conn = sqlite3.connect(path)
            self.assertEqual(conn.execute(
                "SELECT page_cursor FROM reader_ingest_progress "
                "WHERE name='reader_fast'"
            ).fetchone()[0], "f-next")
            conn.close()
            def resume(_token, params):
                self.assertEqual(params.get("pageCursor"), "f-next")
                return {"results": [doc("FAST2")], "nextPageCursor": None}
            with patch("scripts.sync_readwise.api_get", side_effect=resume):
                with patch.dict("os.environ", {"READWISE_TOKEN": "test"}):
                    sync(path, 1, 10, lane="fast")

    def test_queue_distinguishes_tagged_unseen_and_seen_untagged(self):
        tag = {"등급/🔵파랑": {"name": "등급/🔵파랑"}}
        self.assertEqual(queue_state(doc("A", tags=tag, opened=None)),
                         ("tagged_unseen", "등급/🔵파랑"))
        self.assertEqual(queue_state(doc("B", tags={}, opened=None)), ("pending", None))
        self.assertEqual(queue_state(doc("C", tags=tag, opened="2026-10-17T04:30:00Z")),
                         ("graded_seen", "등급/🔵파랑"))
        self.assertEqual(queue_state(doc("D", tags={}, opened="2026-10-17T04:30:00Z")),
                         ("seen_untagged", None))
        self.assertEqual(queue_state({"id": "E", "tags": {}}),
                         ("unverified", None))


if __name__ == "__main__":
    unittest.main()
