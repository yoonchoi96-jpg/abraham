import json
import sqlite3
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from abraham_incremental import (
    MAX_TRANSIENT_ATTEMPTS,
    _failure_status,
    content_hash,
    ensure_schema,
    ensure_stage_rows,
    failure_category,
    mark_failed,
    mark_started,
)
from run_v560_storage import (
    PIPELINE_STAGES,
    _entity_ids_for_documents,
    _entity_candidates_for_documents,
    _in_resume_scope,
    _select_resume_workset,
    _stage_is_eligible,
    build_document_entity_links,
    build_entity_registry,
    classify_entities,
    main,
    pipeline_health_status,
)


def _database(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE documents (readwise_id TEXT PRIMARY KEY, content TEXT, canonical_text TEXT)"
    )
    conn.executemany(
        "INSERT INTO documents (readwise_id, content, canonical_text) VALUES (?, 'x', 'x')",
        [("r1",), ("r2",), ("r3",)],
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
    conn.executemany(
        """INSERT INTO document_stages
           (readwise_id, stage, status, attempt_count, content_hash, updated_at)
           VALUES ('r1', ?, 'completed', 1, ?, '2026-10-04T14:00:00')""",
        [
            ("entity_relation_extraction", content_hash("x", "x")),
            ("document_render", content_hash("x", "x")),
        ],
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
    before_content = db_path.read_bytes()
    before_directory = set(db_path.parent.iterdir())

    status = pipeline_health_status(db_path, log_path)

    assert status["documents"] == {
        "total": 3,
        "pending": 1,
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
    assert db_path.read_bytes() == before_content
    assert set(db_path.parent.iterdir()) == before_directory


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


def test_status_cli_does_not_call_external_services(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("sys.argv", ["run_v560_storage.py", "--status"])
    monkeypatch.setattr("run_v560_storage.get_db_path", lambda: tmp_path / "missing.db")

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("status must not contact external services")

    for name in (
        "get_deepseek_client",
        "get_gemini_client",
        "load_production_snapshot",
        "apply_files",
    ):
        monkeypatch.setattr(f"run_v560_storage.{name}", fail_if_called)

    main()

    assert '"database_exists": false' in capsys.readouterr().out


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


def test_resume_stage_order_and_bounded_workset_for_every_boundary():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    documents = [
        {
            "readwise_id": f"rw-{index}",
            "document_id": index,
            "content": "text",
            "canonical_text": "text",
        }
        for index in range(5)
    ]

    for start_index, start_stage in enumerate(PIPELINE_STAGES):
        assert [
            _stage_is_eligible(start_stage, stage)
            for stage in PIPELINE_STAGES
        ] == [index >= start_index for index in range(len(PIPELINE_STAGES))]
        pending, _ = _select_resume_workset(
            conn,
            documents,
            start_stage,
            limit=2,
        )
        assert len(pending) == 2
        assert all(_in_resume_scope(document, {
            str(pending_document["readwise_id"]) for pending_document in pending
        }) for document in pending)
    assert conn.execute("SELECT COUNT(*) FROM document_stages").fetchone()[0] == 0
    conn.close()


def test_resume_scope_keeps_documents_and_shared_entities_aligned():
    scope = {"rw-A"}
    documents = [
        {"readwise_id": "rw-A", "document_id": 1},
        {"readwise_id": "rw-B", "document_id": 2},
        {"readwise_id": "rw-C", "document_id": 3},
    ]
    links = [
        {"document_id": 1, "entity_id": "entity-shared"},
        {"document_id": 2, "entity_id": "entity-shared"},
        {"document_id": 3, "entity_id": "entity-C"},
    ]

    selected_documents = [
        document for document in documents if _in_resume_scope(document, scope)
    ]
    selected_entities = _entity_ids_for_documents(links, [d["document_id"] for d in selected_documents])

    assert [document["readwise_id"] for document in selected_documents] == ["rw-A"]
    assert selected_entities == {"entity-shared"}
    assert selected_entities.isdisjoint({"entity-C"})


def test_resume_classification_and_entity_render_leave_unrelated_entity_untouched(
    monkeypatch,
):
    documents = [
        {
            "document_id": "A",
            "resolved_entities": [
                {"entity_id": "shared", "canonical_name": "Shared", "entity_type": "Other"}
            ],
        },
        {
            "document_id": "B",
            "resolved_entities": [
                {"entity_id": "shared", "canonical_name": "Shared", "entity_type": "Other"}
            ],
        },
        {
            "document_id": "C",
            "resolved_entities": [
                {"entity_id": "other", "canonical_name": "Other", "entity_type": "Other"}
            ],
        },
    ]
    links = build_document_entity_links(documents)
    selected_entities = _entity_ids_for_documents(links, ["A"])
    candidates = [
        {"document_id": "shared"},
        {"document_id": "other"},
    ]
    assert _entity_candidates_for_documents(candidates, links, ["A"]) == [
        {"document_id": "shared"}
    ]

    registry = build_entity_registry(documents)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE entities (entity_id TEXT PRIMARY KEY, entity_type TEXT)")
    conn.executemany(
        "INSERT INTO entities VALUES (?, 'Other')",
        [("shared",), ("other",)],
    )
    monkeypatch.setattr("run_v560_storage.connect_db", lambda: conn)
    monkeypatch.setattr("run_v560_storage.get_gemini_client", lambda: object())
    monkeypatch.setenv("ABRAHAM_ENTITY_CLASSIFICATION_PROVIDER", "gemini")
    classified_batches = []

    def classify(_client, batch):
        classified_batches.append([entity["entity_id"] for entity in batch])
        return {"shared": "Concept"}

    monkeypatch.setattr("run_v560_storage.classify_entity_batch", classify)
    classify_entities(
        registry,
        SimpleNamespace(apply=True),
        allowed_entity_ids=selected_entities,
    )

    assert classified_batches == [["shared"]]
    assert conn.execute(
        "SELECT entity_type FROM entities WHERE entity_id='shared'"
    ).fetchone()[0] == "Concept"
    assert conn.execute(
        "SELECT entity_type FROM entities WHERE entity_id='other'"
    ).fetchone()[0] == "Other"
    conn.close()


@pytest.mark.parametrize(
    ("message", "category", "status"),
    [
        ("HTTP 429 rate limit", "rate_limit", "transient_failed"),
        ("malformed JSON response", "data", "transient_failed"),
        ("Content Exists Risk", "content_risk", "permanent_failed"),
        ("DeepSeek API unavailable", "api", "transient_failed"),
    ],
)
def test_failure_classification_is_conservative(message, category, status):
    error = RuntimeError(message)
    assert failure_category(error) == category
    assert _failure_status(error) == status


def test_retry_override_retries_only_failed_records_not_completed_ones():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    documents = [
        {
            "readwise_id": f"rw-{index}",
            "document_id": f"rw-{index}",
            "content": "text",
            "canonical_text": "text",
            "_stage_content_hash": content_hash("text", "text"),
        }
        for index in range(3)
    ]
    conn.executemany(
        """INSERT INTO document_stages
           (readwise_id, stage, status, attempt_count, content_hash)
           VALUES (?, 'korean_translation', ?, ?, ?)""",
        [
            ("rw-0", "completed", 1, content_hash("text", "text")),
            ("rw-1", "permanent_failed", MAX_TRANSIENT_ATTEMPTS, content_hash("text", "text")),
            ("rw-2", "transient_failed", MAX_TRANSIENT_ATTEMPTS, content_hash("text", "text")),
        ],
    )
    conn.commit()

    pending, _ = _select_resume_workset(
        conn,
        documents,
        "korean_translation",
        limit=10,
        retry_ids=["rw-0", "rw-1", "rw-2"],
    )

    assert {document["readwise_id"] for document in pending} == {"rw-1", "rw-2"}
    conn.close()


def test_stage_row_lookup_handles_production_document_count():
    from abraham_incremental import get_stage_rows

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    readwise_ids = [f"rw-{index}" for index in range(8802)]

    ensure_stage_rows(conn, readwise_ids, "korean_translation")

    assert len(get_stage_rows(conn, readwise_ids, "korean_translation")) == 8802
    conn.close()


@pytest.mark.parametrize(
    "argv",
    [
        ["run_v560_storage.py", "--resume", "--from-stage", "rating"],
        ["run_v560_storage.py", "--resume", "--from-stage", "entity_render", "--limit", "0"],
    ],
)
def test_resume_requires_a_positive_bound(monkeypatch, argv, capsys):
    monkeypatch.setattr("sys.argv", argv)

    with pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 2
    error = capsys.readouterr().err
    assert "requires" in error or "positive integer" in error
