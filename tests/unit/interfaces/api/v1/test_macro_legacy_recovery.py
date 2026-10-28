"""Recover an interrupted legacy macro request without automatic model calls."""
import json

import httpx
import pytest
from fastapi import FastAPI

from application.ai_invocation.dtos import InvocationPolicy, InvocationSession, InvocationSessionStatus, PromptSnapshot, prompt_hash
from application.engine.services.execution_lease import claim_execution_lease
from application.engine.services.query_service import _hydrate_pending_invocation_from_db
from domain.ai.value_objects.prompt import Prompt
from infrastructure.persistence.database.sqlite_ai_invocation_repository import SqliteInvocationSessionRepository
from interfaces.api.v1.engine import ai_invocation_routes as routes
from tests.unit.application.engine.test_persistence_commit_ack import writer  # noqa: F401


@pytest.fixture
def recovery(writer, monkeypatch):
    _,db=writer
    db.execute("INSERT INTO novels(id,title,slug,autopilot_status,current_stage) VALUES('recover-book','Test','recover-book','stopped','paused_for_review')")
    prompt=Prompt(system='结构规划',user='按约定结构规划。')
    snapshot=PromptSnapshot(prompt=prompt,node_key='planning-quick-macro',node_version_id='frozen-v1',
        asset_link_set_id='',input_binding_set_id='',output_binding_set_id='',variable_snapshot_hash='vars',
        template_hash='template',composition_hash='composition',rendered_prompt_hash=prompt_hash(prompt))
    session=InvocationSession(id='legacy-macro',operation='autopilot.macro.plan',node_key='planning-quick-macro',
        policy=InvocationPolicy.DIRECT,status=InvocationSessionStatus.PROMPT_COMPILED,
        context={'novel_id':'recover-book'},prompt_snapshot=snapshot)
    SqliteInvocationSessionRepository(db).save(session)
    monkeypatch.setattr(routes,'get_database',lambda *args,**kwargs:db)
    monkeypatch.setattr('infrastructure.persistence.database.connection.get_database',lambda *args,**kwargs:db)
    monkeypatch.setattr('application.paths.get_db_path',lambda:db.db_path)
    monkeypatch.setattr(routes,'_refresh_session_variables_from_hub',lambda *args,**kwargs:None)
    monkeypatch.setattr(routes,'_publish_autopilot_session_state',lambda *args:None)
    calls=[]
    async def execute(**kwargs): calls.append(kwargs)
    monkeypatch.setattr(routes,'_run_streaming_invocation_attempt',execute)
    app=FastAPI();app.include_router(routes.router)
    return db,app,calls


@pytest.mark.asyncio
async def test_explicit_retry_recovers_compiled_macro_and_creates_one_durable_attempt(recovery):
    db,app,calls=recovery
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        response=await client.post('/ai-invocations/legacy-macro/retry',json={})
        assert response.status_code==200,response.text
        assert response.json()['session']['status']=='generating'
        second=await client.post('/ai-invocations/legacy-macro/retry',json={})
        assert second.status_code in (400,409)
    assert db.fetch_one("SELECT COUNT(*) n FROM ai_invocation_attempts WHERE session_id='legacy-macro'")['n']==1
    assert db.fetch_one("SELECT autopilot_status FROM novels WHERE id='recover-book'")['autopilot_status']=='stopped'


@pytest.mark.asyncio
async def test_legacy_retry_rejects_active_execution_without_writing_attempt(recovery):
    db,app,_=recovery
    assert claim_execution_lease('recover-book','autopilot','active-job',120,db)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        response=await client.post('/ai-invocations/legacy-macro/retry',json={})
    assert response.status_code==409,response.text
    assert db.fetch_all('SELECT id FROM ai_invocation_attempts')==[]


@pytest.mark.asyncio
async def test_lease_claimed_while_preparing_retry_prevents_the_model_call(recovery,monkeypatch):
    db,app,_=recovery
    def refresh(*args,**kwargs):
        assert claim_execution_lease('recover-book','autopilot','raced-job',120,db)
    monkeypatch.setattr(routes,'_refresh_session_variables_from_hub',refresh)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        response=await client.post('/ai-invocations/legacy-macro/retry',json={})
    assert response.status_code==409,response.text
    assert db.fetch_all('SELECT id FROM ai_invocation_attempts')==[]
    assert db.fetch_one("SELECT status FROM ai_invocation_sessions WHERE id='legacy-macro'")['status']=='prompt_compiled'


@pytest.mark.asyncio
async def test_bad_generation_config_does_not_leave_a_generating_session(recovery):
    db,app,_=recovery
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        response=await client.post('/ai-invocations/legacy-macro/retry',json={'config':{'max_tokens':-1}})
    assert response.status_code==422,response.text
    assert db.fetch_one("SELECT status FROM ai_invocation_sessions WHERE id='legacy-macro'")['status']=='prompt_compiled'
    assert db.fetch_all('SELECT id FROM ai_invocation_attempts')==[]


def test_stopped_status_hydrates_recovery_handle_without_resuming_work(recovery):
    _,_,calls=recovery
    status=_hydrate_pending_invocation_from_db({'novel_id':'recover-book','autopilot_status':'stopped','current_stage':'paused_for_review'})
    assert status['autopilot_status']=='stopped'
    assert status['active_invocation_session_id']=='legacy-macro'
    assert status['active_invocation_status']=='prompt_compiled'
    assert status['requires_ai_review'] is True
    assert calls==[]


def test_completed_newer_session_does_not_resurrect_old_interrupted_request(recovery):
    db,_,_=recovery
    db.execute("INSERT INTO ai_invocation_sessions(id,operation,node_key,policy,status,context_json) VALUES('new-completed','autopilot.macro.plan','planning-quick-macro','DIRECT','completed',?)",
               (json.dumps({'novel_id':'recover-book'}),))
    status=_hydrate_pending_invocation_from_db({'novel_id':'recover-book','autopilot_status':'stopped','current_stage':'paused_for_review'})
    assert not status.get('active_invocation_session_id')
