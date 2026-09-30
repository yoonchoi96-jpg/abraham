"""Abraham V7 local research orchestration with durable provenance.

V7 performs retrieval, diversity filtering and basic evidence comparison locally.
An optional answerer may be supplied by a higher layer; this module itself never
calls an external model.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone

from v6_semantic import search

VERSION = "7.3.0"


def ensure_schema(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS v7_research_runs(
            run_id TEXT PRIMARY KEY,
            question TEXT NOT NULL,
            status TEXT NOT NULL,
            result_json TEXT,
            created_at TEXT NOT NULL,
            completed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS v7_evidence(
            run_id TEXT NOT NULL,
            readwise_id TEXT NOT NULL,
            rank INTEGER NOT NULL,
            score REAL,
            role TEXT,
            PRIMARY KEY(run_id,readwise_id)
        );
        CREATE INDEX IF NOT EXISTS idx_v7_evidence_run
            ON v7_evidence(run_id,rank);
        CREATE TABLE IF NOT EXISTS v7_claims(
            run_id TEXT NOT NULL,
            claim_key TEXT NOT NULL,
            claim TEXT NOT NULL,
            supporting_ids TEXT NOT NULL,
            opposing_ids TEXT NOT NULL,
            confidence REAL NOT NULL,
            PRIMARY KEY(run_id,claim_key)
        );
        """
    )
    conn.commit()


def _tokens(text):
    return set(re.findall(r"[A-Za-z0-9_\-]{2,}|[가-힣]{2,}|[一-龯]{2,}", (text or "").lower()))


def _diverse_sources(conn, question, candidates, max_sources):
    if not candidates:
        return []

    selected = []
    selected_tokens = []
    for candidate in candidates:
        rid = candidate["readwise_id"]
        row = conn.execute(
            "SELECT title, canonical_text, content FROM documents WHERE readwise_id=?",
            (rid,),
        ).fetchone()
        if not row:
            continue
        text = "\n".join(str(x or "") for x in row)
        toks = _tokens(text)

        # Avoid filling the evidence set with near-duplicate documents.
        duplicate = any(
            len(toks & old) / max(1, len(toks | old)) >= 0.82
            for old in selected_tokens
        )
        if duplicate:
            continue

        selected.append(
            {
                **candidate,
                "title": row["title"] or "",
            }
        )
        selected_tokens.append(toks)
        if len(selected) >= max_sources:
            break
    return selected


def _roles(sources):
    if not sources:
        return []
    ranked = sorted(sources, key=lambda x: x.get("score", 0.0), reverse=True)
    out = []
    for i, source in enumerate(ranked, 1):
        role = "primary" if i == 1 else ("supporting" if i <= 4 else "context")
        out.append({**source, "rank": i, "role": role})
    return out




def _compare_evidence(conn, sources):
    """Local disagreement signal; never labels a real-world contradiction as fact."""
    neg = {"not", "no", "never", "without", "없", "아니", "않", "못", "無"}
    items = []
    for source in sources:
        row = conn.execute(
            "SELECT title, canonical_text, content FROM documents WHERE readwise_id=?",
            (source["readwise_id"],),
        ).fetchone()
        if not row:
            continue
        text = "\n".join(str(x or "") for x in row)
        toks = _tokens(text)
        items.append({
            "readwise_id": source["readwise_id"],
            "tokens": toks,
            "negative_signal": bool(toks & neg),
        })

    comparisons = []
    for i, left in enumerate(items):
        for right in items[i + 1:]:
            overlap = len(left["tokens"] & right["tokens"]) / max(1, len(left["tokens"] | right["tokens"]))
            negation_disagreement = left["negative_signal"] != right["negative_signal"]
            if overlap >= 0.08 and negation_disagreement:
                signal = round(min(1.0, overlap * 1.5), 4)
                comparisons.append({
                    "left": left["readwise_id"],
                    "right": right["readwise_id"],
                    "lexical_overlap": round(overlap, 4),
                    "negation_disagreement": True,
                    "conflict_signal": signal,
                })
    return comparisons

def build_plan(conn, question, max_sources=8):
    max_sources = max(1, min(int(max_sources), 8))
    candidates = search(conn, question, min(50, max_sources * 8))
    sources = _roles(_diverse_sources(conn, question, candidates, max_sources))
    comparisons = _compare_evidence(conn, sources)
    retrieval_confidence = round(
        min(1.0, sum(float(x.get("score", 0.0)) for x in sources[:4]) / max(1, min(4, len(sources)))),
        4,
    )
    return {
        "question": question,
        "sources": sources,
        "confidence": retrieval_confidence,
        "evidence_comparison": {
            "items": len(comparisons),
            "signals": comparisons,
        },
        "steps": [
            "decompose",
            "retrieve",
            "deduplicate_and_diversify",
            "compare evidence",
            "detect uncertainty/conflicts",
            "synthesize with provenance",
        ],
    }


def run_research(conn, question, max_sources=8, answerer=None):
    ensure_schema(conn)
    now = datetime.now(timezone.utc).isoformat()
    run_id = hashlib.sha256(
        (question + now).encode("utf-8")
    ).hexdigest()[:24]

    plan = build_plan(conn, question, max_sources)
    conn.execute(
        "INSERT INTO v7_research_runs VALUES(?,?,?,?,?,NULL)",
        (run_id, question, "collecting", None, now),
    )

    for source in plan["sources"]:
        conn.execute(
            "INSERT OR REPLACE INTO v7_evidence VALUES(?,?,?,?,?)",
            (
                run_id,
                source["readwise_id"],
                source["rank"],
                source.get("score"),
                source["role"],
            ),
        )

    answer = answerer(plan) if answerer else None
    result = {"run_id": run_id, "plan": plan, "answer": answer}
    status = "completed" if answer is not None else "prepared"
    conn.execute(
        """UPDATE v7_research_runs
           SET status=?,result_json=?,completed_at=?
           WHERE run_id=?""",
        (
            status,
            json.dumps(result, ensure_ascii=False),
            datetime.now(timezone.utc).isoformat(),
            run_id,
        ),
    )
    conn.commit()
    return result
