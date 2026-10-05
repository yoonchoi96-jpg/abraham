import json
import sqlite3
from contextlib import nullcontext

from abraham_incremental import (
    content_hash,
    ensure_schema,
    ensure_stage_rows,
    mark_failed,
    mark_started,
)
from run_v560_storage import main, pipeline_health_status


def _database(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE documents (readwise_id TEXT PRIMARY KEY, content TEXT, canonical_text TEXT)"
    )
    conn.executemany(
        "INSERT INTO documents (readwise_id, content, canonical_text) VALUES (?, 'x', 'x')",
        [("r1",), ("r2",)],
    )
    conn.execute("CREATE TABLE entities (entity_id TEXT PRIMARY KEY)")
    conn.execute("INSERT INTO entities VALUES ('e1')")
    conn.execute("CREATE TABLE relations (relation_id INTEGER PRIMARY KEY)")
    conn.execute("INSERT INTO relations DEFAULT VALUES")
    conn.execute("CREATE TABLE document_entities (readwise_id TEXT, entity_id TEXT)")
    conn.execute("INSERT INTO document_entities VALUES ('r1', 'e1')")
    ensure_schema(conn)

    conn.execute(
        """INSERT INTO document_stages
           (readwise_id, stage, status, attempt_count, content_hash, updated_at)
           VALUES ('r1', 'korean_translation', 'completed', 1, ?, '2026-10-04T14:00:00')""",
        (content_hash("x", "x"),),
    )
    failed = {"readwise_id": "r2", "document_id": "r2", "content": "x"}
    conn.execute(
        """INSERT INTO document_stages
           (readwise_id, stage, status, attempt_count, last_error, updated_at)
           VALUES ('r2', 'korean_translation', 'transient_failed', 1,
                   'HTTP 429 rate limit', '2026-10-04T14:30:00')"""
    )
    conn.execute(
        """INSERT INTO document_stages
           (readwise_id, stage, status, attempt_count, started_at, updated_at)
           VALUES ('r2', 'entity_relation_extraction', 'running', 1,
                   '2000-01-01T00:00:00+00:00', '2000-01-01T00:00:00+00:00')"""
    )
    conn.commit()
    conn.close()


def test_pipeline_health_reports_database_state_without_mutation(tmp_path):
    db_path = tmp_path / "health.db"
    _database(db_path)
    log_path = tmp_path / "abraham.log"
    log_path.write_text(
        "START 2026-10-04T14:00:00\nSUCCESS 2026-10-04T14:10:00 elapsed=2s\n",
        encoding="utf-8",
    )
    before = db_path.stat().st_mtime_ns

    status = pipeline_health_status(db_path, log_path)

    assert status["documents"] == {
        "total": 2,
        "pending": 0,
        "completed": 1,
        "failed": 1,
    }
    assert status["stages"]["korean_translation"]["retry_eligible"] == 1
    assert status["cache"]["korean_translation"]["hit_rate_pct"] == 50.0
    assert status["graph"] == {
        "entities": 1,
        "relations": 1,
        "document_entity_links": 1,
    }
    assert status["recent_failures"][0]["category"] == "rate_limit"
    assert status["stale_stages"][0]["stage"] == "entity_relation_extraction"
    assert status["stages"]["entity_relation_extraction"]["running"] == 1
    assert status["last_successful_run"] == "2026-10-04T14:10:00"
    assert db_path.stat().st_mtime_ns == before


def test_resume_defaults_to_database_apply_and_defers_obsidian(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr(
        "sys.argv",
        [
            "run_v560_storage.py",
            "--resume",
            "--from-stage",
            "korean_translation",
            "--limit",
            "100",
        ],
    )
    monkeypatch.setattr("run_v560_storage.LOG_DIR", tmp_path)
    monkeypatch.setattr("run_v560_storage.pipeline_lock", lambda: nullcontext())
    monkeypatch.setattr(
        "run_v560_storage.run_pipeline",
        lambda args: captured.update(args=args),
    )

    main()

    args = captured["args"]
    assert args.apply is True
    assert args.skip_obsidian_apply is True
    assert args.from_stage == "korean_translation"
    assert args.limit == 100


def test_status_cli_does_not_create_database_or_log_paths(monkeypatch, tmp_path, capsys):
    db_path = tmp_path / "not-created.db"
    log_dir = tmp_path / "not-created-logs"
    monkeypatch.setattr("sys.argv", ["run_v560_storage.py", "--status"])
    monkeypatch.setattr("run_v560_storage.get_db_path", lambda: db_path)
    monkeypatch.setattr("run_v560_storage.LOG_DIR", log_dir)

    main()

    assert '"database_exists": false' in capsys.readouterr().out
    assert not db_path.exists()
    assert not log_dir.exists()


def test_failure_log_is_structured_and_marks_retry_eligibility(capsys):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    document = {"readwise_id": "r3", "document_id": "r3", "content": "x"}
    ensure_stage_rows(conn, ["r3"], "korean_translation")
    mark_started(conn, [document], "korean_translation")

    mark_failed(
        conn,
        [document],
        "korean_translation",
        RuntimeError("HTTP 429 rate limit"),
    )

    record = json.loads(capsys.readouterr().out.strip())
    assert record["event"] == "stage_failure"
    assert record["document_id"] == "r3"
    assert record["stage"] == "korean_translation"
    assert record["category"] == "rate_limit"
    assert record["retry_eligible"] is True
    conn.close()
