from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True)
class FieldOwner:
    owner: str
    mutable_by_hendrix: bool
    description: str


ABRAHAM_FIELDS: Final[frozenset[str]] = frozenset({
    "entity",
    "entities",
    "rating",
    "source",
    "source_url",
    "source_id",
    "readwise_id",
    "classification",
    "category",
    "relations",
    "relation",
})

ABEL_FIELDS: Final[frozenset[str]] = frozenset({
    "abel_subject",
    "abel_level",
    "abel_learning_state",
    "abel_mastery",
    "abel_priority",
    "abel_wrong_count",
    "abel_correct_count",
    "abel_last_wrong",
    "abel_saved",
})

HENDRIX_FIELDS: Final[frozenset[str]] = frozenset({
    "hendrix_id",
    "hendrix_indexed",
    "hendrix_embedding",
    "hendrix_last_audit",
    "hendrix_integrity",
})

FIELD_OWNERS: Final[dict[str, FieldOwner]] = {
    **{
        key: FieldOwner("abraham", False, "Abraham-owned knowledge/classification metadata.")
        for key in ABRAHAM_FIELDS
    },
    **{
        key: FieldOwner("abel", False, "Abel-owned learning state and mastery metadata.")
        for key in ABEL_FIELDS
    },
    **{
        key: FieldOwner("hendrix", True, "Hendrix-owned infrastructure metadata.")
        for key in HENDRIX_FIELDS
    },
}


def normalize_key(key: str) -> str:
    return key.strip().lower()


def owner_for(key: str) -> str:
    normalized = normalize_key(key)
    return FIELD_OWNERS.get(
        normalized,
        FieldOwner("unknown", False, "No ownership contract has been declared."),
    ).owner


def is_protected_from_hendrix(key: str) -> bool:
    normalized = normalize_key(key)
    owner = owner_for(normalized)
    return owner in {"abraham", "abel"} or owner == "unknown"


def audit_field_ownership(frontmatter: dict[str, str]) -> dict[str, list[str]]:
    observed: dict[str, list[str]] = {
        "abraham": [],
        "abel": [],
        "hendrix": [],
        "unknown": [],
    }
    for key in frontmatter:
        observed[owner_for(key)].append(normalize_key(key))
    for values in observed.values():
        values.sort()
    return observed
