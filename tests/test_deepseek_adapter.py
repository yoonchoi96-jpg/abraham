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
