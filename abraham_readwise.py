from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

DEFAULT_PAGE_SIZE = 100
DEFAULT_API = "https://readwise.io/api/v3/list/"

def _env(name: str, required: bool = True) -> str:
    value = os.getenv(name, "").strip()
    if required and not value:
        raise RuntimeError(f"{name} is required")
    return value

def _db_path() -> Path:
    return Path(_env("ABRAHAM_DB_PATH")).expanduser()

def _api_url() -> str:
    return os.getenv("READWISE_API_URL", DEFAULT_API).strip() or DEFAULT_API

def _headers() -> dict[str, str]:
    return {"Authorization": f"Token {_env('READWISE_TOKEN')}"}

def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

def _canonical_text(item: dict[str, Any]) -> str:
    for key in ("html", "summary", "source_url", "notes"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""

def _columns(conn: sqlite3.Connection) -> set[str]:
    return {str(r[1]) for r in conn.execute("PRAGMA table_info(documents)").fetchall()}

def _pick(columns: set[str], *names: str) -> str | None:
    return next((x for x in names if x in columns), None)

def _ensure_meta(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS abraham_runtime_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.commit()

def _get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM abraham_runtime_meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None

def _set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("""
        INSERT INTO abraham_runtime_meta(key,value,updated_at)
        VALUES(?,?,?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
    """, (key, value, _utc_now()))

def _item_to_row(item: dict[str, Any], columns: set[str]) -> dict[str, Any]:
    rid = str(item.get("id") or item.get("uuid") or "").strip()
    if not rid:
        raise ValueError("Readwise item has no id")

    title = str(item.get("title") or "").strip()
    text = _canonical_text(item)
    now = _utc_now()
    values: dict[str, Any] = {}

    mapping = {
        "readwise_id": rid,
        "id": rid,
        "title": title,
        "content": text,
        "canonical_text": text,
        "source_url": item.get("source_url") or item.get("url") or "",
        "url": item.get("source_url") or item.get("url") or "",
        "author": item.get("author") or "",
        "category": item.get("category") or "",
        "readwise_tags": json.dumps(item.get("tags") or [], ensure_ascii=False),
        "first_seen_at": now,
        "updated_at": now,
        "last_processed_at": now,
    }
    for k, v in mapping.items():
        if k in columns:
            values[k] = v
    return values

def _upsert(conn: sqlite3.Connection, row: dict[str, Any], columns: set[str]) -> bool:
    rid_col = _pick(columns, "readwise_id", "id")
    if not rid_col:
        raise RuntimeError("documents table has neither readwise_id nor id")

    existing = conn.execute(
        f'SELECT rowid FROM documents WHERE "{rid_col}"=?',
        (row[rid_col],),
    ).fetchone()

    if existing:
        update_cols = [k for k in row if k in columns and k not in {"id", "readwise_id", "first_seen_at"}]
        if not update_cols:
            return False
        sets = ",".join(f'"{k}"=?' for k in update_cols)
        conn.execute(
            f'UPDATE documents SET {sets} WHERE "{rid_col}"=?',
            tuple(row[k] for k in update_cols) + (row[rid_col],),
        )
        return True

    cols = [k for k in row if k in columns]
    placeholders = ",".join("?" for _ in cols)
    conn.execute(
        f'INSERT INTO documents ({",".join(chr(34)+k+chr(34) for k in cols)}) VALUES ({placeholders})',
        tuple(row[k] for k in cols),
    )
    return True

def sync_readwise() -> dict[str, Any]:
    db = _db_path()
    if not db.exists():
        raise FileNotFoundError(f"SQLite database not found: {db}")

    with sqlite3.connect(db) as conn:
        columns = _columns(conn)
        _ensure_meta(conn)
        cursor = _get_meta(conn, "readwise_next_page_cursor")
        params: dict[str, Any] = {"page_size": DEFAULT_PAGE_SIZE}
        if cursor:
            params["pageCursor"] = cursor
        else:
            updated_after = _get_meta(conn, "readwise_last_sync_at")
            if updated_after:
                params["updatedAfter"] = updated_after

        imported = 0
        pages = 0
        total = 0
        next_cursor = None

        while True:
            response = requests.get(
                _api_url(),
                headers=_headers(),
                params=params,
                timeout=60,
            )
            if response.status_code == 429:
                retry_after = int(response.headers.get("Retry-After", "30"))
                raise RuntimeError(f"Readwise API rate limited (retry after {retry_after}s)")
            response.raise_for_status()
            payload = response.json()
            pages += 1
            results = payload.get("results") or []
            total += len(results)

            for item in results:
                if _upsert(conn, _item_to_row(item, columns), columns):
                    imported += 1

            next_cursor = payload.get("nextPageCursor")
            if not next_cursor:
                break
            params = {"page_size": DEFAULT_PAGE_SIZE, "pageCursor": next_cursor}

        _set_meta(conn, "readwise_last_sync_at", _utc_now())
        _set_meta(conn, "readwise_next_page_cursor", "")
        conn.commit()

    result = {"pages": pages, "fetched": total, "changed": imported}
    print(f"[READWISE] pages={pages} fetched={total} changed={imported}")
    return result

if __name__ == "__main__":
    print(json.dumps(sync_readwise(), ensure_ascii=False, indent=2))
