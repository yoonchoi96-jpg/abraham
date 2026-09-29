from __future__ import annotations

import argparse
import os
import time
import json
from run_sqlite_to_v461 import load_documents
import re
import shutil
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from google import genai


VERSION = "4.6.1"
PROMPT_VERSION = "4.6.1-korean-presentation"

load_dotenv()

MODEL = os.getenv(
    "MODEL",
    "gemini-3.1-flash-lite",
)

# V4.61 compatibility cache. V5 will move this state into SQLite.
KOREAN_CACHE_PATH = (
    Path("/Users/w/Desktop/Invest/Invest Insight")
    / "90_Dashboard"
    / "auto_reports"
    / "korean_presentation_cache_v46.json"
)

KOREAN_BATCH_SIZE = 20
KOREAN_MAX_RETRIES = 3

VAULT_PATH = Path("/Users/w/Desktop/Invest/Invest Insight")
REPORT_DIR = VAULT_PATH / "90_Dashboard" / "auto_reports"

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


def get_validated_relations(report: dict) -> list[dict]:
    relations = report.get("relations", {})
    if not isinstance(relations, dict):
        return []

    validated = relations.get("validated", [])
    if not isinstance(validated, list):
        return []

    return [
        x for x in validated
        if isinstance(x, dict)
        and isinstance(x.get("subject"), dict)
        and isinstance(x.get("object"), dict)
        and x.get("relation")
    ]



# ============================================================
# V4.6 — Korean Presentation Layer
# ============================================================

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

    for document in documents:
        payload.append({
            "document_id": document["document_id"],
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
      "document_id": 0,
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
                                        "document_id": {
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
                                        "document_id",
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

            expected = {
                d["document_id"]
                for d in documents
            }

            actual = {
                x.get("document_id")
                for x in output
                if isinstance(x, dict)
            }

            if expected != actual:
                raise ValueError(
                    "Korean translation IDs mismatch"
                )

            for item in output:
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
                if attempt < KOREAN_MAX_RETRIES - 1:
                    delay = min(
                        60,
                        10 * (attempt + 1),
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
                    "Skipping batch."
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

def translate_documents_to_korean(
    documents,
    limit=None,
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

        cached = cache.get(readwise_id)

        if (
            isinstance(cached, dict)
            and cached.get("canonical_text")
            == canonical
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
            }

            results[document_id] = cache_item
            cache[readwise_id] = cache_item

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

    for document_id, document in enumerate(documents):
        for entity in get_resolved_entities(document):
            entity_id = entity["entity_id"]
            if entity_id in registry:
                registry[entity_id]["document_ids"].add(document_id)

    return registry


def build_document_entity_links(documents: list[dict]) -> list[dict]:
    links = []

    for document_id, document in enumerate(documents):
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
            "- None",            "",
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



# === V4.6.1 READWISE SOURCE LAYER ===
















def find_existing_document_path(document: dict) -> Path | None:
    """Resolve an existing vault path without scanning the vault.

    SQLite is the source of truth for the stored path. The Korean
    filename fallback is deterministic and does not require rglob.
    """
    original_path = document.get("path")
    if original_path:
        candidate = Path(original_path)
        if candidate.exists():
            return candidate

    korean_title = str(document.get("korean_title", "")).strip()
    if korean_title:
        candidate = DOCUMENT_INBOX / f"{safe_filename(korean_title)}.md"
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


def apply_files(
    document_previews: list[dict],
    entity_previews: list[dict],
) -> dict:

    timestamp = datetime.now().strftime(
        "%Y%m%d_%H%M%S"
    )

    backup_root = (
        VAULT_PATH
        / "90_Dashboard"
        / "auto_reports"
        / f"v4.6.1_backup_{timestamp}"
    )

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
            target.write_text(
                preview["markdown"],
                encoding="utf-8",
            )
        else:
            # Existing document: update in place first.
            current.write_text(
                preview["markdown"],
                encoding="utf-8",
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

        path.write_text(
            preview["markdown"],
            encoding="utf-8",
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


def main():

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually modify Obsidian files.",
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process only the first N documents.",
    )
    parser.add_argument(
        "--readwise-id",
        action="append",
        default=None,
        help="Process only the specified Readwise document ID. Can be repeated.",
    )

    args = parser.parse_args()

    print("=" * 70)
    print(
        f"Obsidian Readwise Automation V{VERSION}"
    )
    print(
        "Knowledge Linking + Korean Presentation"
    )
    print("=" * 70)
    print()

    # --------------------------------------------------------
    # SQLite is the sole production source of truth.
    # V4.4 report loading has been removed from the Freeze cleanup.
    # --------------------------------------------------------
    if args.readwise_id:
        from run_sqlite_to_v461 import load_documents_by_ids

        documents, sqlite_relations = load_documents_by_ids(
            args.readwise_id
        )
    else:
        documents, sqlite_relations = load_documents(
            limit=None
        )

    report = {
        "relations": {
            "validated": sqlite_relations,
            "raw": sqlite_relations,
            "rejected": [],
        }
    }

    # --limit is intentionally applied later,
    # after Korean cache filtering.

    # --------------------------------------------------------
    # Stable document IDs + original source
    # --------------------------------------------------------
    #
    # IMPORTANT:
    # Do NOT read the current Obsidian markdown here.
    #
    # After V4.6 rendering, the physical .md file contains the
    # Korean presentation itself. Reading it again would cause
    # recursive duplication of:
    #
    #   Korean body -> Entities -> Relations -> Original ->
    #   English Canonical
    #
    # SQLite is the authoritative source snapshot.
    # --------------------------------------------------------

    for document_id, document in enumerate(
        documents
    ):
        document["document_id"] = document_id

    # --------------------------------------------------------
    # Readwise original source layer
    # --------------------------------------------------------
    #
    # IMPORTANT:
    # The original article MUST come from the Readwise export.
    # Never recover original content from the rendered Obsidian
    # markdown file.
    #
    # readwise_original becomes the authoritative source text.
    # document["content"] is overwritten only after successful
    # Readwise resolution for compatibility with older functions.
    # --------------------------------------------------------

    # --------------------------------------------------------
    # SQLite source layer
    # --------------------------------------------------------
    #
    # SQLite is now the authoritative source snapshot.
    # Raw Readwise content is already stored in documents.content.
    # Do not perform another Readwise source-resolution pass.
    # --------------------------------------------------------
    for document in documents:
        original = (document.get("content") or "").strip()

        if not original:
            raise RuntimeError(
                "SQLite source content is empty for: "
                + str(document.get("title", "(no title)"))
            )

        document["readwise_original"] = original
        document["readwise_source_status"] = "resolved"

    for document in documents:
        original = (
            document.get(
                "readwise_original",
                "",
            )
            or ""
        ).strip()

        if not original:
            raise RuntimeError(
                "Readwise original source is empty for: "
                + str(document.get("title", "(no title)"))
            )

        document["content"] = original

    # --------------------------------------------------------
    # Korean presentation
    # --------------------------------------------------------

    print("=" * 70)
    print("KOREAN PRESENTATION")
    print("=" * 70)
    print()

    korean_results = (
        translate_documents_to_korean(
            documents,
            limit=args.limit,
        )
    )

    print()

    print(
        "Korean translations :",
        len(korean_results),
    )

    # --------------------------------------------------------
    # Knowledge graph
    # --------------------------------------------------------

    relations = get_validated_relations(
        report
    )

    registry = build_entity_registry(
        documents
    )

    document_entity_links = (
        build_document_entity_links(
            documents
        )
    )

    entity_relation_links = (
        build_entity_relation_links(
            relations
        )
    )

    print()
    print("=" * 70)
    print("KNOWLEDGE LINKING")
    print("=" * 70)
    print()

    print(
        "Document → Entity links :",
        len(document_entity_links),
    )

    print(
        "Entity → Entity links   :",
        len(entity_relation_links),
    )

    # --------------------------------------------------------
    # Render documents
    # --------------------------------------------------------

    document_previews = []

    for document_id, document in enumerate(
        documents
    ):

        korean = korean_results.get(
            document_id,
            {}
        )

        korean_title = (
            korean.get(
                "korean_title"
            )
            or document.get(
                "title"
            )
            or "Untitled"
        )

        document["korean_title"] = korean_title

        markdown = render_document(
            document_id,
            document,
            document_entity_links,
            entity_relation_links,
            korean,
        )

        # Resolve the CURRENT physical file.
        #
        # This works both before and after the Korean filename
        # migration:
        #   old stored path
        #       ↓
        #   Korean filename
        #       ↓
        #   document_id fallback
        current_path = find_existing_document_path(
            document
        )

        if current_path is None:
            print(
                f"[WARN] Current document path not found: "
                f"{korean_title}"
            )

        if current_path is not None:
            new_path = (
                current_path.parent
                / f"{safe_filename(korean_title)}.md"
            )
        else:
            # New Readwise document: create directly in Readwise inbox.
            new_path = (
                DOCUMENT_INBOX
                / f"{safe_filename(korean_title)}.md"
            )

        document_previews.append({
            "document_id": document_id,

            "title": korean_title,

            # Original V4.4 path, retained for provenance.
            "source_path": document.get(
                "path"
            ),

            # Actual file currently present in the vault.
            "current_path": (
                str(current_path)
                if current_path
                else None
            ),

            # Physical filename before sync.
            "old_filename": (
                current_path.name
                if current_path
                else None
            ),

            # Physical filename after sync.
            "new_filename": (
                new_path.name
                if new_path
                else None
            ),

            "target_path": (
                str(new_path)
                if new_path
                else None
            ),

            "markdown": markdown,

            "document": document,
        })

    # --------------------------------------------------------
    # Resolve duplicate document target filenames.
    #
    # Multiple Readwise documents can legitimately produce the
    # same Korean presentation title. Keep every document and
    # make the physical filenames unique:
    #
    #   title.md
    #   title (2).md
    #   title (3).md
    #
    # The first document keeps the original filename.
    # --------------------------------------------------------
    used_targets = set()

    for preview in document_previews:
        raw_target = preview.get("target_path")

        if not raw_target:
            continue

        target = Path(raw_target)
        current_raw = preview.get("current_path")
        current = Path(current_raw) if current_raw else None

        candidate = target
        n = 2

        while True:
            resolved = str(candidate.resolve())

            # Another document in this run already owns this target.
            collision_with_run = resolved in used_targets

            # An existing physical file is allowed only when it is the
            # current file belonging to this same document.
            existing_conflict = (
                candidate.exists()
                and (
                    current is None
                    or candidate.resolve() != current.resolve()
                )
            )

            if not collision_with_run and not existing_conflict:
                break

            candidate = (
                target.parent
                / f"{target.stem} ({n}){target.suffix}"
            )
            n += 1

        used_targets.add(str(candidate.resolve()))

        if candidate != target:
            preview["new_filename"] = candidate.name
            preview["target_path"] = str(candidate)

    print(
        "Target filename collision resolver:",
        "active"
    )

    # --------------------------------------------------------
    # Render entities
    # --------------------------------------------------------

    entity_previews = []

    for entity_id, entity in registry.items():

        outgoing = [
            x
            for x in entity_relation_links
            if x["subject_entity_id"]
            == entity_id
        ]

        incoming = [
            x
            for x in entity_relation_links
            if x["object_entity_id"]
            == entity_id
        ]

        entity_previews.append({
            "entity_id": entity_id,
            "canonical_name": entity[
                "canonical_name"
            ],
            "entity_type": entity[
                "entity_type"
            ],
            "path": str(
                entity_output_path(
                    entity
                )
            ),
            "markdown": render_entity(
                entity,
                outgoing,
                incoming,
            ),
        })

    print()
    print("=" * 70)
    print("MARKDOWN RENDER")
    print("=" * 70)
    print()

    print(
        "Document previews :",
        len(document_previews),
    )

    print(
        "Entity previews   :",
        len(entity_previews),
    )
    print()
    print("=" * 70)
    print("FILENAME SYNC")
    print("=" * 70)
    print()

    rename_candidates = [
        preview
        for preview in document_previews
        if (
            preview.get("old_filename")
            and preview.get("new_filename")
            and preview.get("old_filename")
            != preview.get("new_filename")
        )
    ]

    print(
        "Will rename:",
        len(rename_candidates),
    )

    for index, preview in enumerate(
        rename_candidates,
        start=1,
    ):
        print(
            f"[{index}]"
        )
        print(
            f'OLD: {preview.get("old_filename")}'
        )
        print(
            f'NEW: {preview.get("new_filename")}'
        )
        print()


    # --------------------------------------------------------
    # Report
    # --------------------------------------------------------

    timestamp = datetime.now().strftime(
        "%Y%m%d_%H%M%S"
    )

    output_path = (
        REPORT_DIR
        / f"readwise_auto_v4.6_{timestamp}.json"
    )

    report_output = {
        "version": VERSION,
        "timestamp": datetime.now().isoformat(),
        "mode": (
            "apply"
            if args.apply
            else "dry-run"
        ),
        "source": "sqlite",
        "korean_presentation": {
            "enabled": True,
            "prompt_version": PROMPT_VERSION,
            "model": MODEL,
            "translations": len(
                korean_results
            ),
        },
        "statistics": {
            "documents": len(
                documents
            ),
            "entities": len(
                registry
            ),
            "relations": len(
                relations
            ),
            "document_entity_links": len(
                document_entity_links
            ),
            "entity_entity_links": len(
                entity_relation_links
            ),
            "document_previews": len(
                document_previews
            ),
            "entity_previews": len(
                entity_previews
            ),
        },
        "document_previews": document_previews,
        "entity_previews": entity_previews,
    }

    # --------------------------------------------------------
    # Apply
    # --------------------------------------------------------

    if args.apply:

        print()
        print("=" * 70)
        print("APPLY")
        print("=" * 70)
        print()

        result = apply_files(
            document_previews,
            entity_previews,
        )

        report_output[
            "apply_result"
        ] = result

        print(
            "Backup :",
            result["backup_root"],
        )

        print(
            "Backed up files :",
            result["backed_up"],
        )

        print(
            "Written documents :",
            result["written_documents"],
        )

        print(
            "Renamed documents :",
            result["renamed_documents"],
        )

        print(
            "Written entities :",
            result["written_entities"],
        )

    else:

        print()
        print("=" * 70)
        print("DRY-RUN")
        print("=" * 70)
        print()

        print(
            "No Obsidian files were modified."
        )

    REPORT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    with output_path.open(
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            report_output,
            f,
            ensure_ascii=False,
            indent=2,
        )

    print()
    print("=" * 70)
    print(
        f"V{VERSION} RESULT"
    )
    print("=" * 70)
    print()

    print(
        "Documents             :",
        len(documents),
    )

    print(
        "Entities              :",
        len(registry),
    )

    print(
        "Document→Entity links :",
        len(document_entity_links),
    )

    print(
        "Entity→Entity links   :",
        len(entity_relation_links),
    )

    print(
        "Document previews     :",
        len(document_previews),
    )

    print(
        "Entity previews       :",
        len(entity_previews),
    )

    print(
        "Report                :",
        output_path,
    )

    print()

    if args.apply:

        print(
            "APPLY complete."
        )

    else:

        print(
            "DRY-RUN complete."
        )

        print(
            "No Obsidian files were modified."
        )




if __name__ == "__main__":
    main()    main()