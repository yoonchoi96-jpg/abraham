#!/usr/bin/env python3
"""Incremental Entity/Relation drain independent of Korean translation/render stages."""
from __future__ import annotations

import argparse
import os

from abraham_sqlite import connect as connect_db, load_documents
from abraham_incremental import ensure_schema, select_pending_documents, mark_started, mark_completed
from entity_relation_ai import extract_and_persist
from run_v560_storage import normalize_document_ids, prepare_source_content, _processing_gate


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=1000)
    args = parser.parse_args()

    documents, _relations = load_documents(limit=None)
    documents = normalize_document_ids(documents)
    documents, _unavailable = prepare_source_content(documents)

    gated = []
    for document in documents:
        color, exportable, _rateable = _processing_gate(document)
        if exportable:
            document["_reader_rating_color"] = color
            gated.append(document)

    stage = "entity_relation_extraction"
    with connect_db() as conn:
        ensure_schema(conn)
        pending, skipped = select_pending_documents(conn, gated, stage)
        pending = pending[:max(0, args.limit)]
        if args.apply and pending:
            mark_started(conn, pending, stage, worker_id="abraham-v5.6-graph-drain")

    print("=" * 70)
    print("ENTITY / RELATION DRAIN")
    print("=" * 70)
    print("Graph candidates         :", len(gated))
    print("Graph pending/skipped    :", len(pending), "/", len(skipped))
    print("Apply                    :", args.apply)

    if not pending:
        print("Nothing pending.")
        return

    stats = extract_and_persist(pending, apply=args.apply)
    processed_ids = {str(x) for x in stats.get("processed_readwise_ids", [])}
    processed = [d for d in pending if str(d.get("readwise_id") or "") in processed_ids]

    if args.apply and processed:
        with connect_db() as conn:
            ensure_schema(conn)
            result_by_id = {
                d["document_id"]: {
                    "graph_stats": {
                        "entities_created": stats.get("entities_created", 0),
                        "relations_created": stats.get("relations_created", 0),
                    }
                }
                for d in processed
            }
            mark_completed(conn, processed, stage, result_by_id, result_fields=("graph_stats",))

    print("Processed documents      :", stats.get("documents", 0))
    print("API batches              :", stats.get("api_batches", 0))
    print("Entities created         :", stats.get("entities_created", 0))
    print("Document-entity links    :", stats.get("document_entity_links", 0))
    print("Relations created        :", stats.get("relations_created", 0))


if __name__ == "__main__":
    main()

# Validation trigger: graph drain + Obsidian Headless Sync.
