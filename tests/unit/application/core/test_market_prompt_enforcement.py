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
