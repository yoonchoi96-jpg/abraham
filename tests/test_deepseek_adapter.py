import pytest

from deepseek_adapter import DeepSeekClient


def test_deepseek_json_payload_uses_provider_fields(monkeypatch):
    client = DeepSeekClient("test-key")
    captured = {}

    def fake_request(payload):
        captured.update(payload)
        return type("Response", (), {"text": '{"ok": true}'})()

    monkeypatch.setattr(client, "_request", fake_request)
    client.generate_content(
        model="deepseek-flash",
        contents="Return JSON.",
        config={
            "response_mime_type": "application/json",
            "thinking": "disabled",
            "max_output_tokens": 4096,
            "response_schema": {"type": "OBJECT"},
            "service_tier": "flex",
        },
    )
    assert captured["response_format"] == {"type": "json_object"}
    assert captured["max_tokens"] == 4096
    assert captured["thinking"] == {"type": "disabled"}
    assert "response_schema" not in captured
    assert "service_tier" not in captured


def test_deepseek_default_output_limit_is_configurable(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_MAX_TOKENS", "12345")
    client = DeepSeekClient("test-key")
    captured = {}

    def fake_request(payload):
        captured.update(payload)
        return type("Response", (), {"text": '{"ok": true}'})()

    monkeypatch.setattr(client, "_request", fake_request)
    client.generate_content(
        model="deepseek-flash",
        contents="Return JSON.",
        config={},
    )
    assert captured["max_tokens"] == 12345


def test_deepseek_thinking_effort_is_configurable(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_THINKING", "enabled")
    monkeypatch.setenv("DEEPSEEK_REASONING_EFFORT", "high")
    client = DeepSeekClient("test-key")
    captured = {}

    def fake_request(payload):
        captured.update(payload)
        return type("Response", (), {"text": '{"ok": true}'})()

    monkeypatch.setattr(client, "_request", fake_request)
    client.generate_content(
        model="deepseek-flash",
        contents="Think.",
        config={},
    )
    assert captured["thinking"] == {"type": "enabled"}
    assert captured["reasoning_effort"] == "high"
