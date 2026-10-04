#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from abraham_sqlite import load_documents
from run_v560_storage import normalize_document_ids, prepare_source_content, _processing_gate

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db", required=True)
    p.add_argument("--limit", type=int, default=1000)
    p.add_argument("--output", required=True)
    a = p.parse_args()

    documents, _ = load_documents(limit=None)
    documents = normalize_document_ids(documents)
    documents, _ = prepare_source_content(documents)

    gated = []
    for d in documents:
        color, exportable, _ = _processing_gate(d)
        if exportable:
            d["_reader_rating_color"] = color
            gated.append(d)

    # Stable ordering: newest first, matching the pipeline's bounded workload intent.
    gated.sort(key=lambda d: str(d.get("updated_at") or d.get("last_processed_at") or d.get("first_seen_at") or ""), reverse=True)
    selected = gated[:max(0, a.limit)]
    payload = {
        "version": 1,
        "limit": a.limit,
        "count": len(selected),
        "readwise_ids": [str(d["readwise_id"]) for d in selected],
    }
    path = Path(a.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Frozen workset: {len(selected)} documents")
    print(f"Workset file: {path}")

if __name__ == "__main__":
    main()
