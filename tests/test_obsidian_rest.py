import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import run_v560_storage as pipeline


class _Handler(BaseHTTPRequestHandler):
    files = {}
    calls = []

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        self.__class__.calls.append(("GET", self.path))
        if self.path == "/":
            body = json.dumps({"ok": "OK", "service": "Obsidian Local REST API"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        path = self.path.split("/vault/", 1)[-1]
        if path not in self.files:
            self.send_response(404)
            self.end_headers()
            return
        body = self.files[path].encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/markdown")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_PUT(self):
        self.__class__.calls.append(("PUT", self.path))
        path = self.path.split("/vault/", 1)[-1]
        length = int(self.headers["Content-Length"])
        self.files[path] = self.rfile.read(length).decode()
        self.send_response(204)
        self.end_headers()

    def do_DELETE(self):
        self.__class__.calls.append(("DELETE", self.path))
        path = self.path.split("/vault/", 1)[-1]
        self.files.pop(path, None)
        self.send_response(204)
        self.end_headers()


@pytest.fixture()
def rest_server(monkeypatch, tmp_path):
    _Handler.files = {"00_Inbox/Readwise/old.md": "old content"}
    _Handler.calls = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("OBSIDIAN_API_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("OBSIDIAN_API_KEY", "test-key")
    monkeypatch.setattr(pipeline, "VAULT_PATH", tmp_path / "vault")
    monkeypatch.setattr(pipeline, "BACKUP_ROOT", tmp_path / "backups")
    yield _Handler
    server.shutdown()
    thread.join()


def test_obsidian_rest_health_and_crud(rest_server):
    writer = pipeline.ObsidianRestWriter()
    assert writer.health()["ok"] == "OK"
    assert writer.read("00_Inbox/Readwise/old.md") == "old content"
    writer.write("00_Inbox/Readwise/new note.md", "# New")
    assert writer.read("00_Inbox/Readwise/new note.md") == "# New"
    writer.delete("00_Inbox/Readwise/new note.md")
    assert writer.read("00_Inbox/Readwise/new note.md") is None


def test_apply_files_uses_rest_backend_and_handles_rename(rest_server, tmp_path):
    result = pipeline.apply_files(
        [
            {
                "current_path": str(tmp_path / "vault" / "00_Inbox" / "Readwise" / "old.md"),
                "target_path": str(tmp_path / "vault" / "00_Inbox" / "Readwise" / "new.md"),
                "markdown": "# Updated",
                "title": "Updated",
            }
        ],
        [],
    )
    assert result["backend"] == "obsidian_rest_api"
    assert result["written_documents"] == 1
    assert result["renamed_documents"] == 1
    assert rest_server.files["00_Inbox/Readwise/new.md"] == "# Updated"
    assert "00_Inbox/Readwise/old.md" not in rest_server.files
    assert (pipeline.BACKUP_ROOT / next(iter([p.name for p in pipeline.BACKUP_ROOT.iterdir()])) / "00_Inbox" / "Readwise" / "old.md").read_text() == "old content"
    methods = [x[0] for x in rest_server.calls]
    assert methods.count("GET") >= 3
    assert "PUT" in methods
    assert "DELETE" in methods
