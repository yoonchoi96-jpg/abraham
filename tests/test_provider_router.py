from __future__ import annotations

import pytest

import provider_router


class _Models:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def generate_content(self, *, model, contents, config):
        self.calls.append((model, contents, config))
        if self.error:
            raise self.error
        return self.response


class _Client:
    def __init__(self, response=None, error=None):
        self.models = _Models(response=response, error=error)


def test_deepseek_primary_does_not_call_gemini(monkeypatch):
    primary = _Client(response=type("Response", (), {"text": '{"ok": true}'})())
    fallback = _Client(response=type("Response", (), {"text": '{"fallback": true}'})())
    monkeypatch.setattr(provider_router, "get_deepseek_client", lambda: primary)
    monkeypatch.setattr(provider_router, "get_gemini_client", lambda: fallback)

    client = provider_router.get_rating_client()
    response = client.models.generate_content(
        model="deepseek-flash",
        contents="test",
        config={"max_output_tokens": 100},
    )

    assert response.text == '{"ok": true}'
    assert len(primary.models.calls) == 1
    assert fallback.models.calls == []


def test_deepseek_failure_uses_gemini_fallback(monkeypatch):
    primary = _Client(error=RuntimeError("DeepSeek HTTP 503: unavailable"))
    fallback = _Client(response=type("Response", (), {"text": '{"fallback": true}'})())
    monkeypatch.setattr(provider_router, "get_deepseek_client", lambda: primary)
    monkeypatch.setattr(provider_router, "get_gemini_client", lambda: fallback)
    monkeypatch.setenv("GEMINI_RATING_FALLBACK_MODEL", "gemini-3.8-flash")

    client = provider_router.get_rating_client()
    response = client.models.generate_content(
        model="deepseek-flash",
        contents="test",
        config={"response_mime_type": "application/json"},
    )

    assert response.text == '{"fallback": true}'
    assert len(primary.models.calls) == 1
    assert fallback.models.calls[0][0] == "gemini-3.8-flash"


def test_both_providers_fail(monkeypatch):
    primary = _Client(error=RuntimeError("DeepSeek HTTP 503: unavailable"))
    fallback = _Client(error=RuntimeError("Gemini unavailable"))
    monkeypatch.setattr(provider_router, "get_deepseek_client", lambda: primary)
    monkeypatch.setattr(provider_router, "get_gemini_client", lambda: fallback)

    client = provider_router.get_relation_client()
    with pytest.raises(RuntimeError, match="primary DeepSeek and Gemini fallback both failed"):
        client.models.generate_content(
            model="deepseek-flash",
            contents="test",
            config={},
        )


def test_application_error_does_not_spend_gemini(monkeypatch):
    primary = _Client(error=ValueError("invalid rating payload"))
    fallback = _Client(response=type("Response", (), {"text": '{"fallback": true}'})())
    monkeypatch.setattr(provider_router, "get_deepseek_client", lambda: primary)
    monkeypatch.setattr(provider_router, "get_gemini_client", lambda: fallback)

    client = provider_router.get_rating_client()
    with pytest.raises(ValueError, match="invalid rating payload"):
        client.models.generate_content(
            model="deepseek-flash",
            contents="test",
            config={},
        )

    assert fallback.models.calls == []


def test_provider_http_failure_uses_gemini(monkeypatch):
    primary = _Client(error=RuntimeError("DeepSeek HTTP 503: unavailable"))
    fallback = _Client(response=type("Response", (), {"text": '{"fallback": true}'})())
    monkeypatch.setattr(provider_router, "get_deepseek_client", lambda: primary)
    monkeypatch.setattr(provider_router, "get_gemini_client", lambda: fallback)

    client = provider_router.get_rating_client()
    response = client.models.generate_content(
        model="deepseek-flash",
        contents="test",
        config={},
    )

    assert response.text == '{"fallback": true}'
    assert len(fallback.models.calls) == 1
