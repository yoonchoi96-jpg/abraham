"""Read-only Drive source + resumable, single-writer bulk interface.

No Reader writes, Drive writes, promotion, inference or credential discovery.
Call Bulk.run(source, processor); processor(key, metadata, raw_path) must be
idempotent for key and persist output before returning. Source roots are explicit
IDs for Reader_Uploaded_Files/Library, never guessed from nonunique names.
"""
import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Protocol


class Source(Protocol):
    def page(self, folder: str, token: str | None) -> dict: ...
    def download(self, metadata: dict, destination: Path) -> None: ...


class DriveSource:
    """Adapter around reader_intake.Drive; only GET/list/export, bounded bytes."""
    def __init__(self, drive, max_bytes=32 * 1024**2):
        self.drive, self.max_bytes = drive, max_bytes

    def page(self, folder, token):
        # IDs cannot escape the Drive query language.
        if not folder or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in folder):
            raise ValueError('INVALID_FOLDER_ID')
        params = {'q': f"'{folder}' in parents and trashed=false",
                  'pageSize': 100, 'fields': 'nextPageToken,files(id,name,mimeType,version,modifiedTime,size,shared)'}
        if token:
            params['pageToken'] = token
        return self.drive.api('files', params)

    def download(self, metadata, destination):
        import urllib.request
        import urllib.parse
        fid = urllib.parse.quote(metadata['id'], safe='')
        def version():
            return self.drive.api('files/'+fid, {'fields':'version,shared'})
        before = version()
        if before.get('shared') or str(before.get('version')) != str(metadata['version']):
            raise ValueError('SOURCE_SHARED_OR_CHANGED')
        mime = metadata['mimeType']
        supported = {'text/plain','text/markdown','text/html','application/json','application/vnd.google-apps.document'}
        if mime not in supported:
            raise ValueError('UNSUPPORTED_SOURCE_TYPE')
        suffix = '/export?mimeType=text%2Fplain' if mime.endswith('google-apps.document') else '?alt=media'
        request = urllib.request.Request('https://www.googleapis.com/drive/v3/files/'+fid+suffix,
                                         headers=self.drive.headers())
        with urllib.request.urlopen(request, timeout=120) as response, destination.open('wb') as output:
            size = 0
            while block := response.read(1024*1024):
                size += len(block)
                if size > self.max_bytes:
                    raise ValueError('SOURCE_TOO_LARGE')
                output.write(block)
            output.flush()
            os.fsync(output.fileno())
        if version() != before:
            raise ValueError('SOURCE_CHANGED_DURING_READ')


class Bulk:
    """Persist discovery pages + per-version jobs; default and maximum workers=1.

    Exclusive owner-local lock prevents competing consumers; pending/running jobs
    resume after process death. Never store exception strings or raw names in logs.
    Transient errors get bounded exponential retry; permanent errors are held.
    """
    def __init__(self, work, roots, *, workers=1, retries=3, sleep=time.sleep):
        if workers != 1 or not 1 <= retries <= 5 or not roots:
            raise ValueError('BOUNDED_SINGLE_WRITER_REQUIRED')
        self.work = Path(work)
        self.work.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.roots, self.retries, self.sleep = roots, retries, sleep
        self.db = sqlite3.connect(self.work/'bulk.sqlite')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS folders(id TEXT PRIMARY KEY, token TEXT, done INTEGER DEFAULT 0);
          CREATE TABLE IF NOT EXISTS jobs(key TEXT PRIMARY KEY, metadata TEXT, state TEXT DEFAULT 'pending',
            attempts INTEGER DEFAULT 0, reason TEXT, raw_sha256 TEXT);
        ''')

    def _retry(self, fn):
        for attempt in range(self.retries):
            try:
                return fn()
            except Exception as exc:
                import urllib.error
                transient = (isinstance(exc, (TimeoutError, ConnectionError)) or
                             isinstance(exc, urllib.error.URLError) and not isinstance(exc, urllib.error.HTTPError) or
                             isinstance(exc, urllib.error.HTTPError) and exc.code in {408,429,500,502,503,504} or
                             str(exc) in {'REMOTE_NETWORK_ERROR','REMOTE_HTTP_429','REMOTE_HTTP_500','REMOTE_HTTP_502','REMOTE_HTTP_503','REMOTE_HTTP_504'})
                if not transient or attempt == self.retries-1:
                    raise
                self.sleep(min(30, 2**attempt))

    def run(self, source: Source, processor, *, max_items=1000):
        import fcntl
        if not 1 <= max_items <= 100000:
            raise ValueError('BOUNDED_RUN_REQUIRED')
        with (self.work/'bulk.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.db:
                self.db.executemany('INSERT OR IGNORE INTO folders(id) VALUES(?)', [(x,) for x in self.roots])
                self.db.execute("UPDATE jobs SET state='pending' WHERE state='running'")
            pages = 0
            while pages < max_items:
                row = self.db.execute('SELECT id,token FROM folders WHERE done=0 ORDER BY id LIMIT 1').fetchone()
                if row is None:
                    break
                folder, token = row
                page = self._retry(lambda: source.page(folder, token))
                next_token = page.get('nextPageToken')
                if next_token is not None and next_token == token:
                    raise ValueError('NONADVANCING_PAGE')
                # Persist jobs and the next page atomically. Replaying a page is safe.
                with self.db:
                    for item in page.get('files', []):
                        if item['mimeType'] == 'application/vnd.google-apps.folder':
                            self.db.execute('INSERT OR IGNORE INTO folders(id) VALUES(?)', (item['id'],))
                        else:
                            if not item.get('version'):
                                raise ValueError('SOURCE_VERSION_REQUIRED')
                            key = hashlib.sha256(('drive:'+item['id']+':'+str(item['version'])).encode()).hexdigest()
                            self.db.execute('INSERT OR IGNORE INTO jobs(key,metadata) VALUES(?,?)', (key,json.dumps(item)))
                    self.db.execute('UPDATE folders SET token=?,done=? WHERE id=?', (next_token,not bool(next_token),folder))
                pages += 1
            completed = held = 0
            for _ in range(max_items):
                row = self.db.execute("SELECT key,metadata FROM jobs WHERE state='pending' ORDER BY key LIMIT 1").fetchone()
                if row is None:
                    break
                key, raw_meta = row
                raw, temp = self.work/(key+'.source'), self.work/(key+'.partial')
                with self.db:
                    self.db.execute("UPDATE jobs SET state='running',attempts=attempts+1 WHERE key=?", (key,))
                try:
                    self._retry(lambda: source.download(json.loads(raw_meta), temp))
                    digest = hashlib.sha256(temp.read_bytes()).hexdigest()
                    os.replace(temp, raw)
                    # Raw source is retained even if extraction/processing fails.
                    processor(key, json.loads(raw_meta), raw)
                    with self.db:
                        self.db.execute("UPDATE jobs SET state='done',raw_sha256=? WHERE key=?", (digest,key))
                    completed += 1
                except Exception:
                    with self.db:
                        self.db.execute("UPDATE jobs SET state='held',reason='PROCESSING_FAILED' WHERE key=?", (key,))
                    held += 1
                finally:
                    temp.unlink(missing_ok=True)
            return {'completed':completed, 'held':held, 'pages':pages,
                    'pending':self.db.execute("SELECT count(*) FROM jobs WHERE state='pending'").fetchone()[0],
                    'discovery_complete':not bool(self.db.execute('SELECT 1 FROM folders WHERE done=0 LIMIT 1').fetchone())}

    def start_scan(self):
        # Explicit new discovery pass picks up new files and changed versions.
        # Existing per-version keys stay immutable; removed files are never deleted.
        with self.db:
            self.db.execute("UPDATE folders SET token=NULL,done=0")

    def retry_held(self):
        # Explicit operator retry, not an infinite automatic failure loop.
        with self.db:
            self.db.execute("UPDATE jobs SET state='pending' WHERE state='held'")

    def close(self):
        self.db.close()
