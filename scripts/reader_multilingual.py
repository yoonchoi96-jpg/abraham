"""Unicode-first, multilingual *safety markers* for offline Reader screening.

These are conservative lexical guards, not a machine-translation engine,
a reliable language classifier, or multilingual textual entailment. Unknown
languages cannot authorize deletion/translation skipping.
"""
from __future__ import annotations

import re
import unicodedata

# Word boundaries apply to space-separated languages (Latin/Cyrillic/Arabic).
# No stemming: surface-form false negatives block fewer safe reuse decisions,
# but never authorize an unsafe omission.
BOUND_NEGATIONS = (
    # English
    "no", "not", "never", "without", "denied", "denies",
    # Spanish
    "nunca", "sin", "ningún", "ninguna", "negó", "negado", "desmintió",
    # French
    "pas", "jamais", "sans", "aucun", "aucune", "démenti", "nie",
    # Portuguese
    "não", "nunca", "sem", "nenhum", "nenhuma", "negou", "desmentiu",
    # Russian
    "не", "нет", "без", "никогда", "отрицает", "опроверг", "отменил",
    # Arabic
    "لا", "لم", "لن", "ليس", "غير", "بدون", "نفى", "نفت", "إلغاء",
)
BOUND_UNCERTAINTY = (
    # English, Spanish, French, Portuguese, Russian, Arabic
    "reportedly", "allegedly", "unconfirmed",
    "presuntamente", "supuestamente", "según", "podría",
    "selon", "présumé", "pourrait", "aurait",
    "supostamente", "alegadamente", "segundo", "poderia",
    "предположительно", "якобы", "возможно",
    "بحسب", "ربما", "يزعم", "محتمل",
)
UNSPACED_NEGATIONS = (
    # Korean, Japanese, Simplified/Traditional Chinese
    "아니다", "아닌", "없다", "않", "부인", "취소", "철회",
    "ではない", "じゃない", "ない", "否定", "撤回", "中止",
    "没有", "沒有", "不是", "并非", "並非", "未", "否认", "否認", "取消",
)
UNSPACED_UNCERTAINTY = (
    "의혹", "가능성", "추정", "미확인", "관계자에 따르면",
    "疑惑", "可能性", "未確認", "とみられる",
    "据称", "據稱", "可能", "传闻", "傳聞", "尚未证实", "尚未證實",
)
_BOUND_RE = re.compile(
    r"(?<!\w)(" + "|".join(
        re.escape(term) for term in sorted(
            set(BOUND_NEGATIONS + BOUND_UNCERTAINTY), key=len, reverse=True
        )
    ) + r")(?!\w)",
    re.IGNORECASE | re.UNICODE,
)
_NEGATION_SET = set(BOUND_NEGATIONS)
_UNCERTAINTY_SET = set(BOUND_UNCERTAINTY)


def canonical_numeric_text(text: str) -> str:
    """Normalize decimal glyphs, not ambiguous comma/point conventions."""
    punctuation = {"٫": ".", "٬": ",", "٪": "%"}
    result = []
    for char in unicodedata.normalize("NFKC", text):
        try:
            result.append(str(unicodedata.decimal(char)))
        except (ValueError, TypeError):
            result.append(punctuation.get(char, char))
    return "".join(result)


def localized_critical_markers(text: str) -> set[str]:
    """Fail-closed on changed denials and hedging across requested languages."""
    lowered = unicodedata.normalize("NFKC", text).casefold()
    markers = set()
    for match in _BOUND_RE.finditer(lowered):
        item = match.group().casefold()
        if item in _NEGATION_SET:
            markers.add("negation:" + item)
        if item in _UNCERTAINTY_SET:
            markers.add("attribution:" + item)
    for item in UNSPACED_NEGATIONS:
        if item in lowered:
            markers.add("negation:" + item)
    for item in UNSPACED_UNCERTAINTY:
        if item in lowered:
            markers.add("attribution:" + item)
    return markers


def script_families(text: str) -> set[str]:
    """Language-neutral script hints: never infer a language from Han alone.

    Useful to route *manual review*: shared Chinese characters do not prove
    Chinese instead of Japanese, and Latin alone cannot separate EN/ES/FR/PT.
    """
    found = set()
    for char in text:
        n = ord(char)
        if 0x4E00 <= n <= 0x9FFF or 0x3400 <= n <= 0x4DBF:
            found.add("Han")
        elif 0x3040 <= n <= 0x30FF or 0x31F0 <= n <= 0x31FF:
            found.add("Kana")
        elif 0x1100 <= n <= 0x11FF or 0xAC00 <= n <= 0xD7AF:
            found.add("Hangul")
        elif 0x0400 <= n <= 0x052F:
            found.add("Cyrillic")
        elif 0x0600 <= n <= 0x06FF or 0x0750 <= n <= 0x077F:
            found.add("Arabic")
        elif ("LATIN" in unicodedata.name(char, "")
              and unicodedata.category(char).startswith("L")):
            found.add("Latin")
    return found
