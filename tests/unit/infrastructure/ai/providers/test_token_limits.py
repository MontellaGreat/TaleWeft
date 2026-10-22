from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from domain.ai.services.llm_service import GenerationConfig
from domain.ai.value_objects.prompt import Prompt
from infrastructure.ai.config.settings import Settings
from infrastructure.ai.provider_factory import DynamicLLMService
from infrastructure.ai.providers.anthropic_provider import AnthropicProvider
from infrastructure.ai.providers.gemini_provider import GeminiProvider
from infrastructure.ai.providers.openai_provider import OpenAIProvider
from infrastructure.ai.providers.token_limits import resolve_token_limits


def provider(kind, settings):
    instance = kind.__new__(kind)
    instance.settings = settings
    return instance


def test_explicit_default_sized_request_is_not_replaced_by_profile_setting():
    configured = SimpleNamespace(settings=Settings(default_model='custom', default_max_tokens=120000))
    assert DynamicLLMService._merge_config(GenerationConfig(max_tokens=4096), configured).max_tokens == 4096
    assert DynamicLLMService._merge_config(GenerationConfig(), configured).max_tokens == 120000


@pytest.mark.parametrize('invalid', [0, -1, 0.5, True])
def test_generation_config_rejects_invalid_output_budget(invalid):
    with pytest.raises(ValueError):
        GenerationConfig(max_tokens=invalid)


@pytest.mark.parametrize('kind,model,cap', [
    (OpenAIProvider, 'gpt-4o', 16384),
    (AnthropicProvider, 'claude-3-5-sonnet-20241022', 8192),
    (GeminiProvider, 'gemini-2.0-flash', 8192),
])
@pytest.mark.asyncio
async def test_model_output_limit_rejects_generate_without_any_network_call(kind, model, cap):
    instance = provider(kind, Settings(default_model=model))
    call = AsyncMock()
    instance._use_legacy = True
    instance.async_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=call)),
                                            messages=SimpleNamespace(create=call))
    instance._http_client = SimpleNamespace(post=call)
    with pytest.raises((ValueError, RuntimeError), match='output limit'):
        await instance.generate(Prompt(system='system', user='user'), GenerationConfig(max_tokens=cap + 1))
    call.assert_not_awaited()


@pytest.mark.parametrize('kind,model,cap', [
    (OpenAIProvider, 'gpt-4o', 16384),
    (AnthropicProvider, 'claude-3-5-sonnet-20241022', 8192),
    (GeminiProvider, 'gemini-2.0-flash', 8192),
])
@pytest.mark.asyncio
async def test_model_output_limit_also_blocks_all_streaming_paths(kind, model, cap):
    instance = provider(kind, Settings(default_model=model))
    call = AsyncMock()
    instance._use_legacy = True
    instance.async_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=call)),
                                            messages=SimpleNamespace(stream=call))
    instance._stream_http_client = SimpleNamespace(stream=call)
    instance._http_client = SimpleNamespace(stream=call)
    with pytest.raises((ValueError, RuntimeError), match='output limit'):
        async for _chunk in instance.stream_generate(Prompt(system='system', user='user'), GenerationConfig(max_tokens=cap + 1)):
            pass
    call.assert_not_called()


@pytest.mark.parametrize('api', ['chat', 'responses', 'anthropic', 'gemini'])
def test_extra_body_cannot_raise_the_validated_output_budget(api):
    extra = {'generationConfig': {'maxOutputTokens': 9999}} if api == 'gemini' else {
        'max_output_tokens' if api == 'responses' else 'max_tokens': 9999,
    }
    settings = Settings(default_model='custom', extra_body=extra)
    config = GenerationConfig(max_tokens=512)
    prompt = Prompt(system='system', user='user')
    with pytest.raises(ValueError, match='cannot override'):
        if api == 'chat':
            provider(OpenAIProvider, settings)._build_chat_request_kwargs([], config)
        elif api == 'responses':
            provider(OpenAIProvider, settings)._build_responses_request_kwargs(prompt, config)
        elif api == 'anthropic':
            provider(AnthropicProvider, settings)._build_message_request(prompt, config, stream=True)
        else:
            provider(GeminiProvider, settings)._build_payload(prompt, config)


def test_unknown_model_capability_is_explicit_and_custom_limit_is_enforced():
    assert resolve_token_limits('openai', 'gpt-4o-custom').source == 'unknown'
    assert resolve_token_limits('openai', 'gpt-4o-custom').max_output_tokens is None
    settings = Settings(default_model='gpt-4o-custom', max_output_tokens_limit=100, context_window_limit=512)
    instance = provider(OpenAIProvider, settings)
    with pytest.raises(ValueError, match='output limit'):
        instance._build_responses_request_kwargs(Prompt(system='system', user='user'), GenerationConfig(max_tokens=101))
    with pytest.raises(ValueError, match='context window'):
        instance._build_chat_request_kwargs([{'role': 'user', 'content': '中文' * 100}], GenerationConfig(max_tokens=100))


def test_trace_metadata_reports_effective_budget_and_capability_source():
    instance = provider(OpenAIProvider, Settings(protocol='openai', default_model='gpt-4o'))
    _provider, _profile, _model, metadata = DynamicLLMService._request_metadata(instance, GenerationConfig(max_tokens=512))
    assert metadata['effective_max_tokens'] == 512
    assert metadata['max_output_tokens_limit'] == 16384
    assert metadata['token_limit_source'] == 'documented_model_snapshot'
