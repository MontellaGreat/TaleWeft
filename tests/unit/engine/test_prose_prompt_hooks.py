from dataclasses import replace
import hashlib
import json
from types import SimpleNamespace

import pytest

from application.ai_invocation.dtos import InvocationPolicy, InvocationSession, PromptSnapshot, prompt_hash
from application.ai_invocation.services import AttemptService, InvocationSessionService
from application.engine.rules.prose_prompt_hooks import ProsePromptHooks, generate_direct_prose
from application.engine.rules.anti_ai_stream_guard import prepare_direct_prose_stream
from domain.ai.value_objects.prompt import Prompt
from infrastructure.persistence.database.connection import DatabaseConnection


@pytest.fixture
def hook_db(tmp_path, monkeypatch):
    from infrastructure.persistence.database import connection
    db = DatabaseConnection(str(tmp_path / "hooks.sqlite"))
    monkeypatch.setattr(connection, "get_database", lambda *a, **kw: db)
    for novel in ("one", "two"):
        db.execute("INSERT INTO novels(id,title,author,premise,slug) VALUES (?,?,?,?,?)", (novel,novel,'Test','Test',novel))
        db.execute("INSERT INTO unified_characters(id,novel_id,name,verbal_tic,idle_behavior,voice_style) VALUES (?,?,?,?,?,?)",
            (novel,novel,'阿青',f'{novel}-口癖',f'{novel}-动作','简短'))
    db.commit()
    yield db
    db.close_all(skip_checkpoint=True)


def snapshot(prompt):
    return PromptSnapshot(prompt=prompt,node_key='prose',node_version_id='v',asset_link_set_id='',input_binding_set_id='',
        output_binding_set_id='',variable_snapshot_hash='',template_hash='',composition_hash='',rendered_prompt_hash=prompt_hash(prompt))


def test_all_prompt_layers_have_real_rules_and_scoped_character_state(hook_db):
    hook = ProsePromptHooks(hook_db)
    first, evidence = hook.compose(Prompt(system='story',user='阿青在战斗'), {'novel_id':'one','scene_type':'battle'})
    second, _ = hook.compose(Prompt(system='story',user='阿青在战斗'), {'novel_id':'two','scene_type':'battle'})
    assert 'P1. 信息密度法则' in first.system and 'R1. 场景' in first.system
    assert '当前场景类型：战斗' in first.system and '密度限制' in first.system
    assert 'one-口癖' in first.system and 'two-口癖' not in first.system
    assert 'two-口癖' in second.system and 'one-口癖' not in second.system
    assert all(state['applied'] for state in evidence['layers'].values())
    assert evidence['input_token_estimate'] <= evidence['input_budget_tokens']


def test_no_character_anchors_does_not_invent_observed_state(hook_db):
    prompt, evidence = ProsePromptHooks(hook_db).compose(Prompt(system='story',user='无名的人'), {'novel_id':'one'})
    assert '角色状态锁：' not in prompt.system
    assert evidence['layers']['L4_state_vector']['status'] == 'unavailable'


def test_persisted_scene_policy_is_consumed_by_default_generation_hook(hook_db):
    from application.engine.services.chapter_rewrite_candidates import ChapterRewriteCandidates
    ChapterRewriteCandidates(hook_db).set_policy('one','scan_only',scene_type='horror')
    prompt,evidence=ProsePromptHooks(hook_db).compose(Prompt(system='story',user='阿青'),{'novel_id':'one'})
    assert '当前场景类型：恐怖' in prompt.system and evidence['scene_type']=='horror'


def test_scene_exemptions_obey_density_and_change_rule_version():
    from application.audit.services.anti_ai_audit import AntiAIAuditor
    effective={'version':'rules','patterns': [('呼吸一窒','呼吸一窒','critical','生理性','hint')]}
    body='呼吸一窒。'+ '填'*1000 + '呼吸一窒。'
    default=AntiAIAuditor(rule_provider=lambda:effective).scan_chapter('',body)
    battle=AntiAIAuditor(rule_provider=lambda:effective,scene_type='battle').scan_chapter('',body)
    assert default.metrics.total_hits==2 and len(default.exempted_hits)==0
    assert battle.metrics.total_hits==1 and len(battle.exempted_hits)==1
    assert battle.rule_version!=default.rule_version


@pytest.mark.asyncio
async def test_input_budget_failure_blocks_provider_without_truncating_core(hook_db):
    class Provider:
        async def generate(self, *_):
            pytest.fail('over-budget input must not reach provider')
    with pytest.raises(ValueError, match='超出预算'):
        await generate_direct_prose(Provider(),Prompt(system='system',user='正文'),None,
            context={'novel_id':'one','chapter_number':1,'anti_ai_input_budget_tokens':2},db=hook_db)
    assert hook_db.fetch_one('SELECT count(*) AS n FROM anti_ai_prompt_runs')['n'] == 0


@pytest.mark.asyncio
async def test_review_freezes_exact_prompt_then_provider_receipt_proves_use(hook_db):
    class Provider:
        async def generate(self,prompt,_config):
            seen.append(prompt)
            return SimpleNamespace(content='正文',token_usage=None)
    seen=[]
    session=InvocationSession('session','chapter.prose.generate','prose',InvocationPolicy.REVIEW_BEFORE_CALL,context={'novel_id':'one','chapter_number':1})
    frozen=InvocationSessionService().attach_prompt(session,snapshot(Prompt(system='system',user='阿青继续前进')),None)
    assert 'one-口癖' in frozen.prompt.system
    attempt=await AttemptService(Provider()).generate(session=session,prompt_snapshot=frozen)
    assert seen==[frozen.prompt] and attempt.prompt_snapshot.rendered_prompt_hash==prompt_hash(seen[0])
    row=hook_db.fetch_one('SELECT * FROM anti_ai_prompt_runs WHERE job_id=?',(attempt.id,))
    assert row['status']=='completed' and row['prompt_hash']==session.metadata['anti_ai_prompt']['prompt_hash']
    assert json.loads(row['evidence_json'])['content_hash'] == hashlib.sha256('正文'.encode('utf-8')).hexdigest()
    assert attempt.accounting['anti_ai_prompt']['persistence_status']=='committed'
    scan = attempt.accounting['anti_ai_scan']
    scan_row = hook_db.fetch_one('SELECT * FROM anti_ai_audit_runs WHERE run_id=?', (scan['run_id'],))
    scan_report = json.loads(scan_row['report_json'])
    assert scan['persistence_status'] == scan_row['persistence_status'] == 'committed'
    assert scan['scan_status'] == scan_row['scan_status'] == 'completed'
    assert scan_row['content_hash'] == hashlib.sha256(attempt.content.encode('utf-8')).hexdigest()
    assert scan_report['job_id'] == attempt.id and scan_report['rule_version'] == scan_row['rule_version']
    assert json.loads(row['evidence_json'])['anti_ai_scan']['run_id'] == scan['run_id']


@pytest.mark.asyncio
async def test_reviewed_snapshot_mutation_requires_recompile(hook_db):
    session=InvocationSession('s','chapter.prose','prose',InvocationPolicy.REVIEW_BEFORE_CALL,context={'novel_id':'one'})
    frozen=InvocationSessionService().attach_prompt(session,snapshot(Prompt(system='system',user='阿青')),None)
    changed=replace(frozen,prompt=Prompt(system='other',user='阿青'))
    with pytest.raises(RuntimeError,match='重新编译'):
        await AttemptService(None).generate(session=session,prompt_snapshot=changed)
    assert not session.attempts


@pytest.mark.asyncio
async def test_direct_stream_and_nonstream_paths_consume_hooks_and_archive(hook_db):
    seen=[]
    class Provider:
        async def stream_generate(self,prompt,_config):
            seen.append(prompt)
            yield '正文'
        async def generate(self,prompt,_config):
            seen.append(prompt)
            return SimpleNamespace(content='正文')
    context={'novel_id':'one','chapter_number':1,'scene_type':'battle'}
    prompt=Prompt(system='system',user='阿青出手')
    assert [part async for part in prepare_direct_prose_stream(Provider(),prompt,None,context=context,db=hook_db)]==['正文']
    assert (await generate_direct_prose(Provider(),prompt,None,context=context,db=hook_db)).content=='正文'
    assert all('one-口癖' in item.system and 'P5.' in item.system for item in seen)
    assert hook_db.fetch_one('SELECT count(*) AS n FROM anti_ai_prompt_runs')['n']==2
    prompt_receipt = hook_db.fetch_one('SELECT * FROM anti_ai_prompt_runs ORDER BY rowid DESC LIMIT 1')
    prompt_evidence = json.loads(prompt_receipt['evidence_json'])
    scan_receipt = prompt_evidence['anti_ai_scan']
    scan_row = hook_db.fetch_one('SELECT * FROM anti_ai_audit_runs WHERE run_id=?', (scan_receipt['run_id'],))
    assert scan_receipt['persistence_status'] == scan_row['persistence_status'] == 'committed'
    assert scan_receipt['scan_status'] == scan_row['scan_status'] == 'completed'
    assert scan_row['content_hash'] == hashlib.sha256('正文'.encode('utf-8')).hexdigest()
    assert json.loads(scan_row['report_json'])['job_id'] == prompt_receipt['job_id']


@pytest.mark.asyncio
async def test_provider_failure_has_failed_prompt_receipt(hook_db):
    class Provider:
        async def generate(self,*_):
            raise RuntimeError('provider unavailable')
    with pytest.raises(RuntimeError,match='provider unavailable'):
        await generate_direct_prose(Provider(),Prompt(system='system',user='阿青'),None,context={'novel_id':'one','chapter_number':1},db=hook_db)
    row=hook_db.fetch_one('SELECT * FROM anti_ai_prompt_runs')
    assert row['status']=='failed' and 'provider unavailable' in row['error']


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('context', [
    {}, {'novel_id': 'one'}, {'novel_id': '', 'chapter_number': 1},
    {'novel_id': '  ', 'chapter_number': 1},
    *({'novel_id': 'one', 'chapter_number': number} for number in (None, 0, -1, True, '1', 1.5)),
])
async def test_invalid_scope_blocks_prose_before_provider_or_receipt(hook_db, streaming, context):
    calls = []
    class Provider:
        async def generate(self, *_):
            calls.append('generate')
            return SimpleNamespace(content='正文')
        async def stream_generate(self, *_):
            calls.append('stream')
            yield '正文'
    with pytest.raises(ValueError, match='有效小说或章节号'):
        if streaming:
            _ = [piece async for piece in prepare_direct_prose_stream(
                Provider(), Prompt(system='规则', user='阿青'), None, context=context, db=hook_db)]
        else:
            await generate_direct_prose(
                Provider(), Prompt(system='规则', user='阿青'), None, context=context, db=hook_db)
    assert calls == []
    assert hook_db.fetch_one('SELECT count(*) AS n FROM anti_ai_prompt_runs')['n'] == 0
    assert hook_db.fetch_one('SELECT count(*) AS n FROM anti_ai_stream_runs')['n'] == 0
    assert hook_db.fetch_one('SELECT count(*) AS n FROM anti_ai_audit_runs')['n'] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('change', ['confirmed_edit', 'draft', 'legacy_edit'])
async def test_frozen_prose_market_changes_block_stream_and_nonstream_before_provider(hook_db, streaming, change):
    from application.core.taxonomy.market_selection import normalize_market_preferences
    from tests.unit.application.core.test_market_selection_integrity import custom_selection
    if change == 'legacy_edit':
        raw = normalize_market_preferences({'locked_world_preset': '旧书原基调'})
    else:
        raw = normalize_market_preferences(custom_selection())
    hook_db.execute('UPDATE novels SET generation_prefs_json=? WHERE id=?', (json.dumps(raw), 'one'))
    hook_db.commit()
    session = InvocationSession('market-session', 'chapter.prose.generate', 'prose',
        InvocationPolicy.REVIEW_BEFORE_CALL, context={'novel_id': 'one', 'chapter_number': 1})
    frozen = InvocationSessionService().attach_prompt(session, snapshot(Prompt(system='规则', user='阿青继续行动')), None)
    if change == 'confirmed_edit':
        raw['custom_market_profile']['custom_theme']['profile']['writing_style'] = '修改后已确认的写作风格'
        raw['locked_writing_style'] = ''
    elif change == 'draft':
        raw['custom_market_profile']['profile_review_status'] = 'draft'
    else:
        raw['locked_world_preset'] = '旧书变更后的基调'
    hook_db.execute('UPDATE novels SET generation_prefs_json=? WHERE id=?', (json.dumps(normalize_market_preferences(raw)), 'one'))
    hook_db.commit()
    class NeverProvider:
        async def generate(self, *_):
            pytest.fail('stale profile reached nonstream provider')
        async def stream_generate(self, *_):
            pytest.fail('stale profile reached stream provider')
            yield ''
    method = AttemptService(NeverProvider()).generate_streaming if streaming else AttemptService(NeverProvider()).generate
    with pytest.raises((RuntimeError, ValueError)):
        await method(session=session, prompt_snapshot=frozen)
    assert not session.attempts
    assert hook_db.fetch_one('SELECT count(*) AS n FROM anti_ai_prompt_runs')['n'] == 0


@pytest.mark.asyncio
async def test_nonstream_attempt_cannot_archive_empty_content_as_completed(hook_db):
    class EmptyProvider:
        async def generate(self, *_):
            return SimpleNamespace(content='  ', token_usage=None)
    session = InvocationSession('empty', 'chapter.prose.generate', 'prose', InvocationPolicy.DIRECT,
        context={'novel_id': 'one', 'chapter_number': 1})
    with pytest.raises(RuntimeError, match='empty or invalid content'):
        await AttemptService(EmptyProvider()).generate(session=session, prompt_snapshot=snapshot(Prompt(system='规则', user='阿青')))
    row = hook_db.fetch_one('SELECT status FROM anti_ai_prompt_runs')
    assert row['status'] == 'failed'


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['attempt', 'direct'])
async def test_nonstream_scan_receipt_persistence_failure_blocks_success(hook_db, path):
    from application.ai_invocation.prose_execution_evidence import ProseExecutionEvidenceError

    hook_db.execute('DROP TABLE anti_ai_audit_runs')
    hook_db.commit()

    class Provider:
        async def generate(self, *_):
            return SimpleNamespace(content='完整正文', token_usage=None)

    if path == 'attempt':
        service = AttemptService(Provider())
        session = InvocationSession('scan-failure', 'chapter.prose.generate', 'prose', InvocationPolicy.DIRECT,
            context={'novel_id': 'one', 'chapter_number': 1})
        with pytest.raises(ProseExecutionEvidenceError, match='扫描或回执持久化失败'):
            await service.generate(session=session, prompt_snapshot=snapshot(Prompt(system='规则', user='阿青')))
        attempt = service.get(session.attempts[0])
        assert attempt.status.value == 'failed'
        assert session.status.value == 'failed'
    else:
        with pytest.raises(ProseExecutionEvidenceError, match='扫描或回执持久化失败'):
            await generate_direct_prose(Provider(), Prompt(system='规则', user='阿青'), None,
                context={'novel_id': 'one', 'chapter_number': 1}, db=hook_db)

    prompt = hook_db.fetch_one('SELECT status, error FROM anti_ai_prompt_runs')
    assert prompt['status'] == 'failed'
    assert hook_db.fetch_one("SELECT count(*) AS n FROM anti_ai_prompt_runs WHERE status='completed'")['n'] == 0


@pytest.mark.asyncio
async def test_streaming_attempt_durable_prompt_receipt_binds_returned_body(hook_db):
    class Provider:
        async def stream_generate(self, *_):
            yield '阿青放下'
            yield '手里的钥匙。'
    session = InvocationSession('stream-body', 'chapter.prose.generate', 'prose', InvocationPolicy.DIRECT,
        context={'novel_id': 'one', 'chapter_number': 1})
    attempt = await AttemptService(Provider()).generate_streaming(
        session=session, prompt_snapshot=snapshot(Prompt(system='规则', user='阿青')))
    row = hook_db.fetch_one('SELECT * FROM anti_ai_prompt_runs WHERE job_id=?', (attempt.id,))
    assert row['status'] == 'completed'
    assert json.loads(row['evidence_json'])['content_hash'] == hashlib.sha256(attempt.content.encode('utf-8')).hexdigest()
    assert attempt.accounting['anti_ai_prompt']['content_hash'] == json.loads(row['evidence_json'])['content_hash']


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['truncated', 'empty', 'provider_failure'])
async def test_incomplete_stream_never_archives_completed_prompt_receipt(hook_db, failure):
    class Provider:
        async def stream_generate(self, *_):
            if failure == 'empty':
                yield '  '
                return
            if failure == 'truncated':
                yield '尚未写完的片段'
                raise RuntimeError('Chat stream ended before a normal finish_reason')
            raise RuntimeError('provider execution failed')
            yield ''
    service = AttemptService(Provider())
    session = InvocationSession('stream-incomplete', 'chapter.prose.generate', 'prose', InvocationPolicy.DIRECT,
        context={'novel_id': 'one', 'chapter_number': 1})
    with pytest.raises(RuntimeError):
        await service.generate_streaming(session=session, prompt_snapshot=snapshot(Prompt(system='规则', user='阿青')))
    attempt = service.get(session.attempts[0])
    row = hook_db.fetch_one('SELECT * FROM anti_ai_prompt_runs WHERE job_id=?', (attempt.id,))
    assert row['status'] == 'failed'
    assert json.loads(row['evidence_json'])['content_hash'] == hashlib.sha256(attempt.content.encode('utf-8')).hexdigest()
    assert attempt.accounting['anti_ai_prompt']['status'] == 'failed'
    assert hook_db.fetch_one("SELECT count(*) AS n FROM anti_ai_prompt_runs WHERE status='completed'")['n'] == 0
