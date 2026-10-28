"""Observe actual managed invocation state without writing author data."""
import asyncio
import copy
import json
from unittest.mock import Mock

import pytest

from application.blueprint.services import continuous_planning_service as planning
from infrastructure.persistence.database.connection import DatabaseConnection


@pytest.fixture
def db(tmp_path, monkeypatch):
    value = DatabaseConnection(str(tmp_path / 'progress.sqlite'))
    value.execute("INSERT INTO novels(id,title,slug,current_stage,autopilot_status) VALUES ('novel','Test','test','paused_for_review','stopped')")
    value.commit()
    monkeypatch.setattr(planning, '_get_cross_process_shared_dict', lambda: None)
    monkeypatch.setattr(planning, '_macro_plan_progress_store', {})
    yield value
    value.close_all(skip_checkpoint=True)


def session(db, status='prompt_compiled', *, novel='novel', attempt=None):
    db.execute('''INSERT INTO ai_invocation_sessions(id,operation,node_key,policy,status,context_json)
        VALUES ('session','autopilot.macro.plan','planning-quick-macro','DIRECT',?,?)''',
        (status, json.dumps({'novel_id': novel})))
    if attempt:
        db.execute("INSERT INTO ai_invocation_attempts(id,session_id,status,error) VALUES ('attempt','session',?,?)", attempt)
    db.commit()


def test_old_compiled_without_receipt_is_interrupted_and_preserves_preview_read_only(db):
    session(db)
    preview = [{'title': '已收到的第一部', 'volumes': []}]
    planning._macro_plan_progress_store['novel'] = {'status': 'running', 'preview_parts': preview,
        'llm_stream_text': '{"parts":', 'message': '旧运行状态'}
    before = copy.deepcopy(planning._macro_plan_progress_store)
    result = planning.get_macro_plan_progress('novel', db=db)
    assert result['status'] == 'interrupted'
    assert result['invocation_session_id'] == 'session'
    assert result['invocation_status'] == 'prompt_compiled'
    assert result['autopilot_status'] == 'stopped'
    assert '回执' in result['message'] and '未调用' not in result['message']
    assert result['preview_parts'] == preview
    assert planning._macro_plan_progress_store == before
    assert db.fetch_one("SELECT status FROM ai_invocation_sessions WHERE id='session'")['status'] == 'prompt_compiled'
    assert db.fetch_one('SELECT count(*) AS n FROM ai_invocation_attempts')['n'] == 0


@pytest.mark.parametrize('status,attempt,expected', [
    ('failed', ('failed', 'upstream returned 502'), 'failed'),
    ('cancelled', ('failed', '调用已取消'), 'cancelled'),
    ('awaiting_pre_call_review', None, 'awaiting_review'),
    ('awaiting_acceptance', ('succeeded', ''), 'awaiting_review'),
    ('blocked', None, 'awaiting_review'),
])
def test_durable_terminal_or_review_state_replaces_idle_or_stale_running(db, status, attempt, expected):
    session(db, status, attempt=attempt)
    result = planning.get_macro_plan_progress('novel', db=db)
    assert result['status'] == expected
    assert result['invocation_status'] == status
    if status == 'failed':
        assert '502' in result['message']


def test_live_durable_attempt_is_observed_as_running_without_fabricated_nodes(db):
    session(db, 'generating', attempt=('running', ''))
    db.execute("UPDATE novels SET current_stage='macro_planning',autopilot_status='running'")
    db.commit()
    result = planning.get_macro_plan_progress('novel', db=db)
    assert result['status'] == 'running'
    assert result.get('preview_parts') in (None, [])
    assert result['invocation_status'] == 'generating'


def test_progress_never_uses_another_books_invocation(db):
    session(db, 'failed', novel='other', attempt=('failed', 'other book secret'))
    result = planning.get_macro_plan_progress('novel', db=db)
    assert result['status'] == 'idle'
    assert 'invocation_session_id' not in result
    assert 'other book secret' not in str(result)


def test_new_manual_task_progress_is_not_replaced_by_old_managed_failure(db):
    import time
    session(db, 'failed', attempt=('failed', 'old failure'))
    db.execute("UPDATE ai_invocation_sessions SET created_at='2020-01-01 00:00:00',updated_at='2020-01-01 00:00:00'")
    db.commit()
    planning._macro_plan_progress_store['novel'] = {'status': 'running', 'task_started_at': time.time(),
        'message': '新的手动规划请求', 'preview_parts': [{'title': '新节点'}]}
    result = planning.get_macro_plan_progress('novel', db=db)
    assert result['status'] == 'running' and result['message'] == '新的手动规划请求'
    assert result['preview_parts'] == [{'title': '新节点'}]


def test_completed_invocation_waiting_for_structure_commit_is_not_false_completion(db):
    session(db, 'completed', attempt=('succeeded', ''))
    result = planning.get_macro_plan_progress('novel', db=db)
    assert result['status'] == 'awaiting_review'
    assert result['invocation_status'] == 'completed'
    assert result.get('percent') != 100


def test_latest_completed_request_does_not_revive_an_old_failure(db):
    session(db, 'failed', attempt=('failed', 'old failure'))
    db.execute("UPDATE ai_invocation_sessions SET updated_at='2020-01-01 00:00:00'")
    db.execute('''INSERT INTO ai_invocation_sessions(id,operation,node_key,policy,status,context_json)
        VALUES ('new-session','autopilot.macro.plan','planning-quick-macro','DIRECT','completed','{"novel_id":"novel"}')''')
    db.commit()
    result = planning.get_macro_plan_progress('novel', db=db)
    assert result['invocation_session_id'] == 'new-session'
    assert result['status'] == 'awaiting_review'
    assert 'old failure' not in result['message']


@pytest.mark.asyncio
@pytest.mark.parametrize('invocation_status,expected', [
    ('prompt_compiled', 'interrupted'), ('awaiting_pre_call_review', 'awaiting_review'),
    ('failed', 'failed'), ('cancelled', 'cancelled'),
])
async def test_http_progress_and_sse_report_durable_terminal_without_done_or_lost_nodes(db, monkeypatch, invocation_status, expected):
    from dataclasses import replace
    from interfaces.api.v1.blueprint import continuous_planning_routes as routes
    from interfaces.api.v1.blueprint.planning_runtime_settings import PlanningRuntimeSettings
    session(db, invocation_status)
    preview = [{'title': '已收到节点', 'volumes': []}]
    planning._macro_plan_progress_store['novel'] = {'status': 'running', 'preview_parts': preview}
    monkeypatch.setattr(routes, 'get_database', lambda: db)
    monkeypatch.setattr(routes, 'get_planning_runtime_settings', lambda: replace(PlanningRuntimeSettings(),
        macro_watch_poll_seconds=0.001, macro_watch_node_delay_seconds=0, macro_watch_max_seconds=0.2))
    result = await routes.get_macro_plan_generation_progress('novel')
    assert result['data']['status'] == expected
    assert result['data']['preview_parts'] == preview
    response = await routes.watch_macro_plan_progress_sse('novel')
    async with asyncio.timeout(2):
        events = ''.join([chunk async for chunk in response.body_iterator])
    assert f'"status": "{expected}"' in events
    assert 'event: terminal' in events and 'event: done' not in events
    assert 'event: node' in events and '已收到节点' in events
    assert events.index('event: node') < events.index('event: terminal')


@pytest.mark.asyncio
async def test_stopped_without_macro_request_closes_observation_as_idle(db, monkeypatch):
    from dataclasses import replace
    from interfaces.api.v1.blueprint import continuous_planning_routes as routes
    from interfaces.api.v1.blueprint.planning_runtime_settings import PlanningRuntimeSettings
    monkeypatch.setattr(routes, 'get_database', lambda: db)
    monkeypatch.setattr(routes, 'get_planning_runtime_settings', lambda: replace(PlanningRuntimeSettings(),
        macro_watch_poll_seconds=0.001, macro_watch_max_seconds=0.2))
    response = await routes.watch_macro_plan_progress_sse('novel')
    async with asyncio.timeout(2):
        events = ''.join([chunk async for chunk in response.body_iterator])
    assert 'event: terminal' in events and '"status": "idle"' in events
    assert 'event: done' not in events and '"status": "timeout"' not in events


@pytest.mark.asyncio
@pytest.mark.parametrize('cancelled', [False, True])
async def test_macro_context_failure_or_cancelled_provider_does_not_leave_running(db, monkeypatch, cancelled):
    service = planning.ContinuousPlanningService(Mock(), Mock(), Mock())
    if cancelled:
        monkeypatch.setattr(service, '_get_bible_context', lambda _: {})
        monkeypatch.setattr(service, '_build_macro_planning_prompt', lambda **_: Mock())
        async def interrupted(*_):
            service._set_macro_preview_parts('novel', [{'title': '已收到节点'}])
            raise asyncio.CancelledError()
        monkeypatch.setattr(service, '_stream_macro_llm_text', interrupted)
    else:
        def broken(_):
            raise RuntimeError('context unavailable')
        monkeypatch.setattr(service, '_get_bible_context', broken)
    with pytest.raises(asyncio.CancelledError if cancelled else RuntimeError):
        await service.generate_macro_plan('novel', 100)
    result = planning.get_macro_plan_progress('novel')
    assert result['status'] == ('cancelled' if cancelled else 'failed')
    if cancelled:
        assert result['preview_parts'] == [{'title': '已收到节点'}]


@pytest.mark.asyncio
async def test_broken_model_stream_keeps_partial_preview_without_claiming_complete(db, monkeypatch):
    class Provider:
        async def stream_generate(self, *_):
            yield '{"parts":[{"title":"已收到的第一部","volumes":[{"title":"第一卷","acts":[{"title":"第一幕","description":"已生成资料"}]}]}]}'
            raise RuntimeError('peer closed connection')
    service = planning.ContinuousPlanningService(Mock(), Mock(), Provider())
    monkeypatch.setattr(service, '_get_bible_context', lambda _: {})
    monkeypatch.setattr(service, '_build_macro_planning_prompt', lambda **_: Mock())
    with pytest.raises(RuntimeError, match='peer closed'):
        await service.generate_macro_plan('novel', 100)
    result = planning.get_macro_plan_progress('novel')
    assert result['status'] == 'failed'
    assert result['preview_parts'][0]['title'] == '已收到的第一部'
    assert result['llm_stream_text']
