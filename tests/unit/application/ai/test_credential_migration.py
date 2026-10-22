import json
import os

import pytest

from application.ai.embedding_config_service import EmbeddingConfigService
from application.ai.llm_control_service import LLMControlService, LLMControlConfig, LLMProfile
from infrastructure.security.credential_store import CredentialUnavailable, PREFIX
from tests.unit.application.engine.test_persistence_commit_ack import writer


pytestmark = pytest.mark.skipif(os.name != 'nt', reason='real Windows DPAPI')


def make_service(db):
    service = LLMControlService()
    service._db = lambda: db
    return service


def test_new_profiles_use_references_and_can_reopen_for_provider(writer):
    _queue, db = writer
    service = make_service(db)
    configuration = LLMControlConfig(profiles=[LLMProfile(
        id='profile', name='Profile', api_key='private-api-key', model='test-model',
        extra_headers={'Authorization': 'Bearer header-key'},
        extra_query={'key': 'query-secret'}, extra_body={'nested': {'token': 'body-secret'}},
    )])
    service.save_config(configuration)
    stored = db.fetch_one('SELECT * FROM llm_profiles WHERE id = ?', ('profile',))
    for field in ('api_key', 'extra_headers', 'extra_query', 'extra_body'):
        assert stored[field].startswith(PREFIX)
        assert 'secret' not in stored[field] and 'private-api-key' not in stored[field]
    restored = make_service(db).get_config().profiles[0]
    assert restored.api_key == 'private-api-key'
    assert restored.extra_headers['Authorization'] == 'Bearer header-key'
    assert restored.extra_body['nested']['token'] == 'body-secret'
    service.save_config(service.get_config())
    assert db.fetch_one('SELECT * FROM llm_profiles WHERE id = ?', ('profile',))['api_key'] == stored['api_key']


def test_legacy_plaintext_migrates_without_changing_runtime_configuration(writer):
    _queue, db = writer
    db.execute("INSERT INTO llm_profiles (id,name,api_key,extra_headers) VALUES (?,?,?,?)",
               ('legacy', 'Legacy', 'legacy-key', json.dumps({'Authorization': 'Bearer old-token'})))
    profile = make_service(db).get_config().profiles[0]
    assert profile.api_key == 'legacy-key'
    assert profile.extra_headers == {'Authorization': 'Bearer old-token'}
    stored = db.fetch_one('SELECT api_key,extra_headers FROM llm_profiles WHERE id = ?', ('legacy',))
    assert stored['api_key'].startswith(PREFIX) and stored['extra_headers'].startswith(PREFIX)


def test_failed_migration_and_failed_save_preserve_old_database(writer, monkeypatch):
    _queue, db = writer
    db.execute("INSERT INTO llm_profiles (id,name,api_key) VALUES (?,?,?)", ('old', 'Old', 'legacy-key'))
    class FailedVault:
        def store(self, _value): raise CredentialUnavailable('failed')
    monkeypatch.setattr('application.ai.llm_control_service.credentials_for_database', lambda _db: FailedVault())
    service = make_service(db)
    with pytest.raises(CredentialUnavailable): service.get_config()
    with pytest.raises(CredentialUnavailable):
        service.save_config(LLMControlConfig(profiles=[LLMProfile(id='new', name='New', api_key='new-key')]))
    assert db.fetch_all('SELECT id,api_key FROM llm_profiles') == [{'id': 'old', 'api_key': 'legacy-key'}]


def test_embedding_legacy_migration_and_new_save(writer):
    _queue, db = writer
    embedding = EmbeddingConfigService(db)
    embedding.get_config()
    db.execute("UPDATE embedding_config SET api_key = ? WHERE id = 'default'", ('old-embedding-key',))
    assert embedding.get_config().api_key == 'old-embedding-key'
    assert db.fetch_one('SELECT api_key FROM embedding_config')['api_key'].startswith(PREFIX)
    assert embedding.update_config(api_key='new-embedding-key').api_key == 'new-embedding-key'
    assert db.fetch_one('SELECT api_key FROM embedding_config')['api_key'].startswith(PREFIX)
