"""Adaptive batch planning utilities for Abraham.
No network/API calls. Uses document text size to avoid oversized model responses.
"""
from __future__ import annotations

DEFAULT_TARGET_CHARS = 36000
MIN_BATCH = 4
MAX_BATCH = 80

def document_cost(document: dict) -> int:
    title = str(document.get("title") or "")
    body = str(document.get("canonical_text") or document.get("content") or "")
    return max(1, int((len(title) + len(body)) * 1.15))

def plan_adaptive_batches(documents, target_chars=DEFAULT_TARGET_CHARS,
                          min_batch=MIN_BATCH, max_batch=MAX_BATCH):
    target_chars = max(4000, int(target_chars))
    min_batch = max(1, int(min_batch))
    max_batch = max(min_batch, int(max_batch))
    batches, current, cost = [], [], 0
    for doc in documents:
        c = document_cost(doc)
        if current and (cost + c > target_chars or len(current) >= max_batch):
            batches.append(current)
            current, cost = [], 0
        current.append(doc)
        cost += c
        if c >= target_chars:
            batches.append(current)
            current, cost = [], 0
    if current:
        batches.append(current)
    return batches

def batch_stats(batch):
    costs = [document_cost(d) for d in batch]
    return {
        "documents": len(batch),
        "estimated_input_chars": sum(costs),
        "max_document_chars": max(costs, default=0),
        "avg_document_chars": round(sum(costs) / len(costs), 1) if costs else 0,
    }
