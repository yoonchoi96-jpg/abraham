#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://readwise.io/api/v3/list/"
DETAIL = "https://readwise.io/api/v3/list/"

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
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

_LAST_API_CALL = 0.0

def api_get(token: str, params: dict) -> dict:
    global _LAST_API_CALL
    query = urllib.parse.urlencode(params)

    # Reader base rate is 20 requests/minute per access token.
    spacing = 3.2
    wait = spacing - (time.monotonic() - _LAST_API_CALL)
    if wait > 0:
        time.sleep(wait)

    for attempt in range(5):
        req = urllib.request.Request(
            API + "?" + query,
            headers={"Authorization": f"Token {token}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                _LAST_API_CALL = time.monotonic()
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code != 429 or attempt == 4:
                raise
            retry_after = exc.headers.get("Retry-After")
            try:
                delay = max(float(retry_after), spacing)
            except (TypeError, ValueError):
                delay = spacing * (attempt + 1)
            time.sleep(delay)
            _LAST_API_CALL = time.monotonic()

def fetch_detail(token: str, document_id: str) -> dict:
    params = {"id": document_id, "withHtmlContent": "false"}
    data = api_get(token, params)
    results = data.get("results") or []
    if not results:
        raise RuntimeError(f"Readwise document not found: {document_id}")
    return results[0]

def bootstrap(db_path: Path, limit: int) -> None:
    token = os.environ.get("READWISE_TOKEN")
    if not token:
        raise RuntimeError("READWISE_TOKEN is required for first-run bootstrap.")

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)

    page = api_get(token, {
        "limit": min(max(limit, 1), 100),
        "withHtmlContent": "false",
    })
    documents = page.get("results") or []

    for item in documents[:limit]:
        # Reader's list endpoint may omit full content for some feed items.
        if not item.get("content"):
            item = fetch_detail(token, item["id"])

        conn.execute(
            """
            INSERT INTO documents (
                readwise_id, title, author, source, category, url,
                published_date, saved_date, created_at, updated_at,
                content, canonical_text, source_language, canonical_language,
                status, path
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(readwise_id) DO UPDATE SET
                title=excluded.title,
                author=excluded.author,
                source=excluded.source,
                category=excluded.category,
                url=excluded.url,
                published_date=excluded.published_date,
                saved_date=excluded.saved_date,
                created_at=excluded.created_at,
                updated_at=excluded.updated_at,
                content=excluded.content,
                canonical_text=excluded.canonical_text
            """,
            (
                item["id"],
                item.get("title") or "",
                item.get("author") or "",
                item.get("source") or "",
                item.get("category") or "",
                item.get("url") or "",
                item.get("published_date") or "",
                item.get("saved_at") or "",
                item.get("created_at") or "",
                item.get("updated_at") or "",
                item.get("content") or "",
                item.get("content") or "",
                "",
                "",
                "new",
                "",
            ),
        )

    conn.commit()
    count = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    conn.close()
    print(f"BOOTSTRAP_OK documents={count} path={db_path}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()
    bootstrap(Path(args.db), args.limit)
