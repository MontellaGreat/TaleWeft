"""Deterministic ownership races against temporary SQLite setup runs."""
import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

from application.onboarding import setup_dag as setup
from application.engine.dag.runtime import DAGRuntimeConflict
from application.engine.services.execution_lease import get_execution_lease
from infrastructure.persistence.database.connection import DatabaseConnection


@pytest.fixture
def setup_db(tmp_path):
    db = DatabaseConnection(str(tmp_path / 'setup-adversarial.sqlite'))
    db.execute("INSERT INTO novels(id,title,slug,target_chapters) VALUES ('novel','test','adversarial',6)")
    db.commit()
    yield db
    db.close_all(skip_checkpoint=True)


class Steps:
    def __init__(self, *, block=None, fail=None):
        self.calls = []
        self.block = block
        self.fail = fail

    def handles(self, _node_type):
        return False

    async def run_step(self, action, state):
        self.calls.append(action)
        if action == self.fail:
            raise ValueError('persisted readback does not match generated material')
        if action == 'generate' and self.block:
            self.block.set()
            await asyncio.Future()
        return {f'{action}_ok': True}


@pytest.mark.asyncio
async def test_immediate_stop_before_runner_first_turn_releases_lease_and_persists_terminal_state(setup_db):
    manager = setup.SetupDAGManager(setup_db, lambda *_: Steps())
    ack = await manager.start('novel', 'worldbuilding')
    try:
        result = await manager.stop('novel', ack['run_id'])
        assert result['status'] == 'cancelled'
        assert get_execution_lease('novel', setup_db) is None
        reopened = setup.SetupDAGManager(setup_db, lambda *_: Steps())
        assert (await reopened.status('novel', ack['run_id']))['status'] == 'cancelled'
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_cancelled_start_cannot_leave_a_late_acquired_lease(setup_db, monkeypatch):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    original = setup.claim_execution_lease

    def delayed_claim(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        try:
            return original(*args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(setup, 'claim_execution_lease', delayed_claim)
    manager = setup.SetupDAGManager(setup_db, lambda *_: Steps())
    starting = asyncio.create_task(manager.start('novel', 'characters'))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        starting.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await starting
        assert await asyncio.to_thread(finished.wait, 2)
        assert get_execution_lease('novel', setup_db) is None
    finally:
        release.set()
        await asyncio.gather(starting, return_exceptions=True)
        await asyncio.to_thread(finished.wait, 2)
        await manager.shutdown()


@pytest.mark.asyncio
async def test_stop_keeps_ownership_until_inflight_snapshot_save_has_finished(setup_db, monkeypatch):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    blocked = threading.Lock()
    manager = setup.SetupDAGManager(setup_db, lambda *_: Steps())
    original = manager.store.save

    def delayed_save(snapshot):
        if snapshot['status'] == 'running' and blocked.acquire(blocking=False):
            entered.set()
            try:
                assert release.wait(3)
                return original(snapshot)
            finally:
                finished.set()
        return original(snapshot)

    monkeypatch.setattr(manager.store, 'save', delayed_save)
    ack = await manager.start('novel', 'locations')
    stopping = None
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        stopping = asyncio.create_task(manager.stop('novel', ack['run_id']))
        completed, _ = await asyncio.wait({stopping}, timeout=0.05)
        assert not completed, 'stop returned while an owned SQLite writer was still running'
        lease = get_execution_lease('novel', setup_db)
        assert lease and lease['job_id'] == ack['run_id']
        release.set()
        result = await asyncio.wait_for(stopping, 2)
        assert result['status'] == 'cancelled'
        assert finished.is_set()
        assert get_execution_lease('novel', setup_db) is None
    finally:
        release.set()
        if stopping:
            await asyncio.gather(stopping, return_exceptions=True)
        await asyncio.to_thread(finished.wait, 2)
        await manager.shutdown()


@pytest.mark.asyncio
async def test_two_managers_cannot_start_different_stages_for_the_same_book(setup_db):
    entered = asyncio.Event()
    first_steps, second_steps = Steps(block=entered), Steps()
    first = setup.SetupDAGManager(setup_db, lambda *_: first_steps)
    second = setup.SetupDAGManager(setup_db, lambda *_: second_steps)
    try:
        ack = await first.start('novel', 'characters')
        await asyncio.wait_for(entered.wait(), 2)
        with pytest.raises(DAGRuntimeConflict):
            await second.start('novel', 'locations')
        assert second_steps.calls == []
        assert get_execution_lease('novel', setup_db)['job_id'] == ack['run_id']
    finally:
        await first.shutdown()
        await second.shutdown()


@pytest.mark.asyncio
async def test_failed_persisted_readback_cannot_report_a_completed_workflow(setup_db):
    steps = Steps(fail='verify')
    manager = setup.SetupDAGManager(setup_db, lambda *_: steps)
    try:
        ack = await manager.start('novel', 'plot_outline')
        await manager.tasks['novel']
        result = await manager.status('novel', ack['run_id'])
        assert 'commit' in steps.calls
        assert result['status'] == 'error'
        assert result['node_states']['plot_outline_verify']['status'] == 'error'
        assert result['progress']['percent'] < 100
        assert 'readback' in result['error']
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_lease_renewal_failure_interrupts_generation_before_commit(setup_db, monkeypatch):
    monkeypatch.setattr(setup, 'refresh_execution_lease', lambda *_: False)
    steps = Steps(block=asyncio.Event())
    manager = setup.SetupDAGManager(setup_db, lambda *_: steps, lease_seconds=0.3)
    try:
        ack = await manager.start('novel', 'locations')
        await asyncio.wait_for(manager.tasks['novel'], 2)
        result = await manager.status('novel', ack['run_id'])
        assert result['status'] in {'cancelled', 'interrupted', 'error'}
        assert '租约丢失' in result['error']
        assert 'commit' not in steps.calls
        assert get_execution_lease('novel', setup_db) is None
    finally:
        await manager.shutdown()


@pytest.fixture
def adapter_services(setup_db, monkeypatch):
    from application.world.services.bible_service import BibleService
    from application.world.services.worldbuilding_service import WorldbuildingService
    from application.world.services import bible_setup_continuation as continuation
    from infrastructure.persistence.database import connection
    from infrastructure.persistence.database.sqlite_bible_repository import SqliteBibleRepository
    from infrastructure.persistence.database.worldbuilding_repository import WorldbuildingRepository
    from interfaces.api import dependencies
    from interfaces.api.v1.engine import ai_invocation_routes as invocation, generation

    monkeypatch.setattr(connection, 'get_database', lambda *_args, **_kwargs: setup_db)
    monkeypatch.setattr(invocation, 'get_database', lambda: setup_db)
    monkeypatch.setattr(generation, 'get_database', lambda: setup_db)
    bible = BibleService(SqliteBibleRepository(setup_db))
    bible.create_bible('novel-bible', 'novel')
    world = WorldbuildingService(WorldbuildingRepository(setup_db.db_path))
    monkeypatch.setattr(continuation, '_get_services', lambda _: (bible, world))
    monkeypatch.setattr(continuation, '_refresh_shared_state', lambda _: None)
    novel = SimpleNamespace(target_chapters=20)
    monkeypatch.setattr(dependencies, 'get_novel_service',
                        lambda: SimpleNamespace(get_novel=lambda _: novel))
    return bible, world, novel, invocation


def _setup_market_profile(setup_db, enabled):
    if not enabled:
        return {}
    from application.core.taxonomy.cn_market_v2 import PROFILE_DIMENSIONS
    from application.core.taxonomy.market_selection import normalize_market_preferences

    profile = {key: f'DAG当前确认的{key}' for key in PROFILE_DIMENSIONS}
    preferences = normalize_market_preferences({
        'audience_mode': 'male', 'canonical_genre_id': 'major.xuanhuan',
        'custom_market_profile': {
            'scope': 'project', 'audience_mode': 'male', 'profile_review_status': 'user_confirmed',
            'custom_theme': {'id': 'custom.theme.project', 'label': '作者主题',
                'parent_canonical_category_id': 'major.xuanhuan', 'status': 'user_confirmed',
                'profile': profile},
        },
    })
    setup_db.execute('UPDATE novels SET generation_prefs_json=? WHERE id=?',
                     (json.dumps(preferences, ensure_ascii=False), 'novel'))
    setup_db.commit()
    return profile


def _assert_setup_model_market_profile(prompt, profile):
    if profile:
        rendered = prompt.system + '\n' + prompt.user
        assert '目标读者频道：男频' in rendered
        for dimension, expected in profile.items():
            assert expected in rendered, f'model prompt is missing current market dimension {dimension}'


def _structured_setup_style(shape):
    guide = {'style': {
        '核心规则': 'STYLE_CORE: Each bridge crossing consumes fuel; show the keeper measuring the cost before acting.',
        '叙事视角': ['STYLE_VIEW: Stay with the bridge keeper and describe observable choices.'],
        '对话规则': {'动作': ['STYLE_DIALOGUE: Interrupt dialogue with concrete work at the winch.']},
        '节奏': json.dumps({'规则': ['STYLE_PACE: End each scene with a changed route or a smaller fuel reserve.']},
                           ensure_ascii=False),
    }}
    if shape == 'object':
        return guide
    if shape == 'list':
        return [guide, {'补充': ['STYLE_EXTRA: Track the depot ledger through every crossing.']}]
    if shape == 'nested-json':
        return json.dumps(guide, ensure_ascii=False)
    raise AssertionError(f'unknown structured style shape: {shape}')


def _assert_readable_setup_style(style):
    assert isinstance(style, str) and style.strip()
    for marker in ('STYLE_CORE:', 'STYLE_VIEW:', 'STYLE_DIALOGUE:', 'STYLE_PACE:'):
        assert marker in style
    assert not style.lstrip().startswith(('{', '['))
    assert '"核心规则"' not in style and '\\n' not in style


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['worldbuilding', 'characters', 'locations', 'plot_outline'])
async def test_real_setup_adapter_generates_commits_and_verifies_sqlite_data(stage, adapter_services):
    from application.ai_invocation.dtos import ContinuationRef, InvocationPolicy, InvocationSession, InvocationSessionStatus, PromptSnapshot
    from application.blueprint.services.setup_plot_outline_invocation import setup_plot_outline_output_bindings
    from application.world.services.bible_setup_output_bindings import bible_setup_output_bindings
    from application.world.worldbuilding_schema import WORLD_BUILDING_DIMENSION_KEYS, schema_field_keys
    from domain.ai.value_objects.prompt import Prompt
    from interfaces.api.v1.engine.setup_dag_routes import SetupInvocationSteps

    bible, world, novel, invocation = adapter_services
    phase_names = ['opening', 'development', 'deepening', 'climax', 'ending']
    payloads = {
        'worldbuilding': {'style': 'Describe concrete action and restrained emotions.', 'worldbuilding': {
            dim: {key: 'Mistport depends on tidal energy and every crossing exacts a material price.'
                  for key in schema_field_keys(dim)} for dim in WORLD_BUILDING_DIMENSION_KEYS}},
        'characters': {'characters': [{'id': 'hero', 'name': 'Lin', 'role': 'protagonist',
            'description': 'Investigates the harbor lights', 'gender': 'female', 'age': '27'}]},
        'locations': {'locations': [{'id': 'harbor', 'name': 'Mistport', 'type': 'harbor',
            'description': 'A walled harbor connected by tidal bridges'}]},
        'plot_outline': {'plot_outline': {'main_story_overview': 'Lin follows the harbor lights to their origin.',
            'stage_plan': [{'phase': phase, 'summary': 'Lin faces a growing cost while tracing the harbor signal.'}
                           for phase in phase_names]}},
    }
    text = json.dumps(payloads[stage])

    class OfflineLLM:
        async def stream_generate(self, _prompt, _config):
            try:
                request = json.loads(_prompt.user)
            except ValueError:
                request = {}
            if request.get('task') in {'review_deviation', 'repair_setup_candidate'} or request.get('review_version'):
                yield (await self.generate(_prompt, _config)).content
                return
            yield text[:len(text) // 2]
            yield text[len(text) // 2:]

        async def generate(self, prompt, _config):
            from domain.ai.services.llm_service import GenerationResult
            request = json.loads(prompt.user)
            if request.get('review_version') in {'semantic-style-v1', 'semantic-style-v2', 'semantic-style-v3', 'semantic-style-v4'}:
                from tests.dag.setup_model_fixtures import approved_semantic_review
                return GenerationResult(json.dumps(approved_semantic_review(request)), None)
            assert request['task'] == 'review_deviation'
            return GenerationResult(json.dumps({'passed': True, 'input_hash': request['input_hash'], 'issues': [],
                'checks': {key: True for key in ('synopsis', 'genre', 'requirements', 'predecessors')}}), None)

    node_key = 'planning-plot-outline' if stage == 'plot_outline' else f'bible-{stage}'
    operation = 'setup.plot_outline' if stage == 'plot_outline' else f'bible.setup.{stage}'
    binding_id = f'{node_key}:output:v1'
    bindings = setup_plot_outline_output_bindings() if stage == 'plot_outline' else bible_setup_output_bindings(node_key)
    steps = SetupInvocationSteps('novel', stage)
    steps.generator = SimpleNamespace(llm_service=OfflineLLM(), bible_service=bible, worldbuilding_service=world)
    steps.novel = novel
    steps.unit = 'world_review' if stage == 'worldbuilding' else stage
    steps.base_context = steps._base()
    steps.run_id = f'offline-run-{stage}'
    steps.repos = invocation._repositories()
    steps.repos['variable_hub'].set_bindings(binding_id, node_key, bindings, direction='output')
    prompt = Prompt(system='Return the structured setup result.', user='Generate the next stage.')
    snapshot = PromptSnapshot(prompt=prompt, node_key=node_key, node_version_id='offline-v1',
        asset_link_set_id='', input_binding_set_id='', output_binding_set_id=binding_id,
        variable_snapshot_hash='offline', template_hash='offline', composition_hash='offline',
        rendered_prompt_hash='offline')
    steps.session = InvocationSession(id=f'offline-{stage}', operation=operation, node_key=node_key,
        policy=InvocationPolicy.FULL_INTERACTIVE, status=InvocationSessionStatus.AWAITING_PRE_CALL_REVIEW,
        context={'novel_id': 'novel', 'setup_context': {'target_chapters': 20}},
        continuation=ContinuationRef(handler_key='setup_plot_outline' if stage == 'plot_outline' else f'bible_{stage}'),
        prompt_snapshot=snapshot)
    steps.repos['session'].save(steps.session)
    await steps.generate()
    assert steps.attempt.content == text
    if stage == 'worldbuilding':
        steps.validate('validate_style')
        steps.validate('validate_world')
    else:
        steps.validate('validate')
    await steps.deviation()
    await steps.anti_ai()
    from application.engine.services.execution_lease import claim_execution_lease, release_execution_lease
    from infrastructure.persistence.database.connection import get_database
    assert claim_execution_lease('novel', 'setup', steps.run_id, 120, get_database())
    try:
        await steps.commit()
        assert (await steps._verify_unit())['saved_data_verified'] is True
    finally:
        release_execution_lease('novel', 'setup', steps.run_id, get_database())
    assert steps.repos['session'].get(steps.session.id).status == InvocationSessionStatus.COMPLETED


def test_setup_dependency_gate_rejects_empty_worldbuilding_with_preexisting_style(adapter_services):
    from application.world.dtos.bible_dto import StyleNoteDTO
    from interfaces.api.v1.engine.setup_dag_routes import SetupInvocationSteps

    bible, world, _novel, _invocation = adapter_services
    bible.update_bible(novel_id='novel', characters=[], world_settings=[], locations=[], timeline_notes=[],
                       style_notes=[StyleNoteDTO(id='style', category='style', content='Use restrained prose.')])
    world.create_worldbuilding('novel')
    steps = SetupInvocationSteps('novel', 'characters')
    steps.generator = SimpleNamespace(bible_service=bible, worldbuilding_service=world)
    with pytest.raises(ValueError, match='世界观'):
        steps.check_dependencies()


@pytest.mark.asyncio
@pytest.mark.parametrize('custom_character_path', [False, True])
@pytest.mark.parametrize('structured_market_profile', [False, True], ids=['legacy-market', 'structured-market'])
async def test_four_stage_api_dag_uses_real_cpms_and_manual_edits(adapter_services, setup_db, monkeypatch,
                                                               custom_character_path, structured_market_profile,
                                                               style_shape='plain'):
    """Only the remote LLM is replaced; contracts, commits and storage are real."""
    from dataclasses import asdict
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from application.blueprint.services.setup_plot_outline_service import SetupPlotOutlineService
    from application.world.worldbuilding_schema import WORLD_BUILDING_DIMENSION_KEYS, schema_field_keys
    from infrastructure.ai import prompt_manager, prompt_registry
    from interfaces.api import dependencies
    from interfaces.api.v1.engine import setup_dag_routes as routes
    from interfaces.api.v1.world import bible as bible_routes

    bible, world, novel, invocation = adapter_services
    market_profile = _setup_market_profile(setup_db, structured_market_profile)
    novel.title, novel.premise = 'Mistport', 'A bridge keeper traces a signal through a tidal city.'
    novel.target_words_per_chapter = 2000
    payloads = [
        {'style': 'Use physical detail and restrained emotion.', 'worldbuilding': {
            dim: {key: 'Every tidal bridge consumes stored heat and the city measures passage in fuel.'
                  for key in schema_field_keys(dim)} for dim in WORLD_BUILDING_DIMENSION_KEYS}},
        {'characters': [{'id': 'hero', 'name': ' Lin ', 'role': 'protagonist', 'age': '27',
                         'description': 'A bridge keeper follows the signal despite the price of each crossing.'}]},
        {'locations': [{'id': 'city', 'name': 'Mistport', 'type': 'city',
                        'description': 'A harbor city whose tidal bridges consume the communal fuel reserve.'},
                       {'id': 'bridge', 'name': 'North bridge', 'parent_id': 'city', 'type': 'bridge',
                        'description': 'The northern crossing leads to the abandoned signal tower.'}]},
        {'plot_outline': {'main_story_overview': 'The keeper follows the signal and pays its cost.',
                          'stage_plan': [{'phase': phase, 'summary': 'The keeper faces the price of crossing.'}
                                         for phase in ['opening', 'development', 'deepening', 'climax', 'ending']]}},
    ]
    if style_shape != 'plain':
        payloads[0]['style'] = _structured_setup_style(style_shape)
    prompts, model_tasks = [], set()
    repaired_world_style = False

    class OfflineLLM:
        failure = None

        async def stream_generate(self, prompt, _config):
            _assert_setup_model_market_profile(prompt, market_profile)
            request = json.loads(prompt.user)
            if request.get('task') in {'review_deviation', 'repair_setup_candidate'} or request.get('review_version'):
                yield (await self.generate(prompt, _config)).content
                return
            if self.failure == '502':
                raise RuntimeError('HTTP 502 Bad Gateway')
            if self.failure == 'invalid_json':
                yield 'The model returned prose instead of a structured object.'
                return
            prompts.append(prompt)
            request = json.loads(prompt.user)
            if request.get('review_version') in {'semantic-style-v1', 'semantic-style-v2', 'semantic-style-v3', 'semantic-style-v4'}:
                from tests.dag.setup_model_fixtures import approved_semantic_review
                yield json.dumps(approved_semantic_review(request))
                return
            model_tasks.add(request['task'])
            unit = request['unit']
            if unit == 'style':
                payload = {'style': payloads[0]['style']}
            elif unit in WORLD_BUILDING_DIMENSION_KEYS:
                payload = {'worldbuilding': {unit: payloads[0]['worldbuilding'][unit]}}
            else:
                payload = payloads[{'characters': 1, 'locations': 2, 'plot_outline': 3}[unit]]
            if unit == 'characters' and custom_character_path:
                payload = {'custom': {'cast': payload['characters']}}
            yield json.dumps(payload)

        async def generate(self, prompt, _config):
            nonlocal repaired_world_style
            from domain.ai.services.llm_service import GenerationResult
            _assert_setup_model_market_profile(prompt, market_profile)
            request = json.loads(prompt.user)
            if request.get('review_version') in {'semantic-style-v1', 'semantic-style-v2', 'semantic-style-v3', 'semantic-style-v4'}:
                from tests.dag.setup_model_fixtures import approved_semantic_review
                return GenerationResult(json.dumps(approved_semantic_review(request)), None)
            model_tasks.add(request['task'])
            if request['task'] == 'repair_setup_candidate':
                repaired = request['candidate']
                repaired['style'] = _structured_setup_style(style_shape)
                repaired['worldbuilding']['geography']['terrain'] += ' The depot ledger marks the repaired northern crossing.'
                repaired_world_style = True
                return GenerationResult(json.dumps(repaired), None)
            assert request['task'] == 'review_deviation'
            report = {'passed': True, 'input_hash': request['input_hash'], 'issues': [],
                'checks': {key: True for key in ('synopsis', 'genre', 'requirements', 'predecessors')}}
            if style_shape != 'plain' and request['unit'] == 'world_review' and not repaired_world_style:
                report['passed'] = False
                report['issues'] = [{'description': 'Connect the northern crossing to the established depot ledger.'}]
            return GenerationResult(json.dumps(report), None)

    llm = OfflineLLM()
    generator = SimpleNamespace(llm_service=llm, bible_service=bible, worldbuilding_service=world)
    monkeypatch.setattr(dependencies, 'get_auto_bible_generator', lambda: generator)
    monkeypatch.setattr(dependencies, 'get_setup_plot_outline_service', lambda: SetupPlotOutlineService(
        llm_service=llm, bible_service=bible, novel_service=dependencies.get_novel_service()))
    monkeypatch.setattr(invocation, 'get_llm_service', lambda: llm)
    monkeypatch.setattr(prompt_manager, '_manager_instance', prompt_manager.PromptManager(setup_db))
    monkeypatch.setattr(prompt_registry, '_registry_instance', prompt_registry.PromptRegistry())
    adapters = []

    def adapter_factory(*args):
        adapter = routes.SetupInvocationSteps(*args)
        adapters.append(adapter)
        return adapter

    manager = setup.SetupDAGManager(setup_db, adapter_factory)
    monkeypatch.setattr(routes, '_manager', manager)
    app = FastAPI()
    app.include_router(routes.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        for stage in ['worldbuilding', 'characters', 'locations', 'plot_outline']:
            if stage == 'characters' and custom_character_path:
                from dataclasses import replace
                from application.world.services.bible_setup_output_bindings import bible_setup_output_bindings
                bindings = [replace(binding, alias='ensemble' if binding.variable_key == 'characters.list' else 'lead',
                                    source_path='custom.cast' if binding.variable_key == 'characters.list' else 'custom.cast[0]')
                            for binding in bible_setup_output_bindings('bible-characters')]
                invocation._repositories()['variable_hub'].set_bindings(
                    'bible-characters:output:v1', 'bible-characters', bindings, direction='output')
            ack = await client.post('/dag/novel/setup/run', json={'stage': stage})
            assert ack.status_code == 200, ack.text
            await manager.tasks['novel']
            status = await client.get(f"/dag/novel/setup/runs/{ack.json()['run_id']}")
            assert status.status_code == 200
            result = status.json()
            assert result['status'] == 'completed', result.get('error')
            assert result['progress']['percent'] == 100
            assert all(s['execution_receipt']['executor_completed'] for s in result['node_states'].values())
            if stage == 'worldbuilding':
                dto = bible.get_bible_by_novel('novel')
                if style_shape != 'plain':
                    _assert_readable_setup_style(dto.style_notes[0].content)
                    _assert_readable_setup_style(result['checkpoint']['approved']['style'])
                    stored_style = invocation._repositories()['variable_hub'].get_value('worldbuilding.style', 'novel_id:novel')
                    _assert_readable_setup_style(stored_style.value)
                dto.style_notes[0].content = 'MANUAL_STYLE: economical dialogue; no omniscient exposition.'
                # Same bulk-save contract used by the author's wizard editor.
                request = bible_routes.BulkUpdateBibleRequest(**{
                    key: asdict(dto)[key] for key in
                    ['characters', 'world_settings', 'locations', 'timeline_notes', 'style_notes']})
                await bible_routes.bulk_update_bible('novel', request, service=bible)
            if stage == 'characters':
                if custom_character_path:
                    repos = invocation._repositories()
                    stored = repos['variable_hub'].get_value('characters.list', 'novel_id:novel')
                    assert stored is not None and stored.value[0]['name'] == 'Lin'
                    assert bible.get_bible_by_novel('novel').characters[0].name == 'Lin'
                    global_binding = repos['variable_hub'].get_output_bindings('bible-characters:output:v1', 'bible-characters')[0]
                    assert global_binding.alias == 'ensemble' and global_binding.source_path == 'custom.cast'
                    record = adapters[-1].units['characters']
                    candidate_attempt = repos['attempt'].get(record['attempt_id'])
                    raw_attempt = repos['attempt'].get(candidate_attempt.accounting['source_attempt_id'])
                    assert json.loads(raw_attempt.content)['custom']['cast'][0]['name'] == ' Lin '
                    assert json.loads(prompts[6].user)['unit'] == 'characters'
                dto = bible.get_bible_by_novel('novel')
                dto.characters[0].name = 'MANUAL_HERO'
                request = bible_routes.BulkUpdateBibleRequest(**{
                    key: asdict(dto)[key] for key in
                    ['characters', 'world_settings', 'locations', 'timeline_notes', 'style_notes']})
                await bible_routes.bulk_update_bible('novel', request, service=bible)
        overview = (await client.get('/dag/novel/setup')).json()
        assert overview['active_run'] is None
        assert all(stage['latest_run']['status'] == 'completed' for stage in overview['stages'])
        for failure in ['invalid_json', '502']:
            llm.failure = failure
            ack = (await client.post('/dag/novel/setup/run', json={'stage': 'worldbuilding'})).json()
            await manager.tasks['novel']
            result = (await client.get(f"/dag/novel/setup/runs/{ack['run_id']}")).json()
            assert result['status'] == 'error'
            assert result['node_states']['worldbuilding_style_commit']['status'] == 'bypassed'
            assert invocation._repositories()['session'].get(adapters[-1].session.id).status.value == 'failed'
            assert bible.get_bible_by_novel('novel').style_notes[0].content.startswith('MANUAL_STYLE')
    assert len(prompts) == 9
    assert 'MANUAL_STYLE' in prompts[6].user
    assert 'MANUAL_HERO' in prompts[7].user
    assert 'MANUAL_HERO' in prompts[8].user
    assert 'North bridge' in prompts[8].user
    assert model_tasks == ({'generate_current_unit', 'review_deviation', 'repair_setup_candidate'}
                           if style_shape != 'plain' else {'generate_current_unit', 'review_deviation'})
    await manager.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize('style_shape', ['object', 'list', 'nested-json'])
async def test_four_stage_api_dag_normalizes_structured_style_guides(adapter_services, setup_db, monkeypatch, style_shape):
    await test_four_stage_api_dag_uses_real_cpms_and_manual_edits(
        adapter_services, setup_db, monkeypatch, False, True, style_shape)


@pytest.mark.asyncio
@pytest.mark.parametrize('style_shape', ['object', 'list', 'nested-json'])
async def test_real_setup_resumes_legacy_structured_style_after_validate_failure(adapter_services, setup_db,
                                                                                monkeypatch, style_shape):
    from collections import Counter
    from application.ai_invocation.dtos import InvocationAttemptStatus, stable_hash
    from application.world.worldbuilding_schema import WORLD_BUILDING_DIMENSION_KEYS, schema_field_keys
    from domain.ai.services.llm_service import GenerationResult
    from infrastructure.ai import prompt_manager, prompt_registry
    from interfaces.api import dependencies
    from interfaces.api.v1.engine import setup_dag_routes as routes

    bible, world, novel, invocation = adapter_services
    novel.title, novel.premise = 'Mistport', 'A bridge keeper traces the fuel ledger through a tidal city.'
    market_profile = _setup_market_profile(setup_db, True)
    raw_style = _structured_setup_style(style_shape)
    generation_counts, reviewed_units = Counter(), Counter()

    class OfflineLLM:
        async def stream_generate(self, prompt, _config):
            _assert_setup_model_market_profile(prompt, market_profile)
            request = json.loads(prompt.user)
            if request.get('task') in {'review_deviation', 'repair_setup_candidate'} or request.get('review_version'):
                yield (await self.generate(prompt, _config)).content
                return
            unit = json.loads(prompt.user)['unit']
            generation_counts[unit] += 1
            payload = ({'style': raw_style} if unit == 'style' else {
                'worldbuilding': {unit: {key: 'A tidal crossing consumes fuel recorded in the depot ledger.'
                                        for key in schema_field_keys(unit)}}})
            yield json.dumps(payload, ensure_ascii=False)

        async def generate(self, prompt, _config):
            _assert_setup_model_market_profile(prompt, market_profile)
            request = json.loads(prompt.user)
            if request.get('review_version') in {'semantic-style-v1', 'semantic-style-v2', 'semantic-style-v3', 'semantic-style-v4'}:
                from tests.dag.setup_model_fixtures import approved_semantic_review
                return GenerationResult(json.dumps(approved_semantic_review(request)), None)
            assert request['task'] == 'review_deviation'
            reviewed_units[request['unit']] += 1
            if request['unit'] in {'style', 'world_review'}:
                _assert_readable_setup_style(request['candidate']['style'])
            return GenerationResult(json.dumps({'passed': True, 'input_hash': request['input_hash'], 'issues': [],
                'checks': {key: True for key in ('synopsis', 'genre', 'requirements', 'predecessors')}}), None)

    llm = OfflineLLM()
    generator = SimpleNamespace(llm_service=llm, bible_service=bible, worldbuilding_service=world)
    monkeypatch.setattr(dependencies, 'get_auto_bible_generator', lambda: generator)
    monkeypatch.setattr(invocation, 'get_llm_service', lambda: llm)
    monkeypatch.setattr(prompt_manager, '_manager_instance', prompt_manager.PromptManager(setup_db))
    monkeypatch.setattr(prompt_registry, '_registry_instance', prompt_registry.PromptRegistry())

    class LegacyStyleSteps(routes.SetupInvocationSteps):
        async def _generate_unit(self):
            result = await super()._generate_unit()
            if self.unit == 'style':
                # Keep the exact old successful candidate even after new
                # generation begins normalizing this response shape.
                self._install_candidate({'style': raw_style}, purpose='legacy_generated_style')
                return {'generated_content_hash': self._candidate_hash()}
            return result

        def validate(self, action):
            if self.unit == 'style':
                raise ValueError('文风公约为空，已阻止保存')
            return super().validate(action)

    first = setup.SetupDAGManager(setup_db, LegacyStyleSteps)
    ack = await first.start('novel', 'worldbuilding')
    await first.tasks['novel']
    failed = await first.status('novel', ack['run_id'])
    await first.shutdown()
    assert failed['status'] == 'error' and '文风公约为空' in failed['error']
    assert failed['node_states']['worldbuilding_style_generate']['status'] == 'success'
    assert failed['node_states']['worldbuilding_style_validate']['status'] == 'error'
    assert generation_counts == {'style': 1} and not reviewed_units
    legacy_record = failed['checkpoint']['units']['style']
    assert legacy_record['payload'] == {'style': raw_style}
    legacy_attempt = invocation._repositories()['attempt'].get(legacy_record['attempt_id'])
    assert legacy_attempt.status == InvocationAttemptStatus.SUCCEEDED
    assert json.loads(legacy_attempt.content) == {'style': raw_style}
    assert not bible.get_bible_by_novel('novel').style_notes

    reopened = setup.SetupDAGManager(setup_db, routes.SetupInvocationSteps)
    ack = await reopened.start('novel', 'worldbuilding')
    await reopened.tasks['novel']
    resumed = await reopened.status('novel', ack['run_id'])
    await reopened.shutdown()
    assert resumed['status'] == 'completed', resumed.get('error')
    assert resumed['resumed_node_count'] > 0
    assert generation_counts == {unit: 1 for unit in ('style', *WORLD_BUILDING_DIMENSION_KEYS)}
    assert reviewed_units['style'] == reviewed_units['world_review'] == 1
    record = resumed['checkpoint']['units']['style']
    assert record['session_id'] == legacy_record['session_id']
    assert record['attempt_id'] != legacy_record['attempt_id']
    candidate = invocation._repositories()['attempt'].get(record['attempt_id'])
    assert candidate.accounting['source_attempt_id'] == legacy_record['attempt_id']
    assert candidate.status == InvocationAttemptStatus.SUCCEEDED
    assert json.loads(candidate.content) == record['payload']
    _assert_readable_setup_style(record['payload']['style'])
    for kind in ('deviation', 'anti_ai'):
        audit = record['audits'][kind]
        assert audit['passed'] is True and audit['input_hash'] == stable_hash(record['payload'])
        node = resumed['node_states'][f'worldbuilding_style_{kind}']
        assert node['status'] == 'success' and node['execution_receipt']['executor_completed'] is True
    saved_style = bible.get_bible_by_novel('novel').style_notes[0].content
    assert saved_style == record['payload']['style'] == resumed['checkpoint']['approved']['style']
    stored_style = invocation._repositories()['variable_hub'].get_value('worldbuilding.style', 'novel_id:novel')
    assert stored_style.value == saved_style
    assert all(world.get_worldbuilding('novel').normalized_dimensions()[dim] for dim in WORLD_BUILDING_DIMENSION_KEYS)


@pytest.mark.asyncio
@pytest.mark.parametrize('custom_character_path', [False, True])
async def test_v1_records_restart_as_current_and_execute_both_real_audits(adapter_services, setup_db, monkeypatch,
                                                                  custom_character_path):
    """Old run graphs remain history; fresh runs use the actual current gates."""
    original_start, original_status = setup.SetupDAGManager.start, setup.SetupDAGManager.status
    seeded, checked = set(), set()

    async def start(manager, novel_id, stage, *, restart=False):
        first = stage not in seeded
        if first:
            old = manager.store.create(f'legacy-{stage}', novel_id, stage, setup.build_setup_graph(stage))
            old['workflow_version'] = 1
            old['status'] = 'completed' if stage == 'worldbuilding' else 'interrupted'
            # Preserve six/five-node legacy history without inventing audits.
            legacy_nodes = [node for node in old['graph']['nodes']
                            if node['config']['prompt_variables']['action'] not in {'deviation', 'anti_ai'}]
            old['graph']['nodes'] = legacy_nodes[:6 if stage == 'worldbuilding' else 5]
            old['graph']['edges'] = []
            old['node_states'] = {node['id']: {'status': 'success'} for node in old['graph']['nodes']}
            old['checkpoint'] = {'version': 1, 'untrusted_old_candidate': 'must never resume'}
            manager.store.save(old)
            overview = await manager.overview(novel_id)
            definition = next(row for row in overview['stages'] if row['id'] == stage)
            assert definition['latest_run']['workflow_version'] == 1
            assert not any(node['config']['prompt_variables']['action'] in {'deviation', 'anti_ai'}
                           for node in definition['latest_run']['graph']['nodes'])
            assert any(node['config']['prompt_variables']['action'] == 'deviation'
                       for node in definition['graph']['nodes'])
            assert any(node['config']['prompt_variables']['action'] == 'anti_ai'
                       for node in definition['graph']['nodes'])
            seeded.add(stage)
        result = await original_start(manager, novel_id, stage, restart=restart)
        assert result['workflow_version'] == setup.WORKFLOW_VERSION
        if first:
            assert result['resume_from_run_id'] is None
            assert result['resumed_node_count'] == 0
        return result

    async def status(manager, novel_id, run_id):
        result = await original_status(manager, novel_id, run_id)
        if result.get('workflow_version') == setup.WORKFLOW_VERSION and result['status'] == 'completed':
            for node in result['graph']['nodes']:
                action = node['config']['prompt_variables']['action']
                if action not in {'deviation', 'anti_ai'}:
                    continue
                actual = result['node_states'][node['id']]
                assert actual['status'] == 'success'
                assert actual['execution_receipt']['executor_completed'] is True
                receipt = actual['outputs'][f'{action}_audit']
                assert receipt['passed'] is True and receipt['input_hash']
                if action == 'anti_ai':
                    assert receipt['scope'] == 'leaf_prose' and receipt['reports']
                    assert receipt['semantic_review']['status'] == 'completed'
                    assert receipt['semantic_review']['passed'] is True
                else:
                    assert receipt['attempt_id']
            checked.add(result['stage'])
        return result

    monkeypatch.setattr(setup.SetupDAGManager, 'start', start)
    monkeypatch.setattr(setup.SetupDAGManager, 'status', status)
    await test_four_stage_api_dag_uses_real_cpms_and_manual_edits(
        adapter_services, setup_db, monkeypatch, custom_character_path, False)
    assert seeded == checked == {'worldbuilding', 'characters', 'locations', 'plot_outline'}


@pytest.mark.asyncio
@pytest.mark.parametrize('failure_point', ['generate', 'anti_ai', 'commit_after_write', 'world_review_repair', 'anti_ai_review_after_rewrite', 'world_review_hub_failure', 'world_review_bible_failure', 'prepare_bindings', 'legacy_prepared_snapshot'])
@pytest.mark.parametrize('structured_market_profile', [False, True], ids=['legacy-market', 'structured-market'])
async def test_real_setup_resumes_geography_without_regenerating_approved_units(adapter_services, setup_db, monkeypatch,
                                                                            failure_point, structured_market_profile):
    from collections import Counter
    from domain.ai.services.llm_service import GenerationResult
    from application.engine.dag import anti_ai_scan
    from application.engine.dag.models import NodeResult, NodeStatus
    from application.world.worldbuilding_schema import WORLD_BUILDING_DIMENSION_KEYS, schema_field_keys
    from infrastructure.ai import prompt_manager, prompt_registry
    from interfaces.api import dependencies
    from interfaces.api.v1.engine import setup_dag_routes as routes

    bible, world, novel, invocation = adapter_services
    market_profile = _setup_market_profile(setup_db, structured_market_profile)
    novel.title, novel.premise = 'Mistport', 'A bridge keeper traces a signal in a tidal city.'
    counts, failures, model_tasks = Counter(), set(), set()

    class OfflineLLM:
        async def stream_generate(self, prompt, _config):
            _assert_setup_model_market_profile(prompt, market_profile)
            request = json.loads(prompt.user)
            if request.get('task') in {'review_deviation', 'repair_setup_candidate'} or request.get('review_version'):
                yield (await self.generate(prompt, _config)).content
                return
            if request.get('review_version') in {'semantic-style-v1', 'semantic-style-v2', 'semantic-style-v3', 'semantic-style-v4'}:
                from tests.dag.setup_model_fixtures import approved_semantic_review
                yield json.dumps(approved_semantic_review(request))
                return
            model_tasks.add(request['task'])
            unit = request['unit']
            counts[unit] += 1
            if unit == 'geography' and failure_point == 'generate' and not failures:
                failures.add('generate')
                raise RuntimeError('temporary geography provider failure')
            payload = ({'style': 'Use economical dialogue and concrete physical details.'} if unit == 'style'
                else {'worldbuilding': {unit: {key: 'The tidal bridges consume fuel and each crossing carries a material cost.'
                    for key in schema_field_keys(unit)}}})
            if unit == 'geography' and failure_point == 'anti_ai_review_after_rewrite':
                payload['worldbuilding']['geography']['terrain'] = '守桥人眼中闪过一丝笑意。The route consumes stored fuel at every crossing.'
            yield json.dumps(payload)

        async def generate(self, prompt, _config):
            _assert_setup_model_market_profile(prompt, market_profile)
            request = json.loads(prompt.user)
            if request.get('review_version') in {'semantic-style-v1', 'semantic-style-v2', 'semantic-style-v3', 'semantic-style-v4'}:
                from tests.dag.setup_model_fixtures import approved_semantic_review
                return GenerationResult(json.dumps(approved_semantic_review(request)), None)
            model_tasks.add(request['task'])
            if request['task'] == 'repair_setup_candidate':
                repaired = request['candidate']
                repaired['worldbuilding']['geography']['terrain'] = 'A repaired tidal route connects the fuel depot to the bridge signal tower.'
                if failure_point == 'world_review_bible_failure':
                    repaired['style'] = 'Use pared dialogue and tangible fuel costs as the bridge mystery unfolds.'
                if failure_point == 'anti_ai_review_after_rewrite':
                    failures.add('anti_ai_repaired')
                return GenerationResult(json.dumps(repaired), None)
            if (request['unit'] == 'geography' and failure_point == 'anti_ai_review_after_rewrite'
                    and 'anti_ai_repaired' in failures and 'post_rewrite_outage' not in failures):
                # Fail both bounded calls, then exercise explicit checkpoint resume.
                failures.add('post_rewrite_outage' if 'post_rewrite_first_outage' in failures else 'post_rewrite_first_outage')
                raise RuntimeError('temporary deviation review outage after successful anti-AI repair')
            report = {'passed': True, 'input_hash': request['input_hash'], 'issues': [],
                      'checks': {key: True for key in ('synopsis', 'genre', 'requirements', 'predecessors')}}
            if request['unit'] == 'world_review' and failure_point in {'world_review_repair', 'world_review_hub_failure', 'world_review_bible_failure'} and not failures:
                failures.add('world_review_repair')
                report['passed'] = False
                report['issues'] = [{'description': 'Connect the geography route to the established fuel depot.'}]
            return GenerationResult(json.dumps(report), None)

    llm = OfflineLLM()
    generator = SimpleNamespace(llm_service=llm, bible_service=bible, worldbuilding_service=world)
    monkeypatch.setattr(dependencies, 'get_auto_bible_generator', lambda: generator)
    monkeypatch.setattr(invocation, 'get_llm_service', lambda: llm)
    monkeypatch.setattr(prompt_manager, '_manager_instance', prompt_manager.PromptManager(setup_db))
    monkeypatch.setattr(prompt_registry, '_registry_instance', prompt_registry.PromptRegistry())
    original_scan = anti_ai_scan.scan_chapter

    def scan(text, context):
        if ':geography:' in str(context.get('chapter_number')) and failure_point == 'anti_ai' and not failures:
            failures.add('anti_ai')
            return NodeResult(status=NodeStatus.ERROR, error='temporary scanner unavailable', outputs={'scan_status': 'unavailable'})
        return original_scan(text, context)

    monkeypatch.setattr(anti_ai_scan, 'scan_chapter', scan)
    original_commit = invocation.create_commit

    async def commit(session_id, request):
        result = await original_commit(session_id, request)
        session = invocation._repositories()['session'].get(session_id)
        if session.metadata.get('setup_unit') == 'geography' and failure_point == 'commit_after_write' and not failures:
            failures.add('commit_after_write')
            raise RuntimeError('response lost after durable adoption commit')
        return result

    monkeypatch.setattr(invocation, 'create_commit', commit)
    from infrastructure.persistence.database.sqlite_ai_invocation_repository import SqliteVariableHubRepository
    original_write = SqliteVariableHubRepository.set_value

    def write(repo, value):
        session_id = getattr(value, 'source_session_id', '')
        session = invocation._repositories()['session'].get(session_id) if session_id else None
        if (failure_point == 'world_review_hub_failure' and session is not None
                and session.metadata.get('setup_unit') == 'world_review' and 'hub_after_canonical' not in failures):
            failures.add('hub_after_canonical')
            raise RuntimeError('variable hub unavailable after canonical world review repair was saved')
        return original_write(repo, value)

    monkeypatch.setattr(SqliteVariableHubRepository, 'set_value', write)
    original_bind = SqliteVariableHubRepository.set_bindings

    def bind(repo, binding_set_id, node_key, bindings, **kwargs):
        if failure_point == 'prepare_bindings' and binding_set_id.startswith('setup:') and binding_set_id.endswith(':output'):
            counts['binding_focus'] += 1
            if counts['binding_focus'] == 3 and not failures:
                failures.add('prepare_bindings')
                raise RuntimeError('temporary binding store failure after raw session creation')
        return original_bind(repo, binding_set_id, node_key, bindings, **kwargs)

    monkeypatch.setattr(SqliteVariableHubRepository, 'set_bindings', bind)
    original_bible_update = bible.update_bible

    def update_bible(**kwargs):
        styles = kwargs.get('style_notes') or []
        if (failure_point == 'world_review_bible_failure' and styles
                and styles[0].content.startswith('Use pared dialogue') and 'bible_after_world' not in failures):
            failures.add('bible_after_world')
            raise RuntimeError('bible unavailable after world dimensions were repaired and saved')
        return original_bible_update(**kwargs)

    monkeypatch.setattr(bible, 'update_bible', update_bible)

    class LegacyPreparedSnapshotSteps(routes.SetupInvocationSteps):
        async def _prepare_unit(self):
            result = await super()._prepare_unit()
            if self.unit == 'geography' and 'legacy_prepared_snapshot' not in failures:
                from application.ai_invocation.dtos import prompt_hash
                from application.core.taxonomy.market_prompt import MarketPromptHooks
                from domain.ai.value_objects.prompt import Prompt

                # preview.5 focused the unit after the original CPMS prompt was
                # frozen, leaving its market evidence tied to the source prompt.
                self.session.prompt_snapshot = self._snapshot(self._generation_prompt(legacy_source_omission=True))
                source = self.session.metadata['setup_prompt_source']
                _, evidence = MarketPromptHooks().compose(Prompt(**source), self.session.context)
                self.session.metadata['market_profile_prompt'] = evidence
                prepared = self.session.metadata['setup_prepared']
                prepared.pop('market_prompt_version', None)
                prepared['prompt_hash'] = prompt_hash(self.session.prompt_snapshot.prompt)
                self._save()
                failures.add('legacy_prepared_snapshot')
            return result

        async def _generate_unit(self):
            if self.unit == 'geography' and 'legacy_generate_failure' not in failures:
                failures.add('legacy_generate_failure')
                raise RuntimeError('temporary generation outage after preview.5 prepare checkpoint')
            return await super()._generate_unit()

    steps_factory = LegacyPreparedSnapshotSteps if failure_point == 'legacy_prepared_snapshot' else routes.SetupInvocationSteps
    first = setup.SetupDAGManager(setup_db, steps_factory)
    ack = await first.start('novel', 'worldbuilding')
    await first.tasks['novel']
    failed = await first.status('novel', ack['run_id'])
    if failure_point == 'world_review_repair':
        assert failed['status'] == 'completed', failed.get('error')
        actual = world.get_worldbuilding('novel').normalized_dimensions()
        assert actual['geography']['terrain'].startswith('A repaired tidal route')
        assert failed['checkpoint']['approved']['geography'] == actual['geography']
        assert len(actual) == len(WORLD_BUILDING_DIMENSION_KEYS)
        assert all(counts[unit] == 1 for unit in ('style', *WORLD_BUILDING_DIMENSION_KEYS))
        assert model_tasks == {'generate_current_unit', 'review_deviation', 'repair_setup_candidate'}
        await first.shutdown()
        return
    assert failed['status'] == 'error', failed.get('error')
    if failure_point in {'world_review_hub_failure', 'world_review_bible_failure'}:
        assert world.get_worldbuilding('novel').normalized_dimensions()['geography']['terrain'].startswith('A repaired tidal route')
    if failure_point == 'world_review_bible_failure':
        assert bible.get_bible_by_novel('novel').style_notes[0].content.startswith('Use economical dialogue')
    assert counts['style'] == counts['core_rules'] == 1
    if failure_point == 'legacy_prepared_snapshot':
        from application.ai_invocation.dtos import prompt_hash
        assert 'temporary generation outage after preview.5 prepare checkpoint' in failed['error']
        session_id = failed['checkpoint']['units']['geography']['session_id']
        legacy_session = invocation._repositories()['session'].get(session_id)
        assert 'market_prompt_version' not in legacy_session.metadata['setup_prepared']
        assert legacy_session.metadata['setup_prepared']['prompt_hash'] == prompt_hash(legacy_session.prompt_snapshot.prompt)
        if market_profile:
            assert legacy_session.metadata['market_profile_prompt']['prompt_hash'] != prompt_hash(legacy_session.prompt_snapshot.prompt)
            assert all(value not in legacy_session.prompt_snapshot.prompt.system for value in market_profile.values())
        assert counts['geography'] == 0
    prior = world.get_worldbuilding('novel').normalized_dimensions()['core_rules']
    await first.shutdown()

    reopened = setup.SetupDAGManager(setup_db, routes.SetupInvocationSteps)
    ack = await reopened.start('novel', 'worldbuilding')
    await reopened.tasks['novel']
    resumed = await reopened.status('novel', ack['run_id'])
    assert resumed['status'] == 'completed', resumed.get('error')
    assert resumed['resumed_node_count'] > 0
    assert counts['style'] == counts['core_rules'] == 1
    assert counts['geography'] == (2 if failure_point == 'generate' else 1)
    assert world.get_worldbuilding('novel').normalized_dimensions()['core_rules'] == prior
    assert all(world.get_worldbuilding('novel').normalized_dimensions()[dim] for dim in WORLD_BUILDING_DIMENSION_KEYS)
    assert {'generate_current_unit', 'review_deviation'} <= model_tasks
    if failure_point in {'anti_ai_review_after_rewrite', 'world_review_hub_failure', 'world_review_bible_failure'}:
        assert 'repair_setup_candidate' in model_tasks
    if failure_point == 'legacy_prepared_snapshot':
        assert resumed['checkpoint']['units']['geography']['session_id'] == session_id
        if market_profile:
            upgraded_session = invocation._repositories()['session'].get(session_id)
            assert upgraded_session.metadata['setup_prepared']['market_prompt_version'] == 1
    if failure_point in {'world_review_hub_failure', 'world_review_bible_failure'}:
        actual = world.get_worldbuilding('novel').normalized_dimensions()
        assert resumed['checkpoint']['approved']['geography'] == actual['geography']
        hub = invocation._repositories()['variable_hub'].get_value('worldbuilding.content', 'novel_id:novel')
        assert hub is not None and hub.value['geography'] == actual['geography']
    if failure_point == 'world_review_bible_failure':
        assert bible.get_bible_by_novel('novel').style_notes[0].content.startswith('Use pared dialogue')
    await reopened.shutdown()


@pytest.mark.asyncio
async def test_lost_lease_between_generation_and_commit_blocks_all_writes(setup_db):
    class LeaseStealingSteps(Steps):
        async def run_step(self, action, state):
            result = await super().run_step(action, state)
            if action == 'validate':
                setup_db.execute("UPDATE novel_execution_leases SET owner='dag',job_id='other' WHERE novel_id='novel'")
                setup_db.commit()
            return result

    steps = LeaseStealingSteps()
    manager = setup.SetupDAGManager(setup_db, lambda *_: steps)
    ack = await manager.start('novel', 'locations')
    await manager.tasks['novel']
    result = await manager.status('novel', ack['run_id'])
    assert result['status'] == 'interrupted'
    assert 'commit' not in steps.calls
    assert get_execution_lease('novel', setup_db)['job_id'] == 'other'


@pytest.mark.asyncio
async def test_stale_in_memory_run_cannot_hide_lost_ownership(setup_db):
    manager = setup.SetupDAGManager(setup_db, lambda *_: Steps())
    snapshot = manager.store.create('stale', 'novel', 'locations', setup.build_setup_graph('locations'))
    manager.live['stale'] = snapshot
    result = await manager.status('novel', 'stale')
    assert result['status'] == 'interrupted'
    assert result['completed_at']


@pytest.mark.asyncio
async def test_other_manager_cannot_claim_it_stopped_a_live_run(setup_db):
    entered = asyncio.Event()
    owner = setup.SetupDAGManager(setup_db, lambda *_: Steps(block=entered))
    observer = setup.SetupDAGManager(setup_db, lambda *_: Steps())
    ack = await owner.start('novel', 'locations')
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert (await observer.status('novel', ack['run_id']))['status'] == 'running'
        with pytest.raises(DAGRuntimeConflict, match='另一个执行器'):
            await observer.stop('novel', ack['run_id'])
        assert get_execution_lease('novel', setup_db)['job_id'] == ack['run_id']
    finally:
        await owner.shutdown()


@pytest.mark.asyncio
async def test_setup_graph_is_hidden_from_chapter_palette_and_rejected_by_chapter_runner(setup_db):
    from application.engine.dag.runtime import DAGExecutionManager
    from interfaces.api.v1.engine.dag.dag_routes import list_node_types
    graph = setup.build_setup_graph('worldbuilding')
    assert 'setup_step' not in (await list_node_types())['types']
    assert any('仅可由开书向导' in error for error in DAGExecutionManager.validate_production(graph))
    manager = setup.SetupDAGManager(setup_db, lambda *_: Steps())
    with pytest.raises(LookupError, match='小说不存在'):
        await manager.overview('missing')
