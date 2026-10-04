from __future__ import annotations

import json
import os
from pathlib import Path

def workset_path() -> Path | None:
    raw = os.getenv("ABRAHAM_WORKSET_PATH", "").strip()
    return Path(raw) if raw else None

def load_workset_ids() -> set[str] | None:
    path = workset_path()
    if path is None or not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        ids = payload.get("readwise_ids", []) if isinstance(payload, dict) else payload
        return {str(x).strip() for x in ids if str(x).strip()}
    except Exception:
        return None

def filter_workset(documents: list[dict]) -> list[dict]:
    ids = load_workset_ids()
    if ids is None:
        return documents
    return [d for d in documents if str(d.get("readwise_id") or "").strip() in ids]
