"""Real SQLite/writer accounting with provider-wire usage and budget races."""
import asyncio
import json
from unittest.mock import AsyncMock
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import httpx
import pytest

from application.ai.llm_control_service import LLMControlConfig, LLMControlService, LLMProfile
from application.ai.usage_context import capture_usage, record_stream_usage
from application.ai_invocation.dtos import InvocationPolicy, InvocationSession
from application.ai_invocation.services import AttemptService
from domain.ai.services.llm_service import GenerationConfig, GenerationResult
from domain.ai.value_objects.prompt import Prompt
from domain.ai.value_objects.token_usage import TokenUsage
from infrastructure.ai.config.settings import Settings
from infrastructure.ai.provider_factory import DynamicLLMService, LLMProviderFactory
from infrastructure.ai.providers.anthropic_provider import AnthropicProvider
from infrastructure.ai.providers.gemini_provider import GeminiProvider
from infrastructure.ai.providers.openai_provider import OpenAIProvider
from infrastructure.ai.usage_ledger import BudgetExceededError, UsageLedger
from infrastructure.persistence.database.sqlite_ai_invocation_repository import SqliteInvocationAttemptRepository
from tests.unit.application.ai_invocation.test_ai_invocation_persistence import _Db, _snapshot
from tests.unit.application.engine.test_persistence_commit_ack import writer


def settings(**kwargs):
    return Settings(default_model='custom-model', protocol='openai', profile_id='p',
        input_price_per_million='1', output_price_per_million='2', **kwargs)


def reserve(ledger, book='book', **kwargs):
    return ledger.reserve(novel_id=book, attempt_id='', settings=kwargs.pop('settings', settings()),
        model='custom-model', input_bound=100, max_tokens=100, **kwargs)


def test_budget_reservation_is_atomic_under_competing_calls_and_reopens(writer):
    _, db = writer
    ledger = UsageLedger(db)
    ledger.set_budget('book', currency='USD', limit='0.0003')
    def call():
        try:
            return reserve(ledger)
        except BudgetExceededError:
            return None
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: call(), range(2)))
    assert sum(result is not None for result in results) == 1
    summary = UsageLedger(db).summary('book')
    assert summary['calls'] == 1
    assert summary['currencies'][0]['total_cost'] is None
    assert summary['currencies'][0]['reserved_or_actual_cost'] == '0.0003'
    ledger.finish(next(result for result in results if result), usage=TokenUsage(10, 10), status='succeeded')
    assert UsageLedger(db).summary('book')['currencies'][0]['total_cost'] == '0.00003'
    with pytest.raises(BudgetExceededError):
        reserve(ledger)
    assert reserve(ledger, 'different-book')


def test_unknown_usage_and_missing_price_never_become_zero_cost(writer):
    _, db = writer
    ledger = UsageLedger(db)
    request = reserve(ledger)
    ledger.finish(request, usage=None, status='failed')
    item = UsageLedger(db).summary('book')['currencies'][0]
    assert item['total_cost'] is None and item['total_tokens'] is None
    assert item['unknown_usage_calls'] == 1
    assert item['reserved_or_actual_cost'] == '0.0003'
    ledger.set_budget('book', currency='USD', limit='0.0003')
    with pytest.raises(BudgetExceededError):
        reserve(ledger)


def test_bill_reconciliation_preserves_unknown_tokens_and_audits_once(writer):
    _, db = writer
    ledger = UsageLedger(db)
    request = reserve(ledger)
    with pytest.raises(ValueError, match='已结束'):
        ledger.reconcile(request, cost='0.0001', reason='provider billing id 123')
    ledger.finish(request, usage=None, status='failed')
    def reconcile():
        try:
            return ledger.reconcile(request, cost='0.0001', reason='provider billing id 123')
        except ValueError:
            return None
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(lambda _: reconcile(), range(2)))
    assert sum(response is not None for response in responses) == 1
    item = ledger.summary('book')['currencies'][0]
    assert item['total_cost'] == '0.0001' and item['total_tokens'] is None
    assert len(db.fetch_all('SELECT * FROM ai_usage_reconciliations')) == 1
    ledger.set_budget('book', currency='USD', limit='0.001')
    assert reserve(ledger)
    ledger.set_budget('unpriced', currency='USD', limit='10')
    with pytest.raises(BudgetExceededError):
        reserve(ledger, 'unpriced', settings=Settings(protocol='openai'))
    ledger.set_budget('book', currency='CNY', limit='100')
    with pytest.raises(BudgetExceededError):
        reserve(ledger)


@pytest.mark.asyncio
async def test_stream_capture_is_concurrent_and_attempt_usage_persists():
    class Streaming:
        async def stream_generate(self, prompt, config):
            record_stream_usage({'input_tokens': len(prompt.user)})
            await asyncio.sleep(0)
            yield prompt.user
            record_stream_usage({'output_tokens': config.max_tokens})
    async def run(book, text, maximum):
        snapshot = _snapshot()
        snapshot = __import__('dataclasses').replace(snapshot, prompt=Prompt(system='s', user=text))
        session = InvocationSession(id=book, operation='write', node_key='n', policy=InvocationPolicy.DIRECT,
            context={'novel_id': book})
        return await AttemptService(Streaming()).generate_streaming(session=session, prompt_snapshot=snapshot,
            config=GenerationConfig(max_tokens=maximum))
    one, two = await asyncio.gather(run('one', 'a', 3), run('two', 'long', 7))
    assert one.token_usage == TokenUsage(1, 3)
    assert two.token_usage == TokenUsage(4, 7)
    repository = SqliteInvocationAttemptRepository(_Db())
    one.accounting = {'currency': 'USD', 'cost': '0.25', 'effective_max_tokens': 3}
    repository.save(one)
    loaded = repository.get(one.id)
    assert loaded.token_usage == one.token_usage and loaded.accounting == one.accounting
    two.token_usage = None
    repository.save(two)
    assert repository.get(two.id).token_usage is None


@pytest.mark.asyncio
async def test_dynamic_stream_records_wire_usage_and_failed_partial_remains_unknown(writer):
    _, db = writer
    class Provider:
        settings = settings()
        async def stream_generate(self, prompt, config):
            yield 'text'
            record_stream_usage({'input_tokens': 10, 'output_tokens': 20})
            if prompt.user == 'break':
                raise RuntimeError('disconnected')
    service = DynamicLLMService(usage_ledger=UsageLedger(db))
    provider = Provider()
    async def resolve():
        return provider
    service._resolve_provider = resolve
    with capture_usage(novel_id='book', attempt_id='attempt') as capture:
        assert [chunk async for chunk in service.stream_generate(Prompt(system='s', user='ok'), GenerationConfig(max_tokens=32))] == ['text']
        assert capture.token_usage == TokenUsage(10, 20)
        assert capture.metadata['cost'] == '0.00005'
        assert capture.metadata['effective_max_tokens'] == 32
    with capture_usage(novel_id='book') as capture:
        with pytest.raises(RuntimeError, match='disconnected'):
            _ = [chunk async for chunk in service.stream_generate(Prompt(system='s', user='break'), GenerationConfig(max_tokens=32))]
        assert capture.token_usage is None and capture.metadata['cost'] is None
    entries = UsageLedger(db).entries('book')
    assert {row['status'] for row in entries} == {'failed', 'succeeded'}


@pytest.mark.asyncio
async def test_anthropic_and_gemini_wire_usage_and_partial_failure():
    anthropic = AnthropicProvider(Settings(api_key='test', default_model='custom'))
    gemini = GeminiProvider(Settings(api_key='test', default_model='custom'))
    with capture_usage() as capture:
        anthropic._parse_sse_event('data: ' + json.dumps({'type': 'message_start', 'message': {'usage': {'input_tokens': 12, 'output_tokens': 0}}}))
        assert capture.token_usage is None
        anthropic._parse_sse_event('data: ' + json.dumps({'type': 'message_delta', 'usage': {'output_tokens': 9}}))
        assert capture.token_usage == TokenUsage(12, 9)
    with capture_usage() as capture:
        gemini._parse_sse_event('data: ' + json.dumps({'usageMetadata': {'promptTokenCount': 8, 'candidatesTokenCount': 11}}))
        assert capture.token_usage == TokenUsage(8, 11)
    async def broken(*args):
        yield 'partial'
        raise httpx.ReadError('disconnected')
    anthropic._stream_via_httpx = broken
    with pytest.raises(RuntimeError, match='partial output'):
        _ = [chunk async for chunk in anthropic.stream_generate(Prompt(system='s', user='u'), GenerationConfig(max_tokens=32))]
    await anthropic.aclose()
    await gemini.aclose()


@pytest.mark.asyncio
async def test_openai_responses_uses_native_usage_and_missing_usage_is_unknown():
    provider = OpenAIProvider(Settings(api_key='test', default_model='custom'))
    async def create(**kwargs):
        return SimpleNamespace(status='completed', output=[SimpleNamespace(type='message', content=[SimpleNamespace(type='output_text', text='result')])],
            usage=SimpleNamespace(input_tokens=13, output_tokens=17))
    provider.async_client.responses.create = create
    result = await provider.generate(Prompt(system='s', user='u'), GenerationConfig(max_tokens=32))
    assert result.token_usage == TokenUsage(13, 17)
    async def missing(**kwargs):
        result = await create(**kwargs)
        result.usage = None
        return result
    provider.async_client.responses.create = missing
    assert (await provider.generate(Prompt(system='s', user='u'), GenerationConfig(max_tokens=32))).token_usage is None
    await provider.aclose()


def test_profile_pricing_and_capabilities_roundtrip_preserve_small_output(writer):
    _, db = writer
    service = LLMControlService()
    service._db = lambda: db
    profile = LLMProfile(id='p', name='p', model='custom', max_tokens=512,
        max_output_tokens_limit=1024, context_window_limit=4096,
        input_price_per_million='0.8', output_price_per_million='2.5', currency='CNY')
    service.save_config(LLMControlConfig(profiles=[profile]))
    restored = service.get_config().profiles[0]
    assert restored.max_tokens == 512 and restored.input_price_per_million == '0.8'
    settings_value = LLMProviderFactory(service)._profile_to_settings(restored)
    assert settings_value.context_window_limit == 4096 and settings_value.currency == 'CNY'
    assert isinstance(LLMProviderFactory(service).create_from_profile_id('p'), DynamicLLMService)
    with pytest.raises(ValueError, match='output limit'):
        service.save_config(LLMControlConfig(profiles=[profile.model_copy(update={'max_tokens': 2048})]))


@pytest.mark.asyncio
async def test_nonstream_empty_fallback_bills_all_known_responses_and_respects_budget(writer):
    _, db = writer
    ledger = UsageLedger(db)
    provider = OpenAIProvider(settings(api_key='test', use_legacy_chat_completions=True))
    class Stream:
        def __aiter__(self):
            return self
        async def __anext__(self):
            if getattr(self, 'done', False):
                raise StopAsyncIteration
            self.done = True
            return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content='text'), finish_reason='stop')],
                usage=SimpleNamespace(prompt_tokens=20, completion_tokens=10))
    async def create(**kwargs):
        if kwargs.get('stream'):
            return Stream()
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=''), finish_reason='stop')],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=0))
    provider.async_client.chat.completions.create = AsyncMock(side_effect=create)
    service = DynamicLLMService(usage_ledger=ledger)
    async def resolve():
        return provider
    service._resolve_provider = resolve
    with capture_usage(novel_id='book'):
        result = await service.generate(Prompt(system='s', user='u'), GenerationConfig(max_tokens=32))
        assert result.token_usage == TokenUsage(30, 10)
        assert result.accounting['cost'] == '0.00005'
    ledger.set_budget('budgeted', currency='USD', limit='1')
    with capture_usage(novel_id='budgeted'):
        with pytest.raises(RuntimeError, match='fallback disabled'):
            await service.generate(Prompt(system='s', user='u'), GenerationConfig(max_tokens=32))
    assert ledger.summary('budgeted')['currencies'][0]['total_cost'] == '0.00001'
    await provider.aclose()
