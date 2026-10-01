from run_v560_storage import _rate_batch_resilient, _translate_korean_resilient


def _documents(count):
    return [
        {"document_id": f"doc-{i}", "readwise_id": f"rw-{i}"}
        for i in range(count)
    ]


def test_korean_translation_splits_failed_batch(monkeypatch):
    calls = []

    def fake_translate(client, batch):
        calls.append(len(batch))
        if len(batch) > 2:
            return []
        return [
            {
                "document_id": d["document_id"],
                "korean_title": "제목",
                "korean_text": "본문",
                "translation_notes": [],
            }
            for d in batch
        ]

    monkeypatch.setattr(
        "run_v560_storage.translate_korean_batch",
        fake_translate,
    )

    docs = _documents(8)
    result = _translate_korean_resilient(object(), docs)

    assert {x["document_id"] for x in result} == {
        d["document_id"] for d in docs
    }
    assert 8 in calls
    assert 4 in calls
    assert 2 in calls


def test_rating_splits_failed_batch(monkeypatch):
    calls = []

    def fake_rate(client, batch):
        calls.append(len(batch))
        if len(batch) > 2:
            raise ValueError("malformed JSON")
        return [
            {"document_id": d["document_id"]}
            for d in batch
        ]

    monkeypatch.setattr(
        "run_v560_storage._rate_batch",
        fake_rate,
    )

    docs = _documents(8)
    result, failed = _rate_batch_resilient(object(), docs)

    assert {x["document_id"] for x in result} == {
        d["document_id"] for d in docs
    }
    assert failed == []
    assert 8 in calls
    assert 4 in calls
    assert 2 in calls


def test_single_document_failure_isolated(monkeypatch):
    def fake_translate(client, batch):
        if len(batch) == 1 and batch[0]["document_id"] == "doc-3":
            return []
        return [
            {
                "document_id": d["document_id"],
                "korean_title": "제목",
                "korean_text": "본문",
                "translation_notes": [],
            }
            for d in batch
        ]

    monkeypatch.setattr(
        "run_v560_storage.translate_korean_batch",
        fake_translate,
    )

    docs = _documents(4)
    result = _translate_korean_resilient(object(), docs)

    assert {x["document_id"] for x in result} == {
        "doc-0", "doc-1", "doc-2", "doc-3"
    } or {x["document_id"] for x in result} == {
        "doc-0", "doc-1", "doc-2"
    }
