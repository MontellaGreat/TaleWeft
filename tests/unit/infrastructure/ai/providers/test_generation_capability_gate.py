"""Adversarial checks for the model/reasoning capability gate.

These tests deliberately stop at the request builder or use mocked clients;
they never contact a paid model endpoint.
"""
import asyncio
from unittest.mock import AsyncMock

import pytest

from application.ai.llm_control_service import LLMControlConfig, LLMControlService, LLMProfile
from domain.ai.services.llm_service import GenerationConfig
from domain.ai.value_objects.prompt import Prompt
from infrastructure.ai.config.settings import Settings
from infrastructure.ai.providers.openai_provider import OpenAIProvider
from infrastructure.persistence.database.connection import DatabaseConnection


PROMPT = Prompt(system='system', user='user')


def _provider(model, *, endpoint='chat', base_url=None, extra_body=None):
    return OpenAIProvider(Settings(
        api_key='test-key', default_model=model, endpoint_kind=endpoint,
        base_url=base_url, extra_body=extra_body or {},
    ))


@pytest.mark.parametrize('endpoint', ['chat', 'responses'])
@pytest.mark.asyncio
async def test_known_non_reasoning_model_is_rejected_before_non_stream_http(endpoint):
    provider = _provider('gpt-4o', endpoint=endpoint)
    if endpoint == 'chat':
        provider.async_client.chat.completions.create = AsyncMock()
    else:
        provider.async_client.responses.create = AsyncMock()

    with pytest.raises(ValueError, match='不支持 reasoning_effort'):
        await provider.generate(PROMPT, GenerationConfig(model='gpt-4o', reasoning_effort='high'))

    if endpoint == 'chat':
        provider.async_client.chat.completions.create.assert_not_called()
    else:
        provider.async_client.responses.create.assert_not_called()
    await provider.aclose()


@pytest.mark.asyncio
async def test_known_non_reasoning_model_is_rejected_before_stream_http():
    provider = _provider('gpt-4o', endpoint='chat')
    provider.async_client.chat.completions.create = AsyncMock()
    with pytest.raises(ValueError, match='不支持 reasoning_effort'):
        _ = [part async for part in provider.stream_generate(
            PROMPT, GenerationConfig(model='gpt-4o', reasoning_effort='high'))]
    provider.async_client.chat.completions.create.assert_not_called()
    await provider.aclose()


@pytest.mark.asyncio
async def test_known_non_reasoning_model_is_rejected_before_responses_stream_http():
    provider = _provider('gpt-4o', endpoint='responses')
    provider.async_client.responses.create = AsyncMock()
    with pytest.raises(ValueError, match='不支持 reasoning_effort'):
        _ = [part async for part in provider.stream_generate(
            PROMPT, GenerationConfig(model='gpt-4o', reasoning_effort='high'))]
    provider.async_client.responses.create.assert_not_called()
    await provider.aclose()


def test_known_o3_efforts_are_restricted_but_high_is_valid():
    provider = _provider('o3', endpoint='chat')
    with pytest.raises(ValueError, match='可用值'):
        provider._build_chat_request_kwargs(
            provider._build_messages(PROMPT),
            GenerationConfig(model='o3', reasoning_effort='max'),
        )
    payload = provider._build_chat_request_kwargs(
        provider._build_messages(PROMPT),
        GenerationConfig(model='o3', reasoning_effort='high'),
    )
    assert payload['reasoning_effort'] == 'high'
    asyncio.run(provider.aclose())


@pytest.mark.parametrize('model', ['gpt-4o', 'o3', 'gpt-5'])
@pytest.mark.parametrize('endpoint', ['chat', 'responses'])
def test_none_effort_is_rejected_for_reviewed_models(model, endpoint):
    provider = _provider(model, endpoint=endpoint)
    with pytest.raises(ValueError, match='reasoning_effort'):
        if endpoint == 'chat':
            provider._build_chat_request_kwargs(provider._build_messages(PROMPT), GenerationConfig(model=model, reasoning_effort='none'))
        else:
            provider._build_responses_request_kwargs(PROMPT, GenerationConfig(model=model, reasoning_effort='none'))
    asyncio.run(provider.aclose())


@pytest.mark.asyncio
@pytest.mark.parametrize('endpoint', ['chat', 'responses'])
async def test_known_none_effort_is_rejected_before_any_http(endpoint):
    provider = _provider('o3', endpoint=endpoint)
    client = provider.async_client.chat.completions if endpoint == 'chat' else provider.async_client.responses
    client.create = AsyncMock()
    with pytest.raises(ValueError, match='reasoning_effort'):
        await provider.generate(PROMPT, GenerationConfig(model='o3', reasoning_effort='none'))
    client.create.assert_not_called()
    await provider.aclose()


@pytest.mark.parametrize('endpoint', ['chat', 'responses'])
def test_unknown_gateway_transmits_explicit_none_and_receipt_distinguishes_default(endpoint):
    provider = _provider('custom-model', endpoint=endpoint)
    config = GenerationConfig(model='custom-model', reasoning_effort='none', temperature=0.35)
    payload = (provider._build_chat_request_kwargs(provider._build_messages(PROMPT), config)
               if endpoint == 'chat' else provider._build_responses_request_kwargs(PROMPT, config))
    assert (payload.get('reasoning_effort') if endpoint == 'chat' else payload['reasoning']['effort']) == 'none'
    assert 'temperature' not in payload
    assert provider.last_request_receipt['requested_reasoning_effort'] == 'none'
    assert provider.last_request_receipt['reasoning_effort'] == 'none'
    asyncio.run(provider.aclose())


def test_runtime_summary_marks_legacy_invalid_profile_unavailable():
    legacy = LLMProfile.model_construct(
        id='legacy', name='legacy', protocol='openai', base_url='',
        model='gpt-4o', api_key='key', reasoning_effort='high',
    )
    summary = LLMControlService().get_runtime_summary(
        LLMControlConfig(active_profile_id='legacy', profiles=[legacy]))
    assert summary.status == 'unavailable'
    assert '不支持 reasoning_effort' in summary.reason


@pytest.mark.parametrize('model', ['gpt-5.4', 'vendor-o3-pro'])
def test_future_or_unknown_models_remain_model_dependent(model):
    provider = _provider(model, endpoint='chat')
    payload = provider._build_chat_request_kwargs(
        provider._build_messages(PROMPT),
        GenerationConfig(model=model, reasoning_effort='max'),
    )
    assert payload['reasoning_effort'] == 'max'
    asyncio.run(provider.aclose())


@pytest.mark.parametrize('extra_body', [
    {'reasoning_effort': 'high'},
    {'reasoning': {'effort': 'high'}},
])
def test_extra_body_cannot_bypass_known_non_reasoning_gate(extra_body):
    provider = _provider('gpt-4o', endpoint='chat', extra_body=extra_body)
    with pytest.raises(ValueError, match='不支持 reasoning_effort'):
        provider._build_chat_request_kwargs(
            provider._build_messages(PROMPT), GenerationConfig(model='gpt-4o'))
    asyncio.run(provider.aclose())


def test_conflicting_explicit_and_nested_extra_effort_is_rejected():
    provider = _provider('custom-model', endpoint='chat', extra_body={'reasoning': {'effort': 'max'}})
    with pytest.raises(ValueError, match='conflicts'):
        provider._build_chat_request_kwargs(
            provider._build_messages(PROMPT),
            GenerationConfig(model='custom-model', reasoning_effort='high'),
        )
    asyncio.run(provider.aclose())


@pytest.mark.parametrize('extra_body', [
    {'reasoning_effort': 'high'},
    {'reasoning': {'effort': 'high', 'summary': 'auto'}},
])
def test_none_conflicts_with_non_none_extension_effort(extra_body):
    provider = _provider('custom-model', endpoint='chat', extra_body=extra_body)
    with pytest.raises(ValueError, match='conflicts'):
        provider._build_chat_request_kwargs(
            provider._build_messages(PROMPT),
            GenerationConfig(model='custom-model', reasoning_effort='none'),
        )
    asyncio.run(provider.aclose())


@pytest.mark.parametrize('endpoint', ['chat', 'responses'])
def test_nested_summary_is_preserved_while_effort_is_promoted(endpoint):
    provider = _provider('custom-model', endpoint=endpoint,
                         extra_body={'reasoning': {'effort': 'none', 'summary': 'auto'}})
    config = GenerationConfig(model='custom-model', reasoning_effort='none', temperature=0.3)
    payload = (provider._build_chat_request_kwargs(provider._build_messages(PROMPT), config)
               if endpoint == 'chat' else provider._build_responses_request_kwargs(PROMPT, config))
    if endpoint == 'chat':
        assert payload['reasoning_effort'] == 'none'
        assert payload['extra_body'] == {'reasoning': {'summary': 'auto'}}
        assert payload['max_completion_tokens'] == config.max_tokens
    else:
        assert payload['reasoning'] == {'effort': 'none', 'summary': 'auto'}
    assert 'temperature' not in payload
    assert provider.last_request_receipt['requested_reasoning_effort'] == 'none'
    assert provider.last_request_receipt['reasoning_effort'] == 'none'
    asyncio.run(provider.aclose())


@pytest.mark.parametrize('endpoint', ['chat', 'responses'])
def test_none_effort_allows_top_p_but_omits_temperature(endpoint):
    provider = _provider('custom-model', endpoint=endpoint)
    config = GenerationConfig(model='custom-model', reasoning_effort='none', top_p=0.6)
    payload = (provider._build_chat_request_kwargs(provider._build_messages(PROMPT), config)
               if endpoint == 'chat' else provider._build_responses_request_kwargs(PROMPT, config))
    assert payload['top_p'] == 0.6
    assert 'temperature' not in payload
    asyncio.run(provider.aclose())


@pytest.mark.parametrize('endpoint', ['chat', 'responses'])
def test_null_effort_keeps_provider_default_shape(endpoint):
    provider = _provider('custom-model', endpoint=endpoint)
    config = GenerationConfig(model='custom-model', reasoning_effort=None, temperature=0.35)
    payload = (provider._build_chat_request_kwargs(provider._build_messages(PROMPT), config)
               if endpoint == 'chat' else provider._build_responses_request_kwargs(PROMPT, config))
    assert 'reasoning_effort' not in payload and 'reasoning' not in payload
    assert payload['temperature'] == 0.35
    assert provider.last_request_receipt['requested_reasoning_effort'] is None
    assert provider.last_request_receipt['reasoning_effort'] is None
    asyncio.run(provider.aclose())


def test_custom_gateway_and_model_keep_unknown_reasoning_pass_through():
    provider = _provider(
        'o3', endpoint='chat', base_url='https://gateway.example/v1')
    payload = provider._build_chat_request_kwargs(
        provider._build_messages(PROMPT),
        GenerationConfig(model='o3', reasoning_effort='max'),
    )
    assert payload['reasoning_effort'] == 'max'
    asyncio.run(provider.aclose())


def test_invalid_saved_profile_is_readable_but_save_revalidates(tmp_path, monkeypatch):
    import application.ai.llm_control_service as control
    db = DatabaseConnection(str(tmp_path / 'capability.sqlite'))
    monkeypatch.setattr(control, 'get_database', lambda: db)
    service = LLMControlService()
    row = {
        'id': 'legacy', 'name': 'legacy', 'preset_key': 'openai-official',
        'protocol': 'openai', 'base_url': '', 'api_key': '', 'model': 'gpt-4o',
        'temperature': 0.7, 'max_tokens': 4096, 'timeout_seconds': 300,
        'extra_headers': '{}', 'extra_query': '{}',
        'extra_body': '{}', 'notes': '', 'use_legacy_chat_completions': 0,
    }
    profile = service._row_to_profile(row)
    invalid = profile.model_copy(update={'reasoning_effort': 'high'})
    with pytest.raises(ValueError, match='不支持 reasoning_effort'):
        service.save_config(LLMControlConfig(profiles=[invalid]))
    db.close_all(skip_checkpoint=True)
