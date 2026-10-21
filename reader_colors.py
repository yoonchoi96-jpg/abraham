"""Authoritative Reader tag policy, extracted unchanged from V5.6.3."""
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

