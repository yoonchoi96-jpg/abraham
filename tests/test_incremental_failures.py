from __future__ import annotations

import json
import sqlite3

from abraham_incremental import ensure_schema, mark_started, mark_failed, select_pending_documents

def _db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    ensure_schema(c)
    return c

def test_permanent_failure_is_not_retried():
    c = _db()
    d = {"readwise_id":"r1","document_id":"r1","content":"x","canonical_text":"x"}
    pending,_ = select_pending_documents(c,[d],"korean_translation")
    assert len(pending)==1
    mark_started(c,pending,"korean_translation")
    mark_failed(c,pending,"korean_translation",RuntimeError("Content Exists Risk"))
    pending,skipped = select_pending_documents(c,[d],"korean_translation")
    assert not pending and len(skipped)==1
    c.close()

def test_transient_failure_is_retried_until_bound():
    c = _db()
    d = {"readwise_id":"r2","document_id":"r2","content":"x","canonical_text":"x"}
    for _ in range(5):
        pending,_ = select_pending_documents(c,[d],"korean_translation")
        assert len(pending)==1
        mark_started(c,pending,"korean_translation")
        mark_failed(c,pending,"korean_translation",RuntimeError("503 UNAVAILABLE"))
    pending,skipped = select_pending_documents(c,[d],"korean_translation")
    assert not pending and len(skipped)==1
    c.close()
