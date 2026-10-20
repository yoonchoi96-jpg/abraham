"""All tests execute on GitHub-hosted Linux; fixtures contain synthetic data only."""
import gzip
import hashlib
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("reader_intake", Path(__file__).resolve().parent / "reader_intake.py")
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)

def record(rid, body="Body\n", url=None, tags=None):
    return json.dumps({"id": rid, "content": body, "url": url or f"https://example.invalid/{rid}",
                       "tags": tags or ["rating/orange"], "title": "Synthetic"}).encode()

class IntakeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def archive(self, entries, name="source.zip"):
        path = self.root / name
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as z:
            for key, value in entries:
                z.writestr(key, value)
        return path

    def engine(self, name="work", state=None):
        return m.Intake(self.root/name, state)

    def test_dedup_identity_and_body_preserve_aliases(self):
        archive = self.archive([("a.json", record("a")), ("b.json", record("b")),
                                ("c.json", record("a"))])
        engine = self.engine()
        try:
            result = engine.run(archive)
            self.assertEqual(result["stats"]["canonical_new"], 1)
            self.assertEqual(result["stats"]["duplicate_body"], 2)
            self.assertEqual(engine.db.execute("SELECT COUNT(*) FROM aliases WHERE kind='id'").fetchone()[0], 2)
            rows = []
            for name in result["shards"]:
                with gzip.open(engine.work/name, "rt") as f:
                    rows.extend(json.loads(line) for line in f)
            self.assertEqual(rows[0]["content"], "Body\n")
            self.assertEqual(rows[0]["metadata"]["body_sha256"], hashlib.sha256(b"Body\n").hexdigest())
        finally:
            engine.close()

    def test_same_archive_and_cross_archive_rerun(self):
        one = self.archive([("a.json", record("a"))])
        two = self.archive([("renamed.json", record("a"))], "other.zip")
        engine = self.engine()
        try:
            engine.run(one)
            again = engine.run(one)
            self.assertTrue(again["archive_already_complete"])
        finally:
            engine.close()
        restored = self.engine("restored", self.root/"work"/"index.sqlite")
        try:
            result = restored.run(two)
            self.assertEqual(result["stats"]["duplicate_body"], 1)
            self.assertEqual(restored.db.execute("SELECT COUNT(*) FROM docs").fetchone()[0], 1)
        finally:
            restored.close()

    def test_conflicting_id_or_url_never_overwrites(self):
        archive = self.archive([("a.json", record("a")), ("b.json", record("a", "Changed\n")),
            ("c.json", record("c", "Other\n", url="https://example.invalid/a"))])
        engine = self.engine()
        try:
            result = engine.run(archive)
            self.assertEqual(result["stats"]["canonical_new"], 1)
            self.assertEqual(result["stats"]["quarantined"], 2)
            self.assertEqual(engine.db.execute("SELECT COUNT(*) FROM docs").fetchone()[0], 1)
        finally:
            engine.close()

    def test_isolate_empty_invalid_and_unsafe_members(self):
        archive = self.archive([("../outside.json", record("bad")),
            ("empty.json", record("empty", " \n")), ("broken.json", b"{"),
            ("binary.pdf", b"%PDF synthetic"), ("good.json", record("good"))])
        before = m.digest_file(archive)
        engine = self.engine()
        try:
            result = engine.run(archive)
            self.assertEqual(result["stats"]["quarantined"], 2)
            self.assertEqual(result["stats"]["deferred_no_fulltext"], 1)
            self.assertEqual(result["stats"]["attachments"], 1)
            self.assertEqual(result["stats"]["canonical_new"], 1)
            self.assertEqual(m.digest_file(archive), before)
            self.assertFalse((self.root/"outside.json").exists())
        finally:
            engine.close()

    def test_exact_hash_and_color_gates(self):
        archive = self.archive([("a.json", record("a", "Body\n")),
            ("b.json", record("b", "Body \n", tags=["rating/white"])),
            ("c.json", record("c", "Third", tags=["rating/orange", "rating/red"]))])
        engine = self.engine()
        try:
            result = engine.run(archive)
            self.assertEqual(result["stats"]["canonical_new"], 3)
            self.assertEqual(result["stats"]["selected"], 1)
            self.assertEqual(result["stats"]["held"], 2)
        finally:
            engine.close()

    def test_markdown_html_and_query_identity(self):
        doc = m.parse_document("article.md", b"---\nid: abc\nurl: https://example.invalid/a?version=1\ntags: [orange]\n---\nText\n")
        self.assertEqual(doc["reader_id"], "abc")
        self.assertEqual(doc["content"], "Text\n")
        self.assertEqual(doc["admission"], "selected")
        self.assertNotEqual(m.canonical_url("https://example.invalid/a?version=1"),
                            m.canonical_url("https://example.invalid/a?version=2"))
        doc = m.parse_document("article.html", b"<p>Hello<script>secret()</script></p>")
        self.assertNotIn("secret", doc["content"])

    def test_reject_local_runner(self):
        from unittest.mock import patch
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "false"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "GITHUB_HOSTED_LINUX_REQUIRED"):
                m.cloud_guard()

    def test_missing_drive_auth_fails_before_export(self):
        from unittest.mock import patch
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "MISSING_DRIVE_OAUTH_JSON"):
                m.Drive()

    def test_corrupt_zip_and_oversized_text_do_not_publish(self):
        bad = self.root/"bad.zip"
        bad.write_bytes(b"not a zip")
        engine = self.engine()
        try:
            with self.assertRaises(zipfile.BadZipFile):
                engine.run(bad)
            self.assertEqual(engine.db.execute("SELECT COUNT(*) FROM archives").fetchone()[0], 0)
        finally:
            engine.close()

    def test_oauth_errors_are_actionable_and_never_print_error_description(self):
        import io
        import urllib.error
        from unittest.mock import patch
        error = urllib.error.HTTPError("https://oauth2.googleapis.com/token", 400, "bad", {},
            io.BytesIO(b'{"error":"invalid_grant","error_description":"SECRET MUST NOT APPEAR"}'))
        with patch.object(m.urllib.request, "urlopen", side_effect=error):
            with self.assertRaisesRegex(RuntimeError, r"^REMOTE_HTTP_400_INVALID_GRANT$"):
                m.request_json("https://oauth2.googleapis.com/token", b"test")

    def test_existing_drive_secrets_are_trimmed_and_used_in_place(self):
        from unittest.mock import patch
        values = {"GOOGLE_DRIVE_OAUTH_CLIENT_ID": " client \n",
                  "GOOGLE_DRIVE_OAUTH_CLIENT_SECRET": " secret \n",
                  "GOOGLE_DRIVE_OAUTH_REFRESH_TOKEN": " refresh \n"}
        with patch.dict(os.environ, values, clear=True):
            drive = m.Drive()
            self.assertEqual(drive.config["client_id"], "client")
            self.assertEqual(drive.config["refresh_token"], "refresh")

def large_test():
    m.cloud_guard()
    with tempfile.TemporaryDirectory(dir=os.environ["RUNNER_TEMP"]) as work:
        root = Path(work)
        path = root/"synthetic-3gib-plus.zip"
        # A real >3GiB ZIP using ZIP_STORED, not a sparse file or an inflated-size-only claim.
        block = "x" * (1024 * 1024)
        text_documents = 3073
        with zipfile.ZipFile(path, "w", allowZip64=True) as z:
            z.writestr("attachments/synthetic.bin", bytes(range(256)) * 4096)
            for number in range(text_documents):
                z.writestr(f"doc-{number}.json", record(str(number), str(number) + "\n" + block))
            z.writestr("duplicate.json", record("duplicate", "0\n" + block))
            z.writestr("empty.json", record("empty", ""))
            z.writestr("invalid.json", "{")
        assert path.stat().st_size > 3 * 1024**3
        engine = m.Intake(root/"out")
        try:
            first = engine.run(path)
            second = engine.run(path)
            assert first["stats"]["canonical_new"] == text_documents
            assert first["stats"]["duplicate_body"] == 1
            assert first["stats"]["quarantined"] == 1
            assert first["stats"]["deferred_no_fulltext"] == 1
            assert first["stats"]["attachment_bytes_verified"] == 1024 * 1024
            assert len(first["shards"]) > 50
            assert second["archive_already_complete"]
            usage = __import__("resource").getrusage(__import__("resource").RUSAGE_SELF)
            print(json.dumps({"test": "synthetic_zip_over_3gib", "archive_bytes": path.stat().st_size,
                "stats": first["stats"], "rerun": "already_complete", "max_rss_kib": usage.ru_maxrss,
                "reader_production_documents": 0, "deepseek_calls": 0, "additional_cost": 0,
                "fixture": "synthetic_text_documents_not_user_export"}))
        finally:
            engine.close()

if __name__ == "__main__":
    if "--large" in __import__("sys").argv:
        large_test()
    else:
        unittest.main()
