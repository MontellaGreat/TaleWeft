"""Authenticated HTTP capability/budget contract against the real SQLite writer."""
import httpx
import pytest
from fastapi import FastAPI

from application.ai.llm_control_service import LLMControlConfig, LLMProfile
from application.ai.usage_context import record_stream_usage
from application.ai_invocation.dtos import InvocationAttempt, InvocationAttemptStatus, InvocationPolicy, InvocationSession
from domain.ai.value_objects.token_usage import TokenUsage
from infrastructure.ai.usage_ledger import UsageLedger
from infrastructure.persistence.database.sqlite_ai_invocation_repository import SqliteInvocationAttemptRepository, SqliteInvocationSessionRepository
from interfaces.api.middleware.session_auth import SessionAuthority, SessionAuthMiddleware
from interfaces.api.v1.workbench import llm_control
from interfaces.api.v1.engine import ai_invocation_routes
from tests.unit.application.ai_invocation.test_ai_invocation_persistence import _snapshot
from tests.unit.application.engine.test_persistence_commit_ack import writer

TOKEN = 'model-usage-session-0123456789abcdef'
AUTH = {'Authorization': f'Bearer {TOKEN}'}


@pytest.fixture
def app(writer, monkeypatch):
    _, db = writer
    monkeypatch.setattr(llm_control._service, '_db', lambda: db)
    instance = FastAPI()
    instance.include_router(llm_control.router, prefix='/api/v1')
    instance.add_middleware(SessionAuthMiddleware, authority=SessionAuthority(TOKEN))
    return instance, db


@pytest.mark.asyncio
async def test_book_budget_http_reopens_and_invalid_policy_does_not_save(app):
    instance, db = app
    ledger = UsageLedger(db)
    from infrastructure.ai.config.settings import Settings
    request = ledger.reserve(novel_id='book', attempt_id='', settings=Settings(protocol='openai',
        input_price_per_million='1', output_price_per_million='2'), model='m', input_bound=10, max_tokens=20)
    ledger.finish(request, usage=TokenUsage(10, 20), status='succeeded')
    path = '/api/v1/llm-control/usage/novels/book'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=instance), base_url='http://127.0.0.1') as client:
        assert (await client.get(path)).status_code == 401
        summary = await client.get(path, headers=AUTH)
        assert summary.status_code == 200
        assert summary.json()['currencies'][0]['total_cost'] == '0.00005'
        saved = await client.put(path + '/budget', headers=AUTH, json={'currency': 'USD', 'limit': '1.25'})
        assert saved.status_code == 200
        assert UsageLedger(db).summary('book')['budget']['limit'] == '1.25'
        for value in ['-1', 'NaN', 'Infinity', 'oops']:
            assert (await client.put(path + '/budget', headers=AUTH, json={'currency': 'USD', 'limit': value})).status_code == 422
        assert UsageLedger(db).summary('book')['budget']['limit'] == '1.25'
        assert (await client.get(path + '/entries', headers=AUTH)).json()[0]['input_tokens'] == 10


@pytest.mark.asyncio
async def test_capability_http_unknown_and_configured_limits_are_explicit(app):
    instance, _db = app
    profile = LLMProfile(id='custom', name='custom', model='unknown-model', max_tokens=256)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=instance), base_url='http://127.0.0.1') as client:
        path = '/api/v1/llm-control/capabilities'
        assert (await client.post(path, json=profile.model_dump())).status_code == 401
        response = await client.post(path, headers=AUTH, json=profile.model_dump())
        assert response.json()['max_output_tokens'] is None and response.json()['source'] == 'unknown'
        data = {**profile.model_dump(), 'max_output_tokens_limit': 128, 'context_window_limit': 1024}
        response = await client.post(path, headers=AUTH, json=data)
        assert response.json()['source'] == 'configured'
        assert response.json()['valid'] is False
        assert response.json()['effective_max_tokens'] == 256
        assert (await client.post(path, headers=AUTH, json={**data, 'context_window_limit': -1})).status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol,metadata,source,status', [
    ('gemini', {'inputTokenLimit': 8192, 'outputTokenLimit': 1024}, 'upstream_model_metadata', 'limits_observed'),
    ('openai', {'id': 'model', 'owned_by': 'provider'}, 'unknown', 'model_reachable_limits_unknown'),
    ('anthropic', {'inputTokenLimit': True, 'outputTokenLimit': -1}, 'unknown', 'model_reachable_limits_unknown'),
])
async def test_capability_probe_reads_real_http_metadata_without_generation(app, monkeypatch, protocol, metadata, source, status):
    from types import SimpleNamespace
    instance, _db = app
    original_client = httpx.AsyncClient
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=metadata)
    def upstream_client(**kwargs):
        return original_client(transport=httpx.MockTransport(handler), **kwargs)
    monkeypatch.setattr(llm_control, 'httpx', SimpleNamespace(AsyncClient=upstream_client, HTTPError=httpx.HTTPError))
    profile = LLMProfile(id='probe', name='probe', protocol=protocol, model='test-model', api_key='private-key',
                         base_url='https://models.invalid', max_tokens=256)
    async with original_client(transport=httpx.ASGITransport(app=instance), base_url='http://127.0.0.1') as client:
        response = await client.post('/api/v1/llm-control/capabilities/probe', headers=AUTH, json=profile.model_dump())
    assert response.status_code == 200, response.text
    assert response.json()['source'] == source and response.json()['probe_status'] == status
    assert len(requests) == 1 and requests[0].method == 'GET' and '/models/test-model' in requests[0].url.path
    if protocol == 'gemini':
        assert response.json()['max_output_tokens'] == 1024 and response.json()['context_window'] == 8192
        assert requests[0].headers['x-goog-api-key'] == 'private-key'
    assert 'private-key' not in response.text


@pytest.mark.asyncio
async def test_capability_probe_invalid_upstream_json_has_safe_error(app, monkeypatch):
    from types import SimpleNamespace
    instance, _db = app
    original_client = httpx.AsyncClient
    monkeypatch.setattr(llm_control, 'httpx', SimpleNamespace(HTTPError=httpx.HTTPError,
        AsyncClient=lambda **kwargs: original_client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=['private-key'])), **kwargs)))
    profile = LLMProfile(id='probe', name='probe', protocol='openai', model='test', api_key='private-key', base_url='https://models.invalid')
    async with original_client(transport=httpx.ASGITransport(app=instance), base_url='http://127.0.0.1') as client:
        response = await client.post('/api/v1/llm-control/capabilities/probe', headers=AUTH, json=profile.model_dump())
    assert response.status_code == 502 and 'private-key' not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol,model,base_url,expected_path', [
    ('anthropic', 'claude-test', 'https://api.anthropic.com/v1', '/v1/models/claude-test'),
    ('gemini', 'models/gemini-test', 'https://gateway.example/v1beta/models', '/v1beta/models/gemini-test'),
])
async def test_capability_probe_normalizes_version_and_model_prefix(
    app, monkeypatch, protocol, model, base_url, expected_path
):
    from types import SimpleNamespace
    instance, _db = app
    original_client = httpx.AsyncClient
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={'id': model})

    monkeypatch.setattr(llm_control, 'httpx', SimpleNamespace(
        AsyncClient=lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs),
        HTTPError=httpx.HTTPError,
    ))
    profile = LLMProfile(id='url-probe', name='URL probe', protocol=protocol,
                         model=model, api_key='private-key', base_url=base_url)
    async with original_client(transport=httpx.ASGITransport(app=instance), base_url='http://127.0.0.1') as client:
        response = await client.post('/api/v1/llm-control/capabilities/probe', headers=AUTH,
                                     json=profile.model_dump())

    assert response.status_code == 200, response.text
    assert len(requests) == 1
    assert requests[0].url.path == expected_path
    assert 'private-key' not in response.text


@pytest.mark.asyncio
async def test_streaming_api_worker_persists_usage_and_failed_partial(app, monkeypatch):
    _instance, db = app
    session_repo = SqliteInvocationSessionRepository(db)
    attempt_repo = SqliteInvocationAttemptRepository(db)
    session = InvocationSession(id='s', operation='write', node_key='n', policy=InvocationPolicy.REVIEW_AFTER_CALL,
        context={'novel_id': 'book'}, prompt_snapshot=_snapshot(), attempts=['a'])
    attempt = InvocationAttempt(id='a', session_id='s', status=InvocationAttemptStatus.RUNNING, prompt_snapshot=_snapshot())
    session_repo.save(session)
    attempt_repo.save(attempt)
    monkeypatch.setattr(ai_invocation_routes, '_repositories', lambda: {'session': session_repo, 'attempt': attempt_repo})
    monkeypatch.setattr(ai_invocation_routes, '_publish_autopilot_session_state', lambda _: None)
    class Stream:
        failed = False
        async def stream_generate(self, *_args):
            yield 'generated text'
            if self.failed:
                raise RuntimeError('wire disconnected')
            record_stream_usage({'input_tokens': 7, 'output_tokens': 9})
    stream = Stream()
    monkeypatch.setattr(ai_invocation_routes, 'get_llm_service', lambda: stream)
    await ai_invocation_routes._run_streaming_invocation_attempt(session_id='s', attempt_id='a', config=None)
    loaded = attempt_repo.get('a')
    assert loaded.token_usage == TokenUsage(7, 9)
    assert ai_invocation_routes._attempt_payload(loaded)['token_usage']['total_tokens'] == 16
    stream.failed = True
    # A retry is a new execution generation; reopen the durable attempt fixture
    # explicitly instead of silently reusing a succeeded attempt identity.
    loaded.status = InvocationAttemptStatus.RUNNING
    loaded.error = ""
    loaded.token_usage = None
    attempt_repo.save(loaded)
    await ai_invocation_routes._run_streaming_invocation_attempt(session_id='s', attempt_id='a', config=None)
    assert attempt_repo.get('a').status == InvocationAttemptStatus.FAILED
    assert attempt_repo.get('a').token_usage is None
