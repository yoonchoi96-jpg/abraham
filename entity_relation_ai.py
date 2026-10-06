from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import unicodedata
from datetime import datetime, timezone
from difflib import SequenceMatcher

from provider_router import get_relation_client

ENTITY_TYPES = (
    "Person", "Company", "Organization", "Country", "Place",
    "Product", "Technology", "Concept", "Event", "Institution", "Brand", "Other",
)

DEFAULT_BATCH = 20
MAX_ENTITIES_PER_DOC = 8
MAX_RELATIONS_PER_DOC = 25
MIN_RELATION_CONFIDENCE = 0.80


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash_id(prefix: str, value: str) -> str:
    return f"{prefix}_{hashlib.sha256(value.encode('utf-8')).hexdigest()[:24]}"


def _norm(value: str) -> str:
    value = str(value or "").strip().casefold()
    value = re.sub(r"[\u200b\u200c\u200d]", "", value)
    value = re.sub(r"[\s\-_–—]+", " ", value)
    value = re.sub(r"[^\w\s&.+#]", "", value, flags=re.UNICODE)
    return value.strip()


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row[1])
        for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    }


def ensure_graph_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS entities (
        entity_id TEXT PRIMARY KEY,
        canonical_name TEXT NOT NULL,
        entity_type TEXT NOT NULL DEFAULT 'Other',
        status TEXT NOT NULL DEFAULT 'active',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS document_entities (
        readwise_id TEXT NOT NULL,
        entity_id TEXT NOT NULL,
        mention TEXT,
        confidence REAL,
        evidence_quote TEXT,
        created_at TEXT NOT NULL,
        PRIMARY KEY (readwise_id, entity_id)
    );

    CREATE TABLE IF NOT EXISTS relations (
        relation_id INTEGER PRIMARY KEY AUTOINCREMENT,
        readwise_id TEXT NOT NULL,
        subject_entity_id TEXT NOT NULL,
        relation_type TEXT NOT NULL,
        object_entity_id TEXT NOT NULL,
        confidence REAL,
        evidence_quote TEXT,
        evidence_start_hint TEXT,
        status TEXT NOT NULL DEFAULT 'validated',
        created_at TEXT NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_entities_canonical_name
        ON entities(canonical_name);
    CREATE INDEX IF NOT EXISTS idx_document_entities_readwise
        ON document_entities(readwise_id);
    CREATE INDEX IF NOT EXISTS idx_relations_subject
        ON relations(subject_entity_id);
    CREATE INDEX IF NOT EXISTS idx_relations_object
        ON relations(object_entity_id);
    CREATE TABLE IF NOT EXISTS relation_evidence (
        evidence_id INTEGER PRIMARY KEY AUTOINCREMENT,
        relation_id INTEGER NOT NULL,
        readwise_id TEXT NOT NULL,
        confidence REAL,
        evidence_quote TEXT,
        evidence_start_hint TEXT,
        created_at TEXT NOT NULL,
        UNIQUE(relation_id, readwise_id, evidence_quote)
    );
    CREATE INDEX IF NOT EXISTS idx_relation_evidence_relation
        ON relation_evidence(relation_id);
    """)
    conn.commit()


def _ensure_columns(conn: sqlite3.Connection) -> None:
    ensure_graph_schema(conn)

    specs = {
        "entities": {
            "entity_type": "TEXT NOT NULL DEFAULT 'Other'",
            "status": "TEXT NOT NULL DEFAULT 'active'",
            "created_at": "TEXT",
            "updated_at": "TEXT",
        },
        "document_entities": {
            "mention": "TEXT",
            "confidence": "REAL",
            "evidence_quote": "TEXT",
            "created_at": "TEXT",
        },
        "relations": {
            "confidence": "REAL",
            "evidence_quote": "TEXT",
            "evidence_start_hint": "TEXT",
            "status": "TEXT NOT NULL DEFAULT 'validated'",
            "created_at": "TEXT",
        },
    }
    for table, table_specs in specs.items():
        existing = _columns(conn, table)
        for name, sql_type in table_specs.items():
            if name not in existing:
                conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {sql_type}')
    conn.commit()


def _pick_column(columns: set[str], candidates: tuple[str, ...]) -> str | None:
    for candidate in candidates:
        if candidate in columns:
            return candidate
    return None


def _load_catalog(conn: sqlite3.Connection) -> list[dict]:
    cols = _columns(conn, "entities")
    if "entity_id" not in cols or "canonical_name" not in cols:
        return []
    type_col = "entity_type" if "entity_type" in cols else None
    rows = conn.execute(
        "SELECT entity_id, canonical_name"
        + (", entity_type" if type_col else "")
        + " FROM entities WHERE COALESCE(status,'active') NOT IN ('rejected','deleted','merged')"
    ).fetchall()
    catalog = [
        {
            "entity_id": str(row[0]),
            "canonical_name": str(row[1]),
            "entity_type": str(row[2] or "Other") if type_col else "Other",
            "aliases": [],
        }
        for row in rows
        if row[0] and row[1]
    ]
    by_id = {x["entity_id"]: x for x in catalog}
    if by_id:
        mentions = conn.execute(
            "SELECT entity_id, mention FROM document_entities "
            "WHERE mention IS NOT NULL AND TRIM(mention)<>''"
        ).fetchall()
        for eid, mention in mentions:
            if str(eid) in by_id:
                by_id[str(eid)]["aliases"].append(str(mention))
    return catalog


def _catalog_maps(catalog: list[dict]) -> tuple[dict[str, dict], dict[str, dict]]:
    exact = {}
    for item in catalog:
        exact.setdefault(_norm(item["canonical_name"]), item)
        for alias in item.get("aliases", []):
            exact.setdefault(_norm(alias), item)
    return exact, {str(item["entity_id"]): item for item in catalog}


def _resolve_entity(name: str, entity_type: str, exact: dict[str, dict], catalog_by_id: dict[str, dict]) -> dict:
    normalized = _norm(name)
    if normalized in exact:
        exact_item = exact[normalized]
        if str(exact_item.get("entity_type") or "Other") == str(entity_type or "Other"):
            return dict(exact_item)
        # Never resolve the same surface form across different entity types.

    best = None
    best_score = 0.0
    for item in catalog_by_id.values():
        if str(item.get("entity_type") or "Other") != str(entity_type or "Other"):
            continue
        candidate = _norm(item["canonical_name"])
        if not candidate or not normalized:
            continue
        if candidate.startswith(normalized) or normalized.startswith(candidate):
            score = min(len(candidate), len(normalized)) / max(len(candidate), len(normalized))
            if score > best_score:
                best_score, best = score, item
        elif len(normalized) >= 5 and len(candidate) >= 5:
            score = SequenceMatcher(None, normalized, candidate).ratio()
            if score > best_score:
                best_score, best = score, item
    if best is not None and best_score >= 0.94:
        return dict(best)

    clean_type = entity_type if entity_type in ENTITY_TYPES else "Other"
    canonical = str(name).strip()
    return {
        "entity_id": _hash_id("ent", _norm(canonical)),
        "canonical_name": canonical,
        "entity_type": clean_type,
    }


def _prompt(documents: list[dict], catalog: list[dict]) -> str:
    payload = []
    for d in documents:
        text = str(d.get("canonical_text") or d.get("content") or "")
        # Keep enough context for extraction without letting one giant article
        # consume the whole batch.
        payload.append({
            "readwise_id": str(d.get("readwise_id") or ""),
            "title": str(d.get("title") or ""),
            "text": text[:30000],
        })

    existing = [
        {"canonical_name": x["canonical_name"], "entity_type": x.get("entity_type", "Other")}
        for x in catalog
    ]

    return """You are the entity and relation extraction layer of a long-term knowledge graph.

Return STRICT JSON. Extract only information explicitly supported by the supplied documents.
Do not use outside knowledge. Do not invent entities or relations.

ENTITY RULES:
- Extract ONLY durable, reusable knowledge entities that are useful beyond this single document.
- Prefer entities likely to recur across the knowledge base: named people, companies,
  organizations, countries, places, products, technologies, concepts, events, institutions,
  and brands. Do NOT extract generic nouns, temporary examples, ordinary objects, or one-off phrases.
- Prefer the most specific canonical name actually supported by the text.
- canonical_name MUST be English-only and written with Latin script. Prefer the established official English name when one exists; otherwise use a natural English translation or standard English romanization.
- NEVER use Chinese/Han, Japanese kana, Korean Hangul, Cyrillic, Arabic, Hebrew, Devanagari, or another non-Latin script in canonical_name.
- Preserve the original source spelling exactly in the mention field; mention may contain any language/script.
- Preserve evidence_quote exactly from the source text.
- For Latin-script entities, preserve the established English/Latin spelling when it is the appropriate canonical form.
- The same real-world entity appearing in several documents should use the same English canonical_name across documents.
- Do not create an entity merely because a common noun is capitalized.
- Do not merge distinct entities.
- entity_type must be one of:
  Person, Company, Organization, Country, Place, Product, Technology, Concept, Event, Institution, Brand, Other.

RELATION RULES:
- Extract only explicit, useful entity-to-entity relationships.
- The relation label must be short, factual, and reusable, e.g. founded, acquired, headquartered_in,
  developed, uses, regulates, owns, created, employed_by, located_in, part_of, competes_with,
  partnered_with, caused, occurred_in, associated_with.
- Do not infer causality or intent.
- Every relation MUST have an evidence_quote copied from the supplied text.
- confidence is 0.0-1.0 and should be at least 0.80 only when the relationship is directly supported.

EXISTING ENTITY CATALOG:
Use an existing canonical_name when it clearly refers to the same entity. Do not force a match.

JSON SCHEMA EXAMPLE:
{
  "documents": [
    {
      "readwise_id": "abc",
      "entities": [
        {"mention":"Apple","canonical_name":"Apple","entity_type":"Company"}
      ],
      "relations": [
        {
          "subject":"Apple",
          "relation":"acquired",
          "object":"Beats",
          "confidence":0.95,
          "evidence_quote":"Apple acquired Beats in 2014.",
          "evidence_start_hint":"Apple acquired Beats"
        }
      ]
    }
  ]
}

Existing catalog:
""" + json.dumps(existing, ensure_ascii=False) + "\n\nDocuments:\n" + json.dumps(payload, ensure_ascii=False)


def _call(client, documents: list[dict], catalog: list[dict]) -> dict:
    response = client.models.generate_content(
        model=os.getenv(
            "DEEPSEEK_ENTITY_RELATION_MODEL",
            os.getenv("MODEL", "deepseek-flash"),
        ),
        contents=_prompt(documents, catalog),
        config={
            "response_mime_type": "application/json",
            "thinking": os.getenv("DEEPSEEK_ENTITY_RELATION_THINKING", "disabled"),
            "max_output_tokens": int(
                os.getenv("DEEPSEEK_ENTITY_RELATION_MAX_TOKENS", "8192")
            ),
        },
    )
    data = json.loads(response.text.strip())
    if not isinstance(data, dict) or not isinstance(data.get("documents"), list):
        raise ValueError("Entity/relation response must contain documents[]")
    return data


def _is_latin_canonical(value: str) -> bool:
    """Return True when all alphabetic characters use the Latin script."""
    text = str(value or "").strip()
    if not text:
        return False
    for ch in text:
        if not ch.isalpha():
            continue
        if "LATIN" not in unicodedata.name(ch, ""):
            return False
    return True


def _validate_english_canonical(name: str, field: str) -> str:
    clean = str(name or "").strip()
    if not _is_latin_canonical(clean):
        raise ValueError(f"{field} must use Latin/English canonical text: {clean!r}")
    return clean


def _safe_confidence(value) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _insert_entity(conn, entity: dict) -> None:
    cols = _columns(conn, "entities")
    now = _now()
    values = {
        "entity_id": entity["entity_id"],
        "canonical_name": entity["canonical_name"],
        "entity_type": entity.get("entity_type", "Other"),
        "status": "active",
        "created_at": now,
        "updated_at": now,
    }
    usable = {k: v for k, v in values.items() if k in cols}
    names = list(usable)
    placeholders = ",".join("?" for _ in names)
    conn.execute(
        f'INSERT OR IGNORE INTO entities ({",".join(names)}) VALUES ({placeholders})',
        tuple(usable[n] for n in names),
    )


def _insert_document_entity(conn, readwise_id: str, entity: dict, mention: str, confidence: float, evidence: str) -> None:
    cols = _columns(conn, "document_entities")
    values = {
        "readwise_id": str(readwise_id),
        "entity_id": entity["entity_id"],
        "mention": mention or entity["canonical_name"],
        "confidence": confidence,
        "evidence_quote": evidence,
        "created_at": _now(),
    }
    # Legacy schemas may use a surrogate integer id rather than the composite key.
    usable = {k: v for k, v in values.items() if k in cols}
    if "readwise_id" not in usable or "entity_id" not in usable:
        raise RuntimeError("document_entities must contain readwise_id and entity_id")
    where = " AND ".join(f"{k}=?" for k in ("readwise_id", "entity_id") if k in cols)
    exists = conn.execute(
        f"SELECT 1 FROM document_entities WHERE {where} LIMIT 1",
        tuple(usable[k] for k in ("readwise_id", "entity_id") if k in cols),
    ).fetchone()
    if exists:
        return
    names = list(usable)
    conn.execute(
        f'INSERT INTO document_entities ({",".join(names)}) VALUES ({",".join("?" for _ in names)})',
        tuple(usable[n] for n in names),
    )


def _relation_exists(conn, readwise_id: str, subject_id: str, relation: str, object_id: str) -> bool:
    cols = _columns(conn, "relations")
    predicates = []
    values = []
    for col, value in (
        (_pick_column(cols, ("readwise_id", "document_id")), str(readwise_id)),
        ("subject_entity_id", subject_id),
        ("relation_type", relation),
        ("object_entity_id", object_id),
    ):
        if col:
            predicates.append(f"{col}=?")
            values.append(value)
    if len(predicates) < 4:
        return False
    return conn.execute(
        f"SELECT 1 FROM relations WHERE {' AND '.join(predicates)} LIMIT 1",
        tuple(values),
    ).fetchone() is not None


def _find_existing_relation(conn, subject_id: str, relation: str, object_id: str):
    return conn.execute(
        "SELECT relation_id, confidence FROM relations "
        "WHERE subject_entity_id=? AND relation_type=? AND object_entity_id=? "
        "AND COALESCE(status,'validated') NOT IN ('deleted','rejected') "
        "ORDER BY relation_id LIMIT 1",
        (subject_id, relation, object_id),
    ).fetchone()


def _insert_relation_evidence(conn, relation_id: int, readwise_id: str,
                              confidence: float, evidence: str, hint: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO relation_evidence "
        "(relation_id,readwise_id,confidence,evidence_quote,evidence_start_hint,created_at) "
        "VALUES (?,?,?,?,?,?)",
        (relation_id, str(readwise_id), confidence, evidence, hint, _now()),
    )


def _insert_relation(conn, readwise_id: str, subject: dict, relation: dict, object_: dict) -> bool:
    cols = _columns(conn, "relations")
    if not {"subject_entity_id", "relation_type", "object_entity_id"}.issubset(cols):
        raise RuntimeError("relations must contain subject_entity_id, relation_type, object_entity_id")

    confidence = _safe_confidence(relation.get("confidence"))
    if confidence < float(os.getenv("ABRAHAM_MIN_RELATION_CONFIDENCE", str(MIN_RELATION_CONFIDENCE))):
        return False

    relation_name = _canonical_relation(relation.get("relation"))[:160]
    evidence = str(relation.get("evidence_quote") or "").strip()[:1200]
    hint = str(relation.get("evidence_start_hint") or "").strip()[:500]
    if not relation_name or not evidence or subject["entity_id"] == object_["entity_id"]:
        return False

    existing = _find_existing_relation(
        conn, subject["entity_id"], relation_name, object_["entity_id"]
    )
    if existing:
        relation_id, _old_confidence = existing
        conn.execute(
            "UPDATE relations SET confidence=MAX(COALESCE(confidence,0), ?) WHERE relation_id=?",
            (confidence, relation_id),
        )
        _insert_relation_evidence(conn, relation_id, readwise_id, confidence, evidence, hint)
        return False

    readwise_col = _pick_column(cols, ("readwise_id", "document_id"))
    values = {
        "readwise_id": str(readwise_id),
        "subject_entity_id": subject["entity_id"],
        "relation_type": relation_name,
        "object_entity_id": object_["entity_id"],
        "confidence": confidence,
        "evidence_quote": evidence,
        "evidence_start_hint": hint,
        "status": "validated",
        "created_at": _now(),
    }
    if readwise_col != "readwise_id":
        values["document_id"] = values.pop("readwise_id")
    usable = {k: v for k, v in values.items() if k in cols}
    names = list(usable)
    conn.execute(
        f'INSERT INTO relations ({",".join(names)}) VALUES ({",".join("?" for _ in names)})',
        tuple(usable[n] for n in names),
    )
    relation_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    _insert_relation_evidence(conn, relation_id, readwise_id, confidence, evidence, hint)
    return True


def _validate_item(item: dict, expected_rid: str) -> tuple[list[dict], list[dict]]:
    if not isinstance(item, dict) or str(item.get("readwise_id") or "") != expected_rid:
        raise ValueError("Entity/relation document id mismatch")
    raw_entities = item.get("entities") or []
    raw_relations = item.get("relations") or []
    if not isinstance(raw_entities, list) or not isinstance(raw_relations, list):
        raise ValueError("entities/relations must be lists")
    entities = []
    seen = set()
    for raw in raw_entities[:MAX_ENTITIES_PER_DOC]:
        if not isinstance(raw, dict):
            continue
        name = _validate_english_canonical(raw.get("canonical_name"), "canonical_name")
        if not name or _norm(name) in seen:
            continue
        seen.add(_norm(name))
        et = str(raw.get("entity_type") or "Other").strip()
        entities.append({
            "mention": str(raw.get("mention") or name).strip()[:300],
            "canonical_name": name[:300],
            "entity_type": et if et in ENTITY_TYPES else "Other",
        })
    relations = []
    for raw in raw_relations[:MAX_RELATIONS_PER_DOC]:
        if not isinstance(raw, dict):
            continue
        subject = str(raw.get("subject") or "").strip()
        object_ = str(raw.get("object") or "").strip()
        rel = _validate_english_canonical(raw.get("relation"), "relation")
        evidence = str(raw.get("evidence_quote") or "").strip()
        if subject and object_ and rel and evidence:
            relations.append({
                "subject": subject[:300],
                "object": object_[:300],
                "relation": rel[:160],
                "confidence": _safe_confidence(raw.get("confidence")),
                "evidence_quote": evidence[:1200],
                "evidence_start_hint": str(raw.get("evidence_start_hint") or "")[:500],
            })
    return entities, relations


def extract_and_persist(documents: list[dict], *, apply: bool, limit: int | None = None) -> dict:
    if not apply or not documents:
        return {"documents": 0, "api_batches": 0, "entities_created": 0, "document_entity_links": 0, "relations_created": 0, "processed_readwise_ids": []}

    target = documents[:limit] if limit is not None else documents
    target = [d for d in target if str(d.get("readwise_id") or "").strip()]
    if not target:
        return {"documents": 0, "api_batches": 0, "entities_created": 0, "document_entity_links": 0, "relations_created": 0, "processed_readwise_ids": []}

    batch_size = max(1, int(os.getenv("ENTITY_RELATION_BATCH_SIZE", str(DEFAULT_BATCH))))
    client = get_relation_client()
    created_entities = 0
    links = 0
    relations = 0
    api_batches = 0
    processed_ids = []
    failed_ids = []

    with __import__("abraham_sqlite").connect() as conn:
        _ensure_columns(conn)
        catalog = _load_catalog(conn)
        exact, catalog_by_id = _catalog_maps(catalog)

        for start in range(0, len(target), batch_size):
            batch = target[start:start + batch_size]
            try:
                data = _call(client, batch, catalog)
                api_batches += 1
            except Exception as exc:
                print(f"[ENTITY/RELATION BATCH FAILED] {type(exc).__name__}: {exc}")
                failed_ids.extend(str(d.get("readwise_id") or "") for d in batch)
                continue

            by_rid = {str(x.get("readwise_id") or ""): x for x in data["documents"] if isinstance(x, dict)}
            for document in batch:
                rid = str(document.get("readwise_id") or "")
                item = by_rid.get(rid)
                if not item:
                    failed_ids.append(rid)
                    continue
                try:
                    raw_entities, raw_relations = _validate_item(item, rid)
                except ValueError as exc:
                    print(f"[ENTITY/RELATION INVALID] {rid}: {exc}")
                    failed_ids.append(rid)
                    continue

                resolved = {}
                for raw_entity in raw_entities:
                    entity = _resolve_entity(
                        raw_entity["canonical_name"],
                        raw_entity["entity_type"],
                        exact,
                        catalog_by_id,
                    )
                    if entity["entity_id"] not in catalog_by_id:
                        _insert_entity(conn, entity)
                        catalog_by_id[entity["entity_id"]] = entity
                        exact[_norm(entity["canonical_name"])] = entity
                        created_entities += 1
                    resolved[_norm(raw_entity["canonical_name"])] = entity

                    try:
                        _insert_document_entity(
                            conn, rid, entity,
                            raw_entity["mention"],
                            1.0,
                            "",
                        )
                        links += 1
                    except Exception as exc:
                        print(f"[DOCUMENT ENTITY LINK FAILED] {rid}: {exc}")

                for raw_relation in raw_relations:
                    subject = resolved.get(_norm(raw_relation["subject"]))
                    object_ = resolved.get(_norm(raw_relation["object"]))
                    if subject is None:
                        subject = _resolve_entity(raw_relation["subject"], "Other", exact, catalog_by_id)
                    if object_ is None:
                        object_ = _resolve_entity(raw_relation["object"], "Other", exact, catalog_by_id)
                    for entity in (subject, object_):
                        if entity["entity_id"] not in catalog_by_id:
                            _insert_entity(conn, entity)
                            catalog_by_id[entity["entity_id"]] = entity
                            exact[_norm(entity["canonical_name"])] = entity
                            created_entities += 1
                    try:
                        _insert_document_entity(
                            conn, rid, subject, raw_relation["subject"], 1.0,
                            raw_relation["evidence_quote"],
                        )
                        _insert_document_entity(
                            conn, rid, object_, raw_relation["object"], 1.0,
                            raw_relation["evidence_quote"],
                        )
                        if _insert_relation(conn, rid, subject, raw_relation, object_):
                            relations += 1
                    except Exception as exc:
                        print(f"[RELATION INSERT FAILED] {rid}: {exc}")

                conn.commit()
                processed_ids.append(rid)

            print(
                f"[ENTITY/RELATION {start + len(batch)}/{len(target)}] "
                f"batches={api_batches} entities+={created_entities} relations+={relations}"
            )

    return {
        "documents": len(target),
        "api_batches": api_batches,
        "entities_created": created_entities,
        "document_entity_links": links,
        "relations_created": relations,
        "processed_readwise_ids": processed_ids,
        "failed_readwise_ids": sorted(set(failed_ids) - set(processed_ids)),
    }
