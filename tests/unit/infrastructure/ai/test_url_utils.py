from infrastructure.ai.url_utils import (
    anthropic_messages_url,
    anthropic_models_url,
    normalize_anthropic_base_url,
    normalize_gemini_model_id,
)


def test_anthropic_official_v1_is_not_repeated():
    assert normalize_anthropic_base_url('https://api.anthropic.com/v1') == 'https://api.anthropic.com'
    assert anthropic_models_url('https://api.anthropic.com/v1') == 'https://api.anthropic.com/v1/models'


def test_anthropic_gateway_path_is_preserved():
    assert normalize_anthropic_base_url('https://gateway.example/api/v1') == 'https://gateway.example/api'
    assert normalize_anthropic_base_url('https://gateway.example/api/v1/messages') == 'https://gateway.example/api'
    assert anthropic_messages_url('https://gateway.example/api/v1') == 'https://gateway.example/api/v1/messages'
    assert anthropic_models_url('https://gateway.example/api/v1') == 'https://gateway.example/api/v1/models'
    assert anthropic_messages_url('https://api.anthropic.com/v1') == 'https://api.anthropic.com/v1/messages'


def test_gemini_model_name_prefix_is_normalized_once():
    assert normalize_gemini_model_id('models/gemini-2.5-flash') == 'gemini-2.5-flash'
