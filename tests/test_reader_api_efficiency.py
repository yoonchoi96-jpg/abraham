"""Synthetic, provider-mocked API usage/cost tests. No paid requests."""
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest

from abraham_api_usage import append_record, normalize_usage, singleflight, usage_record
from deepseek_adapter import DeepSeekClient
from scripts.deepseek_usage_report import summarize


def response(*, cached=80, uncached=20):
    return {
        "model": "deepseek-flash",
        "choices": [{"finish_reason": "stop", "message": {"content": '{"ok":true}'}}],
        "usage": {
            "prompt_tokens": cached + uncached,
            "completion_tokens": 50, "total_tokens": cached + uncached + 50,
            "prompt_cache_hit_tokens": cached,
            "prompt_cache_miss_tokens": uncached,
            "completion_tokens_details": {"reasoning_tokens": 4}
        }
    }


class HttpReply:
    def __init__(self, body):
        self.payload = json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.payload


def test_usage_parses_provider_cache_details_without_inventing_missing_counts():
    u = normalize_usage(response()["usage"])
    assert (u["prompt_cache_hit_tokens"], u["prompt_cache_miss_tokens"]) == (80, 20)
    assert u["reasoning_tokens"] == 4
    legacy = normalize_usage({"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 30}})
    assert (legacy["prompt_cache_hit_tokens"], legacy["prompt_cache_miss_tokens"]) == (30, 70)
    unknown = normalize_usage({"prompt_tokens": 100, "completion_tokens": 55})
    assert unknown["prompt_cache_hit_tokens"] is None
    assert unknown["prompt_cache_miss_tokens"] is None
    contradiction = normalize_usage({"prompt_tokens": 10, "prompt_cache_hit_tokens": 20,
                                     "prompt_cache_miss_tokens": 3})
    assert contradiction["prompt_cache_hit_tokens"] is None


def test_log_only_provider_metadata_and_task_and_private_permissions(tmp_path, monkeypatch):
    filename = tmp_path / "private"/"tokens.jsonl"
    monkeypatch.setenv("ABRAHAM_AI_USAGE_LOG", str(filename))
    client = DeepSeekClient(api_key="secret-key")
    captured_payload = []

    def fake(request, timeout):
        captured_payload.append(json.loads(request.data))
        return HttpReply(response())

    with patch("deepseek_adapter.urllib.request.urlopen", side_effect=fake):
        actual = client.models.generate_content(
            model="deepseek-flash", contents="PERSONAL READER TEXT",
            config={"response_mime_type": "application/json",
                    "usage_task": "korean_translation"}
        )
    assert actual.text == '{"ok":true}'
    assert "usage_task" not in captured_payload[0]
    assert captured_payload[0]["messages"][0]["content"] == "PERSONAL READER TEXT"
    saved = filename.read_text()
    assert "secret-key" not in saved and "PERSONAL READER TEXT" not in saved
    row = json.loads(saved)
    assert row["task"] == "korean_translation"
    assert row["prompt_cache_hit_tokens"] == 80
    assert row["prompt_cache_miss_tokens"] == 20
    assert row["reasoning_tokens"] == 4
    assert (filename.stat().st_mode & 0o077) == 0


def test_no_response_cache_across_sequential_requests(tmp_path, monkeypatch):
    monkeypatch.setenv("ABRAHAM_AI_USAGE_LOG", str(tmp_path/"usage.jsonl"))
    client = DeepSeekClient(api_key="secret")
    calls = []
    def fake(request, timeout):
        calls.append(1)
        return HttpReply(response())
    with patch("deepseek_adapter.urllib.request.urlopen", side_effect=fake):
        client.generate_content(model="deepseek-flash", contents="repeat", config={})
        client.generate_content(model="deepseek-flash", contents="repeat", config={})
    assert len(calls) == 2  # persistent caching belongs to validated SQLite stages


def test_identical_simultaneous_paid_requests_singleflight(tmp_path, monkeypatch):
    monkeypatch.setenv("ABRAHAM_AI_USAGE_LOG", str(tmp_path/"usage.jsonl"))
    entered, release = threading.Event(), threading.Event()
    calls = []
    client = DeepSeekClient(api_key="account-secret")
    def fake(request, timeout):
        calls.append(1)
        entered.set()
        assert release.wait(timeout=3)
        return HttpReply(response())

    def invoke():
        return client.generate_content(
            model="deepseek-flash", contents="shared body",
            config={"usage_task": "korean_translation"})
    with patch("deepseek_adapter.urllib.request.urlopen", side_effect=fake):
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(invoke)
            assert entered.wait(timeout=3)
            second = pool.submit(invoke)
            time.sleep(0.08)
            release.set()
            assert first.result(timeout=3).text == second.result(timeout=3).text
    assert len(calls) == 1
    assert len((tmp_path/"usage.jsonl").read_text().splitlines()) == 1


def test_different_tasks_do_not_share_attribution(tmp_path, monkeypatch):
    monkeypatch.setenv("ABRAHAM_AI_USAGE_LOG", str(tmp_path/"usage.jsonl"))
    calls = []
    client = DeepSeekClient(api_key="account-secret")
    def fake(request, timeout):
        calls.append(1)
        return HttpReply(response())
    with patch("deepseek_adapter.urllib.request.urlopen", side_effect=fake):
        for task in ("korean_translation", "rating"):
            client.generate_content(model="deepseek-flash", contents="same", config={"usage_task": task})
    assert len(calls) == 2
    tasks = [json.loads(x)["task"] for x in (tmp_path/"usage.jsonl").read_text().splitlines()]
    assert tasks == ["korean_translation", "rating"]


def test_model_pricing_only_when_fully_observed_and_explicitly_supplied():
    a = usage_record(model="deepseek-flash", task="rating", usage=response()["usage"])
    missing = usage_record(model="deepseek-flash", task="rating", usage={"prompt_tokens": 13, "completion_tokens": 4})
    pricing = {"cache_hit": 0.003, "cache_miss": 0.15, "output": 0.6}
    all_summary = summarize([a, missing], model="deepseek-flash", prices=pricing)
    row = all_summary["rows"][0]
    assert row["requests"] == 2 and row["unknown_cache_requests"] == 1
    assert row["estimated_cost_usd"] is None
    one = summarize([a], model="deepseek-flash", prices=pricing)["rows"][0]
    assert one["known_cache_hit_pct"] == 80.0
    assert one["estimated_cost_usd"] == pytest.approx((80*.003 + 20*.15 + 50*.6)/1e6)
    assert summarize([a])["rows"][0].get("estimated_cost_usd") is None
    with pytest.raises(ValueError):
        summarize([a], prices=pricing)
