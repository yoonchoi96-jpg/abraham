from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass


@dataclass
class GeminiResponse:
    text: str


class _Models:
    def __init__(self, client):
        self._client = client

    def generate_content(self, *, model: str, contents: str, config: dict | None = None):
        return self._client.generate_content(model=model, contents=contents, config=config or {})


class GeminiClient:
    """Minimal REST adapter for Gemini structured-output rating/classification."""

    def __init__(self, api_key: str, base_url: str = "https://generativelanguage.googleapis.com/v1beta"):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.models = _Models(self)

    def generate_content(self, *, model: str, contents: str, config: dict | None = None):
        config = config or {}
        body = {
            "contents": [{"parts": [{"text": contents}]}],
            "generationConfig": {"responseMimeType": "application/json"},
        }
        max_tokens = config.get("max_output_tokens")
        if max_tokens is not None:
            body["generationConfig"]["maxOutputTokens"] = int(max_tokens)

        request = urllib.request.Request(
            f"{self.base_url}/models/{model}:generateContent",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "x-goog-api-key": self.api_key,
                "Content-Type": "application/json",
            },
            method="POST",
        )

        retries = max(1, int(os.getenv("GEMINI_MAX_RETRIES", "4")))
        timeout = int(os.getenv("GEMINI_TIMEOUT_SECONDS", "600"))
        last_error = None

        for attempt in range(retries):
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    data = json.loads(response.read().decode("utf-8"))
                candidates = data.get("candidates") or []
                if not candidates:
                    raise RuntimeError(f"Gemini returned no candidates: {data.get('promptFeedback')}")
                parts = candidates[0].get("content", {}).get("parts", [])
                text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
                if not text.strip():
                    raise RuntimeError("Gemini returned empty content")
                return GeminiResponse(text=text)
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                last_error = RuntimeError(f"Gemini HTTP {exc.code}: {detail[:2000]}")
                if exc.code not in {429, 500, 502, 503, 504} or attempt >= retries - 1:
                    raise last_error from exc
            except (urllib.error.URLError, TimeoutError, KeyError, json.JSONDecodeError) as exc:
                last_error = RuntimeError(f"Gemini request failed: {exc}")
                if attempt >= retries - 1:
                    raise last_error from exc
            time.sleep(min(60, 2 ** attempt))

        raise last_error or RuntimeError("Gemini request failed")


def get_gemini_client() -> GeminiClient:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is missing.")
    return GeminiClient(api_key=api_key)
