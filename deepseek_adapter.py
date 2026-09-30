from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass


@dataclass
class DeepSeekResponse:
    text: str


class _Models:
    def __init__(self, client: "DeepSeekClient"):
        self._client = client

    def generate_content(self, *, model: str, contents: str, config: dict | None = None):
        return self._client.generate_content(model=model, contents=contents, config=config or {})


class DeepSeekClient:
    """Provider adapter for the DeepSeek Chat Completions API used by Abraham."""

    def __init__(self, api_key: str, base_url: str = "https://api.deepseek.com"):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.models = _Models(self)

    def generate_content(self, *, model: str, contents: str, config: dict):
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": contents}],
            "stream": False,
        }

        # Abraham already instructs the model to emit JSON and validates it
        # locally. DeepSeek's JSON Output mode adds a provider-side guarantee.
        if config.get("response_mime_type") == "application/json":
            payload["response_format"] = {"type": "json_object"}

        # Default to non-thinking for high-volume translation/classification.
        # Set DEEPSEEK_THINKING=enabled and optionally
        # DEEPSEEK_REASONING_EFFORT=low/high/max for harder workloads.
        thinking = os.getenv("DEEPSEEK_THINKING", "disabled").lower()
        if thinking in {"enabled", "disabled"}:
            payload["thinking"] = {"type": thinking}
        if thinking == "enabled":
            payload["reasoning_effort"] = os.getenv("DEEPSEEK_REASONING_EFFORT", "low")

        timeout = int(os.getenv("DEEPSEEK_TIMEOUT_SECONDS", "600"))
        retries = int(os.getenv("DEEPSEEK_MAX_RETRIES", "3"))

        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        last_error = None
        for attempt in range(retries):
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    data = json.loads(response.read().decode("utf-8"))
                content = data["choices"][0]["message"]["content"]
                if not isinstance(content, str) or not content.strip():
                    raise RuntimeError("DeepSeek returned empty content")
                return DeepSeekResponse(text=content)
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                last_error = RuntimeError(f"DeepSeek HTTP {exc.code}: {body[:2000]}")
                if exc.code not in {429, 500, 502, 503, 504} or attempt >= retries - 1:
                    raise last_error from exc
            except (urllib.error.URLError, TimeoutError, KeyError, json.JSONDecodeError) as exc:
                last_error = RuntimeError(f"DeepSeek request failed: {exc}")
                if attempt >= retries - 1:
                    raise last_error from exc

            time.sleep(min(60, 2 ** attempt))

        raise last_error or RuntimeError("DeepSeek request failed")


def get_deepseek_client() -> DeepSeekClient:
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY is missing.")
    return DeepSeekClient(api_key=api_key)
