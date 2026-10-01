import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.sync_readwise import sync


class ReadwiseSyncTests(unittest.TestCase):
    def test_inserts_new_document_and_preserves_existing_canonical_text(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "test.db"
            conn = sqlite3.connect(db)
            conn.executescript(
                """
                CREATE TABLE documents (
                    id INTEGER PRIMARY KEY,
                    readwise_id TEXT NOT NULL UNIQUE,
                    title TEXT,
                    author TEXT,
                    source TEXT,
                    category TEXT,
                    url TEXT,
                    published_date TEXT,
                    saved_date TEXT,
                    created_at TEXT,
                    updated_at TEXT,
                    content TEXT,
                    canonical_text TEXT,
                    source_language TEXT,
                    canonical_language TEXT,
                    status TEXT,
                    path TEXT
                );
                """
            )
            conn.execute(
                """
                INSERT INTO documents
                (readwise_id,title,updated_at,content,canonical_text,status,path)
                VALUES (?,?,?,?,?,?,?)
                """,
                ("old", "Old", "2026-09-29T00:00:00+00:00", "old source", "OLD CANONICAL", "done", "Old.md"),
            )
            conn.commit()
            conn.close()

            payload = {
                "results": [
                    {
                        "id": "old",
                        "title": "Old updated",
                        "updated_at": "2026-09-29T00:01:00+00:00",
                        "content": "new source",
                    },
                    {
                        "id": "new",
                        "title": "New",
                        "updated_at": "2026-09-29T00:02:00+00:00",
                        "content": "new article",
                    },
                ],
                "nextPageCursor": None,
            }

            with patch("scripts.sync_readwise.api_get", return_value=payload):
                with patch.dict("os.environ", {"READWISE_TOKEN": "test"}, clear=False):
                    sync(db, max_documents=100, lookback_minutes=10)

            conn = sqlite3.connect(db)
            old = conn.execute(
                "SELECT title, content, canonical_text FROM documents WHERE readwise_id='old'"
            ).fetchone()
            new = conn.execute(
                "SELECT title, content, canonical_text FROM documents WHERE readwise_id='new'"
            ).fetchone()
            count = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
            conn.close()

            self.assertEqual(old, ("Old updated", "new source", "OLD CANONICAL"))
            self.assertEqual(new, ("New", "new article", "new article"))
            self.assertEqual(count, 2)

    def test_missing_content_is_non_fatal_and_preserves_existing_content(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "test.db"
            conn = sqlite3.connect(db)
            conn.executescript(
                """
                CREATE TABLE documents (
                    id INTEGER PRIMARY KEY,
                    readwise_id TEXT NOT NULL UNIQUE,
                    title TEXT,
                    updated_at TEXT,
                    content TEXT,
                    canonical_text TEXT,
                    status TEXT,
                    path TEXT
                );
                INSERT INTO documents
                (readwise_id,title,updated_at,content,canonical_text,status,path)
                VALUES ('old','Old','2026-09-29T00:00:00+00:00','KEEP ME','KEEP CANONICAL','done','Old.md');
                """
            )
            conn.commit()
            conn.close()

            payload = {
                "results": [
                    {
                        "id": "old",
                        "title": "Old refreshed",
                        "updated_at": "2026-09-29T00:05:00+00:00",
                    },
                    {
                        "id": "new",
                        "title": "Metadata only",
                        "updated_at": "2026-09-29T00:06:00+00:00",
                    },
                ],
                "nextPageCursor": None,
            }

            with patch("scripts.sync_readwise.api_get", return_value=payload):
                with patch.dict("os.environ", {"READWISE_TOKEN": "test"}, clear=False):
                    sync(db, max_documents=100, lookback_minutes=10)

            conn = sqlite3.connect(db)
            old = conn.execute(
                "SELECT title, content, canonical_text, status FROM documents WHERE readwise_id='old'"
            ).fetchone()
            new = conn.execute(
                "SELECT title, content, canonical_text, status FROM documents WHERE readwise_id='new'"
            ).fetchone()
            conn.close()

            self.assertEqual(old, ("Old refreshed", "KEEP ME", "KEEP CANONICAL", "done"))
            self.assertEqual(new, ("Metadata only", "", "", "content_unavailable"))

    def test_html_content_counts_as_readable_content(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "test.db"
            conn = sqlite3.connect(db)
            conn.executescript(
                """
                CREATE TABLE documents (
                    id INTEGER PRIMARY KEY,
                    readwise_id TEXT NOT NULL UNIQUE,
                    title TEXT,
                    updated_at TEXT,
                    content TEXT,
                    canonical_text TEXT,
                    status TEXT,
                    path TEXT
                );
                """
            )
            conn.commit()
            conn.close()

            payload = {
                "results": [{
                    "id": "html",
                    "title": "HTML",
                    "updated_at": "2026-09-29T00:00:00+00:00",
                    "html_content": "<p>Hello <b>world</b>.</p>",
                }],
                "nextPageCursor": None,
            }

            with patch("scripts.sync_readwise.api_get", return_value=payload):
                with patch.dict("os.environ", {"READWISE_TOKEN": "test"}, clear=False):
                    sync(db, max_documents=100, lookback_minutes=10)

            conn = sqlite3.connect(db)
            row = conn.execute(
                "SELECT content, canonical_text, status FROM documents WHERE readwise_id='html'"
            ).fetchone()
            conn.close()

            self.assertEqual(row, ("Hello world.", "Hello world.", "new"))


if __name__ == "__main__":
    unittest.main()
