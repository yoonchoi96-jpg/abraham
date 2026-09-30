from __future__ import annotations

import json
import os

ENTITY_TYPES = (
    "Person", "Company", "Organization", "Country", "Place",
    "Product", "Technology", "Concept", "Event", "Institution", "Brand", "Other",
)

def classify_entity_batch(client, entities):
    payload = [{
        "entity_id": str(e["entity_id"]),
        "canonical_name": str(e.get("canonical_name") or ""),
        "mentions": sorted(str(x) for x in (e.get("mentions") or []) if x),
    } for e in entities]

    prompt = (
        "Classify each knowledge-graph entity into exactly one entity_type. "
        "Do not rename, merge, or invent entities. Use only canonical_name and mentions. "
        "Return strict JSON only. Allowed types: " + ", ".join(ENTITY_TYPES) + ". "
        "Company=commercial business; Institution=formal university/government/court/bank; "
        "Organization=non-company group/association/NGO/media/political organization; "
        "Product=named commercial product/service/model offering; "
        "Technology=technical method/standard/protocol/platform; Brand=brand identity; "
        "Country=country; Place=city/region/geographic place; "
        "Concept=abstract idea/process/phenomenon/theory/topic; "
        "Event=named event/incident/conference/war/competition; Other=none fit. "
        "Prefer the most specific type. "
        'Return {"entities":[{"entity_id":"...","entity_type":"Company"}]}. '
        "Entities: " + json.dumps(payload, ensure_ascii=False)
    )

    response = client.models.generate_content(
        model=os.getenv("GEMINI_RATING_MODEL", "gemini-3.6-flash"),
        contents=prompt,
        config={
            "response_mime_type": "application/json",
            "thinking": os.getenv("ABRAHAM_ENTITY_CLASSIFICATION_THINKING", "disabled"),
            "max_output_tokens": int(os.getenv("ABRAHAM_ENTITY_CLASSIFICATION_MAX_TOKENS", "8192")),
        },
    )
    data = json.loads(response.text.strip())
    items = data.get("entities")
    if not isinstance(items, list):
        raise ValueError("entities must be a list")

    expected = {str(e["entity_id"]) for e in entities}
    actual = {str(x.get("entity_id")) for x in items if isinstance(x, dict)}
    if expected != actual:
        raise ValueError("Entity classification IDs mismatch")

    result = {}
    for item in items:
        entity_id = str(item.get("entity_id"))
        entity_type = str(item.get("entity_type") or "").strip()
        if entity_id not in expected or entity_type not in ENTITY_TYPES:
            raise ValueError("Invalid entity classification")
        result[entity_id] = entity_type
    return result
