"""Small deterministic smoke; full 1,000-document check runs in CI step."""
from scripts.reader_novelty_stress import run


def test_100_document_adversarial_stress_smoke():
    report=run(100)
    assert report["synthetic_documents_ingested"] == 100
    assert sum(report["source_decision_distribution"].values()) == 100
    assert report["mocked_local_cache"]["requests_avoided_by_cache"] == 90
    assert report["source_mutation_invalidated_dependent_decisions"]
    assert report["failed_embedding_atomicity_verified"]
    assert not report["automatic_translation_skip_enabled"]
