from __future__ import annotations

import os

from deepseek_adapter import get_deepseek_client
from gemini_adapter import get_gemini_client


class _Models:
    def __init__(self, owner):
        self._owner = owner

    def generate_content(self, *, model: str, contents: str, config: dict | None = None):
        return self._owner.generate_content(
            model=model, contents=contents, config=config or {}
        )


class DeepSeekPrimaryWithGeminiFallback:
    """Use DeepSeek first; invoke Gemini only after DeepSeek exhausts its retries."""

    def __init__(self, task: str):
        self.task = task
        self.primary = get_deepseek_client()
        self.fallback = get_gemini_client()
        self.models = _Models(self)

    @staticmethod
    def _is_provider_failure(exc: Exception) -> bool:
        """Only provider/network failures may spend a Gemini fallback call.

        Application/configuration/validation errors must propagate instead of
        silently escalating to the more expensive fallback provider.
        """
        message = str(exc).lower()
        if not isinstance(exc, (RuntimeError, TimeoutError)):
            return False
        if "deepseek http " in message:
            return any(code in message for code in ("429", "500", "502", "503", "504"))
        return "deepseek request failed" in message or "timed out" in message

    def generate_content(self, *, model: str, contents: str, config: dict | None = None):
        config = dict(config or {})
        try:
            return self.primary.models.generate_content(
                model=model, contents=contents, config=config
            )
        except Exception as primary_error:
            if not self._is_provider_failure(primary_error):
                raise
            fallback_model = os.getenv(
                f"GEMINI_{self.task.upper()}_FALLBACK_MODEL",
                os.getenv("GEMINI_RATING_MODEL", "gemini-3.8-flash"),
            )
            print(
                f"[AI FALLBACK] task={self.task} provider=deepseek "
                f"error={type(primary_error).__name__}; "
                f"switching_to=gemini model={fallback_model}"
            )
            try:
                return self.fallback.models.generate_content(
                    model=fallback_model,
                    contents=contents,
                    config=config,
                )
            except Exception as fallback_error:
                raise RuntimeError(
                    f"{self.task} primary DeepSeek and Gemini fallback both failed: "
                    f"primary={primary_error}; fallback={fallback_error}"
                ) from fallback_error


def get_rating_client():
    return DeepSeekPrimaryWithGeminiFallback("rating")


def get_relation_client():
    return DeepSeekPrimaryWithGeminiFallback("entity_relation")
