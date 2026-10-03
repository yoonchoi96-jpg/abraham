from __future__ import annotations



import argparse
import hashlib
import json
import os
import re
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from deepseek_adapter import get_deepseek_client
from gemini_adapter import get_gemini_client
from entity_classifier import classify_entity_batch
from entity_relation_ai import extract_and_persist

from abraham_sqlite import load_documents, load_documents_by_ids, connect as connect_db
from abraham_incremental import (
    ensure_schema,
    content_hash,
    select_pending_documents,
    mark_started,
    mark_completed,
    mark_failed,
)

from dotenv import load_dotenv
from abraham_storage import storage_status, run_storage_maintenance
from adaptive_batch import plan_adaptive_batches, batch_stats


VERSION = "5.6.1"
PROMPT_VERSION = "5.6.3-korean-adaptive-minimal"

load_dotenv()

MODEL = os.getenv(
    "MODEL",
    "deepseek-flash",
)

# Cloud-ready paths: the same code runs locally or on a Linux VM.
VAULT_PATH = Path(os.getenv(
    "ABRAHAM_VAULT_PATH",
    os.getenv("GITHUB_WORKSPACE", ".") + "/vault",
)).expanduser()
REPORT_DIR = Path(os.getenv(
    "ABRAHAM_REPORT_DIR",
    str(VAULT_PATH / "90_Dashboard" / "auto_reports"),
)).expanduser()
KOREAN_BATCH_SIZE = int(os.getenv("KOREAN_BATCH_SIZE", "80"))
KOREAN_WORKERS = max(1, int(os.getenv("ABRAHAM_TRANSLATION_WORKERS", "3")))
KOREAN_MAX_RETRIES = int(os.getenv("KOREAN_MAX_RETRIES", "3"))
ABRAHAM_SCHEDULE_KST = os.getenv(
    "ABRAHAM_SCHEDULE_KST",
    "00:00,09:00,17:00",
)
RUN_LOCK_PATH = REPORT_DIR / ".abraham_v5.lock"
LOG_DIR = Path(os.getenv("ABRAHAM_LOG_DIR", str(REPORT_DIR / "logs"))).expanduser()
BACKUP_ROOT = Path(os.getenv("ABRAHAM_BACKUP_PATH", str(REPORT_DIR / "backups"))).expanduser()


def stable_text_hash(*parts: object) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update(("" if part is None else str(part)).encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()


@contextmanager
def pipeline_lock():
    RUN_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(RUN_LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise RuntimeError(f"Another Abraham V5 run is active: {RUN_LOCK_PATH}")
    try:
        os.write(fd, str(os.getpid()).encode("utf-8"))
        os.close(fd)
        yield
    finally:
        try: RUN_LOCK_PATH.unlink()
        except FileNotFoundError: pass


ENTITY_DIRS = {
    "Person": VAULT_PATH / "20_Entities" / "People",
    "Company": VAULT_PATH / "20_Entities" / "Companies",
    "Organization": VAULT_PATH / "20_Entities" / "Organizations",
    "Country": VAULT_PATH / "20_Entities" / "Countries",
    "Place": VAULT_PATH / "20_Entities" / "Places",
    "Product": VAULT_PATH / "20_Entities" / "Products",
    "Technology": VAULT_PATH / "20_Entities" / "Technologies",
    "Concept": VAULT_PATH / "20_Entities" / "Concepts",
    "Event": VAULT_PATH / "20_Entities" / "Events",
    "Institution": VAULT_PATH / "20_Entities" / "Institutions",
    "Brand": VAULT_PATH / "20_Entities" / "Brands",
    "Other": VAULT_PATH / "20_Entities" / "Concepts",
}

DOCUMENT_INBOX = VAULT_PATH / "00_Inbox" / "Readwise"


def safe_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]', "-", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:180] or "Untitled"


def yaml_quote(value) -> str:
    if value is None:
        return '""'
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def get_resolved_entities(document: dict) -> list[dict]:
    entities = document.get("resolved_entities", [])
    if not isinstance(entities, list):
        return []
    return [
        x for x in entities
        if isinstance(x, dict)
        and x.get("canonical_name")
        and x.get("entity_id")
    ]


def get_validated_relations(relations) -> list[dict]:
    """Normalize SQLite relation output into validated relation records."""
    if isinstance(relations, dict):
        relations = relations.get("validated", [])
    if not isinstance(relations, list):
        return []
    return [
        x for x in relations
        if isinstance(x, dict)
        and isinstance(x.get("subject"), dict)
        and isinstance(x.get("object"), dict)
        and x.get("relation")
    ]


def get_korean_client():
    return get_deepseek_client()


def build_korean_prompt(documents):

    payload = []

    for index, document in enumerate(documents):

        payload.append({

            "index": index,
            "title": document.get(
                "title",
                "",
            ),
            "canonical_text": (
                document.get("canonical_text")
                or document.get("content")
                or ""
            ),
        })

    return f"""
You are the Korean presentation layer of a
long-term multilingual knowledge management system.

Translate the supplied English canonical documents
into accurate, natural Korean.

THIS IS A FAITHFUL TRANSLATION TASK.
THIS IS NOT SUMMARIZATION.

Rules:

1. Translate the COMPLETE canonical text.

2. Do not summarize.

3. Do not omit important sentences or details.

4. Do not add information that is not present.

5. Do not infer causes, motives, opinions,
   relationships, intentions, or conclusions.

6. Preserve uncertainty and attribution.

7. Preserve distinctions such as:
   may, might, could, reportedly, according to,
   proposed, alleged, expected, confirmed.

8. Preserve all numbers, dates, percentages,
   currencies, measurements, names, companies,
   organizations, products, places, events,
   and technical terms.

9. Use established Korean names for well-known
   entities when appropriate.

10. If no established Korean name exists,
    retain the original proper name.

11. Do not creatively rename entities.

12. Do not change factual scope.

13. Do not use outside knowledge.

14. Do not turn an attributed claim into a fact.

15. Do not turn uncertainty into certainty.

16. Produce readable Korean journalistic/professional prose.

17. Preserve paragraph structure reasonably.

18. Translate the title faithfully.

Return STRICT JSON only.

Schema:

{{
  "documents": [
    {{
      "index": 0,
      "korean_title": "...",
      "korean_text": "..."
    }}
  ]
}}

Documents:

{json.dumps(payload, ensure_ascii=False)}
"""



# ============================================================
# DEEPSEEK JSON OUTPUT / RETRY CONFIGURATION
# ============================================================

def _salvage_korean_documents(raw):
    """Recover complete document objects from a truncated JSON response."""
    if not isinstance(raw, str):
        return []
    start = raw.find('"documents"')
    if start < 0:
        return []
    array_start = raw.find("[", start)
    if array_start < 0:
        return []

    decoder = json.JSONDecoder()
    pos = array_start + 1
    recovered = []

    while pos < len(raw):
        while pos < len(raw) and raw[pos].isspace():
            pos += 1
        if pos >= len(raw) or raw[pos] == "]":
            break
        if raw[pos] == ",":
            pos += 1
            continue
        if raw[pos] != "{":
            next_object = raw.find("{", pos)
            if next_object < 0:
                break
            pos = next_object
        try:
            item, end = decoder.raw_decode(raw, pos)
        except json.JSONDecodeError:
            # The current object is incomplete. Everything before it that
            # decoded successfully is safe to keep; the caller retries only
            # documents not represented by those recovered indexes.
            break
        if isinstance(item, dict):
            recovered.append(item)
        pos = end

    return recovered


def translate_korean_batch(
    client,
    documents,
):
    prompt = build_korean_prompt(
        documents
    )

    for attempt in range(
        KOREAN_MAX_RETRIES
    ):
        try:

            response = client.models.generate_content(
                model=MODEL,
                contents=prompt,
                config={
                    "response_mime_type": "application/json",
                    "thinking": os.getenv("ABRAHAM_TRANSLATION_THINKING", "disabled"),
                    "max_output_tokens": int(os.getenv("ABRAHAM_TRANSLATION_MAX_TOKENS", "32768")),
                },
            )

            raw = response.text.strip()

            if raw.startswith("```"):
                raw = re.sub(
                    r"^```(?:json)?\s*",
                    "",
                    raw,
                )
                raw = re.sub(
                    r"\s*```$",
                    "",
                    raw,
                )

            try:
                result = json.loads(raw)
                output = result.get("documents", [])
            except json.JSONDecodeError as exc:
                output = _salvage_korean_documents(raw)
                if output:
                    print(
                        f"[KOREAN PARTIAL JSON] recovered "
                        f"{len(output)}/{len(documents)} complete documents "
                        f"before JSON truncation: {exc}"
                    )
                else:
                    raise

            if not isinstance(output, list):
                raise ValueError("documents must be a list")

            expected_indexes = set(range(len(documents)))
            actual_indexes = {
                x.get("index")
                for x in output
                if isinstance(x, dict)
            }

            unknown_indexes = actual_indexes - expected_indexes
            if unknown_indexes:
                raise ValueError(
                    "Korean translation indexes out of range"
                )

            missing_indexes = expected_indexes - actual_indexes
            if missing_indexes:
                print(
                    f"[KOREAN PARTIAL] response contains "
                    f"{len(output)}/{len(documents)} documents; "
                    f"missing indexes={sorted(missing_indexes)}"
                )

            for item in output:

                index = item.get("index")

                if not isinstance(index, int):
                    raise ValueError(
                        "Invalid translation index"
                    )

                if index < 0 or index >= len(documents):
                    raise ValueError(
                        "Translation index out of range"
                    )

                if not item.get(
                    "korean_title"
                ):
                    raise ValueError(
                        "Missing korean_title"
                    )

                if not item.get(
                    "korean_text"
                ):
                    raise ValueError(
                        "Missing korean_text"
                    )

                item["document_id"] = documents[index]["document_id"]

            return output

        except Exception as e:
            error_text = str(e)
            is_503 = (
                "503" in error_text
                or "UNAVAILABLE" in error_text
                or "high demand" in error_text.lower()
            )

            is_content_risk = (
                "Content Exists Risk" in error_text
                or "content_exists_risk" in error_text.lower()
            )

            is_429 = (
                "429" in error_text
                or "RESOURCE_EXHAUSTED" in error_text
            )

            if is_content_risk:
                # HTTP 400 content-risk rejection is not transient.
                # Do not spend retry/backoff time; the resilient caller
                # will isolate the offending document by splitting.
                print(
                    "[KOREAN CONTENT RISK] "
                    "DeepSeek rejected this request; skipping retries."
                )
                return []

            if is_503:
                if attempt < KOREAN_MAX_RETRIES - 1:
                    delay = min(
                        60,
                        5 * (2 ** attempt),
                    )

                    print(
                        f"[KOREAN RETRY {attempt + 1}/"
                        f"{KOREAN_MAX_RETRIES}] "
                        f"503 UNAVAILABLE"
                    )
                    print(
                        f"[KOREAN BACKOFF] "
                        f"sleep {delay}s"
                    )

                    time.sleep(delay)
                    continue

                print(
                    "[KOREAN FAILED] "
                    "503 persisted after all retries. "
                    "Skipping batch."
                )
                return []

            if is_429:
                # 429 can mean temporary RPM/rate limiting OR
                # a longer-lived quota exhaustion. Do not treat every
                # 429 as permanent. Retry with increasing backoff.
                if attempt < KOREAN_MAX_RETRIES - 1:
                    delay = min(
                        120,
                        10 * (2 ** attempt),
                    )

                    print(
                        f"[KOREAN RETRY {attempt + 1}/"
                        f"{KOREAN_MAX_RETRIES}] "
                        f"429 RESOURCE_EXHAUSTED"
                    )
                    print(
                        f"[KOREAN BACKOFF] "
                        f"sleep {delay}s"
                    )

                    time.sleep(delay)
                    continue

                print(
                    "[KOREAN FAILED] "
                    "429 persisted after all retries. "
                    "Skipping batch; successful cache "
                    "entries remain preserved."
                )
                return []

            if attempt < KOREAN_MAX_RETRIES - 1:
                delay = 2 * (attempt + 1)

                print(
                    f"[KOREAN RETRY {attempt + 1}/"
                    f"{KOREAN_MAX_RETRIES}] "
                    f"{type(e).__name__}: {e}"
                )
                print(
                    f"[KOREAN BACKOFF] "
                    f"sleep {delay}s"
                )

                time.sleep(delay)
                continue

            print(
                "[KOREAN FAILED] "
                f"{type(e).__name__}: {e}"
            )
            print(
                "[KOREAN FAILED] Skipping batch."
            )
            return []

    return []

RATING_COLORS = (
    "white", "red", "orange", "yellow", "green",
    "blue", "navy", "purple", "gold",
)

_COLOR_ALIASES = {
    "white": ("white", "⚪", "흰색", "흰"),
    "red": ("red", "🔴", "빨강", "빨간"),
    "orange": ("orange", "🟠", "주황", "오렌지"),
    "yellow": ("yellow", "🟡", "노랑"),
    "green": ("green", "🟢", "초록"),
    "blue": ("blue", "🔵", "파랑"),
    "navy": ("navy", "남색"),
    "purple": ("purple", "🟣", "보라"),
    "gold": ("gold", "🏆", "금색"),
}

def _reader_tag_names(document):
    tags = document.get("readwise_tags") or {}
    if isinstance(tags, dict):
        return [
            str(value.get("name") or key) if isinstance(value, dict) else str(key)
            for key, value in tags.items()
        ]
    if isinstance(tags, list):
        return [
            str(value.get("name") or "") if isinstance(value, dict) else str(value)
            for value in tags
        ]
    return []

def _reader_rating_color(document):
    candidates = []
    for index, name in enumerate(_reader_tag_names(document)):
        low = name.strip().lower()
        for color, aliases in _COLOR_ALIASES.items():
            if any(alias.lower() in low for alias in aliases):
                canonical = low.startswith("rating-") or (
                    color in {"red", "orange", "yellow", "green", "blue", "purple", "gold"}
                    and not low.startswith("등급/")
                )
                candidates.append((canonical, index, color))
                break

    if not candidates:
        return None

    canonical = [x for x in candidates if x[0]]
    pool = canonical or candidates
    colors = {x[2] for x in pool}

    # Conflicting rating tags are quarantined rather than guessed.
    if len(colors) != 1:
        return None

    return pool[-1][2]

def _processing_gate(document):
    color = _reader_rating_color(document)

    if color in {"white", "red", None}:
        return color, False, False

    if color in {"orange", "yellow", "green"}:
        return color, True, False

    if color in {"blue", "navy", "purple", "gold"}:
        return color, True, True

    return color, False, False

RATING_DIMENSIONS = ("long_term_knowledge_value","decision_relevance","information_density","uniqueness","knowledge_graph_value","evidence_quality","depth_and_insight","transferability","strategic_structural_significance","future_reference_value")

def _build_rating_prompt(documents):
    payload = [{"document_id":d["document_id"],"title":d.get("title",""),"canonical_text":d.get("canonical_text") or d.get("content") or ""} for d in documents]
    return "Rate each document using 0-100 scores in increments of 5 or 10. Use only supplied text. Return strict JSON ratings array containing document_id, all ten dimensions, and reason. Dimensions: " + ", ".join(RATING_DIMENSIONS) + ". Documents:\n" + json.dumps(payload, ensure_ascii=False)

def _rating_tier(average):
    return "B+" if average >= 80 else "B" if average >= 70 else "C" if average >= 60 else "D"

def _validate_rating(item, expected_id):
    if not isinstance(item,dict) or item.get("document_id") != expected_id: raise ValueError("Rating document_id mismatch")
    clean={"document_id":expected_id}
    for key in RATING_DIMENSIONS:
        value=item.get(key)
        if isinstance(value,bool) or not isinstance(value,(int,float)): raise ValueError("Invalid rating value: "+key)
        value=int(value)
        if value < 0 or value > 100 or value % 5: raise ValueError("Rating must be 0-100 in increments of 5: "+key)
        clean[key]=value
    clean["reason"]=str(item.get("reason") or "").strip()[:4000]
    if not clean["reason"]: raise ValueError("Missing rating reason")
    return clean

def _rate_batch(client, documents):
    response=client.models.generate_content(
        model=os.getenv("GEMINI_RATING_MODEL", "gemini-3.6-flash"),
        contents=_build_rating_prompt(documents),
        config={
            "response_mime_type": "application/json",
            "thinking": os.getenv("ABRAHAM_RATING_THINKING", "disabled"),
            "max_output_tokens": int(os.getenv("ABRAHAM_RATING_MAX_TOKENS", "16384")),
        },
    )
    data=json.loads(response.text.strip().strip("`").replace("json\n","",1))
    # DeepSeek may return either a bare JSON array or an object containing
    # a "ratings" array. Accept both response shapes.
    if isinstance(data,list):
        items=data
    elif isinstance(data,dict):
        items=data.get("ratings")
    else:
        items=None
    if not isinstance(items,list): raise ValueError("ratings must be a list")
    expected={d["document_id"] for d in documents}
    actual={x.get("document_id") for x in items if isinstance(x,dict)}
    if expected != actual: raise ValueError("Rating IDs mismatch")
    return [_validate_rating(x,x.get("document_id")) for x in items]

def _ensure_existing_rating_stages(conn, documents):
    now=datetime.utcnow().isoformat()
    for d in documents:
        rid=str(d.get("readwise_id") or "").strip()
        if not rid: continue
        exists=conn.execute("SELECT 1 FROM ratings WHERE readwise_id=? LIMIT 1",(rid,)).fetchone()
        stage=conn.execute("SELECT 1 FROM document_stages WHERE readwise_id=? AND stage='rating'",(rid,)).fetchone()
        if exists and not stage:
            h=d.get("_stage_content_hash") or stable_text_hash(d.get("content"),d.get("canonical_text"))
            conn.execute("INSERT INTO document_stages(readwise_id,stage,status,attempt_count,completed_at,content_hash,result_hash,updated_at) VALUES(?,'rating','completed',0,?,?,?,?)",(rid,now,h,stable_text_hash(rid,"existing-rating"),now))
    conn.commit()

def _persist_ratings(conn, documents, results):
    now=datetime.utcnow().isoformat()
    by_id={d["document_id"]:d for d in documents}
    for item in results:
        d=by_id[item["document_id"]]
        values=[item[k] for k in RATING_DIMENSIONS]
        total=sum(values); average=round(total/len(values),1); tier=_rating_tier(average)
        conn.execute("""INSERT INTO ratings(readwise_id,rated_at,rating_version,long_term_knowledge_value,decision_relevance,information_density,uniqueness,knowledge_graph_value,evidence_quality,depth_and_insight,transferability,strategic_structural_significance,future_reference_value,total_score,average,tier,global_rank,rank_in_sync,top_pick,reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,0,?) ON CONFLICT(readwise_id) DO UPDATE SET rated_at=excluded.rated_at,rating_version=excluded.rating_version,long_term_knowledge_value=excluded.long_term_knowledge_value,decision_relevance=excluded.decision_relevance,information_density=excluded.information_density,uniqueness=excluded.uniqueness,knowledge_graph_value=excluded.knowledge_graph_value,evidence_quality=excluded.evidence_quality,depth_and_insight=excluded.depth_and_insight,transferability=excluded.transferability,strategic_structural_significance=excluded.strategic_structural_significance,future_reference_value=excluded.future_reference_value,total_score=excluded.total_score,average=excluded.average,tier=excluded.tier,top_pick=excluded.top_pick,reason=excluded.reason""",(str(d["readwise_id"]),now,"v5.6-reader-gated",*values,total,average,tier,item["reason"]))
        item["_total_score"]=total; item["_average"]=average; item["_tier"]=tier
    conn.commit()

def _rate_batch_resilient(client, batch, stage_depth=0):
    """Rate a batch, recursively splitting when the batch response fails."""
    if not batch:
        return [], []

    try:
        rated = _rate_batch(client, batch)
        if stage_depth:
            print(
                f"[RATING SPLIT RECOVERED] {len(batch)} docs "
                f"at depth {stage_depth}"
            )
        return rated, []
    except Exception as exc:
        if len(batch) <= 1:
            print(
                "[RATING SPLIT FAILED] single-document batch still failed; "
                f"document_id={batch[0].get('document_id')}: {exc}"
            )
            return [], list(batch)

        midpoint = max(1, len(batch) // 2)
        left = batch[:midpoint]
        right = batch[midpoint:]
        print(
            f"[RATING SPLIT] batch of {len(batch)} failed; "
            f"retrying as {len(left)} + {len(right)}: {exc}"
        )
        left_results, left_failed = _rate_batch_resilient(
            client, left, stage_depth + 1
        )
        right_results, right_failed = _rate_batch_resilient(
            client, right, stage_depth + 1
        )
        return left_results + right_results, left_failed + right_failed


def _persist_rating_batch(documents, rated):
    """Persist rating table and stage state immediately after each success."""
    if not rated:
        return

    by_id = {d["document_id"]: d for d in documents}
    successful_documents = [
        by_id[item["document_id"]]
        for item in rated
        if item.get("document_id") in by_id
    ]
    if not successful_documents:
        return

    with connect_db() as conn:
        ensure_schema(conn)
        _persist_ratings(conn, successful_documents, rated)
        mark_completed(
            conn,
            successful_documents,
            "rating",
            {item["document_id"]: item for item in rated},
            result_fields=RATING_DIMENSIONS,
        )
    print(
        f"[RATING SQLITE COMMIT] {len(successful_documents)} docs "
        "persisted immediately"
    )


def rate_documents(documents,args):
    rateable=[d for d in documents if d.get("_rating_required")]
    if not rateable: return {},[],[]
    with connect_db() as conn:
        ensure_schema(conn); _ensure_existing_rating_stages(conn,rateable)
        pending,skipped=select_pending_documents(conn,rateable,"rating",force_ids=args.readwise_id)
        if args.limit is not None: pending=pending[:args.limit]
        if args.apply and pending: mark_started(conn,pending,"rating",worker_id="abraham-v5.6")
    if not pending: return {},pending,skipped
    if not args.apply: return {},pending,skipped

    client=get_gemini_client(); results={}
    for start in range(0,len(pending),KOREAN_BATCH_SIZE):
        batch=pending[start:start+KOREAN_BATCH_SIZE]
        rated, failed = _rate_batch_resilient(client,batch)
        if rated:
            _persist_rating_batch(batch,rated)
            for item in rated:
                results[item["document_id"]]=item
        if failed:
            print(
                f"[RATING FAILED] {len(failed)} document(s) remain pending "
                "after recursive splitting."
            )

    if len(results) < len(pending):
        failed_ids=set(d["document_id"] for d in pending) - set(results)
        failed_docs=[d for d in pending if d["document_id"] in failed_ids]
        error=RuntimeError(
            "Rating incomplete; "
            f"missing {len(failed_docs)} document(s)"
        )
        with connect_db() as conn:
            mark_failed(conn,failed_docs,"rating",error)
        raise error

    return results,pending,skipped

def _load_korean_stage_cache(documents):
    results = {}
    if not documents:
        return results
    ids = [
        str(d.get("readwise_id") or d.get("document_id") or "").strip()
        for d in documents
    ]
    ids = [x for x in ids if x]
    if not ids:
        return results
    with connect_db() as conn:
        ensure_schema(conn)
        placeholders = ",".join("?" for _ in ids)
        rows = conn.execute(
            f"""SELECT readwise_id, content_hash, result_json
                FROM document_stages
                WHERE stage='korean_translation'
                  AND status='completed'
                  AND readwise_id IN ({placeholders})""",
            tuple(ids),
        ).fetchall()
    by_rid = {str(d.get("readwise_id")): d for d in documents}
    for row in rows:
        rid = str(row["readwise_id"])
        document = by_rid.get(rid)
        if not document or not row["result_json"]:
            continue
        expected_hash = stable_text_hash(
            document.get("content", ""),
            document.get("canonical_text", ""),
        )
        if row["content_hash"] != expected_hash:
            continue
        try:
            item = json.loads(row["result_json"])
        except (TypeError, ValueError):
            continue
        if item.get("korean_title") and item.get("korean_text"):
            item["document_id"] = document["document_id"]
            item["readwise_id"] = rid
            results[document["document_id"]] = item
    return results


def _translate_korean_resilient(client, batch, stage_depth=0):
    """Translate a batch and retry only documents missing from a partial response.

    A large JSON response may be truncated after several complete document
    objects. Those valid objects are kept immediately; only missing documents
    are sent back to the model. Recursive half-splitting is the last-resort
    fallback for responses that contain no salvageable documents.
    """
    if not batch:
        return []

    translated = translate_korean_batch(client, batch)
    if translated:
        translated_ids = {item.get("document_id") for item in translated}
        missing = [
            d for d in batch
            if d.get("document_id") not in translated_ids
        ]
        if missing:
            print(
                f"[KOREAN PARTIAL] recovered {len(translated)}/{len(batch)} docs; "
                f"retrying only {len(missing)} missing docs"
            )
            recovered = _translate_korean_resilient(
                client, missing, stage_depth + 1
            )
            translated = translated + recovered

        if stage_depth:
            print(
                f"[KOREAN RECOVERED] {len(translated)}/{len(batch)} docs "
                f"at depth {stage_depth}"
            )
        return translated

    if len(batch) <= 1:
        print(
            "[KOREAN FAILED] single-document batch still failed; "
            f"document_id={batch[0].get('document_id')}"
        )
        return []

    midpoint = max(1, len(batch) // 2)
    left = batch[:midpoint]
    right = batch[midpoint:]
    print(
        f"[KOREAN SPLIT] no salvageable output from {len(batch)} docs; "
        f"retrying as {len(left)} + {len(right)}"
    )

    return (
        _translate_korean_resilient(client, left, stage_depth + 1)
        + _translate_korean_resilient(client, right, stage_depth + 1)
    )


def _persist_korean_results(documents, translated):
    """Persist a successfully translated batch immediately to SQLite."""
    if not translated:
        return

    by_id = {d["document_id"]: d for d in documents}
    successful_documents = [
        by_id[item["document_id"]]
        for item in translated
        if item.get("document_id") in by_id
    ]
    if not successful_documents:
        return

    result_by_id = {item["document_id"]: item for item in translated}
    with connect_db() as conn:
        ensure_schema(conn)
        mark_completed(
            conn,
            successful_documents,
            "korean_translation",
            result_by_id,
            result_fields=("korean_title", "korean_text"),
        )
    print(
        f"[KOREAN SQLITE COMMIT] {len(successful_documents)} docs "
        "persisted immediately"
    )


def translate_documents_to_korean(
    documents,
    limit=None,
    allow_ai=True,
):
    results = _load_korean_stage_cache(documents)
    pending = [
        d for d in documents
        if d["document_id"] not in results
    ]

    print("Korean SQLite cache :", len(results))
    print("Korean to translate:", len(pending))

    if limit is not None:
        pending = pending[:limit]
        print("Korean selected for DeepSeek:", len(pending))

    if not pending or not allow_ai:
        if not pending:
            print("Korean translation cache fully satisfied")
        else:
            print("Korean AI skipped (dry-run)")
        return results

    adaptive_target = int(os.getenv("ABRAHAM_KOREAN_TARGET_CHARS", "36000"))
    adaptive_min = int(os.getenv("ABRAHAM_KOREAN_MIN_BATCH", "4"))
    adaptive_max = int(os.getenv("ABRAHAM_KOREAN_MAX_BATCH", str(KOREAN_BATCH_SIZE)))
    batches = plan_adaptive_batches(
        pending,
        target_chars=adaptive_target,
        min_batch=adaptive_min,
        max_batch=adaptive_max,
    )
    print("Korean batches     :", len(batches))
    print("Korean workers     :", KOREAN_WORKERS)
    print("Korean target chars:", adaptive_target)
    for idx, planned_batch in enumerate(batches, 1):
        stats = batch_stats(planned_batch)
        print(
            f"[KOREAN PLAN {idx}/{len(batches)}] "
            f"{stats['documents']} docs, "
            f"estimated chars={stats['estimated_input_chars']}, "
            f"max-doc={stats['max_document_chars']}"
        )

    def process_batch(number, batch):
        # One client per worker avoids sharing mutable client state across threads.
        client = get_korean_client()
        translated = _translate_korean_resilient(client, batch)
        _persist_korean_results(batch, translated)
        return number, batch, translated

    # Translation batches are independent and persist atomically per batch.
    # Parallelism is configurable so production can be tuned without code edits.
    with ThreadPoolExecutor(
        max_workers=KOREAN_WORKERS,
        thread_name_prefix="abraham-korean",
    ) as pool:
        futures = [
            pool.submit(process_batch, number, batch)
            for number, batch in enumerate(batches, 1)
        ]
        for future in as_completed(futures):
            number, batch, translated = future.result()
            by_document_id = {d["document_id"]: d for d in batch}
            for item in translated:
                document = by_document_id.get(item["document_id"])
                if not document:
                    print(
                        f"[KOREAN WARN] translated document_id not in batch: "
                        f"{item.get('document_id')}"
                    )
                    continue
                rid = str(document["readwise_id"]).strip()
                canonical = document.get("canonical_text") or document.get("content") or ""
                cache_key = stable_text_hash(rid, canonical, PROMPT_VERSION, MODEL)
                results[item["document_id"]] = {
                    "document_id": item["document_id"],
                    "readwise_id": rid,
                    "canonical_text": canonical,
                    "korean_title": item["korean_title"],
                    "korean_text": item["korean_text"],
                    "translation_notes": [],
                    "prompt_version": PROMPT_VERSION,
                    "model": MODEL,
                    "cache_key": cache_key,
                }
            print(
                f"[KOREAN {number}/{len(batches)}] "
                f"batch complete ({len(translated)}/{len(batch)} docs)"
            )

    return results


def build_entity_registry(documents: list[dict]) -> dict[str, dict]:
    registry = {}

    for document in documents:
        for entity in get_resolved_entities(document):
            entity_id = entity["entity_id"]

            if entity_id not in registry:
                registry[entity_id] = {
                    "entity_id": entity_id,
                    "canonical_name": entity["canonical_name"],
                    "entity_type": entity.get("entity_type", "Other"),
                    "mentions": set(),
                    "document_ids": set(),
                }

            registry[entity_id]["mentions"].add(
                entity.get("mention", entity["canonical_name"])
            )

    for document in documents:
        document_id = document["document_id"]
        for entity in get_resolved_entities(document):
            entity_id = entity["entity_id"]
            if entity_id in registry:
                registry[entity_id]["document_ids"].add(document_id)

    return registry


def build_document_entity_links(documents: list[dict]) -> list[dict]:
    links = []

    for document in documents:
        document_id = document["document_id"]
        for entity in get_resolved_entities(document):
            links.append({
                "document_id": document_id,
                "entity_id": entity["entity_id"],
                "canonical_name": entity["canonical_name"],
                "entity_type": entity.get("entity_type", "Other"),
            })

    return links


def build_entity_relation_links(relations: list[dict]) -> list[dict]:
    links = []

    for relation in relations:
        subject = relation["subject"]
        obj = relation["object"]

        links.append({
            "document_id": relation.get("document_id"),
            "subject_entity_id": subject.get("entity_id"),
            "subject_name": subject.get("canonical_name"),
            "relation": relation.get("relation"),
            "object_entity_id": obj.get("entity_id"),
            "object_name": obj.get("canonical_name"),
            "confidence": relation.get("confidence"),
            "evidence_quote": relation.get("evidence_quote", ""),
        })

    return links


def sanitize_readwise_markdown_v3(text):
    """
    Clean malformed Markdown produced by Readwise export.

    Render-time only.
    The original Readwise source cache is never modified.
    """
    import re

    text = str(text or "")

    # ------------------------------------------------------------
    # 1. Broken Readwise links with duplicated URL structure.
    #
    # [TEXT]([URL](URL)) -> [TEXT](URL)
    # ------------------------------------------------------------
    text = re.sub(
        r'\[([^\]\n]+)\]\(\[(https?://[^\]\n]+)\]\((https?://[^)\n]+)\)\)',
        r'[\1](\2)',
        text,
    )

    # ------------------------------------------------------------
    # 2. [TEXT](URL "URL") -> [TEXT](URL)
    # ------------------------------------------------------------
    text = re.sub(
        r'(\[[^\]\n]+\])\((https?://[^\s)]+)\s+"https?://[^"]+"\)',
        r'\1(\2)',
        text,
    )

    # ------------------------------------------------------------
    # 3. Readwise broken article-link wrapper.
    #
    # [TEXT]([URL)\n\nNEXT]
    # Keep the readable text and discard the malformed URL wrapper.
    # ------------------------------------------------------------
    text = re.sub(
        r'\[([^\]\n]+)\]\(\[https?://[^\n]*\)\n\n',
        r'\1\n\n',
        text,
    )

    # ------------------------------------------------------------
    # 4. Broken image block.
    #
    # Actual Readwise pattern:
    # ![(Foto: Reuters)]\([URL1)##](URL2\)##)
    #
    # Keep only URL1 and rebuild valid Markdown image syntax.
    # ------------------------------------------------------------
    image_start = "![("
    image_end = r"\)##)"

    while True:
        start = text.find(image_start)
        if start == -1:
            break

        end = text.find(image_end, start)

        if end == -1:
            break

        block_end = end + len(image_end)
        block = text[start:block_end]

        url_start = block.find("https://")

        if url_start == -1:
            text = text[:start] + text[block_end:]
            continue

        url_end = block.find(")", url_start)

        if url_end == -1:
            text = text[:start] + text[block_end:]
            continue

        url = block[url_start:url_end]

        replacement = f"![Foto: Reuters]({url})"

        text = text[:start] + replacement + text[block_end:]

    # ------------------------------------------------------------
    # 5. Normalize repaired image alt text.
    # ![(Foto: Reuters)](URL) -> ![Foto: Reuters](URL)
    # ------------------------------------------------------------
    text = text.replace(
        "![(",
        "![",
    )

    # ------------------------------------------------------------
    # 6. Remove trailing ## accidentally attached to image/link.
    # ------------------------------------------------------------
    text = re.sub(
        r'(\]\(https?://[^)\n]+\))##+',
        r'\1',
        text,
    )

    # ------------------------------------------------------------
    # 6. YouTube artifact.
    # ------------------------------------------------------------
    text = re.sub(
        r'\byt:[A-Za-z0-9_-]+\b',
        '',
        text,
    )

    # ------------------------------------------------------------
    # 7. LEA TAMBIÉN formatting.
    # ------------------------------------------------------------
    text = re.sub(
        r'\*\s+\*\*LEA TAMBIÉN:\*\*',
        '**LEA TAMBIÉN:**',
        text,
        flags=re.IGNORECASE,
    )

    # ------------------------------------------------------------
    # 8. Excessive blank lines.
    # ------------------------------------------------------------
    text = re.sub(r'\n{3,}', '\n\n', text)

    return text.strip()

def render_document(
    document_id: int,
    document: dict,
    document_entity_links: list[dict],
    entity_relation_links: list[dict],
    korean=None,
) -> str:

    title = document.get(
        "title",
        "Untitled",
    )

    source_language = document.get(
        "source_language",
        "",
    )

    canonical_language = document.get(
        "canonical_language",
        "",
    )

    canonical_text = document.get(
        "canonical_text",
        "",
    )
    readwise_id = str(
        document.get("readwise_id", "")
    ).strip()

    original_text = document.get(
        "content",
        "",
    )

    original_text = sanitize_readwise_markdown_v3(
        original_text
    )

    korean_title = (
        korean.get("korean_title")
        if korean
        else None
    ) or title

    korean_text = (
        korean.get("korean_text")
        if korean
        else None
    ) or canonical_text

    entities = [
        x
        for x in document_entity_links
        if x["document_id"] == document_id
    ]

    relations = [
        x
        for x in entity_relation_links
        if x["document_id"] == document_id
    ]

    lines = [
        "---",
        f"title: {yaml_quote(korean_title)}",
        'type: "knowledge_document"',
        f"source_language: {yaml_quote(source_language)}",
        f"canonical_language: {yaml_quote(canonical_language)}",
        f"readwise_id: {yaml_quote(readwise_id)}",
        f"document_id: {document_id}",
        f'pipeline_version: "{VERSION}"',
        "---",
        "",
        f"# {korean_title}",
        "",
        "## 한국어 본문",
        "",
        korean_text.strip(),
        "",
        "---",
    ]

    # ------------------------------------------------------------
    # Entity tags
    # ------------------------------------------------------------

    seen_entities = set()
    entity_tags = []

    for entity in entities:
        key = entity["entity_id"]

        if key in seen_entities:
            continue

        seen_entities.add(key)

        name = entity["canonical_name"].strip()

        # Obsidian-safe hashtag:
        # remove whitespace and punctuation/symbols while preserving
        # Unicode letters and numbers.
        tag = re.sub(r"[^\w]", "", name, flags=re.UNICODE)

        if tag:
            entity_tags.append(f"#{tag}")

    lines.extend([
        "## Relations",
        "",
    ])

    if relations:

        for relation in relations:

            lines.append(
                f'- [[{relation["subject_name"]}]] '
                f'→ `{relation["relation"]}` → '
                f'[[{relation["object_name"]}]]'
            )

            evidence = relation.get(
                "evidence_quote",
                "",
            )

            if evidence:

                lines.append("")

                lines.append(
                    f'  - Evidence: "{evidence}"'
                )

            lines.append("")

    else:

        lines.extend([
            "- None",
            "",
        ])

    lines.extend([
        "## 원문",
        "",
        original_text.strip(),
        "",
        "## English Canonical",
        "",
        canonical_text.strip(),
        "",
    ])

    if entity_tags:
        lines.append(" ".join(entity_tags))
        lines.append("")
    return "\n".join(lines)


def render_entity(
    entity: dict,
    outgoing: list[dict],
    incoming: list[dict],
) -> str:

    name = entity["canonical_name"]
    entity_type = entity.get("entity_type", "Other")
    entity_id = entity["entity_id"]

    lines = [
        "---",
        f"title: {yaml_quote(name)}",
        f'type: {yaml_quote(entity_type)}',
        f'entity_id: {yaml_quote(entity_id)}',
        f'pipeline_version: "{VERSION}"',
        "---",
        "",
        "## Outgoing Relations",
        "",
    ]

    if outgoing:
        for relation in outgoing:
            lines.append(
                f'- `{relation["relation"]}` → '
                f'[[{relation["object_name"]}]]'
            )
            evidence = relation.get("evidence_quote", "")
            if evidence:
                lines.append("")
                lines.append(f'  - Evidence: "{evidence}"')
            lines.append("")
    else:
        lines.extend([
            "- None",
            "",
        ])

    lines.extend([
        "## Incoming Relations",
        "",
    ])

    if incoming:
        for relation in incoming:
            lines.append(
                f'- [[{relation["subject_name"]}]] → '
                f'`{relation["relation"]}`'
            )
            evidence = relation.get("evidence_quote", "")
            if evidence:
                lines.append("")
                lines.append(f'  - Evidence: "{evidence}"')
            lines.append("")
    else:
        lines.extend([
            "- None",
            "",
        ])

    return "\n".join(lines)



# V5 source-of-truth boundary: document content and output identity come from SQLite stage state.
# No recursive vault scan is used. The document_render stage persists its last output path.
def _stage_output_path(document: dict) -> Path | None:
    rid = str(document.get("readwise_id") or document.get("document_id") or "").strip()
    if not rid:
        return None
    try:
        with connect_db() as conn:
            ensure_schema(conn)
            row = conn.execute(
                """SELECT result_json FROM document_stages
                   WHERE readwise_id=? AND stage='document_render' AND status='completed'""",
                (rid,),
            ).fetchone()
        if not row or not row["result_json"]:
            return None
        data = json.loads(row["result_json"])
        raw = data.get("output_path")
        if not raw:
            return None
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = VAULT_PATH / candidate
        return candidate
    except (OSError, ValueError, TypeError):
        return None


def find_existing_document_path(document: dict) -> Path | None:
    # 1. Persisted V5 path from SQLite stage metadata.
    candidate = _stage_output_path(document)
    if candidate is not None:
        # The REST writer targets the runner's connected Obsidian instance;
        # its vault is not the same filesystem as the GitHub checkout.  The
        # persisted stage path is therefore an identity hint even when the
        # local Path does not exist.
        if candidate.exists() or _obsidian_api_enabled():
            return candidate

    # 2. Explicit source path, when it is already valid in the current runtime.
    original_path = str(document.get("path") or "").strip()
    if original_path:
        candidate = Path(original_path)
        if candidate.exists():
            return candidate

    return None

def entity_output_path(entity: dict) -> Path:
    entity_type = entity.get("entity_type", "Other")
    directory = ENTITY_DIRS.get(
        entity_type,
        VAULT_PATH / "20_Entities" / "Concepts",
    )
    return directory / f'{safe_filename(entity["canonical_name"])}.md'


def backup_file(path: Path, backup_root: Path) -> Path:
    relative = path.relative_to(VAULT_PATH)
    target = backup_root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, target)
    return target


def _json_safe(value):
    """Convert non-JSON-native containers into JSON-safe values."""
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, set):
        return sorted(_json_safe(v) for v in value)
    return value


def atomic_write_text(path: Path, text: str) -> None:
    """Write a UTF-8 text file atomically within the same directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _obsidian_api_enabled() -> bool:
    return bool(os.getenv("OBSIDIAN_API_URL", "").strip())


def _obsidian_relative_path(path: str | Path) -> str:
    raw = str(path).replace("\\", "/").strip()
    if not raw:
        raise ValueError("Obsidian path is empty")
    vault_raw = str(VAULT_PATH).replace("\\", "/").rstrip("/")
    if raw == vault_raw:
        return ""
    prefix = vault_raw + "/"
    if raw.startswith(prefix):
        return raw[len(prefix):]
    marker = "/vault/"
    if marker in raw:
        return raw.split(marker, 1)[1].lstrip("/")
    return raw.lstrip("/")


class ObsidianRestWriter:
    def __init__(self):
        self.base_url = os.getenv("OBSIDIAN_API_URL", "").rstrip("/")
        self.api_key = os.getenv("OBSIDIAN_API_KEY", "")
        if not self.base_url:
            raise RuntimeError("OBSIDIAN_API_URL is required for REST writer")
        if not self.api_key:
            raise RuntimeError("OBSIDIAN_API_KEY is required for REST writer")
        self.timeout = float(os.getenv("OBSIDIAN_API_TIMEOUT", "60"))

    def _url(self, relative_path: str) -> str:
        encoded = "/".join(
            urllib.parse.quote(part, safe="")
            for part in relative_path.replace("\\", "/").split("/")
            if part
        )
        return f"{self.base_url}/vault/{encoded}"

    def request(self, method: str, relative_path: str, body: bytes | None = None):
        request = urllib.request.Request(
            self._url(relative_path),
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "text/markdown; charset=utf-8",
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout, context=__import__("ssl")._create_unverified_context()) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return 404, exc.read()
            raise RuntimeError(f"Obsidian REST API {method} {relative_path} failed: HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Obsidian REST API unavailable: {exc}") from exc

    def health(self):
        request = urllib.request.Request(
            self.base_url + "/",
            headers={"Authorization": f"Bearer {self.api_key}"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout, context=__import__("ssl")._create_unverified_context()) as response:
                if response.status != 200:
                    raise RuntimeError(f"Obsidian REST API health check failed: HTTP {response.status}")
                return json.loads(response.read().decode("utf-8") or "{}")
        except Exception as exc:
            raise RuntimeError(f"Obsidian REST API health check failed: {exc}") from exc

    def read(self, relative_path: str) -> str | None:
        status, body = self.request("GET", relative_path)
        if status == 404:
            return None
        return body.decode("utf-8")

    def write(self, relative_path: str, content: str):
        self.request("PUT", relative_path, content.encode("utf-8"))

    def delete(self, relative_path: str):
        self.request("DELETE", relative_path)

def apply_files(
    document_previews: list[dict],
    entity_previews: list[dict],
) -> dict:
    if _obsidian_api_enabled():
        return apply_files_via_obsidian_rest(document_previews, entity_previews)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_root = BACKUP_ROOT / f"v5_backup_{timestamp}"
    written_documents = 0
    written_entities = 0
    backed_up = 0
    renamed_documents = 0

    document_operations = []
    for preview in document_previews:
        current_path = preview.get("current_path")
        target_path = preview.get("target_path")
        if current_path:
            current = Path(current_path)
            if not current.exists():
                raise RuntimeError(f"Current document disappeared: {current}")
            target = Path(target_path) if target_path else current
            if target.resolve() != current.resolve() and target.exists():
                raise RuntimeError(f"Filename target collision:\\nCURRENT: {current}\\nTARGET : {target}")
        else:
            if not target_path:
                raise RuntimeError(f"New document has no target path: {preview.get('title', 'Untitled')}")
            current = None
            target = Path(target_path)
            if target.exists():
                raise RuntimeError(f"New document target already exists:\\nTARGET: {target}")
        document_operations.append((preview, current, target))

    target_paths = [target.resolve() for _, _, target in document_operations]
    if len(target_paths) != len(set(target_paths)):
        raise RuntimeError("Duplicate document target filenames detected.")

    for _, current, _ in document_operations:
        if current is None:
            continue
        backup_file(current, backup_root)
        backed_up += 1

    for preview, current, target in document_operations:
        if current is None:
            target.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(target, preview["markdown"])
        else:
            atomic_write_text(current, preview["markdown"])
            if target.resolve() != current.resolve():
                current.rename(target)
                renamed_documents += 1
        written_documents += 1

    for preview in entity_previews:
        path = Path(preview["path"])
        if path.exists():
            backup_file(path, backup_root)
            backed_up += 1
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, preview["markdown"])
        written_entities += 1

    return {
        "backend": "filesystem",
        "backup_root": str(backup_root),
        "backed_up": backed_up,
        "written_documents": written_documents,
        "renamed_documents": renamed_documents,
        "written_entities": written_entities,
    }


def apply_files_via_obsidian_rest(
    document_previews: list[dict],
    entity_previews: list[dict],
) -> dict:
    writer = ObsidianRestWriter()
    health = writer.health()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_root = BACKUP_ROOT / f"v5_backup_{timestamp}"

    operations = []
    for preview in document_previews:
        current_raw = preview.get("current_path")
        target_raw = preview.get("target_path")
        if not target_raw:
            raise RuntimeError(f"Document has no target path: {preview.get('title', 'Untitled')}")
        target = _obsidian_relative_path(target_raw)
        current = _obsidian_relative_path(current_raw) if current_raw else None
        if current and current == target:
            current = target
        operations.append((preview, current, target))

    target_paths = [x[2] for x in operations]
    if len(target_paths) != len(set(target_paths)):
        raise RuntimeError("Duplicate document target filenames detected.")

    def _same_obsidian_document(existing_content: str, preview: dict) -> bool:
        """Identify an existing remote note belonging to this Readwise document."""
        document = preview.get("document") or {}
        candidates = {
            "readwise_id": str(document.get("readwise_id") or "").strip(),
            "document_id": str(preview.get("document_id") or document.get("document_id") or "").strip(),
        }
        for key, value in candidates.items():
            if not value:
                continue
            pattern = rf"""(?m)^{re.escape(key)}:\s*["']?{re.escape(value)}["']?\s*$"""
            if re.search(pattern, existing_content):
                return True
        return False

    # Preflight remote state. The Mac runner does not share the Obsidian
    # vault filesystem, so local Path.exists() cannot detect remote notes.
    # If the target already exists and is the same Readwise document, treat
    # it as an update rather than a fatal "new document" collision. If it is
    # a different document, move the new note to the next available filename.
    remote_existing = {}
    resolved_operations = []
    reserved_targets = set()
    for preview, current, target in operations:
        if current:
            content = writer.read(current)
            if content is None:
                raise RuntimeError(f"Current document not found in Obsidian: {current}")
            remote_existing[current] = content
            if current != target and writer.read(target) is not None:
                raise RuntimeError(f"Filename target collision in Obsidian: {target}")
            resolved_operations.append((preview, current, target))
            reserved_targets.add(target)
            continue

        existing = writer.read(target)
        if existing is None:
            resolved_operations.append((preview, None, target))
            reserved_targets.add(target)
            continue

        if _same_obsidian_document(existing, preview):
            remote_existing[target] = existing
            resolved_operations.append((preview, target, target))
            reserved_targets.add(target)
            print(f"[OBSIDIAN UPDATE] Existing note belongs to document_id={preview.get('document_id')}; updating {target}")
            continue

        base = Path(target)
        n = 2
        while True:
            candidate = _obsidian_relative_path(str(base.with_name(f"{base.stem} ({n}){base.suffix}")))
            if candidate not in reserved_targets and writer.read(candidate) is None:
                break
            n += 1
        resolved_operations.append((preview, None, candidate))
        reserved_targets.add(candidate)
        print(f"[OBSIDIAN COLLISION] Existing different note at {target}; using {candidate}")

    operations = resolved_operations

    entity_operations = []
    for preview in entity_previews:
        path = _obsidian_relative_path(preview["path"])
        old = writer.read(path)
        entity_operations.append((preview, path, old))

    backup_root.mkdir(parents=True, exist_ok=True)
    backed_up = 0
    for relative, content in remote_existing.items():
        backup_path = backup_root / relative
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(backup_path, content)
        backed_up += 1
    for _, path, old in entity_operations:
        if old is not None:
            backup_path = backup_root / path
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(backup_path, old)
            backed_up += 1

    written_documents = 0
    renamed_documents = 0
    for preview, current, target in operations:
        writer.write(target, preview["markdown"])
        if current and current != target:
            writer.delete(current)
            renamed_documents += 1
        written_documents += 1

    written_entities = 0
    for preview, path, _ in entity_operations:
        writer.write(path, preview["markdown"])
        written_entities += 1

    return {
        "backend": "obsidian_rest_api",
        "api_health": health,
        "backup_root": str(backup_root),
        "backed_up": backed_up,
        "written_documents": written_documents,
        "renamed_documents": renamed_documents,
        "written_entities": written_entities,
    }


def load_production_snapshot(readwise_ids=None):
    """Load the production snapshot directly from SQLite.

    No V4 report is required. SQLite owns document and relation state.
    """
    if readwise_ids:
        return load_documents_by_ids(readwise_ids)
    return load_documents(limit=None)


def normalize_document_ids(documents):
    """Preserve IDs from SQLite; never renumber documents per run."""
    seen = set()
    for document in documents:
        document_id = (
            document.get("document_id")
            or document.get("id")
            or document.get("readwise_id")
        )
        if document_id is None:
            raise RuntimeError(
                f"Document has no stable ID: {document.get('title', '(no title)')}"
            )
        if document_id in seen:
            raise RuntimeError(f"Duplicate document ID in SQLite snapshot: {document_id}")
        seen.add(document_id)
        document["document_id"] = document_id
    return documents


def prepare_source_content(documents):
    """Use SQLite content as the canonical source; skip unavailable documents safely."""
    available = []
    unavailable = []

    for document in documents:
        original = (document.get("content") or "").strip()

        if not original:
            document["readwise_original"] = ""
            document["readwise_source_status"] = "content_unavailable"
            document["content"] = ""
            unavailable.append(document)
            continue

        document["readwise_original"] = original
        document["readwise_source_status"] = "resolved"
        document["content"] = original
        available.append(document)

    print()
    print("=" * 70)
    print("SOURCE CONTENT")
    print("=" * 70)
    print("Total documents       :", len(documents))
    print("Content available     :", len(available))
    print("Content unavailable   :", len(unavailable))
    print()

    if unavailable:
        print("Content-unavailable documents are excluded from AI processing.")

    return available, unavailable



def classify_entities(registry, args):
    if not registry or not args.apply:
        return {}, [], []

    entities = list(registry.values())
    reclassify_all = os.getenv("ABRAHAM_RECLASSIFY_ENTITIES", "0") == "1"
    pending = [
        e for e in entities
        if reclassify_all or str(e.get("entity_type") or "Other") == "Other"
    ]
    if not pending:
        return {}, [], entities

    client = get_deepseek_client()
    batch_size = int(os.getenv("ENTITY_CLASSIFICATION_BATCH_SIZE", "50"))
    results = {}
    for start in range(0, len(pending), batch_size):
        batch = pending[start:start + batch_size]
        results.update(classify_entity_batch(client, batch))

    with connect_db() as conn:
        columns = {str(r["name"]) for r in conn.execute('PRAGMA table_info("entities")').fetchall()}
        if "entity_type" not in columns:
            raise RuntimeError("entities.entity_type column is required")
        for entity_id, entity_type in results.items():
            conn.execute(
                "UPDATE entities SET entity_type=? WHERE entity_id=?",
                (entity_type, entity_id),
            )
        conn.commit()

    for entity in entities:
        if entity["entity_id"] in results:
            entity["entity_type"] = results[entity["entity_id"]]

    return results, pending, [e for e in entities if e["entity_id"] not in results]


def _entity_render_inputs(entity, entity_relation_links):
    entity_id = entity["entity_id"]
    outgoing = [x for x in entity_relation_links if x.get("subject_entity_id") == entity_id]
    incoming = [x for x in entity_relation_links if x.get("object_entity_id") == entity_id]
    outgoing.sort(key=lambda x: (str(x.get("relation", "")), str(x.get("object_entity_id", ""))))
    incoming.sort(key=lambda x: (str(x.get("relation", "")), str(x.get("subject_entity_id", ""))))
    return outgoing, incoming


def run_pipeline(args):
    loaded = load_production_snapshot(args.readwise_id)
    if not isinstance(loaded, tuple) or len(loaded) != 2:
        raise RuntimeError("SQLite loader must return (documents, relations).")

    documents, sqlite_relations = loaded
    documents = normalize_document_ids(documents)
    documents, unavailable_documents = prepare_source_content(documents)
    sqlite_documents_total = len(documents)

    # Reader tags are the source of truth for V5 processing/export.
    gated_documents = []
    gate_counts = {color: 0 for color in RATING_COLORS}
    gate_counts["unclassified_or_conflicting"] = 0

    for document in documents:
        color, exportable, rateable = _processing_gate(document)
        document["_reader_rating_color"] = color
        document["_obsidian_export"] = exportable
        document["_rating_required"] = rateable

        if color in gate_counts:
            gate_counts[color] += 1
        else:
            gate_counts["unclassified_or_conflicting"] += 1

        if exportable:
            gated_documents.append(document)

    print("Reader color gate    :", gate_counts)
    print("Obsidian eligible    :", len(gated_documents), "/", len(documents))

    # White/Red/unclassified never enter translation/render.
    documents = gated_documents

    # ------------------------------------------------------------
    # 1-2. Independent AI stages.
    # Rating and Korean translation have no dependency on each other,
    # so production can run them concurrently. Each worker owns its
    # own model client and SQLite connection; persistence remains per batch.
    # ------------------------------------------------------------
    korean_stage = "korean_translation"
    with connect_db() as stage_conn:
        ensure_schema(stage_conn)
        korean_pending, korean_skipped = select_pending_documents(
            stage_conn,
            documents,
            korean_stage,
            force_ids=args.readwise_id,
        )
        if args.limit is not None:
            korean_pending = korean_pending[:args.limit]

        print("=" * 70)
        print("V5.6 INCREMENTAL SELECTION")
        print("=" * 70)
        print("SQLite documents       :", len(documents))
        print("Korean pending/changed :", len(korean_pending))
        print("Korean skipped         :", len(korean_skipped))
        print("Stage                  :", korean_stage)
        print()

        if args.apply and korean_pending:
            mark_started(
                stage_conn,
                korean_pending,
                korean_stage,
                worker_id="abraham-v5.6-korean",
            )

    parallel_ai = (
        args.apply
        and os.getenv("ABRAHAM_PARALLEL_AI", "1") == "1"
        and args.readwise_id is None
    )
    if parallel_ai:
        workers = max(2, int(os.getenv("ABRAHAM_AI_MAX_WORKERS", "4")))
        print(f"[AI PARALLEL] enabled workers={workers} rating+translation")
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="abraham-ai") as pool:
            rating_future = pool.submit(rate_documents, documents, args)
            korean_future = pool.submit(
                translate_documents_to_korean,
                korean_pending,
                None,
                True,
            )
            rating_results, rating_pending, rating_skipped = rating_future.result()
            korean_results = korean_future.result()
    else:
        rating_results, rating_pending, rating_skipped = rate_documents(documents, args)
        korean_results = translate_documents_to_korean(
            korean_pending,
            limit=None,
            allow_ai=args.apply,
        )

    print("Rating required       :", sum(1 for d in documents if d.get("_rating_required")))
    print("Rating AI pending     :", len(rating_pending))
    print("Rating skipped        :", len(rating_skipped))

    # Existing cached translations are needed for render-stage evaluation.
    # This is a SQLite-only cache read and never invokes the AI.
    all_korean_results = translate_documents_to_korean(
        documents,
        limit=0,
        allow_ai=False,
    )
    all_korean_results.update(korean_results)

    if args.apply and korean_pending:
        translated_ids = set(korean_results)
        pending_ids = {d["document_id"] for d in korean_pending}
        missing_ids = pending_ids - translated_ids
        if missing_ids:
            failed_docs = [
                d for d in korean_pending
                if d["document_id"] in missing_ids
            ]
            error = RuntimeError(
                "Korean translation incomplete; "
                f"missing {len(missing_ids)} document(s)"
            )
            with connect_db() as stage_conn:
                mark_failed(
                    stage_conn,
                    failed_docs,
                    korean_stage,
                    error,
                )
            print(
                "[KOREAN PARTIAL RUN] "
                f"{len(missing_ids)} document(s) failed translation; "
                "keeping them pending for the next run."
            )

            # Do not abort the entire pipeline because a small number of
            # documents produced malformed model output. Successful
            # translations continue through entity/render/V6-V9 stages.
            all_korean_results = translate_documents_to_korean(
                documents,
                limit=0,
                allow_ai=False,
            )
            all_korean_results.update(korean_results)

    # Render only documents with a completed Korean translation. Failed
    # translation items remain in SQLite as failed and are retried later.

    # ------------------------------------------------------------
    # 3. Build / grow the knowledge graph.
    # ------------------------------------------------------------
    # Entity/relation extraction is an explicit incremental stage. During
    # the sample-building phase it uses DeepSeek in batches; once a document
    # has been successfully processed its content hash prevents another API
    # call unless the source changes or the stage is forced.
    graph_stage = "entity_relation_extraction"
    graph_pending = []
    graph_skipped = []
    with connect_db() as graph_conn:
        ensure_schema(graph_conn)
        graph_pending, graph_skipped = select_pending_documents(
            graph_conn,
            documents,
            graph_stage,
            force_ids=args.readwise_id,
        )
        if args.limit is not None:
            graph_pending = graph_pending[:args.limit]
        if args.apply and graph_pending:
            mark_started(
                graph_conn,
                graph_pending,
                graph_stage,
                worker_id="abraham-v5.6-graph",
            )

    graph_stats = {
        "documents": 0,
        "api_batches": 0,
        "entities_created": 0,
        "document_entity_links": 0,
        "relations_created": 0,
        "processed_readwise_ids": [],
    }
    if graph_pending:
        graph_stats = extract_and_persist(
            graph_pending,
            apply=args.apply,
        )
        processed_ids = [
            str(x) for x in graph_stats.get("processed_readwise_ids", [])
        ]
        processed_docs = [
            d for d in graph_pending
            if str(d.get("readwise_id") or "") in set(processed_ids)
        ]
        if args.apply and processed_docs:
            with connect_db() as graph_conn:
                ensure_schema(graph_conn)
                mark_completed(
                    graph_conn,
                    processed_docs,
                    graph_stage,
                    {
                        d["document_id"]: {
                            "graph_stats": {
                                "entities_created": graph_stats.get("entities_created", 0),
                                "relations_created": graph_stats.get("relations_created", 0),
                            }
                        }
                        for d in processed_docs
                    },
                    result_fields=("graph_stats",),
                )

        # Re-read the just-persisted graph so render sees newly created
        # entities and relations, including canonical names resolved from DB.
        refreshed, refreshed_relations = load_documents_by_ids(
            [str(d.get("readwise_id") or "") for d in graph_pending]
        )
        refreshed_by_rid = {
            str(d.get("readwise_id") or ""): d
            for d in refreshed
        }
        for document in documents:
            rid = str(document.get("readwise_id") or "")
            if rid in refreshed_by_rid:
                document["resolved_entities"] = refreshed_by_rid[rid].get(
                    "resolved_entities", []
                )
        if refreshed_relations:
            sqlite_relations = refreshed_relations

    print()
    print("=" * 70)
    print("KNOWLEDGE GRAPH EXTRACTION")
    print("=" * 70)
    print("Graph pending/skipped   :", len(graph_pending), "/", len(graph_skipped))
    print("Graph API batches       :", graph_stats.get("api_batches", 0))
    print("Entities created        :", graph_stats.get("entities_created", 0))
    print("Document-entity links   :", graph_stats.get("document_entity_links", 0))
    print("Relations created       :", graph_stats.get("relations_created", 0))

    relations = get_validated_relations(sqlite_relations)
    registry = build_entity_registry(documents)
    registry = build_entity_registry(documents)

    entity_classification_results, entity_classification_pending, entity_classification_skipped = classify_entities(registry, args)
    if entity_classification_results:
        for document in documents:
            for entity in document.get("resolved_entities", []):
                entity_id = str(entity.get("entity_id") or "")
                if entity_id in entity_classification_results:
                    entity["entity_type"] = entity_classification_results[entity_id]

    document_entity_links = build_document_entity_links(documents)
    entity_relation_links = build_entity_relation_links(relations)

    print()
    print("=" * 70)
    print("KNOWLEDGE LINKING")
    print("=" * 70)
    print("Document → Entity links :", len(document_entity_links))
    print("Entity → Entity links   :", len(entity_relation_links))

    # ------------------------------------------------------------
    # 4. Document render stage.
    # Hash the complete render inputs, not merely source content.
    # This catches relation/entity changes even when the article itself
    # did not change.
    # ------------------------------------------------------------
    document_candidates = []
    for document in documents:
        did = document["document_id"]
        if did not in all_korean_results:
            print(
                f"[RENDER SKIP] Korean translation incomplete; "
                f"document_id={did} will be retried on the next run."
            )
            continue
        korean = all_korean_results[did]
        document_id_links = [x for x in document_entity_links if x["document_id"] == did]
        document_relation_links = [x for x in entity_relation_links if x.get("document_id") == did]
        render_hash = stable_text_hash(
            document.get("content", ""),
            document.get("canonical_text", ""),
            json.dumps(korean, ensure_ascii=False, sort_keys=True),
            json.dumps(document_id_links, ensure_ascii=False, sort_keys=True),
            json.dumps(document_relation_links, ensure_ascii=False, sort_keys=True),
            VERSION,
        )
        document["_render_hash"] = render_hash
        document["_stage_content_hash"] = render_hash
        document_candidates.append(document)

    render_stage = "document_render"
    with connect_db() as stage_conn:
        ensure_schema(stage_conn)
        render_pending, render_skipped = select_pending_documents(
            stage_conn,
            document_candidates,
            render_stage,
            force_ids=args.readwise_id,
        )
        if args.limit is not None:
            # Limit the render workload to the same user-selected working set.
            allowed = {d["document_id"] for d in korean_pending}
            if korean_pending:
                render_pending = [d for d in render_pending if d["document_id"] in allowed]

        if args.apply and render_pending:
            mark_started(stage_conn, render_pending, render_stage, worker_id="abraham-v5.6")

        document_previews = []
        try:
            for document in render_pending:
                document_id = document["document_id"]
                korean = all_korean_results.get(document_id, {})
                korean_title = korean.get("korean_title") or document.get("title") or "Untitled"
                document["korean_title"] = korean_title

                markdown = render_document(
                    document_id,
                    document,
                    document_entity_links,
                    entity_relation_links,
                    korean,
                )
                current_path = find_existing_document_path(document)
                if current_path is None:
                    print(f"[WARN] Current document path not found: {korean_title}")
                    new_path = DOCUMENT_INBOX / f"{safe_filename(korean_title)}.md"
                else:
                    new_path = current_path.parent / f"{safe_filename(korean_title)}.md"

                document["_output_path"] = str(new_path)
                document_previews.append({
                    "document_id": document_id,
                    "title": korean_title,
                    "source_path": document.get("path"),
                    "current_path": str(current_path) if current_path else None,
                    "old_filename": current_path.name if current_path else None,
                    "new_filename": new_path.name,
                    "target_path": str(new_path),
                    "markdown": markdown,
                    "document": document,
                })

        except Exception as exc:
            if args.apply and not args.skip_obsidian_apply and render_pending:
                mark_failed(stage_conn, render_pending, render_stage, exc)
            raise

    # Resolve duplicate targets only among files actually being changed.
    used_targets = set()
    for preview in document_previews:
        target = Path(preview["target_path"])
        current_raw = preview.get("current_path")
        current = Path(current_raw) if current_raw else None
        candidate = target
        n = 2
        while True:
            resolved = str(candidate.resolve())
            if resolved not in used_targets and ((current is not None and candidate.resolve() == current.resolve()) or not candidate.exists()):
                break
            candidate = target.with_name(f"{target.stem} ({n}){target.suffix}")
            n += 1
        preview["target_path"] = str(candidate)
        preview["new_filename"] = candidate.name
        used_targets.add(str(candidate.resolve()))

    # ------------------------------------------------------------
    # 5. Entity render stage: only entities whose rendered graph changed.
    # ------------------------------------------------------------
    entity_candidates = []
    for entity in registry.values():
        outgoing, incoming = _entity_render_inputs(entity, entity_relation_links)
        render_hash = stable_text_hash(
            entity.get("entity_id"),
            entity.get("canonical_name"),
            entity.get("entity_type"),
            json.dumps(outgoing, ensure_ascii=False, sort_keys=True),
            json.dumps(incoming, ensure_ascii=False, sort_keys=True),
            VERSION,
        )
        entity_candidates.append({
            "document_id": str(entity["entity_id"]),
            "readwise_id": str(entity["entity_id"]),
            "entity": entity,
            "_render_hash": render_hash,
            "_stage_content_hash": render_hash,
            "outgoing": outgoing,
            "incoming": incoming,
        })

    entity_stage = "entity_render"
    with connect_db() as stage_conn:
        ensure_schema(stage_conn)
        entity_pending, entity_skipped = select_pending_documents(stage_conn, entity_candidates, entity_stage)
        if args.apply and not args.skip_obsidian_apply and entity_pending:
            mark_started(stage_conn, entity_pending, entity_stage, worker_id="abraham-v5.6")

        entity_previews = []
        try:
            for item in entity_pending:
                entity = item["entity"]
                entity_previews.append({
                    "entity": entity,
                    "path": str(entity_output_path(entity)),
                    "markdown": render_entity(entity, item["outgoing"], item["incoming"]),
                })
        except Exception as exc:
            if args.apply and not args.skip_obsidian_apply and entity_pending:
                mark_failed(stage_conn, entity_pending, entity_stage, exc)
            raise

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = REPORT_DIR / f"readwise_auto_v5.6_{timestamp}.json"
    report_output = {
        "version": VERSION,
        "timestamp": datetime.now().isoformat(),
        "mode": "apply" if args.apply else "dry-run",
        "source": "sqlite",
        "reader_color_gate": gate_counts,
        "schedule_kst": [x.strip() for x in ABRAHAM_SCHEDULE_KST.split(",") if x.strip()],
        "knowledge_graph": graph_stats,
        "incremental": {
            "rating_pending": len(rating_pending),
            "rating_skipped": len(rating_skipped),
            "korean_pending": len(korean_pending),
            "korean_skipped": len(korean_skipped),
            "document_render_pending": len(render_pending),
            "document_render_skipped": len(render_skipped),
            "entity_render_pending": len(entity_pending),
            "entity_render_skipped": len(entity_skipped),
            "entity_classification_pending": len(entity_classification_pending),
            "entity_classification_skipped": len(entity_classification_skipped),
            "graph_extraction_pending": len(graph_pending),
            "graph_extraction_skipped": len(graph_skipped),
        },
        "korean_presentation": {
            "enabled": True,
            "prompt_version": PROMPT_VERSION,
            "model": MODEL,
            "translations": len(korean_results),
        },
        "statistics": {
            "sqlite_documents": sqlite_documents_total,
            "exportable_documents": len(documents),
            "entities": len(registry),
            "relations": len(relations),
            "document_entity_links": len(document_entity_links),
            "entity_entity_links": len(entity_relation_links),
            "document_previews": len(document_previews),
            "entity_previews": len(entity_previews),
        },
        "document_previews": document_previews,
        "entity_previews": entity_previews,
    }

    if args.apply and args.skip_obsidian_apply:
        print()
        print("=" * 70)
        print("CORE APPLY / OBSIDIAN DEFERRED")
        print("=" * 70)
        print("AI/graph/translation/rating results are persisted in SQLite.")
        print("Document/entity render stages remain pending for the Mac worker.")
    elif args.apply:
        print()
        print("=" * 70)
        print("APPLY")
        print("=" * 70)
        try:
            result = apply_files(document_previews, entity_previews)
            report_output["apply_result"] = result
            with connect_db() as stage_conn:
                ensure_schema(stage_conn)
                if render_pending:
                    mark_completed(
                        stage_conn,
                        render_pending,
                        render_stage,
                        {d["document_id"]: {"render_hash": d["_render_hash"], "output_path": d.get("_output_path") or d.get("target_path"), "title": d.get("title", ""), "old_filename": d.get("old_filename"), "new_filename": d.get("new_filename")} for d in render_pending},
                        result_fields=("render_hash",),
                    )
                if entity_pending:
                    mark_completed(
                        stage_conn,
                        entity_pending,
                        entity_stage,
                        {x["document_id"]: {"render_hash": x["_render_hash"]} for x in entity_pending},
                        result_fields=("render_hash",),
                    )
        except Exception as exc:
            with connect_db() as stage_conn:
                ensure_schema(stage_conn)
                if render_pending:
                    mark_failed(stage_conn, render_pending, render_stage, exc)
                if entity_pending:
                    mark_failed(stage_conn, entity_pending, entity_stage, exc)
            raise
        print("Backup :", result["backup_root"])
        print("Backed up files :", result["backed_up"])
        print("Written documents :", result["written_documents"])
        print("Renamed documents :", result["renamed_documents"])
        print("Written entities :", result["written_entities"])
    else:
        print()
        print("=" * 70)
        print("DRY-RUN")
        print("=" * 70)
        print("No Obsidian files were modified.")

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write_text(output_path, json.dumps(_json_safe(report_output), ensure_ascii=False, indent=2))

    print()
    print("=" * 70)
    print(f"V{VERSION} RESULT")
    print("=" * 70)
    print("SQLite documents        :", len(documents))
    print("Korean pending          :", len(korean_pending))
    print("Document render pending :", len(render_pending))
    print("Entity classification   :", len(entity_classification_results))
    print("Entity render pending   :", len(entity_pending))
    print("Korean skipped          :", len(korean_skipped))
    print("Document render skipped :", len(render_skipped))
    print("Entity render skipped   :", len(entity_skipped))
    print("Report                  :", output_path)
    print("Schedule KST            :", ABRAHAM_SCHEDULE_KST)
    return report_output


def main():
    parser = argparse.ArgumentParser(description="Abraham V5 Cloud-ready pipeline")
    parser.add_argument("--apply", action="store_true", help="Persist pipeline results and, unless deferred, modify Obsidian files.")
    parser.add_argument("--skip-obsidian-apply", action="store_true", help="Persist all core AI/graph results but defer document/entity Obsidian writes to the Mac worker.")
    parser.add_argument("--limit", type=int, default=None, help="Process only the first N documents after cache filtering.")
    parser.add_argument("--readwise-id", action="append", default=None, help="Process only specified Readwise IDs. Can be repeated.")
    parser.add_argument("--print-schedule", action="store_true", help="Print the configured KST schedule and exit.")
    parser.add_argument("--storage-status", action="store_true", help="Show Abraham storage usage and exit.")
    parser.add_argument("--storage-maintenance", action="store_true", help="Run safe storage maintenance: compress old backups, prune old logs/reports/backups, and enforce disk thresholds.")
    args = parser.parse_args()

    print("=" * 70)
    print(f"Abraham V{VERSION} — Cloud-ready incremental pipeline")
    print("=" * 70)
    print("Schedule KST :", ABRAHAM_SCHEDULE_KST)
    print()

    if args.print_schedule:
        print("Configured runs:", ", ".join(x.strip() for x in ABRAHAM_SCHEDULE_KST.split(",") if x.strip()))
        return

    if args.storage_status:
        print(json.dumps(storage_status(), ensure_ascii=False, indent=2))
        return

    if args.storage_maintenance:
        result = run_storage_maintenance(dry_run=not args.apply)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / "abraham.log"
    started = time.time()
    with log_path.open("a", encoding="utf-8") as log:
        log.write(f"START {datetime.now().isoformat()} version={VERSION} mode={'apply' if args.apply else 'dry-run'}\n")
    try:
        with pipeline_lock():
            result = run_pipeline(args)
        elapsed = time.time() - started
        with log_path.open("a", encoding="utf-8") as log:
            log.write(f"SUCCESS {datetime.now().isoformat()} elapsed={elapsed:.2f}s\n")
        return result
    except Exception as exc:
        elapsed = time.time() - started
        with log_path.open("a", encoding="utf-8") as log:
            log.write(f"FAIL {datetime.now().isoformat()} elapsed={elapsed:.2f}s error={type(exc).__name__}: {exc}\n")
        raise


if __name__ == "__main__":
    main()