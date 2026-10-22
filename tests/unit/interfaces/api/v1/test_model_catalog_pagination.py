from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import HTTPException

from interfaces.api.v1.workbench import llm_control


def fake_catalog(monkeypatch, pages):
    calls = []
    class Response:
        def __init__(self, data):
            self.data = data
        def raise_for_status(self):
            pass
        def json(self):
            return self.data
    class Client:
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_args):
            return False
        async def get(self, url, headers):
            calls.append((url, headers))
            return Response(pages[min(len(calls)-1, len(pages)-1)])
    monkeypatch.setattr(llm_control.httpx, 'AsyncClient', Client)
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol,pages,param,cursor', [
    ('gemini', [{'models': [{'name': 'models/a'}], 'nextPageToken': 'next &1'},
                {'models': [{'name': 'models/b'}, {'name': 'models/a'}]}], 'pageToken', 'next &1'),
    ('anthropic', [{'data': [{'id': 'a'}], 'has_more': True, 'last_id': 'a'},
                   {'data': [{'id': 'b'}, {'id': 'a'}], 'has_more': False}], 'after_id', 'a'),
])
async def test_all_pages_are_consumed_and_deduplicated(monkeypatch, protocol, pages, param, cursor):
    calls = fake_catalog(monkeypatch, pages)
    result = await llm_control.list_models(llm_control.ModelListRequest(protocol=protocol, api_key='key'))
    assert result.count == 2
    assert {item.id for item in result.items} == {'a', 'b'}
    assert len(calls) == 2
    assert parse_qs(urlparse(calls[1][0]).query)[param] == [cursor]
    assert 'Authorization' not in calls[0][1]


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol,page', [
    ('gemini', {'models': [], 'nextPageToken': 'same'}),
    ('anthropic', {'data': [], 'has_more': True, 'last_id': 'same'}),
    ('anthropic', {'data': [], 'has_more': True}),
    ('gemini', {'data': []}),
])
async def test_incomplete_catalog_is_explicit_error(monkeypatch, protocol, page):
    calls = fake_catalog(monkeypatch, [page])
    with pytest.raises(HTTPException) as error:
        await llm_control.list_models(llm_control.ModelListRequest(protocol=protocol, api_key='key'))
    assert error.value.status_code == 502
    assert len(calls) <= 2


@pytest.mark.asyncio
async def test_local_catalog_omits_authorization_header(monkeypatch):
    calls = fake_catalog(monkeypatch, [{'data': [{'id': 'local'}, {'id': None}, {'id': ''}, {'id': 12}]}])
    result = await llm_control.list_models(llm_control.ModelListRequest(
        protocol='openai', base_url='http://localhost:11434/v1', auth_mode='none'))
    assert [item.id for item in result.items] == ['local']
    assert calls[0][1] == {}


@pytest.mark.asyncio
async def test_anthropic_v1_base_url_is_not_repeated_in_catalog(monkeypatch):
    calls = fake_catalog(monkeypatch, [{'data': [{'id': 'claude'}]}])
    await llm_control.list_models(llm_control.ModelListRequest(
        protocol='anthropic', base_url='https://api.anthropic.com/v1', api_key='key'))

    assert urlparse(calls[0][0]).path == '/v1/models'


@pytest.mark.asyncio
async def test_anthropic_gateway_prefix_is_preserved_in_catalog(monkeypatch):
    calls = fake_catalog(monkeypatch, [{'data': [{'id': 'claude'}]}])
    await llm_control.list_models(llm_control.ModelListRequest(
        protocol='anthropic', base_url='https://gateway.example/api/v1', api_key='key'))

    assert urlparse(calls[0][0]).path == '/api/v1/models'


@pytest.mark.asyncio
async def test_gateway_headers_and_query_survive_all_catalog_pages(monkeypatch):
    calls = fake_catalog(monkeypatch, [
        {'models': [{'name': 'models/a'}], 'nextPageToken': 'next &1'},
        {'models': [{'name': 'models/b'}]},
    ])
    await llm_control.list_models(llm_control.ModelListRequest(protocol='gemini', api_key='key',
        extra_headers={'X-Gateway': 'gateway-auth'}, extra_query={'tenant': 'my tenant', 'pageToken': 'start'}))
    assert len(calls) == 2
    for url, headers in calls:
        assert headers['X-Gateway'] == 'gateway-auth'
        assert headers['x-goog-api-key'] == 'key'
        assert parse_qs(urlparse(url).query)['tenant'] == ['my tenant']
    assert parse_qs(urlparse(calls[1][0]).query)['pageToken'] == ['next &1']


@pytest.mark.asyncio
async def test_masked_gateway_secrets_are_restored_only_for_selected_profile(monkeypatch):
    from types import SimpleNamespace
    from application.ai.llm_control_service import LLMProfile
    profile = LLMProfile(id='gateway', name='Gateway', api_key='stored-key',
        extra_headers={'Authorization': 'Bearer stored-secret'}, extra_query={'token': 'stored-query-secret'})
    monkeypatch.setattr(llm_control._service, 'get_config', lambda: SimpleNamespace(profiles=[profile]))
    calls = fake_catalog(monkeypatch, [{'data': [{'id': 'model'}]}])
    await llm_control.list_models(llm_control.ModelListRequest(protocol='openai', profile_id='gateway',
        api_key='********', extra_headers={'Authorization': '********'}, extra_query={'token': '********'}))
    assert calls[0][1]['Authorization'] == 'Bearer stored-secret'
    assert parse_qs(urlparse(calls[0][0]).query)['token'] == ['stored-query-secret']


@pytest.mark.asyncio
async def test_unsaved_keyless_local_profile_can_fetch_catalog(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(llm_control._service, 'get_config', lambda: SimpleNamespace(profiles=[]))
    calls = fake_catalog(monkeypatch, [{'data': [{'id': 'local'}]}])
    await llm_control.list_models(llm_control.ModelListRequest(protocol='openai', profile_id='unsaved',
        base_url='http://localhost:11434/v1', auth_mode='none'))
    assert calls[0][1] == {}


@pytest.mark.parametrize('protocol,endpoint,expected_stops', [
    ('openai', 'responses', 'unavailable'), ('openai', 'chat', 'configurable'),
    ('openai', 'auto', 'configurable'), ('anthropic', 'auto', 'configurable'),
    ('gemini', 'auto', 'configurable'),
])
def test_capability_parameters_match_actual_adapter_contract(protocol, endpoint, expected_stops):
    from datetime import datetime
    from infrastructure.ai.providers.capabilities import adapter_capabilities
    capabilities = adapter_capabilities(protocol, 'custom-model', endpoint)
    assert capabilities['features']['top_p'] == 'configurable'
    assert capabilities['features']['stop_sequences'] == expected_stops
    assert capabilities['capability_source'] == 'adapter_parameter_mapping'
    assert capabilities['confidence'] == 'endpoint_acceptance_required'
    assert datetime.fromisoformat(capabilities['expires_at']) > datetime.fromisoformat(capabilities['observed_at'])
