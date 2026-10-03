#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, os, re, sqlite3, unicodedata
from datetime import datetime, timezone
from pathlib import Path
from gemini_adapter import get_gemini_client

MIGRATION = "entity_consolidation_v1"
BATCH = 40
GENERIC_OTHER = {
    "article","company","companies","country","countries","government","industry","market","markets",
    "organization","organizations","person","people","place","product","products","technology","technologies",
    "website","websites","internet","world","research","study","studies","data","information","system","systems",
    "business","businesses","economy","economic","news","media","software","hardware","application","applications",
    "platform","platforms","service","services","project","projects","program","programs","model","models",
}

def now(): return datetime.now(timezone.utc).isoformat()

def norm(s):
    s = str(s or "").strip().casefold()
    s = re.sub(r"[\u200b\u200c\u200d]", "", s)
    s = re.sub(r"[\s\-_–—]+", " ", s)
    s = re.sub(r"[^\w\s&.+#]", "", s, flags=re.UNICODE)
    return s.strip()

def latin(s):
    t = str(s or "").strip()
    return bool(t) and all((not c.isalpha()) or "LATIN" in unicodedata.name(c, "") for c in t)

def ensure(c):
    c.executescript("""
    CREATE TABLE IF NOT EXISTS entity_consolidation_runs (
        migration_id TEXT PRIMARY KEY, completed_at TEXT NOT NULL,
        scanned INTEGER NOT NULL, merged INTEGER NOT NULL,
        rejected INTEGER NOT NULL, ai_names INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS entity_consolidation_audit (
        audit_id INTEGER PRIMARY KEY AUTOINCREMENT, migration_id TEXT NOT NULL,
        action TEXT NOT NULL, entity_id TEXT NOT NULL, old_value TEXT,
        new_value TEXT, survivor_id TEXT, created_at TEXT NOT NULL
    );
    """)
    c.commit()

def parse_json(text):
    s = (text or "").strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        dec = json.JSONDecoder()
        for i, ch in enumerate(s):
            if ch in "[{":
                try:
                    return dec.raw_decode(s[i:])[0]
                except json.JSONDecodeError:
                    continue
        raise

def ai_names(rows):
    if not rows: return {}
    client = get_gemini_client()
    out = {}
    for start in range(0, len(rows), BATCH):
        batch = rows[start:start+BATCH]
        prompt = """Return ONLY JSON.
Convert each entity canonical_name to ONE stable English/Latin canonical name.
Preserve identity; do not merge entities. Prefer official English names, otherwise
standard English romanization or natural English translation. Keep Latin diacritics.
Schema: {"entities":[{"entity_id":"...","canonical_name":"..."}]}
Entities:
""" + json.dumps(batch, ensure_ascii=False)
        for attempt in range(3):
            try:
                r = client.models.generate_content(
                    model=os.getenv("GEMINI_ENTITY_RELATION_MODEL", "gemini-3.8-flash"),
                    contents=prompt,
                    config={
                        "response_mime_type": "application/json",
                        "thinking": os.getenv("ABRAHAM_ENTITY_RELATION_THINKING", "low"),
                        "max_output_tokens": int(os.getenv("GEMINI_ENTITY_RELATION_MAX_TOKENS", "16384")),
                    },
                )
                data = parse_json(r.text)
                items = data.get("entities") or []
                got = {
                    str(x.get("entity_id")): str(x.get("canonical_name") or "").strip()
                    for x in items if isinstance(x, dict)
                }
                if not all(str(x["entity_id"]) in got and got[str(x["entity_id"])] and latin(got[str(x["entity_id"])]) for x in batch):
                    raise ValueError("incomplete or non-Latin AI response")
                out.update(got)
                break
            except Exception as exc:
                if attempt == 2:
                    raise RuntimeError(f"AI canonicalization batch failed at {start}: {exc}") from exc
        print(f"[ENTITY AI {min(start+BATCH, len(rows))}/{len(rows)}]")
    return out

def remap(c, old, new):
    if old == new: return
    rows = c.execute(
        "SELECT readwise_id,mention,confidence,evidence_quote,created_at "
        "FROM document_entities WHERE entity_id=?", (old,)
    ).fetchall()
    for rid, mention, conf, evidence, created in rows:
        if not c.execute(
            "SELECT 1 FROM document_entities WHERE readwise_id=? AND entity_id=?", (rid, new)
        ).fetchone():
            c.execute(
                "INSERT INTO document_entities(readwise_id,entity_id,mention,confidence,evidence_quote,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (rid, new, mention, conf, evidence, created or now()),
            )
    c.execute("DELETE FROM document_entities WHERE entity_id=?", (old,))
    c.execute("UPDATE relations SET subject_entity_id=? WHERE subject_entity_id=?", (new, old))
    c.execute("UPDATE relations SET object_entity_id=? WHERE object_entity_id=?", (new, old))
    c.execute("UPDATE entities SET status='merged',updated_at=? WHERE entity_id=?", (now(), old))

def merge_group(c, ids, byid, audit, reason):
    ids = sorted(set(ids), key=lambda eid: (byid[eid][3] or "", eid))
    survivor = ids[0]
    for old in ids[1:]:
        remap(c, old, survivor)
        audit.append((reason, old, byid[old][1], byid[survivor][1], survivor))
    return survivor

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    path = Path(args.db)
    if not path.is_file(): raise SystemExit(f"DB not found: {path}")
    c = sqlite3.connect(path)
    c.execute("PRAGMA foreign_keys=OFF")
    try:
        ensure(c)
        if not args.force and c.execute(
            "SELECT 1 FROM entity_consolidation_runs WHERE migration_id=?", (MIGRATION,)
        ).fetchone():
            print("ENTITY_CONSOLIDATION_ALREADY_COMPLETED")
            return

        rows = c.execute(
            "SELECT entity_id,canonical_name,entity_type,created_at,status FROM entities "
            "WHERE COALESCE(status,'active') NOT IN ('deleted','rejected','merged') "
            "ORDER BY COALESCE(created_at,''),entity_id"
        ).fetchall()
        byid = {r[0]: r for r in rows}
        audit, merged, rejected = [], 0, 0

        groups = {}
        for r in rows: groups.setdefault(norm(r[1]), []).append(r[0])
        for ids in groups.values():
            if len(ids) > 1:
                merge_group(c, ids, byid, audit, "duplicate_canonical")
                merged += len(set(ids)) - 1

        active = c.execute(
            "SELECT entity_id,canonical_name,entity_type,created_at,status FROM entities "
            "WHERE COALESCE(status,'active') NOT IN ('deleted','rejected','merged')"
        ).fetchall()
        byid = {r[0]: r for r in active}
        mentions = c.execute(
            "SELECT entity_id,mention FROM document_entities "
            "WHERE mention IS NOT NULL AND TRIM(mention)<>''"
        ).fetchall()
        alias_map = {}
        for eid, mention in mentions:
            k = norm(mention)
            if k: alias_map.setdefault(k, set()).add(eid)
        for ids in alias_map.values():
            ids = [x for x in ids if x in byid]
            if len(set(ids)) > 1:
                merge_group(c, ids, byid, audit, "duplicate_mention")
                merged += len(set(ids)) - 1

        active = c.execute(
            "SELECT entity_id,canonical_name,entity_type,created_at,status FROM entities "
            "WHERE COALESCE(status,'active') NOT IN ('deleted','rejected','merged')"
        ).fetchall()
        byid = {r[0]: r for r in active}
        nonlatin = [
            {"entity_id": r[0], "canonical_name": r[1], "entity_type": r[2]}
            for r in active if not latin(r[1])
        ]
        mapped = ai_names(nonlatin)

        for eid, new in mapped.items():
            old = byid[eid][1]
            if old != new:
                c.execute(
                    "UPDATE entities SET canonical_name=?,updated_at=? WHERE entity_id=?",
                    (new, now(), eid)
                )
                audit.append(("canonicalized", eid, old, new, eid))

        active = c.execute(
            "SELECT entity_id,canonical_name,entity_type,created_at,status FROM entities "
            "WHERE COALESCE(status,'active') NOT IN ('deleted','rejected','merged')"
        ).fetchall()
        byid = {r[0]: r for r in active}
        groups = {}
        for r in active: groups.setdefault(norm(r[1]), []).append(r[0])
        for ids in groups.values():
            if len(ids) > 1:
                merge_group(c, ids, byid, audit, "canonicalized_duplicate")
                merged += len(set(ids)) - 1

        generic_ids = c.execute(
            "SELECT e.entity_id,e.canonical_name FROM entities e "
            "LEFT JOIN (SELECT entity_id,COUNT(DISTINCT readwise_id) n FROM document_entities GROUP BY entity_id) d "
            "ON d.entity_id=e.entity_id "
            "WHERE COALESCE(e.status,'active')='active' AND e.entity_type='Other' AND COALESCE(d.n,0)<=1"
        ).fetchall()
        for eid, name in generic_ids:
            if norm(name) in GENERIC_OTHER or len(norm(name)) <= 2:
                c.execute(
                    "UPDATE entities SET status='rejected',updated_at=? WHERE entity_id=?",
                    (now(), eid)
                )
                c.execute(
                    "DELETE FROM relations WHERE subject_entity_id=? OR object_entity_id=?",
                    (eid, eid)
                )
                audit.append(("rejected_generic", eid, name, "", None))
                rejected += 1

        for action, eid, old, new, survivor in audit:
            c.execute(
                "INSERT INTO entity_consolidation_audit(migration_id,action,entity_id,old_value,new_value,survivor_id,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (MIGRATION, action, eid, old, new, survivor, now())
            )
        c.execute(
            "INSERT INTO entity_consolidation_runs VALUES(?,?,?,?,?)",
            (MIGRATION, now(), len(rows), merged, rejected, len(mapped))
        )
        c.commit()
        active_count = c.execute(
            "SELECT COUNT(*) FROM entities WHERE COALESCE(status,'active') NOT IN ('deleted','rejected','merged')"
        ).fetchone()[0]
        print("ENTITY_CONSOLIDATION_OK")
        print(f"scanned={len(rows)}")
        print(f"merged={merged}")
        print(f"rejected={rejected}")
        print(f"ai_names={len(mapped)}")
        print(f"active_entities={active_count}")
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()

if __name__ == "__main__":
    main()
