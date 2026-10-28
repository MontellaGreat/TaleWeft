"""Committed manual recovery must survive restart without another model call."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock

import pytest

from application.ai_invocation.continuation import ContinuationContext,execute_continuation
from application.ai_invocation.autopilot.continuations import register_autopilot_continuations
from application.ai_invocation.dtos import AdoptionDecision,ContinuationRef,InvocationPolicy,InvocationSession
from application.engine.services.autopilot_recovery_policy import AutopilotRecoveryPolicy
from domain.novel.entities.novel import AutopilotStatus,NovelStage
from engine.runtime import macro_planning_delegate as macro
from tests.unit.interfaces.api.v1.test_macro_legacy_recovery import recovery,writer  # noqa: F401


PARTS=[{'title':'第一部','volumes':[{'title':'第一卷','acts':[{'title':'第一幕','suggested_chapter_count':12}]}]}]


def test_stopped_manual_commit_keeps_daemon_stopped_for_both_policies(monkeypatch):
    register_autopilot_continuations()
    resumed=Mock();published=Mock()
    monkeypatch.setattr('application.ai_invocation.autopilot.continuations._resume_autopilot_stage',resumed)
    monkeypatch.setattr('application.ai_invocation.autopilot.continuations._clear_invocation_state',published)
    for policy in (InvocationPolicy.DIRECT,InvocationPolicy.AUTOPILOT_PAUSE):
        session=InvocationSession('s','autopilot.macro.plan','planning-quick-macro',policy,
            context={'novel_id':'book','target_chapters':12},metadata={'manual_stop_recovery':True},
            continuation=ContinuationRef('autopilot_macro_plan'))
        decision=AdoptionDecision('d','s','a',accepted_content=json.dumps({'parts':PARTS}))
        result=execute_continuation(ContinuationContext(session,decision))
        assert result['deferred_apply'] is True
        assert result['macro_plan']['structure']==PARTS
    resumed.assert_not_called();published.assert_not_called()


def prepared_host(recovery,monkeypatch):
    db,_,_=recovery
    accepted=json.dumps({'parts':PARTS})
    result={'continuation':{'deferred_apply':True,'target_chapters':12,'macro_plan':{'success':True,'structure':PARTS}}}
    db.execute("UPDATE ai_invocation_sessions SET status='completed',metadata_json=? WHERE id='legacy-macro'",
               (json.dumps({'manual_stop_recovery':True}),))
    db.execute("INSERT INTO ai_invocation_attempts(id,session_id,status,content) VALUES('a','legacy-macro','succeeded',?)",(accepted,))
    db.execute("INSERT INTO ai_adoption_decisions(id,session_id,attempt_id,decision,accepted_content) VALUES('d','legacy-macro','a','accepted',?)",(accepted,))
    db.execute("INSERT INTO ai_adoption_commits(id,session_id,decision_id,status,idempotency_key,result_json) VALUES('c','legacy-macro','d','succeeded','recovery-key',?)",(json.dumps(result),))
    host=Mock();host.novel_repository=SimpleNamespace(db=db)
    host.planning_service.apply_macro_plan_from_llm_result=AsyncMock(return_value={'success':True})
    host._is_still_running.return_value=True
    novel=SimpleNamespace(novel_id=SimpleNamespace(value='recover-book'),target_chapters=12,auto_approve_mode=False,
        current_stage=NovelStage.MACRO_PLANNING,autopilot_status=AutopilotStatus.RUNNING)
    monkeypatch.setattr(macro,'_read_shared_state',lambda _: {})
    request=AsyncMock(side_effect=AssertionError('Must reuse committed text, never request model again'))
    monkeypatch.setattr(macro,'_request_macro_invocation',request)
    return db,host,novel,request


def restart_after_cleanup(db,novel):
    """Follow the real explicit-start order before the daemon runs its stage."""
    db.execute("UPDATE novels SET autopilot_status='stopped',current_stage='paused_for_review' WHERE id='recover-book'")
    policy=AutopilotRecoveryPolicy(db)
    decision=policy.decide_on_start('recover-book')
    assert decision.next_stage==NovelStage.MACRO_PLANNING.value
    assert decision.discard_transient_invocations is True
    policy.apply_transient_cleanup(decision)
    novel.current_stage=NovelStage(decision.next_stage)
    novel.autopilot_status=AutopilotStatus.RUNNING


@pytest.mark.asyncio
@pytest.mark.parametrize('has_old_structure',[False,True])
async def test_explicit_restart_applies_committed_result_once_without_model(recovery,monkeypatch,has_old_structure):
    db,host,novel,request=prepared_host(recovery,monkeypatch)
    if has_old_structure:
        db.execute("INSERT INTO story_nodes(id,novel_id,node_type,title,number,order_index) VALUES('old-volume','recover-book','volume','old',1,0)")
    restart_after_cleanup(db,novel)
    await macro.run_macro_planning(host,novel)
    request.assert_not_called()
    host.planning_service.apply_macro_plan_from_llm_result.assert_awaited_once()
    assert novel.current_stage==NovelStage.PAUSED_FOR_REVIEW
    assert json.loads(db.fetch_one("SELECT metadata_json FROM ai_invocation_sessions WHERE id='legacy-macro'")['metadata_json'])['macro_plan_applied'] is True


@pytest.mark.asyncio
async def test_failed_structure_write_retains_durable_result_for_next_explicit_start(recovery,monkeypatch):
    db,host,novel,request=prepared_host(recovery,monkeypatch)
    restart_after_cleanup(db,novel)
    host.planning_service.apply_macro_plan_from_llm_result.side_effect=RuntimeError('synthetic storage failure')
    with pytest.raises(RuntimeError,match='storage failure'):
        await macro.run_macro_planning(host,novel)
    assert not json.loads(db.fetch_one("SELECT metadata_json FROM ai_invocation_sessions WHERE id='legacy-macro'")['metadata_json']).get('macro_plan_applied')
    host.planning_service.apply_macro_plan_from_llm_result.side_effect=None
    restart_after_cleanup(db,novel)
    await macro.run_macro_planning(host,novel)
    request.assert_not_called()
    assert host.planning_service.apply_macro_plan_from_llm_result.await_count==2


@pytest.mark.asyncio
@pytest.mark.parametrize('has_old_structure',[False,True])
async def test_changed_target_blocks_old_result_without_requesting_again(recovery,monkeypatch,has_old_structure):
    db,host,novel,request=prepared_host(recovery,monkeypatch)
    if has_old_structure:
        db.execute("INSERT INTO story_nodes(id,novel_id,node_type,title,number,order_index) VALUES('old-volume','recover-book','volume','old',1,0)")
    novel.target_chapters=24
    restart_after_cleanup(db,novel)
    with pytest.raises(ValueError,match='目标章节数'):
        await macro.run_macro_planning(host,novel)
    request.assert_not_called()


@pytest.mark.parametrize('damage', [
    'flag_only', 'failed_commit', 'rejected_decision', 'failed_attempt',
    'mismatched_structure', 'not_deferred', 'already_applied', 'different_operation',
])
def test_cleanup_keeps_normal_discard_rules_without_valid_unapplied_recovery(recovery,monkeypatch,damage):
    db,_,novel,_=prepared_host(recovery,monkeypatch)
    if damage=='flag_only':
        db.execute("DELETE FROM ai_adoption_commits WHERE id='c'")
    elif damage=='failed_commit':
        db.execute("UPDATE ai_adoption_commits SET status='failed' WHERE id='c'")
    elif damage=='rejected_decision':
        db.execute("UPDATE ai_adoption_decisions SET decision='rejected' WHERE id='d'")
    elif damage=='failed_attempt':
        db.execute("UPDATE ai_invocation_attempts SET status='failed' WHERE id='a'")
    elif damage in {'mismatched_structure','not_deferred'}:
        result=json.loads(db.fetch_one("SELECT result_json FROM ai_adoption_commits WHERE id='c'")['result_json'])
        if damage=='mismatched_structure':
            result['continuation']['macro_plan']['structure']=[{'title':'unaccepted'}]
        else:
            result['continuation']['deferred_apply']=False
        db.execute("UPDATE ai_adoption_commits SET result_json=? WHERE id='c'",(json.dumps(result),))
    elif damage=='already_applied':
        db.execute("UPDATE ai_invocation_sessions SET metadata_json=? WHERE id='legacy-macro'",
            (json.dumps({'manual_stop_recovery':True,'macro_plan_applied':True}),))
    else:
        db.execute("UPDATE ai_invocation_sessions SET operation='autopilot.act.plan' WHERE id='legacy-macro'")
    policy=AutopilotRecoveryPolicy(db)
    decision=policy.decide_on_start('recover-book')
    policy.apply_transient_cleanup(decision)
    assert db.fetch_one("SELECT status FROM ai_invocation_sessions WHERE id='legacy-macro'")['status']=='cancelled'
