from __future__ import annotations



import argparse
import hashlib
import json
import os
import re
import shutil
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from google import genai

from abraham_sqlite import load_documents, load_documents_by_ids, connect as connect_db
from abraham_incremental import (
    ensure_schema,
    select_pending_documents,
    mark_started,
    mark_completed,
    mark_failed,
)

from dotenv import load_dotenv
from abraham_storage import storage_status, run_storage_maintenance


VERSION = "5.6.0"
PROMPT_VERSION = "5.6.0-korean-presentation"

load_dotenv()

MODEL = os.getenv(
    "MODEL",
    "gemini-3.1-flash-lite",
)

# Cloud-ready paths: the same code runs locally or on a Linux VM.
VAULT_PATH = Path(os.getenv(
    "ABRAHAM_VAULT_PATH",
    "/Users/w/Desktop/Invest/Invest Insight",
)).expanduser()
REPORT_DIR = Path(os.getenv(
    "ABRAHAM_REPORT_DIR",
    str(VAULT_PATH / "90_Dashboard" / "auto_reports"),
)).expanduser()
KOREAN_CACHE_PATH = Path(os.getenv(
    "ABRAHAM_KOREAN_CACHE_PATH",
    str(REPORT_DIR / "korean_presentation_cache_v50.json"),
)).expanduser()

KOREAN_BATCH_SIZE = int(os.getenv("KOREAN_BATCH_SIZE", "20"))
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


def load_korean_cache():
    if not KOREAN_CACHE_PATH.exists():
        return {}

    try:
        with KOREAN_CACHE_PATH.open(
            encoding="utf-8"
        ) as f:
            data = json.load(f)

        return data if isinstance(data, dict) else {}

    except Exception:
        return {}


def save_korean_cache(cache):
    KOREAN_CACHE_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    tmp = KOREAN_CACHE_PATH.with_suffix(".tmp")

    with tmp.open(
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(
            cache,
            f,
            ensure_ascii=False,
            indent=2,
        )

    tmp.replace(KOREAN_CACHE_PATH)


def get_korean_client():
    api_key = os.getenv("GEMINI_API_KEY")

    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY is missing."
        )

    return genai.Client(
        api_key=api_key
    )


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
      "korean_text": "...",
      "translation_notes": []
    }}
  ]
}}

Documents:

{json.dumps(payload, ensure_ascii=False)}
"""



# ============================================================
# GEMINI FREE-TIER RPM LIMITER
# ============================================================

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
                    "response_schema": {
                        "type": "OBJECT",
                        "properties": {
                            "documents": {
                                "type": "ARRAY",
                                "items": {
                                    "type": "OBJECT",
                                    "properties": {
                                        "index": {
                                            "type": "INTEGER",
                                        },
                                        "korean_title": {
                                            "type": "STRING",
                                        },
                                        "korean_text": {
                                            "type": "STRING",
                                        },
                                        "translation_notes": {
                                            "type": "ARRAY",
                                            "items": {
                                                "type": "STRING",
                                            },
                                        },
                                    },
                                    "required": [
                                        "index",
                                        "korean_title",
                                        "korean_text",
                                    ],
                                },
                            },
                        },
                        "required": [
                            "documents",
                        ],
                    },
                    "service_tier": "flex",
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

            result = json.loads(raw)

            output = result.get(
                "documents",
                [],
            )

            if not isinstance(
                output,
                list,
            ):
                raise ValueError(
                    "documents must be a list"
                )

            expected_indexes = set(
                range(len(documents))
            )

            actual_indexes = {
                x.get("index")
                for x in output
                if isinstance(x, dict)
            }

            if expected_indexes != actual_indexes:
                raise ValueError(
                    "Korean translation indexes mismatch"
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

            is_429 = (
                "429" in error_text
                or "RESOURCE_EXHAUSTED" in error_text
            )

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
    response=client.models.generate_content(model=MODEL,contents=_build_rating_prompt(documents),config={"response_mime_type":"application/json","service_tier":"flex"})
    data=json.loads(response.text.strip().strip("`").replace("json\n","",1))
    items=data.get("ratings")
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
    client=get_korean_client(); results={}
    try:
        for start in range(0,len(pending),KOREAN_BATCH_SIZE):
            for item in _rate_batch(client,pending[start:start+KOREAN_BATCH_SIZE]): results[item["document_id"]]=item
        with connect_db() as conn:
            _persist_ratings(conn,pending,list(results.values()))
            mark_completed(conn,pending,"rating",results,result_fields=RATING_DIMENSIONS)
    except Exception as exc:
        with connect_db() as conn: mark_failed(conn,pending,"rating",exc)
        raise
    return results,pending,skipped

def translate_documents_to_korean(
    documents,
    limit=None,
    allow_ai=True,
):
    cache = load_korean_cache()
    results = {}
    pending = []

    for document in documents:
        document_id = document["document_id"]
        readwise_id = str(
            document.get("readwise_id", "")
        ).strip()

        if not readwise_id:
            raise RuntimeError(
                "Missing readwise_id for document: "
                + str(document.get("title", "(no title)"))
            )

        canonical = (
            document.get("canonical_text")
            or document.get("content")
            or ""
        )

        cache_key = stable_text_hash(readwise_id, canonical, PROMPT_VERSION, MODEL)
        cached = cache.get(cache_key)

        if (
            isinstance(cached, dict)
            and cached.get("cache_key") == cache_key
            and cached.get("korean_title")
            and cached.get("korean_text")
        ):
            results[document_id] = cached
        else:
            pending.append(document)

    print(
        "Korean cache       :",
        len(results),
    )
    print(
        "Korean to translate:",
        len(pending),
    )

    # --limit applies AFTER Korean cache filtering.
    # Therefore --limit N means at most N uncached documents
    # are sent to Gemini.
    if limit is not None:
        pending = pending[:limit]
        print(
            "Korean selected for Gemini:",
            len(pending),
        )

    if not pending:
        return results

    if not allow_ai:
        print("Korean AI skipped (dry-run)")
        return results

    client = get_korean_client()

    batches = [
        pending[i:i + KOREAN_BATCH_SIZE]
        for i in range(
            0,
            len(pending),
            KOREAN_BATCH_SIZE,
        )
    ]

    print(
        "Korean batches     :",
        len(batches),
    )

    for number, batch in enumerate(
        batches,
        1,
    ):
        translated = translate_korean_batch(
            client,
            batch,
        )

        for item in translated:
            document_id = item[
                "document_id"
            ]

            document = next(
                d
                for d in batch
                if d["document_id"]
                == document_id
            )

            readwise_id = str(
                document["readwise_id"]
            ).strip()

            canonical = (
                document.get("canonical_text")
                or document.get("content")
                or ""
            )

            # Recompute the cache key for THIS document.


            # The earlier cache_key belongs to the document scan loop


            # and may refer to a different document.


            document_cache_key = stable_text_hash(


                readwise_id,


                canonical,


                PROMPT_VERSION,


                MODEL,


            )



            cache_item = {


                "document_id": document_id,


                "readwise_id": readwise_id,


                "canonical_text": canonical,


                "korean_title": item[


                    "korean_title"


                ],


                "korean_text": item[


                    "korean_text"


                ],


                "translation_notes": item.get(


                    "translation_notes",


                    [],


                ),


                "prompt_version": PROMPT_VERSION,


                "model": MODEL,


                "cache_key": document_cache_key,


            }


            results[document_id] = cache_item


            cache[document_cache_key] = cache_item

        save_korean_cache(cache)

        print(
            f"[KOREAN {number}/"
            f"{len(batches)}] "
            f"batch complete "
            f"({len(translated)} docs)"
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



# V5 source-of-truth boundary: document content comes from SQLite only.
# The legacy Readwise export/source-cache layer was removed in V5.5.

# Cached once per process: Readwise ID -> existing Markdown path.
_EXISTING_READWISE_PATH_INDEX = None


def _build_existing_readwise_path_index():
    global _EXISTING_READWISE_PATH_INDEX

    if _EXISTING_READWISE_PATH_INDEX is not None:
        return _EXISTING_READWISE_PATH_INDEX

    index = {}

    try:
        import frontmatter

        for candidate in DOCUMENT_INBOX.rglob("*.md"):
            try:
                post = frontmatter.load(candidate)
                readwise_id = str(
                    post.metadata.get("readwise_id", "")
                ).strip()

                if readwise_id and readwise_id not in index:
                    index[readwise_id] = candidate

            except Exception:
                continue

    except Exception:
        pass

    _EXISTING_READWISE_PATH_INDEX = index

    print(
        f"Existing document index built: {len(index)} Readwise IDs"
    )

    return index


def find_existing_document_path(document: dict) -> Path | None:
    # ------------------------------------------------------------
    # 1. Stable document identity: Readwise ID
    # ------------------------------------------------------------
    readwise_id = str(
        document.get("readwise_id", "")
    ).strip()

    if readwise_id:
        index = _build_existing_readwise_path_index()

        candidate = index.get(readwise_id)

        if candidate is not None and candidate.exists():
            return candidate

    # ------------------------------------------------------------
    # 2. Existing path from the source document.
    # ------------------------------------------------------------
    original_path = document.get("path")

    if original_path:
        candidate = Path(original_path)

        if candidate.exists():
            return candidate

    # ------------------------------------------------------------
    # 3. V4.6.1: locate an already-renamed Korean file.
    # ------------------------------------------------------------
    korean_title = document.get(
        "korean_title",
        ""
    )

    if korean_title:
        matches = list(
            DOCUMENT_INBOX.rglob(
                f"{safe_filename(korean_title)}.md"
            )
        )

        if matches:
            return matches[0]

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


def apply_files(
    document_previews: list[dict],
    entity_previews: list[dict],
) -> dict:

    timestamp = datetime.now().strftime(
        "%Y%m%d_%H%M%S"
    )

    backup_root = BACKUP_ROOT / f"v5_backup_{timestamp}"

    written_documents = 0
    written_entities = 0
    backed_up = 0
    renamed_documents = 0

    # --------------------------------------------------------
    # Preflight ALL document paths and targets.
    # Nothing is modified until every document passes.
    # --------------------------------------------------------

    document_operations = []

    for preview in document_previews:

        current_path = preview.get(
            "current_path"
        )

        target_path = preview.get(
            "target_path"
        )


        # Existing document:
        #   update the current file and optionally rename it.
        # New document:
        #   create target_path directly.
        if current_path:
            current = Path(current_path)

            if not current.exists():
                raise RuntimeError(
                    f"Current document disappeared: {current}"
                )

            target = (
                Path(target_path)
                if target_path
                else current
            )

            # Never overwrite another existing file.
            if (
                target.resolve() != current.resolve()
                and target.exists()
            ):
                raise RuntimeError(
                    "Filename target collision:\n"
                    f"CURRENT: {current}\n"
                    f"TARGET : {target}"
                )

        else:
            if not target_path:
                raise RuntimeError(
                    "New document has no target path: "
                    f'{preview.get("title", "Untitled")}'
                )

            current = None
            target = Path(target_path)

            # Never overwrite an existing file.
            if target.exists():
                raise RuntimeError(
                    "New document target already exists:\n"
                    f"TARGET: {target}"
                )

        document_operations.append(
            (
                preview,
                current,
                target,
            )
        )

    # Detect duplicate targets.
    target_paths = [
        target.resolve()
        for _, _, target in document_operations
    ]

    if len(target_paths) != len(
        set(target_paths)
    ):
        raise RuntimeError(
            "Duplicate document target filenames detected."
        )

    # --------------------------------------------------------
    # Backup
    # --------------------------------------------------------

    for _, current, _ in document_operations:

        # New document: nothing to back up.
        if current is None:
            continue

        rel = current.relative_to(
            VAULT_PATH
        )

        backup_file_path = (
            backup_root / rel
        )

        backup_file_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        backup_file(
            current,
            backup_root,
        )

        backed_up += 1

    # --------------------------------------------------------
    # Write + rename documents
    # --------------------------------------------------------

    for preview, current, target in document_operations:

        if current is None:
            # New document: create directly at target path.
            target.parent.mkdir(
                parents=True,
                exist_ok=True,
            )
            atomic_write_text(
                target,
                preview["markdown"],
            )
        else:
            # Existing document: update in place first.
            atomic_write_text(
                current,
                preview["markdown"],
            )

            if (
                target.resolve()
                != current.resolve()
            ):
                current.rename(
                    target
                )

                renamed_documents += 1

        written_documents += 1

    # --------------------------------------------------------
    # Entities
    # --------------------------------------------------------

    for preview in entity_previews:

        path = Path(
            preview["path"]
        )

        if path.exists():

            backup_file(
                path,
                backup_root,
            )

            backed_up += 1

        path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        atomic_write_text(
            path,
            preview["markdown"],
        )

        written_entities += 1

    return {
        "backup_root": str(
            backup_root
        ),
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
    # 1. Korean stage: only new/changed source content goes to AI.
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
            mark_started(stage_conn, korean_pending, korean_stage, worker_id="abraham-v5.6")

        try:
            korean_results = translate_documents_to_korean(
                korean_pending,
                limit=None,
                allow_ai=args.apply,
            )
        except Exception as exc:
            if args.apply and korean_pending:
                mark_failed(stage_conn, korean_pending, korean_stage, exc)
            raise

        # Existing cached translations are needed for render-stage evaluation.
        # Fetch them by running the cache resolver over the full snapshot; this
        # does not invoke Gemini for unchanged documents.
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
                failed_docs = [d for d in korean_pending if d["document_id"] in missing_ids]
                error = RuntimeError(
                    "Korean translation incomplete; "
                    f"missing {len(missing_ids)} document(s)"
                )
                mark_failed(stage_conn, failed_docs, korean_stage, error)
                raise error
            mark_completed(stage_conn, korean_pending, korean_stage, korean_results)

    # ------------------------------------------------------------
    # 2. Build complete knowledge indexes from SQLite.
    # ------------------------------------------------------------
    relations = get_validated_relations(sqlite_relations)
    registry = build_entity_registry(documents)
    document_entity_links = build_document_entity_links(documents)
    entity_relation_links = build_entity_relation_links(relations)

    print()
    print("=" * 70)
    print("KNOWLEDGE LINKING")
    print("=" * 70)
    print("Document → Entity links :", len(document_entity_links))
    print("Entity → Entity links   :", len(entity_relation_links))

    # ------------------------------------------------------------
    # 3. Document render stage.
    # Hash the complete render inputs, not merely source content.
    # This catches relation/entity changes even when the article itself
    # did not change.
    # ------------------------------------------------------------
    document_candidates = []
    for document in documents:
        did = document["document_id"]
        korean = all_korean_results.get(did, {})
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
            if args.apply and render_pending:
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
    # 4. Entity render stage: only entities whose rendered graph changed.
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
        if args.apply and entity_pending:
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
            if args.apply and entity_pending:
                mark_failed(stage_conn, entity_pending, entity_stage, exc)
            raise

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = REPORT_DIR / f"readwise_auto_v5.6_{timestamp}.json"
    report_output = {
        "version": VERSION,
        "timestamp": datetime.now().isoformat(),
        "mode": "apply" if args.apply else "dry-run",
        "source": "sqlite",
        "schedule_kst": [x.strip() for x in ABRAHAM_SCHEDULE_KST.split(",") if x.strip()],
        "incremental": {
            "korean_pending": len(korean_pending),
            "korean_skipped": len(korean_skipped),
            "document_render_pending": len(render_pending),
            "document_render_skipped": len(render_skipped),
            "entity_render_pending": len(entity_pending),
            "entity_render_skipped": len(entity_skipped),
        },
        "korean_presentation": {
            "enabled": True,
            "prompt_version": PROMPT_VERSION,
            "model": MODEL,
            "translations": len(korean_results),
        },
        "statistics": {
            "sqlite_documents": len(documents),
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

    if args.apply:
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
                        {d["document_id"]: {"render_hash": d["_render_hash"]} for d in render_pending},
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
    print("Entity render pending   :", len(entity_pending))
    print("Korean skipped          :", len(korean_skipped))
    print("Document render skipped :", len(render_skipped))
    print("Entity render skipped   :", len(entity_skipped))
    print("Report                  :", output_path)
    print("Schedule KST            :", ABRAHAM_SCHEDULE_KST)
    return report_output


def main():
    parser = argparse.ArgumentParser(description="Abraham V5 Cloud-ready pipeline")
    parser.add_argument("--apply", action="store_true", help="Actually modify Obsidian files.")
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
