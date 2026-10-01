import inspect

from deepseek_adapter import DeepSeekClient


def test_deepseek_adapter_has_provider_json_contract():
    source = inspect.getsource(DeepSeekClient.generate_content)
    assert 'response_format' in source
    assert 'json_object' in source
    assert 'max_tokens' in source


def test_deepseek_adapter_has_thinking_controls():
    source = inspect.getsource(DeepSeekClient.generate_content)
    assert 'DEEPSEEK_THINKING' in source
    assert 'DEEPSEEK_REASONING_EFFORT' in source
    assert 'reasoning_effort' in source


def test_v56_pipeline_has_no_direct_gemini_dependency():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    pipeline = (root / "run_v560_storage.py").read_text(encoding="utf-8")
    classifier = (root / "entity_classifier.py").read_text(encoding="utf-8")

    assert "get_gemini_client" not in pipeline
    assert "get_gemini_client" not in classifier
    assert "get_deepseek_client" in pipeline
