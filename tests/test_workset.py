from __future__ import annotations

import json

from abraham_workset import filter_workset

def test_filter_workset_uses_frozen_ids(tmp_path, monkeypatch):
    path = tmp_path / "workset.json"
    path.write_text(json.dumps({"readwise_ids":["r2","r4"]}), encoding="utf-8")
    monkeypatch.setenv("ABRAHAM_WORKSET_PATH", str(path))
    docs = [{"readwise_id":"r1"},{"readwise_id":"r2"},{"readwise_id":"r4"},{"readwise_id":"r5"}]
    assert [d["readwise_id"] for d in filter_workset(docs)] == ["r2","r4"]
