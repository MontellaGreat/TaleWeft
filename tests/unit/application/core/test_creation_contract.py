"""The contract editor writes canonical sources without replacing other setup data."""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from infrastructure.persistence.database.connection import DatabaseConnection
from application.core.taxonomy.market_selection import normalize_market_preferences
from application.core.taxonomy.market_prompt import load_verified_market_profile, MarketPromptHooks
from application.engine.services.execution_lease import claim_execution_lease
from domain.ai.value_objects.prompt import Prompt
from tests.unit.application.engine.test_persistence_commit_ack import writer


@pytest.fixture
def contract_env(tmp_path):
    from interfaces.api.v1.core.creation_contract import router, get_contract_database
    db = DatabaseConnection(str(tmp_path / 'contract.db'))
    db.execute("INSERT INTO novels(id,slug,title,target_chapters,target_words_per_chapter) VALUES('book','book','书',10,2500)")
    db.execute("INSERT INTO bibles(id,novel_id) VALUES('bible','book')")
    db.execute("INSERT INTO bible_style_notes(id,novel_id,category,content) VALUES('style','book','general','旧文风')")
    db.execute("INSERT INTO bible_world_settings(id,novel_id,name,description,setting_type) VALUES('world','book','规则','保留规则','general')")
    db.execute("INSERT INTO knowledge(id,novel_id,premise_lock) VALUES('knowledge','book','旧梗概')")
    db.commit()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_contract_database] = lambda: db
    client = TestClient(app)
    yield db, client
    db.close()


def read(client):
    response = client.get('/novels/book/creation-contract')
    assert response.status_code == 200
    return response.json()


def write(client, before, **changes):
    return client.put('/novels/book/creation-contract', json={
        'version': before['version'], 'contract': {**before['contract'], **changes},
    })


def test_save_refresh_and_new_connection_read_canonical_values_without_losing_world(contract_env):
    db, client = contract_env
    before = read(client)
    changes = dict(locked_genre='玄幻 / 新主题', locked_world_preset='完整世界基调' * 80,
                   locked_story_structure='新结构', locked_pacing_control='新节奏',
                   locked_writing_style='新规则', locked_special_requirements='不可违背的新要求',
                   target_chapters=120, target_words_per_chapter=3500,
                   style_notes='作者亲自修改的完整文风公约', premise_lock='新的完整梗概锁定')
    saved = write(client, before, **changes)
    assert saved.status_code == 200, saved.text
    assert read(client)['contract'] == {**before['contract'], **changes}
    reopened = DatabaseConnection(db.db_path)
    try:
        prefs = json.loads(reopened.fetch_one("SELECT generation_prefs_json FROM novels WHERE id='book'")['generation_prefs_json'])
        assert prefs['locked_world_preset'] == changes['locked_world_preset']
        assert reopened.fetch_one("SELECT content FROM bible_style_notes WHERE novel_id='book'")['content'] == changes['style_notes']
        assert reopened.fetch_one("SELECT premise_lock FROM knowledge WHERE novel_id='book'")['premise_lock'] == changes['premise_lock']
        assert reopened.fetch_one("SELECT description FROM bible_world_settings WHERE id='world'")['description'] == '保留规则'
        from infrastructure.persistence.database.sqlite_ai_invocation_repository import SqliteVariableHubRepository
        hub = SqliteVariableHubRepository(reopened)
        assert hub.get_value('novel.world_preset', 'novel_id:book').value == changes['locked_world_preset']
        assert hub.get_value('worldbuilding.style', 'novel_id:book').value == changes['style_notes']
    finally:
        reopened.close()


def test_stale_contract_and_active_runtime_cannot_overwrite_saved_data(contract_env):
    db, client = contract_env
    old = read(client)
    assert write(client, old, locked_genre='新题材').status_code == 200
    assert write(client, old, locked_genre='过期题材').status_code == 409
    current = read(client)
    assert claim_execution_lease('book', 'autopilot', 'runtime', db=db)
    assert write(client, current, locked_genre='运行中修改').status_code == 409
    assert read(client)['contract']['locked_genre'] == '新题材'


def test_unfinished_setup_audit_locks_contract_and_style_before_any_write(contract_env):
    db, client = contract_env
    before = read(client)
    db.execute('CREATE TABLE setup_dag_runs(id TEXT,novel_id TEXT,stage TEXT,status TEXT,snapshot_json TEXT,started_at REAL)')
    db.execute("INSERT INTO setup_dag_runs VALUES('run','book','characters','paused',?,1)", (json.dumps({'workflow_version': 2}),))
    db.commit()
    response = write(client, before, style_notes='不允许提交的文风')
    assert response.status_code == 409
    assert response.json()['detail']['code'] == 'SETUP_AUDIT_INCOMPLETE'
    assert read(client)['contract'] == before['contract']


def test_market_edit_requires_confirmation_rebuilds_snapshot_and_invalidates_frozen_evidence(contract_env):
    db, client = contract_env
    prefs = normalize_market_preferences({'audience_mode': 'male', 'canonical_genre_id': 'major.xuanhuan',
        'custom_market_profile': {'scope': 'project', 'audience_mode': 'male', 'profile_review_status': 'user_confirmed',
            'custom_theme': {'id': 'custom.theme.test', 'label': '作者主题', 'parent_canonical_category_id': 'major.xuanhuan',
                'status': 'user_confirmed', 'profile': {key: '旧' + key for key in ('world_tone','story_structure','pacing_control','writing_style','special_requirements')}}}})
    db.execute("UPDATE novels SET generation_prefs_json=? WHERE id='book'", (json.dumps(prefs),))
    db.commit()
    before = read(client)
    prompt, evidence = MarketPromptHooks(db).compose(Prompt(system='system', user='user'), {'novel_id': 'book'})
    assert write(client, before, locked_pacing_control='新的作者节奏').status_code == 422
    response = client.put('/novels/book/creation-contract', json={'version': before['version'],
        'contract': {**before['contract'], 'locked_pacing_control': '新的作者节奏'}, 'confirm_market_profile': True})
    assert response.status_code == 200, response.text
    snapshot, block = load_verified_market_profile(db, 'book')
    assert snapshot['profile']['pacing_control'] == '新的作者节奏'
    assert '新的作者节奏' in block
    with pytest.raises(ValueError, match='重新编译'):
        MarketPromptHooks(db).validate_frozen(prompt, {'novel_id': 'book'}, evidence)


def test_variable_sync_failure_rolls_back_canonical_contract(contract_env):
    db, client = contract_env
    before = read(client)
    db.execute("CREATE TRIGGER reject_contract_variable BEFORE INSERT ON variable_values BEGIN SELECT RAISE(ABORT,'variable storage unavailable'); END")
    db.commit()
    with pytest.raises(Exception, match='variable storage unavailable'):
        write(client, before, style_notes='无法提交的公约')
    assert read(client)['contract'] == before['contract']


def test_save_materializes_book_variables_even_when_a_global_fallback_has_same_value(contract_env):
    db, client = contract_env
    db.execute("UPDATE novels SET generation_prefs_json=? WHERE id='book'", (json.dumps({'locked_world_preset': '作品世界'}),))
    from infrastructure.persistence.database.sqlite_ai_invocation_repository import SqliteVariableHubRepository
    from application.ai_invocation.variable_hub import VariableWrite
    repo = SqliteVariableHubRepository(db)
    repo.set_value(VariableWrite(key='novel.world_preset', value='作品世界', context_key='global'))
    assert write(client, read(client), style_notes='作者修改文风').status_code == 200
    repo.set_value(VariableWrite(key='novel.world_preset', value='其他全局世界', context_key='global'))
    assert repo.get_value('novel.world_preset', 'novel_id:book').value == '作品世界'


def test_contract_commits_with_real_writer_and_variable_lineage(writer):
    queue, db = writer
    from application.core.services.creation_contract_service import CreationContractService
    db.execute("INSERT INTO novels(id,slug,title,target_chapters,target_words_per_chapter) VALUES('book','book','书',10,2500)")
    service = CreationContractService(db)
    before = service.get('book')
    updated = {**before['contract'], 'locked_world_preset': '真正经过单写者提交的新世界', 'style_notes': '新文风公约'}
    result = service.save('book', version=before['version'], contract=updated)
    assert result['contract'] == updated
    reopened = DatabaseConnection(db.db_path)
    try:
        assert reopened.fetch_one("SELECT content FROM bible_style_notes WHERE novel_id='book'")['content'] == '新文风公约'
        assert reopened.fetch_one("SELECT COUNT(*) AS n FROM variable_lineage WHERE source_node_key='creation_contract'")['n'] > 0
        assert reopened.fetch_one("SELECT COUNT(*) AS n FROM creation_contract_mutations")['n'] == 1
    finally:
        reopened.close()
    assert queue.get_stats()['failed'] == 0


@pytest.mark.parametrize('race', ['contract', 'lease', 'audit'])
def test_writer_rechecks_inputs_and_ownership_after_batch_was_queued(writer, monkeypatch, race):
    _, db = writer
    from application.core.services.creation_contract_service import CreationContractService, ContractConflict
    from infrastructure.persistence.database import write_dispatch
    db.execute("INSERT INTO novels(id,slug,title,target_chapters,target_words_per_chapter) VALUES('book','book','书',10,2500)")
    db.execute('CREATE TABLE setup_dag_runs(id TEXT,novel_id TEXT,stage TEXT,status TEXT,snapshot_json TEXT,started_at REAL)')
    from application.engine.services.execution_lease import ensure_execution_schema
    ensure_execution_schema(db)
    service = CreationContractService(db)
    before = service.get('book')
    submit = write_dispatch.enqueue_txn_batch
    intercepted = False
    def delayed(operations, **kwargs):
        nonlocal intercepted
        if not intercepted and 'INSERT INTO creation_contract_mutations' in operations[0][0]:
            intercepted = True
            if race == 'contract':
                db.execute("UPDATE novels SET target_chapters=999 WHERE id='book'")
            elif race == 'lease':
                assert claim_execution_lease('book', 'autopilot', 'racing-runtime', db=db)
            else:
                db.execute("INSERT INTO setup_dag_runs VALUES('racing','book','characters','paused',?,1)", (json.dumps({'workflow_version': 2}),))
        return submit(operations, **kwargs)
    monkeypatch.setattr(write_dispatch, 'enqueue_txn_batch', delayed)
    with pytest.raises(ContractConflict, match='未写入任何改动'):
        service.save('book', version=before['version'], contract={**before['contract'], 'style_notes': '不应落库'})
    assert intercepted
    assert service.get('book')['contract']['style_notes'] == ''
    assert db.fetch_one("SELECT COUNT(*) AS n FROM creation_contract_mutations")['n'] == 0
    assert db.fetch_one("SELECT COUNT(*) AS n FROM variable_values WHERE source_node_key='creation_contract'")['n'] == 0
