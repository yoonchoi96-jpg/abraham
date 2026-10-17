"""Durable Reader ingestion cursor and first-pass triage inventory.

Tables live inside the existing canonical SQLite state, NEVER in git or a
public GitHub release. This module does not perform AI classification.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

PROGRESS = "reader_ingest_progress"
QUEUE = "reader_first_pass_queue"


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS reader_ingest_progress(
            name TEXT PRIMARY KEY,
            window_start TEXT,
            window_end TEXT,
            page_cursor TEXT,
            pages INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS reader_first_pass_queue(
            readwise_id TEXT PRIMARY KEY,
            state TEXT NOT NULL,
            grade_tag TEXT,
            first_opened_at TEXT,
            source_location TEXT,
            source_updated_at TEXT,
            observed_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_reader_first_pass_queue_state
            ON reader_first_pass_queue(state, source_location);
        """
    )
    # NULL window_start on first run means a COMPLETE initial Reader scan.
    # Never seed it from MAX(documents.updated_at): earlier capped runs may
    # already have skipped records that the database doesn't know about.
    conn.execute(
        """INSERT OR IGNORE INTO reader_ingest_progress
           (name, window_start, window_end, page_cursor, pages, updated_at)
           VALUES ('reader', NULL, NULL, NULL, 0, ?)""",
        (iso_now(),),
    )
    conn.commit()


def get_scan(conn: sqlite3.Connection) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM reader_ingest_progress WHERE name='reader'"
    ).fetchone()
    if row is None:
        raise RuntimeError("Reader progress row missing")
    if row["window_end"] is None:
        conn.execute(
            """UPDATE reader_ingest_progress
               SET window_end=?, updated_at=? WHERE name='reader'""",
            (iso_now(), iso_now()),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM reader_ingest_progress WHERE name='reader'"
        ).fetchone()
    return row


def _tags(item: dict) -> list[str]:
    tags = item.get("tags")
    if isinstance(tags, dict):
        return [v["name"] for v in tags.values()
                if isinstance(v, dict) and isinstance(v.get("name"), str)]
    if isinstance(tags, list):
        return [v if isinstance(v, str) else v.get("name")
                for v in tags if isinstance(v, str) or
                (isinstance(v, dict) and isinstance(v.get("name"), str))]
    return []


def _grades(item: dict) -> list[str]:
    colors = ("⚪", "🔴", "🟠", "🟡", "🟢", "🔵", "🟦", "🟣", "🟨", "🥇")
    english = ("white", "red", "orange", "yellow", "green", "blue",
               "navy", "purple", "gold")
    found = []
    for name in _tags(item):
        lower = name.lower().strip()
        if lower.startswith("등급/") or any(lower == c or lower.startswith(c + " ") for c in colors) or lower in english:
            found.append(name)
    return found


def queue_state(item: dict) -> tuple[str, str | None]:
    # An omitted field is NOT the same as explicitly empty tags / unseen.
    if "first_opened_at" not in item or "tags" not in item or item.get("tags") is None:
        return "unverified", None
    grades = _grades(item)
    if len(grades) > 1:
        return "conflict", None
    grade = grades[0] if grades else None
    if item["first_opened_at"] is None:
        return ("tagged_unseen" if grade else "pending", grade)
    return ("graded_seen" if grade else "seen_untagged", grade)


def update_queue(conn: sqlite3.Connection, item: dict) -> None:
    rid = item.get("id")
    if not rid:
        return
    state, grade = queue_state(item)
    conn.execute(
        """INSERT INTO reader_first_pass_queue
           (readwise_id, state, grade_tag, first_opened_at,
            source_location, source_updated_at, observed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(readwise_id) DO UPDATE SET
            state=excluded.state,
            grade_tag=excluded.grade_tag,
            first_opened_at=excluded.first_opened_at,
            source_location=excluded.source_location,
            source_updated_at=excluded.source_updated_at,
            observed_at=excluded.observed_at""",
        (str(rid), state, grade, item.get("first_opened_at"),
         item.get("location"), item.get("updated_at"), iso_now()),
    )


def mark_page(conn: sqlite3.Connection, next_cursor: str | None,
              window_end: str, lookback_minutes: int) -> None:
    if next_cursor:
        conn.execute(
            """UPDATE reader_ingest_progress
               SET page_cursor=?, pages=pages+1, updated_at=?
               WHERE name='reader'""",
            (next_cursor, iso_now()),
        )
    else:
        # Only an EXHAUSTED cursor can advance the completed watermark.
        # Use the START of this scan minus overlap, not the time it finished.
        end = datetime.fromisoformat(window_end.replace("Z", "+00:00"))
        safe_watermark = (end - timedelta(minutes=lookback_minutes)).isoformat()
        conn.execute(
            """UPDATE reader_ingest_progress SET
               window_start=?, window_end=NULL, page_cursor=NULL,
               pages=0, updated_at=? WHERE name='reader'""",
            (safe_watermark, iso_now()),
        )


def stats(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        "SELECT state, COUNT(*) FROM reader_first_pass_queue GROUP BY state"
    ).fetchall()
    progress = conn.execute(
        "SELECT window_start,window_end,page_cursor,pages FROM "
        "reader_ingest_progress WHERE name='reader'"
    ).fetchone()
    return {
        "inventory": {row[0]: row[1] for row in rows},
        "scan": dict(progress) if progress else None,
    }
