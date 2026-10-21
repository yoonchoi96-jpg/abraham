from __future__ import annotations

import json
import hashlib

from abraham_api_usage import append_record, singleflight, usage_record
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
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

    def _record_usage(self, model: str, usage: dict, *, task: str,
                      finish_reason: str | None, response_model: str | None) -> None:
        if not usage:
            return
        path = os.getenv("ABRAHAM_AI_USAGE_LOG")
        if not path:
            return
        record = usage_record(model=model, task=task, usage=usage,
                              finish_reason=finish_reason,
                              response_model=response_model)
        try:
            append_record(path, record)
        except OSError:
            # Logging is best-effort: never bill for a retry only because a
            # successful provider response could not be written to disk.
            print("[AI USAGE LOG FAILED] deepseek: local_log_unavailable")

    def generate_content(self, *, model: str, contents: str, config: dict):
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": contents}],
            "stream": False,
            "max_tokens": int(os.getenv("DEEPSEEK_MAX_TOKENS", "32768")),
        }

        if config.get("max_output_tokens") is not None:
            payload["max_tokens"] = max(
                1,
                min(int(config["max_output_tokens"]), 393216),
            )

        # Abraham already instructs the model to emit JSON and validates it
        # locally. DeepSeek's JSON Output mode adds a provider-side guarantee.
        if config.get("response_mime_type") == "application/json":
            payload["response_format"] = {"type": "json_object"}

        # Gemini-only schema/service-tier fields are intentionally ignored.

        # Default to non-thinking for high-volume translation/classification.
        # Set DEEPSEEK_THINKING=enabled and optionally
        # DEEPSEEK_REASONING_EFFORT=low/high/max for harder workloads.
        thinking = str(
            config.get(
                "thinking",
                os.getenv("DEEPSEEK_THINKING", "disabled"),
            )
        ).lower()
        if thinking not in {"enabled", "disabled"}:
            raise ValueError("thinking must be 'enabled' or 'disabled'")
        payload["thinking"] = {"type": thinking}
        if thinking == "enabled":
            effort = str(
                config.get(
                    "reasoning_effort",
                    os.getenv("DEEPSEEK_REASONING_EFFORT", "low"),
                )
            ).lower()
            effort = {
                "minimal": "low",
                "medium": "high",
                "xhigh": "high",
                "ultra": "max",
            }.get(effort, effort)
            if effort not in {"low", "high", "max"}:
                raise ValueError("reasoning_effort must be low, high, or max")
            payload["reasoning_effort"] = effort

        timeout = int(os.getenv("DEEPSEEK_TIMEOUT_SECONDS", "600"))
        retries = max(1, int(os.getenv("DEEPSEEK_MAX_RETRIES", "4")))

        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        task = str(config.get("usage_task", "unattributed"))
        def execute_once():
            last_error = None
            for attempt in range(retries):
                try:
                    with urllib.request.urlopen(request, timeout=timeout) as response:
                        data = json.loads(response.read().decode("utf-8"))
                    choice = data["choices"][0]
                    content = choice["message"]["content"]
                    self._record_usage(
                        model, data.get("usage") or {}, task=task,
                        finish_reason=choice.get("finish_reason"),
                        response_model=data.get("model")
                    )
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

        # Only simultaneous byte-identical provider requests sharing an account
        # can coalesce. No paid response is persisted or reused indefinitely.
        if os.getenv("DEEPSEEK_SINGLEFLIGHT", "1") == "0":
            return execute_once()
        fingerprint = hashlib.sha256(json.dumps(
            [self.api_key, self.base_url, task, payload],
            ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest()
        return singleflight(fingerprint, execute_once)


def get_deepseek_client() -> DeepSeekClient:
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY is missing.")
    return DeepSeekClient(api_key=api_key)
