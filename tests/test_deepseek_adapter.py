import json

from deepseek_adapter import DeepSeekClient


def test_deepseek_json_payload_uses_provider_fields(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_MAX_TOKENS", "12345")
    client = DeepSeekClient("test-key")
    payload = client._build_payload(
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
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["max_tokens"] == 4096
    assert payload["thinking"] == {"type": "disabled"}
    assert "response_schema" not in payload
    assert "service_tier" not in payload


def test_deepseek_thinking_effort_is_configurable():
    client = DeepSeekClient("test-key")
    payload = client._build_payload(
        model="deepseek-flash",
        contents="Think.",
        config={
            "thinking": "enabled",
            "reasoning_effort": "high",
        },
    )
    assert payload["thinking"] == {"type": "enabled"}
    assert payload["reasoning_effort"] == "high"
