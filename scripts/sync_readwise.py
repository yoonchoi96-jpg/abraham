#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import json
import os
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

API = "https://readwise.io/api/v3/list/"
RATE_SPACING = 3.2


class TextExtractor(__import__("html.parser", fromlist=["HTMLParser"]).HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in {"p", "div", "br", "li", "h1", "h2", "h3", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"p", "div", "li", "h1", "h2", "h3", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def html_to_text(value: str) -> str:
    parser = TextExtractor()
    parser.feed(value or "")
    text = html.unescape("".join(parser.parts))
    return "\n\n".join(line.strip() for line in text.splitlines() if line.strip()).strip()


_last_api_call = 0.0


def api_get(token: str, params: dict) -> dict:
    global _last_api_call
    spacing = RATE_SPACING
    wait = spacing - (time.monotonic() - _last_api_call)
    if wait > 0:
        time.sleep(wait)

    query = urllib.parse.urlencode(params)
    for attempt in range(5):
        req = urllib.request.Request(
            API + "?" + query,
            headers={"Authorization": f"Token {token}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=90) as response:
                _last_api_call = time.monotonic()
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
            _last_api_call = time.monotonic()


def parse_dt(value):
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def table_columns(conn, table):
    return {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}


def existing_documents(conn):
    columns = table_columns(conn, "documents")
    select_cols = [c for c in ("readwise_id", "updated_at", "content", "canonical_text") if c in columns]
    if "readwise_id" not in columns:
        raise RuntimeError("documents table has no readwise_id column")
    rows = conn.execute(
        "SELECT " + ", ".join(select_cols) + " FROM documents"
    ).fetchall()
    return {
        str(row[0]): dict(zip(select_cols, row))
        for row in rows
    }


def fetch_recent(token, updated_after=None, max_documents=1000):
    results = []
    cursor = None
    while len(results) < max_documents:
        params = {
            "limit": min(100, max_documents - len(results)),
            "withHtmlContent": "true",
        }
        if updated_after:
            params["updatedAfter"] = updated_after.isoformat()
        if cursor:
            params["pageCursor"] = cursor

        data = api_get(token, params)
        page = data.get("results") or []
        results.extend(page)

        cursor = data.get("nextPageCursor")
        if not cursor or not page:
            break

    return results[:max_documents]


def normalize_content(item):
    content = item.get("content") or ""
    if not content and item.get("html_content"):
        content = html_to_text(item["html_content"])
    return content.strip()


def writable_values(item, columns, content, canonical_text=None):
    mapping = {
        "readwise_id": item.get("id"),
        "title": item.get("title") or "",
        "author": item.get("author") or "",
        "source": item.get("source") or "",
        "category": item.get("category") or "",
        "url": item.get("url") or item.get("source_url") or "",
        "published_date": item.get("published_date") or "",
        "saved_date": item.get("saved_at") or "",
        "created_at": item.get("created_at") or "",
        "updated_at": item.get("updated_at") or "",
        "content": content,
        "canonical_text": canonical_text if canonical_text is not None else content,
        "source_language": item.get("language") or "",
        "canonical_language": item.get("language") or "",
        # Reader tags are the authoritative V5 color/rating gate.
        "readwise_tags": json.dumps(
            item.get("tags") or {},
            ensure_ascii=False,
            sort_keys=True,
        ),
        "status": "new",
        "path": "",
    }
    return {k: v for k, v in mapping.items() if k in columns and v is not None}


def upsert(conn, item, columns, existing):
    rid = str(item["id"])
    content = normalize_content(item)
    if not content:
        raise RuntimeError(f"Readwise document has no readable content: {rid}")

    old = existing.get(rid)
    if old is None:
        values = writable_values(item, columns, content)
        required = {
            row[1]: row for row in conn.execute('PRAGMA table_info("documents")').fetchall()
        }
        missing_required = [
            name for name, row in required.items()
            if row[3] and row[4] is None and name not in values and name not in {"id"}
        ]
        if missing_required:
            raise RuntimeError(
                "Cannot safely insert new Readwise document; required columns without "
                f"defaults are missing: {missing_required}"
            )
        names = list(values)
        placeholders = ",".join("?" for _ in names)
        conn.execute(
            f'INSERT INTO documents ({",".join(names)}) VALUES ({placeholders})',
            tuple(values[n] for n in names),
        )
        return "inserted"

    old_content = str(old.get("content") or "")
    old_updated = parse_dt(old.get("updated_at"))
    new_updated = parse_dt(item.get("updated_at"))
    if new_updated and old_updated and new_updated <= old_updated and content == old_content:
        return "unchanged"

    values = writable_values(item, columns, content, canonical_text=None)
    # Preserve the V4.61 canonical text on existing rows. A future canonicalization
    # stage can replace it deliberately; ingestion must not silently destroy it.
    values.pop("canonical_text", None)

    # Refresh Reader tags when supplied; never fabricate them.
    if "readwise_tags" in columns and item.get("tags") is not None:
        values["readwise_tags"] = json.dumps(
            item.get("tags") or {},
            ensure_ascii=False,
            sort_keys=True,
        )
    assignments = [f'"{name}" = ?' for name in values if name != "readwise_id"]
    params = [values[name] for name in values if name != "readwise_id"]
    if assignments:
        conn.execute(
            f'UPDATE documents SET {",".join(assignments)} WHERE readwise_id = ?',
            (*params, rid),
        )
    return "updated"


def sync(db_path: Path, max_documents: int, lookback_minutes: int):
    token = os.environ.get("READWISE_TOKEN")
    if not token:
        raise RuntimeError("READWISE_TOKEN is required.")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    columns = table_columns(conn, "documents")
    existing = existing_documents(conn)

    latest = max(
        (parse_dt(row.get("updated_at")) for row in existing.values()),
        default=None,
    )
    updated_after = (
        latest - timedelta(minutes=lookback_minutes)
        if latest else None
    )

    items = fetch_recent(token, updated_after, max_documents)
    stats = {"fetched": len(items), "inserted": 0, "updated": 0, "unchanged": 0}

    for item in items:
        if not item.get("id"):
            continue
        result = upsert(conn, item, columns, existing)
        stats[result] += 1

    conn.commit()
    count = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    conn.close()

    print(
        "READWISE_SYNC_OK "
        f"fetched={stats['fetched']} inserted={stats['inserted']} "
        f"updated={stats['updated']} unchanged={stats['unchanged']} "
        f"documents={count} updated_after={updated_after.isoformat() if updated_after else 'none'}"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Incrementally sync Readwise Reader into Abraham SQLite.")
    parser.add_argument("--db", required=True)
    parser.add_argument("--max-documents", type=int, default=1000)
    parser.add_argument("--lookback-minutes", type=int, default=10)
    args = parser.parse_args()
    sync(Path(args.db), max(1, args.max_documents), max(0, args.lookback_minutes))
