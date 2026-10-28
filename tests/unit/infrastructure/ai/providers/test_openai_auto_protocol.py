"""Offline protocol negotiation through the real OpenAI SDK and HTTP decoder."""
import asyncio
import json
from contextlib import asynccontextmanager

import httpx
import pytest
from openai import AsyncOpenAI

from application.ai.usage_context import capture_usage
from domain.ai.services.llm_service import GenerationConfig
from domain.ai.value_objects.prompt import Prompt
from infrastructure.ai.config.settings import Settings
from infrastructure.ai.providers.openai_provider import OpenAIProvider


PROMPT = Prompt(system='Write the scene', user='A light in the mist')
CONFIG = GenerationConfig(model='custom-model', max_tokens=128, reasoning_effort='high',
                          response_format={'type': 'json_object'})


@pytest.fixture(autouse=True)
def isolated_endpoint_cache(monkeypatch):
    monkeypatch.setattr(OpenAIProvider, '_fallback_to_chat_cache', set())


@asynccontextmanager
async def offline_provider(handler, **options):
    settings = Settings(api_key='offline-credential', base_url='https://offline.invalid/v1',
                        default_model='custom-model', **options)
    provider = OpenAIProvider(settings)
    await provider.async_client.close()
    provider.async_client = AsyncOpenAI(api_key=settings.api_key, base_url=settings.base_url,
        default_headers=settings.extra_headers or None, default_query=settings.extra_query or None,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), max_retries=0)
    try:
        yield provider
    finally:
        await provider.aclose()


def chat_reply(streaming):
    if streaming:
        chunks = [
            {'id': 'chat-test', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'custom-model',
             'choices': [{'index': 0, 'delta': {'content': '{"scene":"light"}'}, 'finish_reason': None}]},
            {'id': 'chat-test', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'custom-model',
             'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}],
             'usage': {'prompt_tokens': 3, 'completion_tokens': 4, 'total_tokens': 7}},
        ]
        return httpx.Response(200, headers={'content-type': 'text/event-stream'},
            text=''.join('data: ' + json.dumps(chunk) + '\n\n' for chunk in chunks) + 'data: [DONE]\n\n')
    return httpx.Response(200, json={'id': 'chat-test', 'object': 'chat.completion', 'created': 1,
        'model': 'custom-model', 'choices': [{'index': 0, 'message': {'role': 'assistant',
            'content': '{"scene":"light"}'}, 'finish_reason': 'stop'}],
        'usage': {'prompt_tokens': 3, 'completion_tokens': 4, 'total_tokens': 7}})


async def generate(provider, streaming):
    with capture_usage() as usage:
        if streaming:
            content = ''.join([part async for part in provider.stream_generate(PROMPT, CONFIG)])
            tokens = usage.token_usage
        else:
            result = await provider.generate(PROMPT, CONFIG)
            content, tokens = result.content, result.token_usage
    return content, tokens


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('status,message', [
    (404, 'Not Found'), (405, 'Method Not Allowed'), (501, 'Not Implemented'),
    (400, 'Responses API is not supported'),
    (400, 'This endpoint does not support the Responses API'),
    (400, 'Unsupported endpoint: /v1/responses'),
])
async def test_auto_falls_back_only_for_unsupported_endpoint_and_preserves_request(status, message, streaming):
    requests = []

    async def serve(request):
        requests.append(request)
        if request.url.path == '/v1/responses':
            return httpx.Response(status, json={'error': {'message': message}})
        return chat_reply(streaming)

    async with offline_provider(serve, extra_headers={'x-provider-option': 'retained'},
                                extra_query={'api-version': 'offline'}) as provider:
        for _ in range(2):
            content, usage = await generate(provider, streaming)
            assert content == '{"scene":"light"}'
            assert (usage.input_tokens, usage.output_tokens) == (3, 4)
            assert provider.last_request_receipt['endpoint'] == 'openai.chat'
            assert provider.last_request_receipt['reasoning_effort'] == 'high'
            assert provider.last_request_receipt['response_format_type'] == 'json_object'

    assert [request.url.path for request in requests] == [
        '/v1/responses', '/v1/chat/completions', '/v1/chat/completions']
    for request in requests:
        assert request.headers['authorization'] == 'Bearer offline-credential'
        assert request.headers['x-provider-option'] == 'retained'
        assert request.url.params['api-version'] == 'offline'
        payload = json.loads(request.content)
        assert payload['model'] == 'custom-model'
        assert 'temperature' not in payload
        if request.url.path == '/v1/responses':
            assert payload['reasoning'] == {'effort': 'high'}
            assert payload['max_output_tokens'] == 128
            assert payload['text']['format'] == {'type': 'json_object'}
        else:
            assert payload['reasoning_effort'] == 'high'
            assert payload['max_completion_tokens'] == 128
            assert payload['response_format'] == {'type': 'json_object'}
            if streaming:
                assert payload['stream_options']['include_usage'] is True


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('status,message,code', [
    (401, 'Responses API unsupported; upstream returned 404', 'invalid_api_key'),
    (403, 'Responses API unsupported; upstream returned 404', 'permission_denied'),
    (429, 'Responses API unsupported; upstream returned 404', 'rate_limit_exceeded'),
    (502, 'Responses API unsupported; upstream returned 404', 'bad_gateway'),
    (400, 'Referenced document not found', 'invalid_request'),
    (400, 'Unsupported parameter: reasoning_effort for /responses', 'unsupported_parameter'),
    (404, 'No such model', 'model_not_found'),
])
async def test_auto_does_not_hide_auth_transient_model_or_parameter_errors(status, message, code, streaming):
    paths = []

    async def serve(request):
        paths.append(request.url.path)
        if request.url.path == '/v1/responses':
            return httpx.Response(status, json={'error': {'message': message, 'code': code}})
        return chat_reply(streaming)

    async with offline_provider(serve) as provider:
        with pytest.raises(RuntimeError, match=message.split(';')[0]):
            await generate(provider, streaming)
        assert not provider._fallback_to_chat_cache
    assert paths == ['/v1/responses']


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('error', [httpx.ReadTimeout('Responses API unsupported; 404'),
                                  asyncio.CancelledError()])
async def test_auto_never_falls_back_for_transport_timeout_or_cancellation(streaming, error):
    paths = []

    async def serve(request):
        paths.append(request.url.path)
        raise error

    async with offline_provider(serve) as provider:
        with pytest.raises(asyncio.CancelledError if isinstance(error, asyncio.CancelledError) else RuntimeError):
            await generate(provider, streaming)
        assert not provider._fallback_to_chat_cache
    assert paths == ['/v1/responses']


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('endpoint,legacy,expected', [
    ('responses', False, '/v1/responses'), ('chat', False, '/v1/chat/completions'),
    ('auto', True, '/v1/chat/completions'),
])
async def test_explicit_and_legacy_protocols_remain_fixed(endpoint, legacy, expected, streaming):
    paths = []

    async def serve(request):
        paths.append(request.url.path)
        return httpx.Response(405, json={'error': {'message': 'Responses API unsupported'}})

    async with offline_provider(serve, endpoint_kind=endpoint,
                                use_legacy_chat_completions=legacy) as provider:
        with pytest.raises(RuntimeError):
            await generate(provider, streaming)
    assert paths == [expected]


@pytest.mark.asyncio
@pytest.mark.parametrize('after_text', [False, True])
async def test_stream_errors_after_opening_response_are_never_hidden_by_protocol_switch(after_text):
    paths = []
    events = []
    if after_text:
        events.append({'type': 'response.output_text.delta', 'delta': 'partial',
                       'output_index': 0, 'content_index': 0, 'item_id': 'msg-test'})
    events.append({'type': 'error', 'code': 'unsupported_endpoint',
                   'message': 'Responses API unsupported', 'param': None})

    async def serve(request):
        paths.append(request.url.path)
        return httpx.Response(200, headers={'content-type': 'text/event-stream'},
            text=''.join('event: ' + event['type'] + '\ndata: ' + json.dumps(event) + '\n\n' for event in events))

    async with offline_provider(serve) as provider:
        pieces = []
        with pytest.raises(RuntimeError):
            async for piece in provider.stream_generate(PROMPT, CONFIG):
                pieces.append(piece)
        assert pieces == (['partial'] if after_text else [])
        assert not provider._fallback_to_chat_cache
    assert paths == ['/v1/responses']


@pytest.mark.asyncio
async def test_arbitrary_404_message_is_not_endpoint_evidence():
    async with offline_provider(lambda request: chat_reply(False)) as provider:
        assert provider._may_fallback(RuntimeError('404 records were not found')) is False


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
async def test_gateway_extension_body_uses_sdk_extra_body_and_survives_auto_fallback(streaming):
    payloads = []

    async def serve(request):
        payloads.append(json.loads(request.content))
        if request.url.path == '/v1/responses':
            return httpx.Response(404, json={'error': {'message': 'Not Found'}})
        return chat_reply(streaming)

    async with offline_provider(serve, extra_body={'vendor_option': 'retained'}) as provider:
        content, _ = await generate(provider, streaming)
        assert content == '{"scene":"light"}'
    assert len(payloads) == 2
    assert all(payload['vendor_option'] == 'retained' for payload in payloads)
