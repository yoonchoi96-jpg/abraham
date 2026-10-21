"""Multilingual falsification cases, all offline and fully synthetic."""
import sqlite3

import pytest

from scripts.reader_multilingual import (
    canonical_numeric_text, localized_critical_markers, script_families)
from scripts.reader_novelty import NoveltyIndex, critical_markers
from tests.test_reader_novelty import FakeEmbed


@pytest.mark.parametrize("label,text,cue", [
    ("English", "The board has not approved the merger.", "not"),
    ("Spanish", "El ministerio nunca aprobó la propuesta.", "nunca"),
    ("French", "Le ministère n'a jamais approuvé l'accord.", "jamais"),
    ("Portuguese", "O governo não aprovou o contrato.", "não"),
    ("Russian", "Министерство не одобрило соглашение.", "не"),
    ("Arabic", "لم توافق الوزارة على الاتفاق.", "لم"),
    ("Chinese", "该合同尚未批准。", "未"),
    ("Japanese", "契約はまだ承認されていない。", "ない"),
    ("Korean", "정부가 해당 계약을 승인하지 않았다.", "않"),
])
def test_nine_language_negation_markers(label, text, cue):
    assert "negation:" + cue in localized_critical_markers(text), label


@pytest.mark.parametrize("label,text,cue", [
    ("Spanish", "Según el informe, la empresa podría cerrar.", "podría"),
    ("French", "Selon la presse, la société pourrait fermer.", "pourrait"),
    ("Portuguese", "A empresa supostamente fechará.", "supostamente"),
    ("Russian", "Предположительно компания закроется.", "предположительно"),
    ("Arabic", "بحسب التقارير قد تغلق الشركة.", "بحسب"),
    ("Korean", "회사 측은 폐쇄 가능성이 있다고 밝혔다.", "가능성"),
    ("Japanese", "閉鎖の可能性があるとみられる。", "可能性"),
    ("Chinese", "据称公司可能关闭。", "可能"),
    ("English", "The company reportedly plans to close.", "reportedly"),
])
def test_nine_language_uncertainty_markers(label, text, cue):
    assert "attribution:" + cue in localized_critical_markers(text), label


@pytest.mark.parametrize("word,expected", [
    ("日本語の記事です", {"Han", "Kana"}),
    ("한국어 기사입니다", {"Hangul"}),
    ("这是中文文章", {"Han"}),
    ("Этот русский текст", {"Cyrillic"}),
    ("هذه مقالة بالعربية", {"Arabic"}),
    ("Este informe es español", {"Latin"}),
    ("Este relatório é português", {"Latin"}),
    ("Cette nouvelle est française", {"Latin"}),
    ("This article is English", {"Latin"}),
])
def test_unicode_scripts_are_hints_not_unreliable_language_certification(word,expected):
    assert expected.issubset(script_families(word))


def test_arabic_indic_persian_and_fullwidth_digits_share_numeric_guard():
    a = critical_markers("Revenue reached ٣٠% and １２ million.")
    b = critical_markers("Revenue reached 30% and 12 million.")
    assert {x for x in a if x.startswith("number:")} == {
        x for x in b if x.startswith("number:")}
    assert canonical_numeric_text("۱۲٫۵٪") == "12.5%"


def test_french_guillemets_quote_changes_are_protected():
    a = critical_markers("Le ministre a déclaré «accord refusé» aujourd'hui.")
    b = critical_markers("Le ministre a déclaré «accord signé» aujourd'hui.")
    assert "quote:accord refusé" in a
    assert "quote:accord signé" in b
    assert a != b


@pytest.mark.parametrize("language,body,denied", [
    ("es", "El gobierno publicó la resolución sobre la obra.", "El gobierno nunca publicó la resolución sobre la obra."),
    ("fr", "Le gouvernement a publié le projet de loi.", "Le gouvernement n'a jamais publié le projet de loi."),
    ("pt", "O governo aprovou a nova resolução administrativa.", "O governo não aprovou a nova resolução administrativa."),
    ("ru", "Министерство одобрило это важное решение.", "Министерство не одобрило это важное решение."),
    ("ar", "وافقت الوزارة على الاتفاق بعد الاجتماع.", "لم توافق الوزارة على الاتفاق بعد الاجتماع."),
    ("zh", "主管机构已经批准该项建设申请和下一步安排。", "主管机构尚未批准该项建设申请和下一步安排。"),
    ("ja", "政府は新しい重要な協定を正式に承認しました。", "政府は新しい重要な協定をまだ承認していない。"),
    ("ko", "정부는 새로운 중요한 계약을 최종 승인했다고 발표했다.", "정부는 새로운 중요한 계약을 승인하지 않았다고 발표했다."),
    ("en", "The government officially approved the new construction plan.", "The government has never approved the new construction plan."),
])
def test_changed_negation_never_semantically_skipped(tmp_path, language, body, denied):
    db = sqlite3.connect(tmp_path / (language + ".sqlite"))
    idx = NoveltyIndex(db, model_id="synthetic-multilingual")
    embed = FakeEmbed()
    padded_before = body + "\n" + body
    padded_after = denied + "\n" + denied
    before = idx.assess("old", "Shared national policy bulletin", padded_before, embed)
    after = idx.assess("new", "Shared national policy bulletin", padded_after, embed)
    assert before["decision"] == "novel"
    assert after["decision"] != "redundancy_candidate"
    assert after["decision"] != "exact_duplicate"
    assert after["match_key"] is None
    db.close()
