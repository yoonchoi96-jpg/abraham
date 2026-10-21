#!/usr/bin/env python3
"""Cloud-only Reader ZIP intake. No billable infrastructure, AI, or public artifacts."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

import yaml

# Direct script execution and import-based tests share the same policy.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from reader_colors import _processing_gate, _reader_tag_names
from scripts.reader_quality import extract_html, NearIndex

PIPELINE = "reader-intake-v1"
CHUNK = 1024 * 1024
MAX_TEXT = 32 * CHUNK
MAX_ARCHIVE = 12 * 1024**3
MAX_EXPANDED = 128 * 1024**3
MAX_MEMBERS = 250_000

def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()

def digest_file(path, algorithm="sha256"):
    h = hashlib.new(algorithm)
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()

def cloud_guard():
    if not (os.getenv("GITHUB_ACTIONS") == "true"
            and os.getenv("RUNNER_ENVIRONMENT") == "github-hosted"
            and os.getenv("RUNNER_OS") == "Linux"):
        raise RuntimeError("GITHUB_HOSTED_LINUX_REQUIRED")
    if os.getenv("READER_PUBLIC_STANDARD_RUNNER") != "true":
        raise RuntimeError("FREE_PUBLIC_STANDARD_RUNNER_GUARD_REQUIRED")

def safe_member(info):
    p = PurePosixPath(info.filename)
    mode = info.external_attr >> 16
    return (not p.is_absolute() and ".." not in p.parts
            and "\\" not in info.filename and "\x00" not in info.filename
            and not stat.S_ISLNK(mode) and not info.flag_bits & 1)

def canonical_url(value):
    if not value:
        return ""
    p = urllib.parse.urlsplit(str(value).strip())
    if p.scheme.lower() not in {"http", "https"} or not p.hostname or p.username or p.password:
        return ""
    # Preserve query and path semantics: different article/version query strings are not merged.
    return urllib.parse.urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path, p.query, ""))

def parse_document(name, payload):
    text = payload.decode("utf-8-sig", errors="strict")
    suffix = PurePosixPath(name).suffix.lower()
    meta = {}
    if suffix == ".json":
        value = json.loads(text)
        if not isinstance(value, dict):
            raise ValueError("UNSUPPORTED_JSON_DOCUMENT")
        meta = value
        text = value.get("content") or value.get("canonical_text") or value.get("html_content") or ""
        if not isinstance(text, str):
            raise ValueError("INVALID_BODY")
        if (not value.get("content") and value.get("html_content")) or re.search(r"<(?:html|body|article|p|div)[\s>]", text, re.I):
            text = extract_html(text)
    elif suffix in {".md", ".markdown"}:
        match = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|\Z)", text, re.S)
        if match:
            if len(match.group(1)) > 128 * 1024:
                raise ValueError("METADATA_TOO_LARGE")
            meta = yaml.safe_load(match.group(1)) or {}
            if not isinstance(meta, dict):
                raise ValueError("INVALID_FRONTMATTER")
            text = text[match.end():]
    elif suffix in {".html", ".htm"}:
        text = extract_html(text)
    elif suffix != ".txt":
        raise ValueError("UNSUPPORTED_DOCUMENT_TYPE")
    # Retain the exact extracted body; no Unicode/whitespace normalization for the hash.
    rid = meta.get("readwise_id") or meta.get("reader_id") or meta.get("document_id") or meta.get("id")
    reader_url = str(meta.get("reader_url") or meta.get("readwise_url") or "")
    if not rid and reader_url:
        match = re.search(r"read\.readwise\.io/read/([A-Za-z0-9_-]+)", reader_url)
        rid = match.group(1) if match else None
    url = canonical_url(meta.get("source_url") or meta.get("url") or "")
    tags = meta.get("tags") or meta.get("readwise_tags") or []
    if isinstance(tags, str):
        tags = [x.strip() for x in tags.split(",") if x.strip()]
    tags = _reader_tag_names({"readwise_tags": tags})
    color, exportable, _ = _processing_gate({"readwise_tags": tags})
    title = str(meta.get("title") or PurePosixPath(name).stem)
    return {"reader_id": str(rid) if rid is not None else None,
            "url": url, "title": title, "content": text, "tags": tags,
            "source_language": str(meta.get("language") or meta.get("source_language") or ""),
            "rating": color, "admission": "selected" if exportable else "held"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS docs(
 body_hash TEXT PRIMARY KEY, canonical_id TEXT UNIQUE NOT NULL,
 shard TEXT NOT NULL, reader_id TEXT, source_url TEXT, admission TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS aliases(
 kind TEXT NOT NULL, value TEXT NOT NULL, body_hash TEXT NOT NULL,
 PRIMARY KEY(kind,value), FOREIGN KEY(body_hash) REFERENCES docs(body_hash));
CREATE TABLE IF NOT EXISTS archives(
 sha256 TEXT PRIMARY KEY, bytes INTEGER NOT NULL, completed_at TEXT NOT NULL);
"""

class Intake:
    def __init__(self, work, state=None):
        self.work = Path(work)
        self.work.mkdir(parents=True, exist_ok=True)
        self.db_path = self.work / "index.sqlite"
        if state and Path(state).resolve() != self.db_path.resolve():
            shutil.copyfile(state, self.db_path)
        self.db = sqlite3.connect(self.db_path)
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)
        self.near = NearIndex(self.db)
        if self.db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("INVALID_PRIOR_STATE")

    def run(self, archive):
        archive = Path(archive)
        if archive.stat().st_size > MAX_ARCHIVE:
            raise RuntimeError("ARCHIVE_LIMIT_EXCEEDED")
        sha = digest_file(archive)
        stats = Counter()
        if self.db.execute("SELECT 1 FROM archives WHERE sha256=?", (sha,)).fetchone():
            return {"archive_sha256": sha, "archive_bytes": archive.stat().st_size,
                    "archive_already_complete": True, "stats": {}, "shards": []}
        shards, writer, uncompressed = [], None, 0
        issues_path = self.work / "issues.jsonl.gz"
        with zipfile.ZipFile(archive) as z, gzip.open(issues_path, "wt", encoding="utf-8") as issues:
            members = z.infolist()
            if len(members) > MAX_MEMBERS or sum(x.file_size for x in members) > MAX_EXPANDED:
                raise RuntimeError("ZIP_RESOURCE_LIMIT_EXCEEDED")
            try:
                for info in members:
                    if info.is_dir():
                        continue
                    stats["members"] += 1
                    # Filenames and document content are private: only hashed references reach issues/logs.
                    member_key = hashlib.sha256(info.filename.encode()).hexdigest()
                    try:
                        if not safe_member(info):
                            raise ValueError("UNSAFE_MEMBER")
                        suffix = PurePosixPath(info.filename).suffix.lower()
                        if suffix not in {".md", ".markdown", ".json", ".txt", ".html", ".htm"}:
                            with z.open(info) as f:
                                for block in iter(lambda: f.read(CHUNK), b""):
                                    stats["attachment_bytes_verified"] += len(block)
                            stats["attachments"] += 1
                            continue
                        if info.file_size > MAX_TEXT:
                            raise ValueError("TEXT_MEMBER_TOO_LARGE")
                        with z.open(info) as f:
                            payload = f.read(MAX_TEXT + 1)
                        if len(payload) > MAX_TEXT:
                            raise ValueError("TEXT_MEMBER_TOO_LARGE")
                        doc = parse_document(info.filename, payload)
                        if not doc["content"].strip():
                            stats["deferred_no_fulltext"] += 1
                            issues.write(json_bytes({"member": member_key, "reason": "DEFERRED_NO_FULLTEXT"}).decode()+"\n")
                            continue
                        body_hash = hashlib.sha256(doc["content"].encode()).hexdigest()
                        aliases = [("id", doc["reader_id"]), ("url", doc["url"])]
                        conflict = False
                        for kind, value in aliases:
                            if value:
                                old = self.db.execute("SELECT body_hash FROM aliases WHERE kind=? AND value=?", (kind, value)).fetchone()
                                conflict = conflict or bool(old and old[0] != body_hash)
                        if conflict:
                            raise ValueError("IDENTITY_CONTENT_CONFLICT")
                        existing = self.db.execute("SELECT canonical_id FROM docs WHERE body_hash=?", (body_hash,)).fetchone()
                        if existing:
                            stats["duplicate_body"] += 1
                        else:
                            if writer is None or uncompressed >= 32 * CHUNK:
                                if writer:
                                    writer.close()
                                name = f"canonical-{sha[:16]}-{len(shards):05d}.jsonl.gz"
                                shards.append(name)
                                writer = gzip.open(self.work / name, "wb")
                                uncompressed = 0
                            cid = "sha256:" + body_hash
                            near_matches = self.near.add(body_hash, doc["content"])
                            record = {"id": doc["reader_id"] or cid, "canonical_id": cid,
                                      "title": doc["title"], "content": doc["content"],
                                      "source": "readwise", "metadata": {
                                          "readwise_id": doc["reader_id"], "url": doc["url"],
                                          "body_sha256": body_hash, "archive_sha256": sha,
                                          "near_duplicate_candidates": near_matches,
                                          "near_duplicate_policy": "advisory-v1-never-merge",
                                          "member_sha256": member_key, "source_language": doc["source_language"]},
                                      "tags": doc["tags"], "rating": doc["rating"],
                                      "admission": doc["admission"], "translation_ko": "",
                                      "entities": [], "relations": []}
                            line = json_bytes(record) + b"\n"
                            writer.write(line)
                            uncompressed += len(line)
                            self.db.execute("INSERT INTO docs VALUES(?,?,?,?,?,?)",
                                            (body_hash, cid, name, doc["reader_id"], doc["url"], doc["admission"]))
                            stats["canonical_new"] += 1
                            stats["selected" if doc["admission"] == "selected" else "held"] += 1
                        for kind, value in aliases:
                            if value:
                                self.db.execute("INSERT OR IGNORE INTO aliases VALUES(?,?,?)", (kind, value, body_hash))
                    except (ValueError, UnicodeError, yaml.YAMLError, zipfile.BadZipFile, NotImplementedError) as exc:
                        reason = str(exc) if str(exc) in {
                            "UNSAFE_MEMBER", "TEXT_MEMBER_TOO_LARGE", "IDENTITY_CONTENT_CONFLICT",
                            "INVALID_FRONTMATTER", "METADATA_TOO_LARGE", "INVALID_BODY",
                            "UNSUPPORTED_JSON_DOCUMENT", "HTML_EXTRACTION_FAILED"} else type(exc).__name__
                        issues.write(json_bytes({"member": member_key, "reason": reason}).decode()+"\n")
                        stats["deferred_extraction" if reason == "HTML_EXTRACTION_FAILED" else "quarantined"] += 1
                    # One archive transaction: partial indexes must never reference
                    # truncated shards after interruption. Bulk checkpoints are per item.
                if writer:
                    writer.close()
                    writer = None
                # Durable shard bytes precede the index commit.
                for shard in shards:
                    with (self.work/shard).open("rb") as durable:
                        os.fsync(durable.fileno())
                issues.close()
                with issues_path.open("rb") as durable:
                    os.fsync(durable.fileno())
                self.db.execute("INSERT INTO archives VALUES(?,?,?)",
                                (sha, archive.stat().st_size, datetime.now(timezone.utc).isoformat()))
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise
            finally:
                if writer:
                    writer.close()
        return {"archive_sha256": sha, "archive_bytes": archive.stat().st_size,
                "archive_already_complete": False, "stats": dict(stats), "shards": shards}

    def close(self):
        self.db.close()

def request_json(url, data=None, headers=None, method=None):
    request = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        # Never propagate signed URLs, bodies, credentials, or document text to public logs.
        suffix = ""
        if urllib.parse.urlsplit(url).hostname == "oauth2.googleapis.com":
            try:
                marker = json.loads(exc.read(16 * 1024)).get("error", "")
                if marker in {"invalid_grant", "invalid_client", "unauthorized_client", "invalid_scope"}:
                    suffix = "_" + marker.upper()
            except (ValueError, AttributeError):
                pass
        raise RuntimeError(f"REMOTE_HTTP_{exc.code}" + suffix) from None
    except urllib.error.URLError:
        raise RuntimeError("REMOTE_NETWORK_ERROR") from None

class Drive:
    def __init__(self):
        raw = os.getenv("DRIVE_OAUTH_JSON", "")
        if not raw:
            config = {k: os.getenv(env, "").strip() for k, env in {
                "client_id": "GOOGLE_DRIVE_OAUTH_CLIENT_ID",
                "client_secret": "GOOGLE_DRIVE_OAUTH_CLIENT_SECRET",
                "refresh_token": "GOOGLE_DRIVE_OAUTH_REFRESH_TOKEN"}.items()}
            if not all(config.values()):
                raise RuntimeError("MISSING_DRIVE_OAUTH_JSON")
            raw = json.dumps(config)
        try:
            self.config = {k: str(v).strip() for k, v in json.loads(raw).items()}
            if not all(self.config.get(k) for k in ("client_id", "client_secret", "refresh_token")):
                raise ValueError()
        except (ValueError, TypeError):
            raise RuntimeError("INVALID_DRIVE_OAUTH_JSON") from None
        self.token, self.expires = "", 0

    def headers(self):
        if time.time() > self.expires:
            response = request_json("https://oauth2.googleapis.com/token",
                data=urllib.parse.urlencode({k: self.config[k] for k in ("client_id", "client_secret", "refresh_token")} |
                                            {"grant_type": "refresh_token"}).encode())
            self.token = response["access_token"]
            self.expires = time.time() + int(response.get("expires_in", 3600)) - 60
        return {"Authorization": "Bearer " + self.token}

    def api(self, path, params=None, data=None, method=None):
        url = "https://www.googleapis.com/drive/v3/" + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = self.headers() | ({"Content-Type": "application/json"} if data is not None else {})
        return request_json(url, json_bytes(data) if data is not None else None, headers, method)

    def list(self, query, fields):
        rows, token = [], None
        while True:
            params = {"q": query, "fields": f"nextPageToken,files({fields})",
                      "pageSize": 1000, "orderBy": "createdTime desc"}
            if token:
                params["pageToken"] = token
            page = self.api("files", params)
            rows.extend(page.get("files", []))
            token = page.get("nextPageToken")
            if not token:
                return rows

    def folder(self):
        configured = os.getenv("READER_DRIVE_FOLDER_ID", "")
        if configured:
            folder = self.api("files/" + urllib.parse.quote(configured, safe=""),
                              {"fields": "id,mimeType,shared,capabilities(canAddChildren)"})
            if folder["mimeType"] != "application/vnd.google-apps.folder" or folder.get("shared"):
                raise RuntimeError("PRIVATE_DRIVE_FOLDER_REQUIRED")
            if not folder.get("capabilities", {}).get("canAddChildren"):
                raise RuntimeError("DRIVE_FOLDER_NOT_WRITABLE")
            return folder["id"]
        matches = self.list("trashed=false and mimeType='application/vnd.google-apps.folder' and "
                            "appProperties has { key='pipeline' and value='"+PIPELINE+"' }", "id,shared")
        if matches:
            if matches[0].get("shared"):
                raise RuntimeError("PRIVATE_DRIVE_FOLDER_REQUIRED")
            return matches[0]["id"]
        return self.api("files", data={"name": "Reader Intake — GitHub", "mimeType":
                        "application/vnd.google-apps.folder", "appProperties": {"pipeline": PIPELINE}},
                        method="POST")["id"]

    def source_request(self, doc_id):
        meta = self.api("files/" + urllib.parse.quote(doc_id, safe=""),
                        {"fields": "mimeType,shared"})
        if meta.get("shared") or meta.get("mimeType") != "application/vnd.google-apps.document":
            raise RuntimeError("PRIVATE_REQUEST_DOCUMENT_REQUIRED")
        url = ("https://www.googleapis.com/drive/v3/files/" +
               urllib.parse.quote(doc_id, safe="") + "/export?mimeType=text%2Fplain")
        try:
            request = urllib.request.Request(url, headers=self.headers())
            with urllib.request.urlopen(request, timeout=120) as response:
                payload = response.read(64 * 1024 + 1)
            if len(payload) > 64 * 1024:
                raise RuntimeError("SOURCE_REQUEST_TOO_LARGE")
            data = json.loads(payload.decode("utf-8-sig").strip())
        except (ValueError, urllib.error.HTTPError, urllib.error.URLError):
            raise RuntimeError("SOURCE_REQUEST_UNREADABLE") from None
        if data.get("schema") != "reader-source-request-v1" or not data.get("download_url"):
            raise RuntimeError("INVALID_SOURCE_REQUEST")
        return data

    def require_capacity(self, required_bytes):
        quota = self.api("about", {"fields": "storageQuota"})["storageQuota"]
        limit = quota.get("limit")
        if not limit:
            raise RuntimeError("DRIVE_STORAGE_LIMIT_UNVERIFIED")
        if int(limit) - int(quota.get("usage", 0)) < required_bytes:
            raise RuntimeError("DRIVE_EXISTING_QUOTA_EXHAUSTED")

    def get_media(self, file_id, destination):
        request = urllib.request.Request("https://www.googleapis.com/drive/v3/files/"
                    + urllib.parse.quote(file_id, safe="") + "?alt=media", headers=self.headers())
        try:
            with urllib.request.urlopen(request, timeout=120) as src, Path(destination).open("wb") as dst:
                shutil.copyfileobj(src, dst, CHUNK)
        except (urllib.error.HTTPError, urllib.error.URLError):
            raise RuntimeError("DRIVE_DOWNLOAD_FAILED") from None

    def upload(self, path, folder, kind):
        path = Path(path)
        sha, size = digest_file(path), path.stat().st_size
        # Content-addressed immutable files; reuse only when Drive's size and MD5 agree.
        query = ("trashed=false and '"+folder+"' in parents and appProperties has "
                 "{ key='pipeline' and value='"+PIPELINE+"' } and appProperties has "
                 "{ key='sha256' and value='"+sha+"' }")
        existing = self.list(query, "id,size,md5Checksum")
        md5 = digest_file(path, "md5")
        for f in existing:
            if int(f.get("size", -1)) == size and f.get("md5Checksum") == md5:
                return f["id"]
        metadata = {"name": f"{sha[:16]}-{path.name}", "parents": [folder], "appProperties":
                    {"pipeline": PIPELINE, "sha256": sha, "kind": kind}}
        url = "https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&fields=id,size,md5Checksum"
        request = urllib.request.Request(url, data=json_bytes(metadata), method="POST",
            headers=self.headers() | {"Content-Type": "application/json", "X-Upload-Content-Length": str(size)})
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                session = response.headers["Location"]
            parsed = urllib.parse.urlsplit(session)
            if parsed.scheme != "https" or parsed.hostname != "www.googleapis.com":
                raise RuntimeError("INVALID_UPLOAD_SESSION")
            with path.open("rb") as f:
                offset = 0
                while offset < size:
                    block = f.read(8 * CHUNK)
                    req = urllib.request.Request(session, data=block, method="PUT",
                        headers=self.headers() | {"Content-Length": str(len(block)),
                            "Content-Range": f"bytes {offset}-{offset+len(block)-1}/{size}"})
                    try:
                        with urllib.request.urlopen(req, timeout=120) as response:
                            result = json.load(response)
                    except urllib.error.HTTPError as exc:
                        if exc.code != 308:
                            raise
                    offset += len(block)
        except (urllib.error.HTTPError, urllib.error.URLError):
            # Retry the whole job. Completed immutable uploads are reused; incomplete sessions expire.
            raise RuntimeError("DRIVE_UPLOAD_FAILED") from None
        if int(result.get("size", -1)) != size or result.get("md5Checksum") != md5:
            raise RuntimeError("DRIVE_UPLOAD_CHECKSUM_MISMATCH")
        return result["id"]

def cli_json(*args):
    process = subprocess.run(["readwise", *args, "--json"], capture_output=True, text=True, timeout=180)
    if process.returncode:
        raise RuntimeError("READWISE_CLI_FAILED")
    try:
        return json.loads(process.stdout)
    except ValueError:
        raise RuntimeError("READWISE_CLI_INVALID_JSON") from None

def reader_export(work):
    token = os.getenv("READWISE_TOKEN", "")
    if not token:
        raise RuntimeError("MISSING_READWISE_TOKEN")
    login = subprocess.run(["readwise", "login-with-token"], input=token+"\n",
                           text=True, capture_output=True, timeout=120)
    if login.returncode:
        raise RuntimeError("READWISE_LOGIN_FAILED")
    export_id = os.getenv("READER_EXPORT_ID", "").strip()
    if not export_id:
        result = cli_json("reader-export-documents")
        export_id = result.get("export_id") or result.get("id")
        if not export_id:
            raise RuntimeError("READER_EXPORT_ID_MISSING")
    deadline = time.monotonic() + 60 * 60
    while time.monotonic() < deadline:
        result = cli_json("reader-get-export-documents-status", "--export-id", export_id)
        if result.get("status") == "completed" and result.get("download_url"):
            return download_zip(result["download_url"], work)
        time.sleep(30)
    raise RuntimeError("READER_EXPORT_NOT_READY_RERUN_WITH_SAME_EXPORT_ID")

def download_zip(url, work):
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or ""
    if parsed.scheme != "https" or not (host.endswith(".amazonaws.com") or host.endswith(".readwise.io")):
        raise RuntimeError("UNEXPECTED_EXPORT_HOST")
    path = Path(work) / "source.zip"
    try:
        with urllib.request.urlopen(url, timeout=120) as src, path.open("wb") as dst:
            size = 0
            for block in iter(lambda: src.read(CHUNK), b""):
                size += len(block)
                if size > MAX_ARCHIVE or shutil.disk_usage(work).free < 1024**3:
                    raise RuntimeError("DOWNLOAD_RESOURCE_LIMIT")
                dst.write(block)
    except (urllib.error.HTTPError, urllib.error.URLError):
        raise RuntimeError("EXPORT_DOWNLOAD_FAILED") from None
    return path

def run_cloud():
    cloud_guard()
    with tempfile.TemporaryDirectory(prefix="reader-intake-", dir=os.environ["RUNNER_TEMP"]) as work:
        drive = Drive()
        folder = drive.folder()
        prior = drive.list("'"+folder+"' in parents and trashed=false and appProperties has "
                           "{ key='pipeline' and value='"+PIPELINE+"' } and appProperties has "
                           "{ key='kind' and value='commit' }", "id")
        parent, state = None, None
        if prior:
            manifest_path = Path(work) / "previous.json"
            parent = prior[0]["id"]
            drive.get_media(parent, manifest_path)
            previous = json.loads(manifest_path.read_text())
            state = Path(work) / "restored.sqlite"
            drive.get_media(previous["index"]["id"], state)
            if digest_file(state) != previous["index"]["sha256"]:
                raise RuntimeError("PRIOR_STATE_CHECKSUM_MISMATCH")
        source_doc = os.getenv("READER_REQUEST_DOC_ID", "")
        source_request = drive.source_request(source_doc) if source_doc else None
        archive = (download_zip(source_request["download_url"], work)
                   if source_request else reader_export(work))
        if shutil.disk_usage(work).free < 2 * 1024**3:
            raise RuntimeError("INSUFFICIENT_EPHEMERAL_DISK")
        engine = Intake(work, state)
        try:
            result = engine.run(archive)
        finally:
            engine.close()
        if result["archive_already_complete"]:
            print(json.dumps({"event": "reader_intake", "status": "already_complete", "additional_api_cost": 0}))
            return
        # Preserve the untouched ZIP before any new state becomes discoverable.
        required = archive.stat().st_size + sum(
            (Path(work)/name).stat().st_size for name in result["shards"] + ["issues.jsonl.gz", "index.sqlite"])
        drive.require_capacity(required + 1024 * 1024)
        source_id = drive.upload(archive, folder, "source")
        files = []
        for name in result["shards"] + ["issues.jsonl.gz"]:
            files.append({"name": name, "id": drive.upload(Path(work)/name, folder, "canonical" if name in result["shards"] else "quarantine"),
                          "sha256": digest_file(Path(work)/name)})
        index = Path(work) / "index.sqlite"
        index_id = drive.upload(index, folder, "index")
        manifest = result | {"schema": PIPELINE, "parent_commit": parent, "source_id": source_id,
            "files": files, "index": {"id": index_id, "sha256": digest_file(index)},
            "run_id": os.getenv("GITHUB_RUN_ID"), "commit_sha": os.getenv("GITHUB_SHA"),
            "abraham_status": "intake_staged_not_promoted",
            "nav_status": "not_promoted_pending_abraham_qa",
            "translation_calls": 0, "additional_infrastructure_cost": 0}
        manifest_path = Path(work) / "manifest.json"
        manifest_path.write_bytes(json_bytes(manifest))
        # Commit marker last. Failures never replace the previous canonical index.
        drive.upload(manifest_path, folder, "commit")
        print(json.dumps({"event": "reader_intake", "status": "committed",
                          "archive_bytes": result["archive_bytes"], "stats": result["stats"],
                          "translation_calls": 0, "infrastructure_cost": 0}))

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cloud", action="store_true")
    args = parser.parse_args()
    if not args.cloud:
        parser.error("--cloud is required; production data never runs on a local machine")
    try:
        run_cloud()
    except Exception as exc:
        # Whitelist operational error codes; never print exception repr/traceback containing URLs/data.
        code = str(exc)
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,100}", code):
            code = type(exc).__name__
        print(json.dumps({"event": "reader_intake_failed", "reason": code}), file=sys.stderr)
        raise SystemExit(1)

if __name__ == "__main__":
    main()
