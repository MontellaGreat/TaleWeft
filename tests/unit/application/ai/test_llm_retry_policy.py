"""Server cooldowns and HTTP status take precedence over error wording."""
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx
import openai
import pytest

from application.ai.llm_retry_policy import is_retryable_llm_error, llm_error_status, llm_retry_delay_seconds


def error(status=524, body=None, headers=None):
    response = httpx.Response(status, headers=headers, request=httpx.Request('POST', 'https://offline.invalid'))
    return openai.APIStatusError('timeout from upstream', response=response, body=body)


@pytest.mark.parametrize('headers,body,expected', [
    ({'Retry-After': '120'}, {}, 120),
    ({'Retry-After-Ms': '2500'}, {}, 2.5),
    ({}, {'retry_after': 120}, 120),
    ({}, {'error': {'retry_after': 300}}, 300),
    ({'Retry-After': '60'}, {'retry_after': 120}, 120),
    ({'Retry-After': 'NaN'}, {'retry_after': True}, 120),
    ({'Retry-After': '-3'}, {'retry_after': float('inf')}, 120),
])
def test_retry_delay_respects_real_headers_json_and_rejects_invalid_values(headers, body, expected):
    assert llm_retry_delay_seconds(error(body=body, headers=headers)) == expected


def test_retry_after_http_date_and_wrapped_original_error():
    upstream = error(headers={'Retry-After': format_datetime(datetime.now(timezone.utc) + timedelta(seconds=90))})
    wrapper = RuntimeError('Failed to generate text')
    wrapper.__cause__ = upstream
    assert 88 < llm_retry_delay_seconds(wrapper) <= 90
    assert llm_error_status(wrapper) == 524
    assert is_retryable_llm_error(wrapper)


@pytest.mark.parametrize('status', [400, 401, 403, 404, 422])
def test_permanent_http_status_wins_over_timeout_in_message(status):
    assert not is_retryable_llm_error(error(status))


def test_explicit_no_retry_and_unstructured_gateway_timeout():
    assert not is_retryable_llm_error(error(headers={'x-should-retry': 'false'}))
    assert not is_retryable_llm_error(error(body={'retryable': False}))
    assert is_retryable_llm_error('Failed to generate text: Error code: 524')
    assert llm_retry_delay_seconds('Error code: 524') == 120


@pytest.mark.asyncio
@pytest.mark.parametrize('cooldown,expected_calls', [(120, 2), (300, 1)])
async def test_structured_json_pipeline_honors_server_cooldown(monkeypatch, cooldown, expected_calls):
    from pydantic import BaseModel
    from application.ai.structured_json_pipeline import structured_json_generate_with_status
    from domain.ai.services.llm_service import GenerationConfig, GenerationResult
    from domain.ai.value_objects.prompt import Prompt

    class Ready(BaseModel):
        ready: bool

    class Model:
        calls = 0
        async def generate(self, *_):
            self.calls += 1
            if self.calls == 1:
                raise error(body={'retry_after': cooldown})
            return GenerationResult('{"ready":true}', None)

    waits = []
    async def wait(seconds):
        waits.append(seconds)
    monkeypatch.setattr('application.ai.structured_json_pipeline.asyncio.sleep', wait)
    model = Model()
    result = await structured_json_generate_with_status(model, Prompt(system='Return JSON', user='ready'), GenerationConfig(), Ready)
    assert model.calls == expected_calls
    assert waits == ([120] if cooldown == 120 else [])
    assert result.ok is (cooldown == 120)


@pytest.mark.asyncio
async def test_chat_provider_honors_524_cooldown_without_repeating_early(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from domain.ai.services.llm_service import GenerationConfig
    from domain.ai.value_objects.prompt import Prompt
    from infrastructure.ai.config.settings import Settings
    from infrastructure.ai.providers.openai_provider import OpenAIProvider

    provider = OpenAIProvider(Settings(api_key='offline-key', use_legacy_chat_completions=True))
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='complete'), finish_reason='stop')], usage=None)
    create = AsyncMock(side_effect=[error(body={'retry_after': 120}), response])
    monkeypatch.setattr(provider.async_client.chat.completions, 'create', create)
    waits = []
    async def wait(seconds):
        waits.append(seconds)
    monkeypatch.setattr('infrastructure.ai.providers.openai_provider.asyncio.sleep', wait)
    try:
        result = await provider.generate(Prompt(system='Test', user='Return complete'), GenerationConfig(model='offline-model'))
        assert result.content == 'complete'
        assert create.await_count == 2
        assert waits == [120]
    finally:
        await provider.aclose()
