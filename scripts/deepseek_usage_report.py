"""Read-only DeepSeek usage ledger summary; never touches provider APIs.

Charges are ESTIMATES only if operator explicitly supplies model-specific
per-million-token rates. Unknown cached token breakdown remains unknown.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path


FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens",
          "prompt_cache_hit_tokens", "prompt_cache_miss_tokens",
          "reasoning_tokens")


def summarize(records, *, model=None, prices=None):
    if prices is not None:
        if model is None or set(prices) != {"cache_hit", "cache_miss", "output"}:
            raise ValueError("MODEL_SPECIFIC_THREE_PRICE_RATES_REQUIRED")
        if any(isinstance(p, bool) or not isinstance(p, (int, float))
               or p < 0 for p in prices.values()):
            raise ValueError("INVALID_PRICE")
    groups = defaultdict(lambda: {
        "requests": 0, "known_cache_requests": 0,
        "unknown_cache_requests": 0, "unknown_output_requests": 0,
        "finish_length": 0,
        **{field: 0 for field in FIELDS},
    })
    for record in records:
        if not isinstance(record, dict) or record.get("provider") != "deepseek":
            continue
        if model is not None and record.get("model") != model:
            continue
        task = record.get("task") if isinstance(record.get("task"), str) else "unattributed"
        label = (str(record.get("model") or "unknown"), task)
        out = groups[label]
        out["requests"] += 1
        if record.get("finish_reason") == "length":
            out["finish_length"] += 1
        cache_values = (record.get("prompt_cache_hit_tokens"),
                        record.get("prompt_cache_miss_tokens"))
        if all(type(v) is int and v >= 0 for v in cache_values):
            out["known_cache_requests"] += 1
            for field, value in zip(("prompt_cache_hit_tokens",
                                      "prompt_cache_miss_tokens"), cache_values):
                out[field] += value
        else:
            out["unknown_cache_requests"] += 1
        output_value = record.get("completion_tokens")
        if type(output_value) is not int or output_value < 0:
            out["unknown_output_requests"] += 1
        for field in ("prompt_tokens", "completion_tokens", "total_tokens", "reasoning_tokens"):
            n = record.get(field)
            if type(n) is int and n >= 0:
                out[field] += n
    rows = []
    for (model_key, task), counts in sorted(groups.items()):
        hit, miss = counts["prompt_cache_hit_tokens"], counts["prompt_cache_miss_tokens"]
        known_prompt = hit + miss
        row = {"model": model_key, "task": task, **counts,
               "known_cache_hit_pct": round(100 * hit / known_prompt, 2)
               if known_prompt else None}
        if prices is not None:
            if counts["unknown_cache_requests"] or counts["unknown_output_requests"]:
                # Cannot meaningfully price unaccounted prompts as cache misses.
                row["estimated_cost_usd"] = None
                row["cost_note"] = "unknown_cache_or_output_usage_prevents_full_estimate"
            else:
                estimated = (hit * prices["cache_hit"] +
                             miss * prices["cache_miss"] +
                             counts["completion_tokens"] * prices["output"]) / 1_000_000
                row["estimated_cost_usd"] = round(estimated, 8)
                row["cache_savings_vs_all_miss_usd"] = round(
                    hit * (prices["cache_miss"] - prices["cache_hit"]) / 1_000_000, 8
                )
                row["cost_note"] = "model_rate_scenario_not_provider_invoice"
        rows.append(row)
    return {"rows": rows, "model_filter": model,
            "price_scenario_per_million_tokens": prices}


def main():
    p = argparse.ArgumentParser(description="Summarize DeepSeek metadata usage only")
    p.add_argument("--log", required=True, type=Path)
    p.add_argument("--model", help="Required if pricing a single model")
    p.add_argument("--cache-hit-rate", type=float)
    p.add_argument("--cache-miss-rate", type=float)
    p.add_argument("--output-rate", type=float)
    args = p.parse_args()
    options = (args.cache_hit_rate, args.cache_miss_rate, args.output_rate)
    if any(x is not None for x in options):
        if not args.model or any(x is None for x in options):
            p.error("for cost estimate provide --model and all three rates")
        rates = dict(zip(("cache_hit", "cache_miss", "output"), options))
    else:
        rates = None
    with args.log.open("r", encoding="utf-8") as file:
        records = [json.loads(line) for line in file if line.strip()]
    print(json.dumps(summarize(records, model=args.model, prices=rates),
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
