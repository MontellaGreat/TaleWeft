"""Saved/readiness/test profiles must agree with the real offline builders."""
import asyncio

import pytest

from application.ai.llm_control_service import LLMControlConfig, LLMControlService, LLMProfile
from application.ai.embedding_config_service import EmbeddingConfigModel
from application.ai.model_readiness_service import ModelReadinessService
from domain.ai.services.llm_service import GenerationConfig, GenerationResult
from domain.ai.value_objects.prompt import Prompt
from infrastructure.ai.config.settings import Settings
from infrastructure.ai.providers.anthropic_provider import AnthropicProvider
from infrastructure.ai.providers.gemini_provider import GeminiProvider
from infrastructure.ai.providers.openai_provider import OpenAIProvider
from infrastructure.persistence.database.connection import DatabaseConnection


PROMPT = Prompt(system='offline', user='offline')
INVALID = [
    pytest.param({'protocol': 'anthropic', 'thinking_budget_tokens': 1024, 'top_p': 0.8}, id='claude-thinking-top-p'),
    pytest.param({'stop_sequences': ['a', 'b', 'c', 'd', 'e']}, id='auto-five-stops'),
    pytest.param({'endpoint_kind': 'chat', 'stop_sequences': ['a'] * 5}, id='chat-five-stops'),
    pytest.param({'endpoint_kind': 'responses', 'stop_sequences': ['a']}, id='responses-stop'),
    pytest.param({'reasoning_effort': 'high', 'top_p': 0}, id='reasoning-top-p-zero'),
    pytest.param({'extra_body': {'reasoning': {'effort': 'high'}}, 'top_p': 0.8}, id='extension-reasoning-top-p'),
    pytest.param({'protocol': 'anthropic', 'extra_body': {'thinking': {'type': 'enabled', 'budget_tokens': 1024}}, 'top_p': 0.8}, id='extension-thinking-top-p'),
    pytest.param({'protocol': 'anthropic', 'extra_body': {'thinking': {'type': 'enabled', 'budget_tokens': 512}}}, id='extension-small-budget'),
    pytest.param({'protocol': 'gemini', 'stop_sequences': ['a'] * 6}, id='gemini-six-stops'),
]


def make_profile(**options):
    return LLMProfile(id='p', name='Offline model', model='unknown-custom-model',
                      base_url='https://offline.invalid/v1', api_key='offline-test',
                      max_tokens=4096, **options)


def build_request(profile, config=None):
    settings = Settings(api_key='offline-test', default_model=profile.model,
                        base_url=profile.base_url, endpoint_kind=profile.endpoint_kind,
                        use_legacy_chat_completions=profile.use_legacy_chat_completions,
                        extra_body=profile.extra_body)
    config = config or GenerationConfig(model=profile.model, max_tokens=profile.max_tokens,
                                         reasoning_effort=profile.reasoning_effort,
                                         thinking_budget_tokens=profile.thinking_budget_tokens,
                                         top_p=profile.top_p, stop_sequences=profile.stop_sequences)
    kind = {'openai': OpenAIProvider, 'anthropic': AnthropicProvider, 'gemini': GeminiProvider}[profile.protocol]
    # Exercise production request builders without creating transport clients.
    provider = kind.__new__(kind)
    provider.settings = settings
    if profile.protocol == 'anthropic':
        return provider._build_message_request(PROMPT, config)[1]
    if profile.protocol == 'gemini':
        return provider._build_payload(PROMPT, config)
    provider._force_responses = settings.endpoint_kind == 'responses'
    provider._use_legacy = settings.endpoint_kind == 'chat' or (
        settings.endpoint_kind == 'auto' and settings.use_legacy_chat_completions)
    if provider._should_use_responses(config):
        return provider._build_responses_request_kwargs(PROMPT, config)
    return provider._build_chat_request_kwargs(provider._build_messages(PROMPT), config)


@pytest.fixture
def control(tmp_path, monkeypatch):
    import application.ai.llm_control_service as module
    db = DatabaseConnection(str(tmp_path / 'models.sqlite'))
    monkeypatch.setattr(module, 'get_database', lambda: db)
    service = LLMControlService()
    # Initialize only this isolated fixture; rejected saves must leave it intact.
    baseline = make_profile().model_copy(update={'api_key': '', 'auth_mode': 'none'})
    service.save_config(LLMControlConfig(profiles=[baseline], active_profile_id='p'))
    yield service, db
    db.close_all(skip_checkpoint=True)


@pytest.mark.parametrize('options', INVALID)
def test_save_rejects_the_same_deterministic_error_as_real_provider_builder(control, options):
    service, db = control
    profile = make_profile(**options)
    before = db.fetch_all('SELECT * FROM llm_profiles')
    with pytest.raises(ValueError) as provider_error:
        build_request(profile)
    with pytest.raises(ValueError) as save_error:
        service.save_config(LLMControlConfig(profiles=[profile], active_profile_id='p'))
    assert str(save_error.value) == str(provider_error.value)
    assert db.fetch_all('SELECT * FROM llm_profiles') == before


@pytest.mark.parametrize('options', INVALID)
def test_existing_invalid_profile_is_unready_without_constructing_provider(control, monkeypatch, options):
    service, _ = control
    config = LLMControlConfig(profiles=[make_profile(**options)], active_profile_id='p')
    monkeypatch.setattr(service, 'get_config_readonly', lambda: config)
    embedding = type('OfflineEmbedding', (), {'get_config_readonly': lambda self: EmbeddingConfigModel(
        mode='openai', model='embed', base_url='https://offline.invalid/v1', api_key='offline-test')})()
    result = ModelReadinessService(llm_control_service=service, embedding_config_service=embedding).get_readiness()
    assert result['ready'] is False
    assert result['llm']['ready'] is False
    assert result['llm']['reasons']
    assert service.get_runtime_summary(config).status == 'unavailable'


@pytest.mark.parametrize('options', INVALID)
def test_connection_test_rejects_bad_profile_before_constructing_provider(control, options):
    service, _ = control
    constructed = []
    def provider_factory(profile):
        constructed.append(profile)
        raise AssertionError('Invalid configuration reached provider construction')
    result = asyncio.run(service.test_profile_model(make_profile(**options), provider_factory))
    assert result.ok is False
    assert constructed == []
    assert result.error and 'Invalid configuration reached' not in result.error


@pytest.mark.parametrize('options', [
    {'stop_sequences': ['a', 'b', 'c', 'd']},
    {'endpoint_kind': 'responses', 'use_legacy_chat_completions': True},
    {'protocol': 'anthropic', 'thinking_budget_tokens': 0, 'top_p': 0},
    {'protocol': 'anthropic', 'thinking_budget_tokens': 1024},
    {'protocol': 'gemini', 'thinking_budget_tokens': 1024, 'top_p': 0, 'stop_sequences': ['a'] * 5},
    {'reasoning_effort': 'none', 'top_p': 0},
    {'reasoning_effort': 'max'},
    {'extra_body': {'reasoning': {'effort': {'vendor': 'adaptive'}, 'summary': 'auto'}}},
])
def test_supported_and_unknown_model_options_remain_saveable(control, options):
    service, _ = control
    profile = make_profile(**options)
    service.save_config(LLMControlConfig(profiles=[profile], active_profile_id='p'))
    assert service.get_runtime_summary().status == 'configured'
    assert build_request(profile)


def test_connection_test_preserves_native_thinking_budget_in_effective_request():
    profile = make_profile(protocol='anthropic', extra_body={'thinking': {'type': 'enabled', 'budget_tokens': 1024}})
    submitted = []
    class OfflineBuilder:
        async def generate(self, prompt, config):
            submitted.append(build_request(profile, config))
            return GenerationResult(content='连接成功', token_usage=None)
    result = asyncio.run(LLMControlService().test_profile_model(profile, lambda _: OfflineBuilder()))
    assert result.ok is True
    assert result.effective_max_tokens == 4096
    assert submitted[0]['thinking'] == {'type': 'enabled', 'budget_tokens': 1024}
    assert submitted[0]['max_tokens'] == 4096


@pytest.mark.parametrize('options', [
    {'extra_body': {'model': 'different-model'}},
    {'extra_body': {'max_tokens': 8192}},
    {'extra_body': {'input': []}},
    {'extra_body': {'temperature': 0.1}},
    {'protocol': 'anthropic', 'extra_body': {'messages': []}},
    {'protocol': 'anthropic', 'extra_body': {'temperature': 0.1}},
    {'protocol': 'gemini', 'extra_body': {'contents': []}},
    {'protocol': 'gemini', 'extra_body': {'generationConfig': {'maxOutputTokens': 8192}}},
    {'protocol': 'gemini', 'extra_body': {'generationConfig': {'temperature': 0.1}}},
    {'protocol': 'gemini', 'thinking_budget_tokens': 1024,
     'extra_body': {'generationConfig': {'thinkingConfig': {'thinkingBudget': 0}}}},
])
def test_extension_conflicts_are_rejected_before_storage_is_opened(monkeypatch, options):
    profile = make_profile(**options)
    service = LLMControlService()
    def forbidden_database():
        raise AssertionError('Invalid profile opened storage before validation')
    monkeypatch.setattr(service, '_db', forbidden_database)
    with pytest.raises(ValueError):
        build_request(profile)
    with pytest.raises(ValueError):
        service.save_config(LLMControlConfig(profiles=[profile], active_profile_id='p'))


@pytest.mark.parametrize('options', [
    {'extra_body': {'max_tokens': 4096}},
    {'extra_body': {'max_output_tokens': 4096}},
    {'protocol': 'anthropic', 'extra_body': {'max_tokens': 4096}},
    {'protocol': 'gemini', 'extra_body': {'generationConfig': {'maxOutputTokens': 4096}}},
])
def test_connection_test_preserves_valid_extension_output_budget(options):
    profile = make_profile(**options)
    submitted = []
    class OfflineBuilder:
        async def generate(self, prompt, config):
            submitted.append(build_request(profile, config))
            return GenerationResult(content='连接成功', token_usage=None)
    result = asyncio.run(LLMControlService().test_profile_model(profile, lambda _: OfflineBuilder()))
    assert result.ok is True
    assert result.effective_max_tokens == 4096
    assert submitted
