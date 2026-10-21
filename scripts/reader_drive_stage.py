"""Read-only Google Drive Library staging for Abraham.

Requires an explicitly authorized user OAuth credential already understood by
reader_intake.Drive; no Reader API writes, Drive mutations or production import.
The 9-color model labels in filenames are *unverified existing metadata*, not
human ground truth. Work and SQLite are private, persistent, LOCAL storage.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import stat
from pathlib import Path

from scripts.reader_bulk import Bulk, DriveSource
from scripts.reader_intake import Drive, parse_document
from scripts.reader_quality import NearIndex

DEFAULT_LIBRARY = "1KIsyDd_EUJuDxMIvrnIvi2VWriaxaB2U"
COLOR_BY_KOREAN = {
    "⚪하양": "white", "⚪흰색": "white", "🔴빨강": "red",
    "🟠주황": "orange", "🟡노랑": "yellow",
    "🟢초록": "green", "🔵파랑": "blue",
    "🟦남색": "navy", "🟣보라": "purple",
    "🟨금색": "gold", "🏆금색": "gold",
}
PREFIX = re.compile(r"^【([^】]+)】\s*")
READER_ID = re.compile(r"\((01[a-z0-9]{20,32})\)\.(?:html?|md|txt)$", re.I)


def _private_work(directory):
    work = Path(directory).expanduser().resolve()
    if any((ancestor / ".git").exists() for ancestor in (work, *work.parents)):
        raise RuntimeError("PUBLIC_REPOSITORY_WORKSPACE_FORBIDDEN")
    work.mkdir(parents=True, exist_ok=True, mode=0o700)
    if stat.S_IMODE(work.stat().st_mode) & 0o077:
        raise RuntimeError("PRIVATE_WORKSPACE_REQUIRED")
    return work


def _existing_label(name):
    match = PREFIX.match(name or "")
    if not match:
        return None, None
    label = match.group(1)
    if label.startswith("보류·"):
        return None, "EXISTING_DEFERRED_MARKER"
    if label.startswith("중복·"):
        return None, "EXISTING_DUPLICATE_MARKER"
    found = [v for prefix, v in COLOR_BY_KOREAN.items() if label.startswith(prefix)]
    if len(set(found)) == 1:
        return found[0], "EXISTING_FILE_NAME_LABEL"
    return None, "UNRECOGNIZED_FILE_NAME_LABEL"


class DriveLibraryStager:
    """No classification, no translation, no content deletion, no Drive writes.

    All extracted identity rows and MinHash sketches are stored locally. Original
    Drive files are downloaded by Bulk into private .source files to support
    idempotent processor replay; they are NOT committed to GitHub.
    """

    def __init__(self, work: str | Path):
        self.work = _private_work(work)
        self.db = sqlite3.connect(self.work / "reader-library-stage.sqlite")
        os.chmod(self.work / "reader-library-stage.sqlite", 0o600)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS stage (
                job_key TEXT PRIMARY KEY, drive_file_id TEXT NOT NULL,
                drive_version TEXT NOT NULL, body_sha256 TEXT,
                reader_id TEXT, source_color TEXT, filename_color TEXT,
                state TEXT NOT NULL, reason TEXT NOT NULL,
                near_candidate_count INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_stage_body ON stage(body_sha256);
        """)
        self.near = NearIndex(self.db)

    def process(self, key, metadata, raw_path):
        if self.db.execute("SELECT 1 FROM stage WHERE job_key=?", (key,)).fetchone():
            return
        name = metadata.get("name") or metadata["id"] + ".html"
        filename_color, label_reason = _existing_label(name)
        found_id = READER_ID.search(name)
        reader_id = found_id.group(1) if found_id else None
        source_color = None
        body_hash = None
        near_count = 0
        reason = "NEEDS_CLASSIFICATION"
        state = "review_required"
        try:
            if not name.lower().endswith((".html", ".htm", ".md", ".markdown", ".txt", ".json")):
                reason, state = "UNSUPPORTED_SOURCE_TYPE", "deferred_no_fulltext"
            else:
                doc = parse_document(name, raw_path.read_bytes())
                text = doc["content"]
                source_color = doc["rating"]
                reader_id = doc["reader_id"] or reader_id
                if not text.strip():
                    reason, state = "DEFERRED_NO_FULLTEXT", "deferred_no_fulltext"
                else:
                    body_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
                    seen = self.db.execute(
                        "SELECT job_key FROM stage WHERE body_sha256=? LIMIT 1",
                        (body_hash,)
                    ).fetchone()
                    if seen:
                        state, reason = "duplicate", "EXACT_BODY_DUPLICATE"
                    else:
                        near_count = len(self.near.add(body_hash, text))
                        if source_color:
                            # Tagged in source, but not independently confirmed.
                            state, reason = "existing_tag_unverified", "SOURCE_METADATA_COLOR"
                        elif filename_color:
                            state, reason = "existing_tag_unverified", "DRIVE_FILENAME_COLOR"
                        elif label_reason == "EXISTING_DEFERRED_MARKER":
                            state, reason = "review_required", "PREVIOUS_DEFERRED_LABEL_RECHECK"
                        else:
                            state, reason = "review_required", "NEEDS_CLASSIFICATION"
        except (UnicodeError, ValueError, json.JSONDecodeError):
            state, reason = "deferred_no_fulltext", "EXTRACTION_FAILED"
        with self.db:
            self.db.execute(
                """INSERT OR IGNORE INTO stage
                   (job_key,drive_file_id,drive_version,body_sha256,reader_id,
                    source_color,filename_color,state,reason,near_candidate_count)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (key, metadata["id"], str(metadata["version"]), body_hash,
                 reader_id, source_color, filename_color, state, reason, near_count)
            )

    def status(self):
        return dict(self.db.execute(
            "SELECT state, COUNT(*) FROM stage GROUP BY state ORDER BY state"
        ).fetchall())

    def close(self):
        self.db.close()


def main():
    parser = argparse.ArgumentParser(description="Read-only Drive Reader Library staging")
    parser.add_argument("--work", required=True, help="Private persistent LOCAL directory, never a GitHub checkout")
    parser.add_argument("--folder-id", default=DEFAULT_LIBRARY)
    parser.add_argument("--max-items", type=int, default=1000)
    parser.add_argument("--status-only", action="store_true")
    parser.add_argument("--rescan", action="store_true",
                        help="Explicitly start a new full scan; preserve all prior keys")
    args = parser.parse_args()
    stage = DriveLibraryStager(args.work)
    try:
        if args.status_only:
            print(json.dumps({"staging": stage.status()}))
            return
        bulk = Bulk(stage.work, [args.folder_id], workers=1)
        try:
            if args.rescan:
                bulk.start_scan()
            result = bulk.run(DriveSource(Drive()), stage.process, max_items=args.max_items)
            print(json.dumps({
                "run": result, "staging": stage.status(),
                "drive_writes": 0, "reader_api_writes": 0,
                "verified_new_colors": 0, "api_cost": 0
            }))
        finally:
            bulk.close()
    finally:
        stage.close()


if __name__ == "__main__":
    main()
