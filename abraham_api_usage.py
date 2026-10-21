"""DeepSeek API accounting helpers. No prompts, completions, keys or Reader text.

All token counts come from provider usage fields, never from estimates.
Cost calculations require explicit per-million-token rates from the operator.
"""
from __future__ import annotations

import fcntl
import threading
from concurrent.futures import Future
import json
import os
from datetime import datetime, timezone
from pathlib import Path


TASKS = frozenset({
    "korean_translation", "rating", "entity_classification",
    "entity_relation", "entity_canonicalization", "unattributed",
})


def _count(value):
    # bools, floats and negative values are not token counts.
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    return None


def normalize_usage(usage: dict) -> dict:
    """Preserve unknown cache usage as None (never misreport it as a miss)."""
    usage = usage if isinstance(usage, dict) else {}
    prompt = _count(usage.get("prompt_tokens"))
    completion = _count(usage.get("completion_tokens"))
    total = _count(usage.get("total_tokens"))
    hit = _count(usage.get("prompt_cache_hit_tokens"))
    miss = _count(usage.get("prompt_cache_miss_tokens"))
    prompt_details = usage.get("prompt_tokens_details") or {}
    if hit is None and isinstance(prompt_details, dict):
        hit = _count(prompt_details.get("cached_tokens"))
    if hit is not None and miss is None and prompt is not None and hit <= prompt:
        miss = prompt - hit
    if miss is not None and hit is None and prompt is not None and miss <= prompt:
        hit = prompt - miss
    if (prompt is not None and hit is not None and miss is not None
            and prompt != hit + miss):
        hit, miss = None, None
    completion_details = usage.get("completion_tokens_details") or {}
    reasoning = _count(usage.get("reasoning_tokens"))
    if reasoning is None and isinstance(completion_details, dict):
        reasoning = _count(completion_details.get("reasoning_tokens"))
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "prompt_cache_hit_tokens": hit,
        "prompt_cache_miss_tokens": miss,
        "reasoning_tokens": reasoning,
    }


def usage_record(*, model: str, task: str, usage: dict,
                 finish_reason: str | None = None,
                 response_model: str | None = None) -> dict:
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "provider": "deepseek",
        "model": model,
        "response_model": response_model if isinstance(response_model, str) else None,
        "task": task if task in TASKS else "unattributed",
        "finish_reason": finish_reason if finish_reason in {
            "stop", "length", "content_filter", "tool_calls"
        } else "other",
        **normalize_usage(usage),
    }


def append_record(path: str, record: dict) -> None:
    """Append metadata in one write, guarded against cross-thread/process races.

    The log must stay out of publicly tracked files. Existing symlink paths
    are rejected; failures must not interrupt a successful paid API response.
    """
    target = Path(os.path.abspath(os.path.expanduser(path)))
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise OSError("USAGE_LOG_PATH_INVALID")
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(target, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        blob = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        with os.fdopen(fd, "ab", closefd=False) as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                handle.write(blob)
                handle.flush()
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        os.close(fd)

# In-process single-flight only. Persistent translation cache remains in SQLite.
_INFLIGHT_LOCK = threading.Lock()
_INFLIGHT: dict[str, Future] = {}


def singleflight(key: str, callback):
    """Execute identical simultaneous paid requests just once per Python process.

    Failed leaders release the key; there is no permanent response cache and
    no reuse across distinct payloads/accounts. Followers share the exact
    response or failure without making additional API requests.
    """
    with _INFLIGHT_LOCK:
        future = _INFLIGHT.get(key)
        leader = future is None
        if leader:
            future = Future()
            _INFLIGHT[key] = future
    if not leader:
        return future.result()
    try:
        result = callback()
    except BaseException as exc:
        future.set_exception(exc)
        raise
    else:
        future.set_result(result)
        return result
    finally:
        with _INFLIGHT_LOCK:
            if _INFLIGHT.get(key) is future:
                del _INFLIGHT[key]
