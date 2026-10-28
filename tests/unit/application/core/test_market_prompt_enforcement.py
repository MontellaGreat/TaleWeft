import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from application.ai_invocation.dtos import InvocationPolicy, InvocationSession, PromptSnapshot, prompt_hash
from application.ai_invocation.services import AttemptService, InvocationSessionService
from application.core.taxonomy.cn_market_v2 import PROFILE_DIMENSIONS
from application.core.taxonomy.market_selection import normalize_market_preferences
from application.world.services.auto_bible_generator import AutoBibleGenerator
from domain.ai.services.llm_service import GenerationResult
from domain.ai.value_objects.prompt import Prompt
from infrastructure.persistence.database.connection import DatabaseConnection


def confirmed_preferences(suffix='初版'):
    return normalize_market_preferences({
        'audience_mode': 'male', 'canonical_genre_id': 'major.xuanhuan',
        'custom_market_profile': {
            'scope': 'project', 'audience_mode': 'male', 'profile_review_status': 'user_confirmed',
            'custom_theme': {'id': 'custom.theme.project', 'label': '作者主题',
                'parent_canonical_category_id': 'major.xuanhuan', 'status': 'user_confirmed',
                'profile': {key: f'{suffix}确认的{key}' for key in PROFILE_DIMENSIONS}},
        },
    })


@pytest.fixture
def market_db(tmp_path, monkeypatch):
    from infrastructure.persistence.database import connection
    db = DatabaseConnection(str(tmp_path / 'market.sqlite'))
    monkeypatch.setattr(connection, 'get_database', lambda *a, **kw: db)
    db.execute('INSERT INTO novels(id,title,author,premise,slug,generation_prefs_json) VALUES (?,?,?,?,?,?)',
        ('book', '书', '作者', '创意', 'book', json.dumps(confirmed_preferences(), ensure_ascii=False)))
    db.commit()
    yield db
    db.close_all(skip_checkpoint=True)


def prompt_snapshot():
    prompt = Prompt(system='规划系统', user='创作一个故事')
    return PromptSnapshot(prompt=prompt, node_key='planning', node_version_id='v1', asset_link_set_id='',
        input_binding_set_id='', output_binding_set_id='', variable_snapshot_hash='', template_hash='',
        composition_hash='', rendered_prompt_hash=prompt_hash(prompt))


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['setup.main_plot_options', 'setup.plot_outline', 'bible.setup.characters'])
async def test_planning_provider_consumes_frozen_final_five_dimensions(market_db, operation):
    provider = Mock(generate=AsyncMock(return_value=GenerationResult('结果', None)))
    session = InvocationSession('s', operation, 'planning', InvocationPolicy.REVIEW_BEFORE_CALL,
                                context={'novel_id': 'book'})
    frozen = InvocationSessionService().attach_prompt(session, prompt_snapshot(), None)
    await AttemptService(provider).generate(session=session, prompt_snapshot=frozen)
    sent = provider.generate.await_args.args[0]
    assert sent == frozen.prompt
    for key in PROFILE_DIMENSIONS:
        assert f'初版确认的{key}' in sent.system
    assert '目标读者频道：男频' in sent.system


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
async def test_changed_profile_blocks_previously_reviewed_planning_before_provider(market_db, streaming):
    provider = Mock(generate=AsyncMock(), stream_generate=Mock())
    session = InvocationSession('s', 'setup.plot_outline', 'planning', InvocationPolicy.REVIEW_BEFORE_CALL,
                                context={'novel_id': 'book'})
    frozen = InvocationSessionService().attach_prompt(session, prompt_snapshot(), None)
    market_db.execute('UPDATE novels SET generation_prefs_json=? WHERE id=?',
                     (json.dumps(confirmed_preferences('修改后'), ensure_ascii=False), 'book'))
    market_db.commit()
    method = AttemptService(provider).generate_streaming if streaming else AttemptService(provider).generate
    with pytest.raises(ValueError, match='重新编译'):
        await method(session=session, prompt_snapshot=frozen)
    provider.generate.assert_not_called()
    provider.stream_generate.assert_not_called()
    assert not session.attempts


@pytest.mark.asyncio
async def test_legacy_bible_generation_and_style_stream_use_final_profile(market_db, monkeypatch):
    import infrastructure.ai.prompt_registry as registry_module
    monkeypatch.setattr(registry_module, 'get_prompt_registry', lambda: SimpleNamespace(
        get_node=lambda *_args, **_kwargs: None,
        render_to_prompt=lambda *_args, **_kwargs: Prompt(system='设定助手', user='写设定')))
    calls = []
    async def stream(prompt, config):
        calls.append(prompt)
        yield '文风公约'
    provider = Mock(generate=AsyncMock(return_value=GenerationResult('{"style":"风格"}', None)),
                    stream_generate=stream)
    generator = AutoBibleGenerator(provider, Mock())
    result = await generator._generate_bible_data('创意', 10, novel_id='book')
    events = [item async for item in generator._stream_style('创意', 10, novel_id='book')]
    assert result['style'] == '风格'
    assert events[-1] == {'type': 'done', 'style': '文风公约'}
    for prompt in [provider.generate.await_args.args[0], *calls]:
        for key in PROFILE_DIMENSIONS:
            assert f'初版确认的{key}' in prompt.system


@pytest.mark.asyncio
async def test_invalid_market_profile_blocks_bible_mutation_and_provider(market_db):
    market_db.execute('UPDATE novels SET generation_prefs_json=? WHERE id=?',
                     (json.dumps({'canonical_genre_id': 'missing', 'audience_mode': 'male'}), 'book'))
    provider, bible = Mock(generate=AsyncMock()), Mock()
    with pytest.raises(ValueError):
        await AutoBibleGenerator(provider, bible).generate_and_save('book', '创意', 10)
    assert not bible.mock_calls
    provider.generate.assert_not_called()


@pytest.mark.asyncio
async def test_invalid_market_profile_blocks_legacy_sse_before_mutation(market_db, monkeypatch):
    from interfaces.api import dependencies
    from interfaces.api.v1.world import bible
    market_db.execute('UPDATE novels SET generation_prefs_json=? WHERE id=?',
                     (json.dumps({'canonical_genre_id': 'missing', 'audience_mode': 'male'}), 'book'))
    novel = SimpleNamespace(id='book', title='书', premise='创意', target_chapters=10)
    monkeypatch.setattr(dependencies, 'get_novel_service', lambda: SimpleNamespace(get_novel=lambda _: novel))
    monkeypatch.setattr(bible, '_BIBLE_SETUP_NODE_BY_STAGE', {})
    generator = Mock()
    events = ''.join([event async for event in bible._sse_bible_generator('book', 'all', generator, Mock())])
    assert '市场画像校验失败' in events
    assert 'event: error' in events
    assert 'event: done' not in events
    assert not generator.mock_calls


@pytest.fixture
def setup_market_steps(market_db, monkeypatch):
    from application.onboarding.setup_generation import SetupInvocationSteps
    from application.world.services.bible_service import BibleService
    from application.world.services.worldbuilding_service import WorldbuildingService
    from infrastructure.persistence.database.sqlite_bible_repository import SqliteBibleRepository
    from infrastructure.persistence.database.worldbuilding_repository import WorldbuildingRepository
    from interfaces.api.v1.engine import ai_invocation_routes as inv
    monkeypatch.setattr(inv, 'get_database', lambda: market_db)
    steps = SetupInvocationSteps('book', 'worldbuilding')
    steps.unit = 'style'
    steps.novel = SimpleNamespace(premise='A bridge keeper investigates a tidal signal.')
    steps.base_context = steps._base()
    steps.session = InvocationSession('setup-market', 'bible.setup.worldbuilding', 'planning',
                                     InvocationPolicy.REVIEW_BEFORE_CALL, context={'novel_id': 'book'})
    InvocationSessionService().attach_prompt(steps.session, prompt_snapshot(), None)
    steps.repos = inv._repositories()
    bible = BibleService(SqliteBibleRepository(market_db))
    bible.create_bible('setup-market-bible', 'book')
    steps.generator = SimpleNamespace(llm_service=None, bible_service=bible,
        worldbuilding_service=WorldbuildingService(WorldbuildingRepository(market_db.db_path)))
    steps._config = lambda: None
    steps._install_candidate({'style': 'Use physical clues and short dialogue.'}, purpose='fixture')
    return steps


@pytest.mark.asyncio
@pytest.mark.parametrize('fails', [False, True])
async def test_setup_audit_uses_own_frozen_prompt_without_corrupting_generation_evidence(setup_market_steps, fails):
    from copy import deepcopy
    steps = setup_market_steps
    original_snapshot = steps.session.prompt_snapshot
    original_metadata = deepcopy(steps.session.metadata)
    sent = []

    class Provider:
        async def generate(self, prompt, _config):
            sent.append(prompt)
            for key in PROFILE_DIMENSIONS:
                assert f'初版确认的{key}' in prompt.system
            if fails:
                # Isolate prompt/metadata ownership from the separately tested retry policy.
                error = RuntimeError('temporary provider outage')
                error.body = {'retryable': False}
                raise error
            return GenerationResult('{"review":"complete"}', None)

        async def stream_generate(self, prompt, config):
            yield (await self.generate(prompt, config)).content

    steps.generator.llm_service = Provider()
    if fails:
        with pytest.raises(RuntimeError, match='provider outage'):
            await steps._model_json(Prompt(system='Independent audit', user='Inspect the candidate'), 'deviation_review')
    else:
        result, attempt_id = await steps._model_json(
            Prompt(system='Independent audit', user='Inspect the candidate'), 'deviation_review')
        assert result == {'review': 'complete'}
        attempt = steps.repos['attempt'].get(attempt_id)
        from application.core.taxonomy.market_prompt import MarketPromptHooks
        MarketPromptHooks().validate_frozen(attempt.prompt_snapshot.prompt, steps.session.context,
                                            attempt.accounting['market_profile_prompt'])
    assert len(sent) == 1
    assert steps.session.prompt_snapshot == original_snapshot
    assert steps.session.metadata == original_metadata
    persisted = steps.repos['session'].get(steps.session.id)
    assert persisted.prompt_snapshot == original_snapshot
    assert persisted.metadata == original_metadata


@pytest.mark.asyncio
async def test_setup_repair_reaudits_with_structured_market_profile(setup_market_steps):
    steps = setup_market_steps
    tasks = []

    class Provider:
        async def generate(self, prompt, _config):
            for key in PROFILE_DIMENSIONS:
                assert f'初版确认的{key}' in prompt.system
            request = json.loads(prompt.user)
            tasks.append(request['task'])
            if request['task'] == 'repair_setup_candidate':
                payload = {'style': 'Ground the bridge mystery in physical clues and economical dialogue.'}
            else:
                passed = len(tasks) > 1
                payload = {'passed': passed, 'input_hash': request['input_hash'],
                           'issues': [] if passed else [{'description': 'Keep the bridge mystery.'}],
                           'checks': {key: passed for key in ('synopsis', 'genre', 'requirements', 'predecessors')}}
            return GenerationResult(json.dumps(payload), None)

        async def stream_generate(self, prompt, config):
            yield (await self.generate(prompt, config)).content

    steps.generator.llm_service = Provider()
    await steps.deviation()
    assert tasks == ['review_deviation', 'repair_setup_candidate', 'review_deviation']
    assert steps._record()['audits']['deviation']['passed'] is True


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['audit', 'resume', 'commit'])
async def test_setup_rejects_changed_profile_even_when_base_fields_are_unchanged(setup_market_steps, market_db, action):
    steps = setup_market_steps
    before = steps._base()
    market_db.execute('UPDATE novels SET generation_prefs_json=? WHERE id=?',
                     (json.dumps(confirmed_preferences('变更后'), ensure_ascii=False), 'book'))
    market_db.commit()
    assert steps._base() == before
    if action == 'resume':
        steps._ensure_services = lambda: None
        assert await steps.validate_resume() is False
    else:
        if action == 'commit':
            # Candidate audits are independently covered; isolate the later market guard.
            steps._assert_audited = lambda: None
            steps._ensure_services = lambda: None
        with pytest.raises(ValueError, match='市场画像'):
            if action == 'audit':
                await steps._model_json(Prompt(system='Audit', user='Inspect'), 'deviation_review')
            else:
                await steps._commit_audited()


@pytest.mark.parametrize('change', ['none', 'legacy_style_contract', 'source', 'prompt', 'evidence', 'snapshot'])
def test_only_known_legacy_setup_transform_can_refresh_market_evidence(setup_market_steps, change):
    from dataclasses import replace
    from application.ai_invocation.dtos import stable_hash
    from application.core.taxonomy.market_prompt import MarketPromptHooks
    steps = setup_market_steps
    original = steps.session.prompt_snapshot.prompt
    steps.session.metadata = {**steps.session.metadata, 'setup_prompt_source': {
        'system': original.system, 'user': original.user}}
    steps.session.prompt_snapshot = steps._snapshot(steps._generation_prompt(
        legacy_style_contract=change == 'legacy_style_contract'))
    marker = {'unit': 'style', 'base_hash': stable_hash(steps.base_context),
              'prompt_hash': prompt_hash(steps.session.prompt_snapshot.prompt),
              'output_bindings': steps.session.prompt_snapshot.output_binding_set_id}
    steps.session.metadata = {**steps.session.metadata, 'setup_prepared': marker}
    if change == 'source':
        steps.session.metadata['setup_prompt_source']['user'] += ' Unreviewed change.'
    elif change == 'prompt':
        prompt = steps.session.prompt_snapshot.prompt
        steps.session.prompt_snapshot = replace(steps.session.prompt_snapshot,
            prompt=Prompt(system=prompt.system, user=prompt.user + ' Unknown transform.'))
        marker['prompt_hash'] = prompt_hash(steps.session.prompt_snapshot.prompt)
    elif change == 'evidence':
        steps.session.metadata = {**steps.session.metadata, 'market_profile_prompt': {}}
    elif change == 'snapshot':
        steps.session.prompt_snapshot = None
    if change in {'none', 'legacy_style_contract'}:
        steps._ensure_market_generation_prompt()
        MarketPromptHooks().validate_frozen(steps.session.prompt_snapshot.prompt, steps.session.context,
                                            steps.session.metadata['market_profile_prompt'])
        assert '初版确认的writing_style' in steps.session.prompt_snapshot.prompt.system
        # Migration is idempotent and does not compound composition hashes.
        migrated = steps.session.prompt_snapshot
        steps._ensure_market_generation_prompt()
        assert steps.session.prompt_snapshot == migrated
    else:
        with pytest.raises(ValueError):
            steps._ensure_market_generation_prompt()


@pytest.mark.asyncio
async def test_setup_new_unit_still_checks_previous_units_market_anchor(setup_market_steps, market_db):
    steps = setup_market_steps
    steps._capture()
    steps.session = None
    market_db.execute('UPDATE novels SET generation_prefs_json=? WHERE id=?',
                     (json.dumps(confirmed_preferences('变更后'), ensure_ascii=False), 'book'))
    market_db.commit()
    with pytest.raises(ValueError, match='市场画像'):
        await steps._prepare_unit()
