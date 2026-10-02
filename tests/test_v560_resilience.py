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
        if any(d["document_id"] == "doc-3" for d in batch):
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
        "doc-0", "doc-1", "doc-2"
    }


def test_korean_partial_response_retries_only_missing_documents(monkeypatch):
    calls = []

    def fake_translate(client, batch):
        calls.append([d["document_id"] for d in batch])
        if len(calls) == 1:
            return [
                {
                    "document_id": d["document_id"],
                    "korean_title": "제목",
                    "korean_text": "본문",
                    "translation_notes": [],
                }
                for d in batch[:6]
            ]
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
    assert calls == [
        [f"doc-{i}" for i in range(8)],
        ["doc-6", "doc-7"],
    ]


def test_salvage_truncated_korean_json():
    from run_v560_storage import _salvage_korean_documents

    raw = '{"documents":['
    raw += '{"index":0,"korean_title":"A","korean_text":"첫째","translation_notes":[]},'
    raw += '{"index":1,"korean_title":"B","korean_text":"둘째","translation_notes":[]},'
    raw += '{"index":2,"korean_title":"C","korean_text":"셋'

    result = _salvage_korean_documents(raw)

    assert [x["index"] for x in result] == [0, 1]


def test_translate_parser_maps_indexes_to_document_ids(monkeypatch):
    import run_v560_storage as mod
    class Response:
        text = '{"documents":[{"index":0,"korean_title":"A","korean_text":"첫째","translation_notes":[]},{"index":2,"korean_title":"C","korean_text":"셋째","translation_notes":[]}]}'
    class Models:
        def generate_content(self, **kwargs): return Response()
    class Client:
        models = Models()
    docs = _documents(3)
    result = mod.translate_korean_batch(Client(), docs)
    assert [x["document_id"] for x in result] == ["doc-0", "doc-2"]
