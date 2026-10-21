"""Synthetic tests only; not a model benchmark or Reader grading validation."""
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from scripts.reader_infer_queue import LocalInferenceQueue, QueueError
from scripts.reader_local import Held


def task(queue, prompt="classify SOURCE", source="SOURCE"):
    return queue.submit(
        backend="ollama", model="qwen3:8b",
        prompt=prompt, source_text=source
    )


def output(color="blue", quote="SOURCE", topic="AI"):
    return {"output": json.dumps({
        "color": color, "topic": topic,
        "reason": "structural analysis", "evidence_quote": quote
    })}


def test_private_deduplicated_input(tmp_path):
    q = LocalInferenceQueue(tmp_path/"private")
    key = task(q)
    assert task(q) == key
    assert q.status() == {"pending": 1}
    assert (tmp_path/"private"/"private-requests"/(key+".json")).stat().st_mode & 0o077 == 0
    assert (tmp_path/"private").stat().st_mode & 0o077 == 0
    q.close()


def test_queue_bound_and_prompt_guard(tmp_path):
    q = LocalInferenceQueue(tmp_path/"private", max_pending=1)
    task(q)
    with pytest.raises(QueueError, match="QUEUE_FULL"):
        task(q, "another SOURCE")
    with pytest.raises(QueueError, match="PROMPT_EXCEEDS_CONTEXT"):
        task(q, "x"*4097)
    q.close()


def test_candidate_never_becomes_verified_and_is_cached(tmp_path):
    q = LocalInferenceQueue(tmp_path/"private")
    task(q)
    executor = Mock(return_value=output())
    assert q.run(amando_root=tmp_path, infer_fn=executor)["candidate"] == 1
    assert q.status() == {"candidate": 1}
    state, body = q.db.execute("SELECT state,outcome FROM jobs").fetchone()
    assert state == "candidate"
    assert json.loads(body)["status"] == "classified_candidate"
    assert q.run(amando_root=tmp_path, infer_fn=executor)["candidate"] == 0
    executor.assert_called_once()
    q.close()


@pytest.mark.parametrize("response", [
    {"output": "not-json"},
    {"output": json.dumps({"color":"gold","topic":"AI","reason":"r","evidence_quote":"FAKE"})},
    {"output": json.dumps({"color":"gold","topic":"AI","reason":"r","evidence_quote":"SOURCE","verified":True})},
    {"output": json.dumps({"color":"unknown","topic":"AI","reason":"r","evidence_quote":"SOURCE"})},
])
def test_bad_or_unsubstantiated_outputs_are_review_only(response, tmp_path):
    q = LocalInferenceQueue(tmp_path/"private")
    task(q)
    assert q.run(amando_root=tmp_path, infer_fn=lambda *a,**k: response)["review_required"] == 1
    assert q.status() == {"review_required": 1}
    assert q.db.execute("SELECT outcome FROM jobs").fetchone()[0] is None
    q.close()


def test_gearshift_hold_is_not_auto_retried(tmp_path):
    q = LocalInferenceQueue(tmp_path/"private")
    task(q)
    def denied(*a,**k):
        raise Held("GEARSHIFT_HELD")
    assert q.run(amando_root=tmp_path, infer_fn=denied)["held"] == 1
    assert q.run(amando_root=tmp_path, infer_fn=denied)["held"] == 0
    assert q.retry_held() == 1
    assert q.run(
        amando_root=tmp_path, infer_fn=lambda *a,**k: output()
    )["candidate"] == 1
    assert q.retry_held() == 0
    q.close()


def test_resumes_after_crash_without_duplicate_record(tmp_path):
    location = tmp_path/"private"
    q = LocalInferenceQueue(location)
    task(q)
    q.db.execute("UPDATE jobs SET state='running'")
    q.db.commit()
    q.close()
    q = LocalInferenceQueue(location)
    assert q.run(
        amando_root=tmp_path, infer_fn=lambda *a,**k: output()
    )["candidate"] == 1
    assert q.status() == {"candidate": 1}
    q.close()


def test_changed_request_is_not_inferred(tmp_path):
    location = tmp_path/"private"
    q = LocalInferenceQueue(location)
    key = task(q)
    (location/"private-requests"/(key+".json")).write_text('{"prompt":"tampered"}')
    executor = Mock(return_value=output())
    assert q.run(amando_root=tmp_path, infer_fn=executor)["held"] == 1
    executor.assert_not_called()
    assert q.db.execute("SELECT reason FROM jobs").fetchone()[0] == "LOCAL_EXECUTION_FAILED"
    q.close()


def test_private_exception_and_held_reason_not_logged(tmp_path):
    q = LocalInferenceQueue(tmp_path/"private")
    task(q)
    def leaking(*a,**k):
        raise RuntimeError("private content in exception")
    assert q.run(amando_root=tmp_path, infer_fn=leaking)["held"] == 1
    assert "private" not in q.db.execute("SELECT reason FROM jobs").fetchone()[0]
    q.retry_held()
    def leaking_held(*a,**k):
        raise Held("PRIVATE ARTICLE TEXT")
    assert q.run(amando_root=tmp_path, infer_fn=leaking_held)["held"] == 1
    assert q.db.execute("SELECT reason FROM jobs").fetchone()[0] == "LOCAL_HELD"
    q.close()
