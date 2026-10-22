from infrastructure.ai.config.settings import Settings
from infrastructure.ai.providers.capabilities import adapter_capabilities, registered_protocols
from infrastructure.ai.providers.openai_provider import OpenAIProvider
from domain.ai.services.llm_service import GenerationConfig


def test_protocol_registry_matches_shipped_text_adapters():
    assert registered_protocols() == ('openai', 'anthropic', 'gemini')


def test_openai_auto_matrix_describes_runtime_endpoint_selection():
    capabilities = adapter_capabilities('openai', 'custom-model', 'auto')
    provider = OpenAIProvider(Settings(api_key='key', default_model='custom-model', endpoint_kind='auto'))

    assert capabilities['protocol_id'] == 'openai.responses'
    assert capabilities['endpoint_kind'] == 'responses'
    assert capabilities['requested_endpoint_kind'] == 'auto'
    assert capabilities['endpoint_selection'] == {
        'requested': 'auto', 'default': 'responses', 'with_stop_sequences': 'chat',
    }
    assert capabilities['features']['stop_sequences'] == 'configurable'
    assert capabilities['feature_matrix']['stop_sequences'] == {
        'status': 'configurable', 'parameter': 'stop', 'endpoint': 'openai.chat',
        'condition': '自动端点模式仅在存在停止序列时选择 Chat Completions',
    }
    assert provider._should_use_responses(GenerationConfig()) is True
    assert provider._should_use_responses(GenerationConfig(stop_sequences=['END'])) is False


def test_explicit_openai_responses_matrix_does_not_claim_stop_support():
    capabilities = adapter_capabilities('openai', 'custom-model', 'responses')

    assert capabilities['features']['stop_sequences'] == 'unavailable'
    assert capabilities['endpoint_selection']['with_stop_sequences'] == 'rejected'
    assert capabilities['feature_matrix']['reasoning_effort']['parameter'] == 'reasoning.effort'


def test_openai_reasoning_capability_is_model_and_endpoint_aware():
    legacy = adapter_capabilities('openai', 'gpt-4o-mini')
    reasoning = adapter_capabilities('openai', 'o3-mini-2025-01-31')
    gpt5 = adapter_capabilities('openai', 'gpt-5-mini')
    unknown = adapter_capabilities('openai', 'custom-model')
    compatible_gateway = adapter_capabilities('openai', 'o3-mini', base_url='https://gateway.example/v1')

    assert legacy['features']['reasoning_effort'] == 'unavailable'
    assert legacy['reasoning_efforts'] == []
    assert reasoning['reasoning_efforts'] == ['low', 'medium', 'high']
    assert reasoning['reasoning_capability_basis'] == 'official_model_family_inference'
    assert gpt5['reasoning_efforts'] == ['minimal', 'low', 'medium', 'high']
    assert unknown['features']['reasoning_effort'] == 'model_dependent'
    assert unknown['reasoning_efforts'] == []
    assert compatible_gateway['features']['reasoning_effort'] == 'model_dependent'
    assert compatible_gateway['reasoning_capability_basis'] == 'compatible_gateway_requires_probe'


def test_native_adapter_matrix_names_payload_parameters_and_unavailable_features():
    anthropic = adapter_capabilities('anthropic', 'claude-model')
    gemini = adapter_capabilities('gemini', 'gemini-model')

    assert anthropic['protocol_id'] == 'anthropic.messages'
    assert anthropic['feature_matrix']['thinking_budget_tokens']['parameter'] == 'thinking.budget_tokens'
    assert anthropic['features']['tools'] == 'unavailable'
    assert anthropic['features']['json_schema'] == 'unavailable'
    assert gemini['protocol_id'] == 'gemini.generate_content'
    assert gemini['feature_matrix']['thinking_budget_tokens']['parameter'] == 'generationConfig.thinkingConfig.thinkingBudget'
    assert gemini['feature_matrix']['json_schema']['parameter'] == 'generationConfig.responseSchema'


def test_unknown_protocol_is_explicitly_unregistered():
    capabilities = adapter_capabilities('bedrock', 'model')

    assert capabilities['source'] == 'unregistered'
    assert capabilities['confidence'] == 'unknown'
    assert all(value == 'unavailable' for value in capabilities['features'].values())
