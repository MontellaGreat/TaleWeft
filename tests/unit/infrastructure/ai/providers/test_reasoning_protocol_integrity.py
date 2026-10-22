from types import SimpleNamespace
from unittest.mock import AsyncMock
import json

import httpx
import openai
import pytest

from application.ai.llm_control_service import LLMProfile
from application.ai.usage_context import capture_usage
from domain.ai.services.llm_service import GenerationConfig
from domain.ai.value_objects.prompt import Prompt
from infrastructure.ai.config.settings import Settings
from infrastructure.ai.provider_factory import DynamicLLMService, LLMProviderFactory, _make_cache_key
from infrastructure.ai.providers.anthropic_provider import AnthropicProvider
from infrastructure.ai.providers.gemini_provider import GeminiProvider
from infrastructure.ai.providers.openai_provider import OpenAIProvider

PROMPT = Prompt(system='system', user='user')


@pytest.mark.asyncio
@pytest.mark.parametrize('endpoint,state', [('chat', None), ('chat', 'length'), ('responses', None),
    ('responses', 'in_progress'), ('responses', 'queued'), ('responses', 'incomplete')])
async def test_nonstream_output_requires_a_normal_terminal_state(endpoint, state):
    provider = OpenAIProvider(Settings(api_key='key', default_model='custom', endpoint_kind=endpoint))
    if endpoint == 'chat':
        provider.async_client.chat.completions.create = AsyncMock(return_value=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='partial'), finish_reason=state)], usage=None))
    else:
        provider.async_client.responses.create = AsyncMock(return_value=SimpleNamespace(status=state,
            output=[SimpleNamespace(type='message', content=[SimpleNamespace(type='output_text', text='partial')])], usage=None))
    with pytest.raises(RuntimeError, match='finish|complete'):
        await provider.generate(PROMPT, GenerationConfig())
    await provider.aclose()


@pytest.mark.asyncio
async def test_schema_rejection_is_never_downgraded_to_json_object():
    provider = OpenAIProvider(Settings(api_key='key', default_model='custom', endpoint_kind='chat'))
    request = httpx.Request('POST', 'https://gateway.test/v1/chat/completions')
    response = httpx.Response(400, request=request)
    provider.async_client.chat.completions.create = AsyncMock(side_effect=openai.BadRequestError(
        'response_format json_schema unsupported', response=response, body={'error': 'unsupported'}))
    with pytest.raises(RuntimeError, match='json_schema'):
        await provider.generate(PROMPT, GenerationConfig(response_format={
            'type': 'json_schema', 'json_schema': {'name': 'document', 'schema': {'type': 'object'}}}))
    provider.async_client.chat.completions.create.assert_awaited_once()
    assert provider.last_request_receipt['response_format_type'] == 'json_schema'
    assert provider.last_request_receipt['response_schema_hash']
    await provider.aclose()


def test_endpoint_fallback_cache_isolated_by_protocol_model_and_endpoint():
    provider = OpenAIProvider(Settings(api_key='key', default_model='first', base_url='http://cache-isolation.test/v1'))
    first = GenerationConfig(model='first')
    key = provider._endpoint_cache_key(first)
    provider._fallback_to_chat_cache.add(key)
    try:
        assert not provider._should_use_responses(first)
        assert provider._should_use_responses(GenerationConfig(model='second'))
        forced = OpenAIProvider(Settings(api_key='key', default_model='first', base_url='http://cache-isolation.test/v1', endpoint_kind='responses'))
        assert forced._should_use_responses(first)
    finally:
        provider._fallback_to_chat_cache.discard(key)


def test_model_not_found_is_not_reclassified_as_an_unsupported_endpoint():
    provider = OpenAIProvider(Settings(api_key='key', default_model='custom'))
    request = httpx.Request('POST', 'https://gateway.test/v1/responses')
    error = openai.NotFoundError('The model custom does not exist',
        response=httpx.Response(404, request=request), body={'code': 'model_not_found'})
    assert provider._may_fallback(error) is False


@pytest.mark.parametrize('protocol,extra', [('openai', {'stream': True}), ('openai', {'text': {'format': {'type': 'text'}}}),
    ('anthropic', {'messages': []}), ('anthropic', {'stream': True}), ('gemini', {'contents': []}),
    ('gemini', {'systemInstruction': {'parts': [{'text': 'replacement'}]}})])
def test_extension_body_cannot_replace_request_or_format_controls(protocol, extra):
    settings = Settings(api_key='key', default_model='custom', extra_body=extra)
    provider = {'openai': OpenAIProvider, 'anthropic': AnthropicProvider, 'gemini': GeminiProvider}[protocol](settings)
    with pytest.raises(ValueError, match='protected'):
        if protocol == 'openai':
            provider._build_responses_request_kwargs(PROMPT, GenerationConfig())
        elif protocol == 'anthropic':
            provider._build_message_request(PROMPT, GenerationConfig())
        else:
            provider._build_payload(PROMPT, GenerationConfig())


@pytest.mark.parametrize('protocol', ['openai.chat', 'openai.responses', 'anthropic', 'gemini'])
def test_structured_format_receipt_records_actual_mode_and_schema_hash(protocol):
    config = GenerationConfig(response_format={'type': 'json_schema', 'json_schema': {'name': 'payload', 'schema': {}}})
    settings = Settings(api_key='key', default_model='custom')
    if protocol.startswith('openai'):
        provider = OpenAIProvider(settings)
        if protocol.endswith('chat'):
            provider._build_chat_request_kwargs(provider._build_messages(PROMPT), config)
        else:
            provider._build_responses_request_kwargs(PROMPT, config)
    elif protocol == 'anthropic':
        provider = AnthropicProvider(settings)
        with capture_usage() as capture:
            provider._build_message_request(PROMPT, config, stream=True)
            assert capture.metadata['request_receipt']['structured_output_mode'] == 'prompt_json_instruction'
    else:
        provider = GeminiProvider(settings)
        provider._build_payload(PROMPT, config)
    assert provider.last_request_receipt['response_format_type'] == 'json_schema'
    assert len(provider.last_request_receipt['response_schema_hash']) == 64


@pytest.mark.parametrize('endpoint', ['chat', 'responses'])
@pytest.mark.parametrize('effort', ['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'])
def test_openai_transmits_explicit_effort_in_both_native_protocols(endpoint, effort):
    provider = OpenAIProvider(Settings(api_key='key', default_model='custom', endpoint_kind=endpoint))
    config = GenerationConfig(reasoning_effort=effort)
    kwargs = (provider._build_chat_request_kwargs(provider._build_messages(PROMPT), config)
              if endpoint == 'chat' else provider._build_responses_request_kwargs(PROMPT, config))
    if effort == 'none':
        assert (kwargs.get('reasoning_effort') if endpoint == 'chat' else kwargs['reasoning']['effort']) == 'none'
        assert 'temperature' not in kwargs
        assert provider.last_request_receipt['requested_reasoning_effort'] == 'none'
        assert provider.last_request_receipt['reasoning_effort'] == 'none'
        assert provider.last_request_receipt['temperature_omitted'] is True
    else:
        assert (kwargs.get('reasoning_effort') if endpoint == 'chat' else kwargs['reasoning']['effort']) == effort
        assert 'temperature' not in kwargs
        assert provider.last_request_receipt['reasoning_effort'] == effort
        assert provider.last_request_receipt['temperature_omitted'] is True


@pytest.mark.parametrize('endpoint', ['chat', 'responses'])
@pytest.mark.parametrize('configured_effort', [None, 'none'])
def test_openai_extra_body_none_is_promoted_to_native_reasoning(endpoint, configured_effort):
    provider = OpenAIProvider(Settings(api_key='key', default_model='custom', endpoint_kind=endpoint,
                                       extra_body={'reasoning_effort': 'none'}))
    config = GenerationConfig(reasoning_effort=configured_effort, temperature=0.4)
    kwargs = (provider._build_chat_request_kwargs(provider._build_messages(PROMPT), config)
              if endpoint == 'chat' else provider._build_responses_request_kwargs(PROMPT, config))
    assert (kwargs.get('reasoning_effort') if endpoint == 'chat' else kwargs['reasoning']['effort']) == 'none'
    assert kwargs.get('extra_body') is None
    assert 'temperature' not in kwargs
    assert provider.last_request_receipt['reasoning_effort'] == 'none'


@pytest.mark.asyncio
@pytest.mark.parametrize('endpoint,streaming', [('chat', False), ('chat', True),
                                                 ('responses', False), ('responses', True)])
@pytest.mark.parametrize('effort_source', ['explicit', 'direct_extra', 'nested_extra'])
async def test_openai_none_effort_is_present_in_actual_mock_client_payload(endpoint, streaming, effort_source):
    extra_body = ({'reasoning_effort': 'none'} if effort_source == 'direct_extra' else
                  {'reasoning': {'effort': 'none', 'summary': 'auto'}} if effort_source == 'nested_extra' else {})
    provider = OpenAIProvider(Settings(api_key='key', default_model='custom', endpoint_kind=endpoint, extra_body=extra_body))
    if endpoint == 'chat' and not streaming:
        response = SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop',
            message=SimpleNamespace(content='正文'))], usage=None)
        provider.async_client.chat.completions.create = AsyncMock(return_value=response)
    elif endpoint == 'responses' and not streaming:
        response = SimpleNamespace(status='completed', output=[SimpleNamespace(type='message',
            content=[SimpleNamespace(type='output_text', text='正文')])], usage=None)
        provider.async_client.responses.create = AsyncMock(return_value=response)
    elif endpoint == 'chat':
        async def chunks():
            yield SimpleNamespace(choices=[SimpleNamespace(finish_reason=None,
                delta=SimpleNamespace(content='正文'))], usage=None)
            yield SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop',
                delta=SimpleNamespace(content=None))], usage=None)
        provider.async_client.chat.completions.create = AsyncMock(return_value=chunks())
    else:
        async def chunks():
            yield SimpleNamespace(type='response.output_text.delta', delta='正文')
            yield SimpleNamespace(type='response.completed', response=SimpleNamespace(usage=None))
        provider.async_client.responses.create = AsyncMock(return_value=chunks())

    config = GenerationConfig(reasoning_effort='none' if effort_source == 'explicit' else None)
    if streaming:
        assert ''.join([part async for part in provider.stream_generate(PROMPT, config)]) == '正文'
        call = (provider.async_client.chat.completions.create if endpoint == 'chat'
                else provider.async_client.responses.create).await_args
    else:
        assert (await provider.generate(PROMPT, config)).content == '正文'
        call = (provider.async_client.chat.completions.create if endpoint == 'chat'
                else provider.async_client.responses.create).await_args
    assert (call.kwargs.get('reasoning_effort') if endpoint == 'chat' else call.kwargs['reasoning']['effort']) == 'none'
    assert 'temperature' not in call.kwargs
    if endpoint == 'chat':
        assert call.kwargs['max_completion_tokens'] == config.max_tokens
        assert 'max_tokens' not in call.kwargs
        if effort_source == 'nested_extra':
            assert call.kwargs['extra_body'] == {'reasoning': {'summary': 'auto'}}
    elif effort_source == 'nested_extra':
        assert call.kwargs['reasoning'] == {'effort': 'none', 'summary': 'auto'}
    assert provider.last_request_receipt['requested_reasoning_effort'] == config.reasoning_effort
    assert provider.last_request_receipt['reasoning_effort'] == 'none'
    await provider.aclose()


def test_responses_structured_output_is_not_silently_omitted():
    provider = OpenAIProvider(Settings(api_key='key', default_model='custom'))
    schema = {'type': 'object', 'properties': {'text': {'type': 'string'}}, 'required': ['text']}
    config = GenerationConfig(response_format={'type': 'json_schema', 'json_schema': {'name': 'writing', 'schema': schema, 'strict': True}})
    assert provider._build_responses_request_kwargs(PROMPT, config)['text']['format'] == {
        'type': 'json_schema', 'name': 'writing', 'schema': schema, 'strict': True,
    }


@pytest.mark.asyncio
async def test_force_responses_does_not_hide_endpoint_failure_by_using_chat():
    provider = OpenAIProvider(Settings(api_key='key', default_model='custom', endpoint_kind='responses'))
    provider.async_client.responses.create = AsyncMock(side_effect=RuntimeError('404 Not Found'))
    provider.async_client.chat.completions.create = AsyncMock()
    with pytest.raises(RuntimeError, match='404'):
        await provider.generate(PROMPT, GenerationConfig())
    provider.async_client.chat.completions.create.assert_not_called()
    await provider.aclose()


@pytest.mark.parametrize('value', [True, -0.01, 1.01, float('nan'), float('inf'), '0.5'])
def test_top_p_validation(value):
    with pytest.raises(ValueError, match='top_p'):
        GenerationConfig(top_p=value)


@pytest.mark.parametrize('value', ['stop', [None], [''], ['x'] * 17])
def test_stop_sequence_validation(value):
    with pytest.raises(ValueError, match='stop_sequences'):
        GenerationConfig(stop_sequences=value)


@pytest.mark.parametrize('protocol', ['openai.chat', 'openai.responses', 'anthropic', 'gemini'])
def test_sampling_controls_reach_actual_payload_and_receipt(protocol):
    config = GenerationConfig(top_p=0, stop_sequences=[] if protocol == 'openai.responses' else ['\nEND', ' STOP '])
    settings = Settings(api_key='key', default_model='custom')
    if protocol.startswith('openai'):
        provider = OpenAIProvider(settings)
        payload = (provider._build_chat_request_kwargs(provider._build_messages(PROMPT), config)
                   if protocol.endswith('chat') else provider._build_responses_request_kwargs(PROMPT, config))
        assert payload['top_p'] == 0
        if config.stop_sequences:
            assert payload['stop'] == config.stop_sequences
    elif protocol == 'anthropic':
        provider = AnthropicProvider(settings)
        _, payload = provider._build_message_request(PROMPT, config)
        assert payload['top_p'] == 0 and payload['stop_sequences'] == config.stop_sequences
    else:
        provider = GeminiProvider(settings)
        payload = provider._build_payload(PROMPT, config)
        assert payload['generationConfig']['topP'] == 0
        assert payload['generationConfig']['stopSequences'] == config.stop_sequences
    assert provider.last_request_receipt['top_p'] == 0
    assert provider.last_request_receipt['stop_sequences'] == (config.stop_sequences or None)


def test_sampling_request_overrides_profile_defaults_including_zero_and_empty_stops():
    provider = SimpleNamespace(settings=Settings(default_model='custom', top_p=0.9, stop_sequences=['profile stop']))
    inherited = DynamicLLMService._merge_config(GenerationConfig(), provider)
    assert inherited.top_p == 0.9 and inherited.stop_sequences == ['profile stop']
    overridden = DynamicLLMService._merge_config(GenerationConfig(top_p=0, stop_sequences=[]), provider)
    assert overridden.top_p == 0 and overridden.stop_sequences == []
    profile = LLMProfile(id='p', name='p', model='custom', top_p=0.5, stop_sequences=['x'])
    assert _make_cache_key(profile) != _make_cache_key(profile.model_copy(update={'top_p': 0}))
    assert _make_cache_key(profile) != _make_cache_key(profile.model_copy(update={'stop_sequences': []}))


@pytest.mark.asyncio
async def test_auto_stops_select_chat_but_explicit_responses_rejects():
    provider = OpenAIProvider(Settings(api_key='key', default_model='custom'))
    provider.async_client.chat.completions.create = AsyncMock(return_value=SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content='正文'), finish_reason='stop')], usage=None))
    provider.async_client.responses.create = AsyncMock()
    result = await provider.generate(PROMPT, GenerationConfig(stop_sequences=['END']))
    assert result.request_receipt['endpoint'] == 'openai.chat'
    assert provider.async_client.chat.completions.create.await_args.kwargs['stop'] == ['END']
    provider.async_client.responses.create.assert_not_called()
    provider._force_responses = True
    with pytest.raises(ValueError, match='stop_sequences'):
        await provider.generate(PROMPT, GenerationConfig(stop_sequences=['END']))
    await provider.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('terminal', [None, 'length', 'stop'])
async def test_chat_stream_requires_normal_terminal_even_after_text(terminal):
    provider = OpenAIProvider(Settings(api_key='key', default_model='custom', endpoint_kind='chat'))
    async def stream():
        yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content='partial'), finish_reason=None)])
        if terminal:
            yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=None), finish_reason=terminal)])
    provider.async_client.chat.completions.create = AsyncMock(return_value=stream())
    if terminal == 'stop':
        assert [part async for part in provider.stream_generate(PROMPT, GenerationConfig())] == ['partial']
    else:
        with pytest.raises(RuntimeError, match='finish'):
            _ = [part async for part in provider.stream_generate(PROMPT, GenerationConfig())]
    await provider.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol', ['anthropic', 'gemini'])
@pytest.mark.parametrize('terminal', [None, 'truncated', 'complete'])
async def test_native_sse_requires_normal_terminal_after_text(protocol, terminal):
    if protocol == 'anthropic':
        provider = AnthropicProvider(Settings(api_key='key', default_model='custom'))
        events = [{'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': 'partial'}}]
        if terminal == 'truncated':
            events += [{'type': 'message_delta', 'delta': {'stop_reason': 'max_tokens'}}]
        elif terminal == 'complete':
            events += [{'type': 'message_stop'}]
        client_name = '_stream_http_client'
        method = provider._stream_via_httpx
    else:
        provider = GeminiProvider(Settings(api_key='key', default_model='custom'))
        events = [{'candidates': [{'content': {'parts': [{'text': 'partial'}]}}]}]
        if terminal:
            events += [{'candidates': [{'finishReason': 'STOP' if terminal == 'complete' else 'MAX_TOKENS'}]}]
        client_name = '_http_client'
        method = provider.stream_generate
    await getattr(provider, client_name).aclose()
    body = ''.join('data: ' + json.dumps(event) + '\n\n' for event in events)
    setattr(provider, client_name, httpx.AsyncClient(transport=httpx.MockTransport(lambda request:
        httpx.Response(200, text=body, headers={'content-type': 'text/event-stream'}))))
    if terminal == 'complete':
        assert [part async for part in method(PROMPT, GenerationConfig())] == ['partial']
    else:
        with pytest.raises(RuntimeError, match='finish|message_stop'):
            _ = [part async for part in method(PROMPT, GenerationConfig())]
    await provider.aclose()


@pytest.mark.asyncio
async def test_responses_partial_stream_cannot_fallback_or_claim_success_without_completion():
    provider = OpenAIProvider(Settings(api_key='key', default_model='custom'))
    async def stream():
        yield SimpleNamespace(type='response.output_text.delta', delta='partial')
        raise RuntimeError('404 Not Found')
    provider.async_client.responses.create = AsyncMock(return_value=stream())
    provider.async_client.chat.completions.create = AsyncMock()
    with pytest.raises(RuntimeError, match='404'):
        _ = [part async for part in provider.stream_generate(PROMPT, GenerationConfig())]
    provider.async_client.chat.completions.create.assert_not_called()
    async def truncated():
        yield SimpleNamespace(type='response.output_text.delta', delta='partial')
    provider.async_client.responses.create = AsyncMock(return_value=truncated())
    with pytest.raises(RuntimeError, match='response.completed'):
        _ = [part async for part in provider.stream_generate(PROMPT, GenerationConfig())]
    await provider.aclose()


@pytest.mark.parametrize('budget', [0, 1024, 2048])
def test_anthropic_thinking_mapping_is_identical_for_stream_and_nonstream(budget):
    provider = AnthropicProvider(Settings(api_key='key', default_model='custom'))
    config = GenerationConfig(max_tokens=4096, thinking_budget_tokens=budget)
    _, normal = provider._build_message_request(PROMPT, config)
    with capture_usage() as capture:
        _, streaming = provider._build_message_request(PROMPT, config, stream=True)
        assert capture.metadata['request_receipt']['endpoint'] == 'anthropic.messages'
    assert {key: value for key, value in streaming.items() if key != 'stream'} == normal
    assert normal['thinking'] == ({'type': 'disabled'} if budget == 0 else {'type': 'enabled', 'budget_tokens': budget})
    assert ('temperature' in normal) == (budget == 0)


@pytest.mark.parametrize('budget', [1, 1023, 4096, 8192])
def test_invalid_anthropic_budget_fails_before_http(budget):
    provider = AnthropicProvider(Settings(api_key='key', default_model='custom'))
    with pytest.raises(ValueError, match='at least 1024'):
        provider._build_message_request(PROMPT, GenerationConfig(max_tokens=4096, thinking_budget_tokens=budget))


@pytest.mark.parametrize('budget', [0, 1024])
def test_gemini_thinking_config_and_receipt_include_explicit_off(budget):
    provider = GeminiProvider(Settings(api_key='key', default_model='custom'))
    with capture_usage() as capture:
        payload = provider._build_payload(PROMPT, GenerationConfig(thinking_budget_tokens=budget))
        assert payload['generationConfig']['thinkingConfig']['thinkingBudget'] == budget
        assert capture.metadata['request_receipt']['thinking_budget_tokens'] == budget


def test_request_effort_overrides_profile_budget_and_cache_changes_when_controls_change():
    provider = SimpleNamespace(settings=Settings(default_model='custom', thinking_budget_tokens=1024))
    assert DynamicLLMService._merge_config(GenerationConfig(), provider).thinking_budget_tokens == 1024
    explicit_off = DynamicLLMService._merge_config(GenerationConfig(reasoning_effort='none'), provider)
    assert explicit_off.reasoning_effort == 'none' and explicit_off.thinking_budget_tokens is None
    profile = LLMProfile(id='p', name='p', model='custom', reasoning_effort='low')
    assert _make_cache_key(profile) != _make_cache_key(profile.model_copy(update={'reasoning_effort': 'high'}))
    assert _make_cache_key(profile) != _make_cache_key(profile.model_copy(update={'endpoint_kind': 'chat'}))


@pytest.mark.asyncio
async def test_explicit_no_auth_reaches_local_openai_endpoint_without_bearer_header():
    provider = OpenAIProvider(Settings(default_model='custom', base_url='http://localhost:1234/v1', auth_mode='none', endpoint_kind='chat'))
    received = []
    async def handler(request):
        received.append(request)
        return httpx.Response(200, json={'id': 'response', 'object': 'chat.completion', 'created': 0, 'model': 'custom',
            'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': '正文'}, 'finish_reason': 'stop'}]})
    await provider._http_client.aclose()
    provider._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), event_hooks={'request': [provider._strip_auth_header]})
    provider.async_client._client = provider._http_client
    result = await provider.generate(PROMPT, GenerationConfig())
    assert result.content == '正文'
    assert 'authorization' not in received[0].headers
    assert result.request_receipt['endpoint'] == 'openai.chat'
    await provider.aclose()
