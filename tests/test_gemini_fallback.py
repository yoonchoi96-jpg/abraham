from __future__ import annotations

import io
import urllib.error

import gemini_adapter


class _FallbackModels:
    def generate_content(self, *, model, contents, config):
        return type("Response", (), {"text": '{"ok": true}'})()


class _FallbackClient:
    models = _FallbackModels()


def test_gemini_402_falls_back_to_deepseek(monkeypatch):
    def fail_gemini(*_args, **_kwargs):
        raise urllib.error.HTTPError(
            "https://example.invalid",
            402,
            "Payment Required",
            {},
            io.BytesIO(b'{"error":"prepaid credits exhausted"}'),
        )

    monkeypatch.setattr(gemini_adapter.urllib.request, "urlopen", fail_gemini)
    monkeypatch.setattr(gemini_adapter, "get_deepseek_client", lambda: _FallbackClient())
    monkeypatch.setenv("DEEPSEEK_GEMINI_FALLBACK_MODEL", "deepseek-flash")

    client = gemini_adapter.GeminiClient("unused")
    response = client.generate_content(
        model="gemini-3.8-flash",
        contents="test",
        config={"response_mime_type": "application/json"},
    )

    assert response.text == '{"ok": true}'
