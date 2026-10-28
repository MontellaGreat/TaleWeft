"""Old successful raw aggregate commits may resume only their exact saved prose."""
from copy import deepcopy
import json

import pytest

from dataclasses import replace
from application.ai_invocation.dtos import AdoptionCommit, AdoptionCommitStatus, AdoptionDecision, ContinuationRef, InvocationSessionStatus
from application.audit.services import anti_ai_audit
from domain.ai.value_objects.prompt import Prompt
from tests.dag.test_setup_dag_adversarial import adapter_services, setup_db
from tests.dag.test_setup_generation_gates import approved_review
from tests.dag.test_setup_world_review_normalization import aggregate_candidate, whole_world


async def old_committed_world(adapter_services, monkeypatch, post_replies=None):
    monkeypatch.setattr(anti_ai_audit, 'get_anti_ai_auditor', lambda **_: anti_ai_audit.AntiAIAuditor())
    bible, world, _, invocation = adapter_services
    steps = aggregate_candidate([approved_review, *(post_replies or [approved_review])])
    steps.novel_id = 'novel'
    repos = invocation._repositories()
    from application.world.services.bible_setup_output_bindings import bible_setup_output_bindings
    binding_id = 'legacy-world-review-output'
    repos['variable_hub'].set_bindings(binding_id, 'bible-worldbuilding', bible_setup_output_bindings('bible-worldbuilding'), direction='output')
    steps.session.context = {'novel_id': 'novel'}
    steps.session.continuation = ContinuationRef(handler_key='bible_worldbuilding')
    steps.session.metadata['setup_unit'] = 'world_review'
    steps.session.prompt_snapshot = steps._snapshot(Prompt(system='Return the complete world setup JSON.', user='Generate the world.'))
    steps.session.prompt_snapshot = replace(steps.session.prompt_snapshot, output_binding_set_id=binding_id)
    steps.attempt.prompt_snapshot = steps.session.prompt_snapshot
    steps.generator.bible_service, steps.generator.worldbuilding_service = bible, world
    repos['session'].save(steps.session)
    repos['attempt'].save(steps.attempt)
    steps.repos = repos
    steps._save = lambda: (repos['session'].save(steps.session), repos['attempt'].save(steps.attempt))
    prior_narrow = steps._narrow
    steps._narrow = lambda payload: deepcopy(payload)  # Reproduce the historical aggregate projection.
    steps.run_id = 'old-raw-commit'
    await steps.deviation()
    await steps.anti_ai()
    steps._assert_audited()
    source = deepcopy(steps.attempt)
    raw_hash = steps._candidate_hash()
    decision = AdoptionDecision(id='legacy-decision', session_id=steps.session.id, attempt_id=source.id,
        accepted_content=source.content, metadata={'audits': deepcopy(steps._record()['audits'])})
    repos['adoption'].save_decision(decision)
    repos['adoption'].save_commit(AdoptionCommit(id='legacy-commit', session_id=steps.session.id,
        decision_id=decision.id, status=AdoptionCommitStatus.SUCCEEDED))
    world.update_worldbuilding('novel', **steps.payload['worldbuilding'])
    style = steps.payload['style']
    steps._canonical = lambda: {'style': style, **world.get_worldbuilding('novel').normalized_dimensions()}
    steps._base = lambda: deepcopy(steps.base_context)
    steps._record().update(commit_id='legacy-commit', decision_id=decision.id, committed_hash=raw_hash)
    steps._capture()
    steps._narrow = prior_narrow
    return steps, source, decision, world


@pytest.mark.asyncio
async def test_legacy_saved_aggregate_normalizes_reaudits_and_reuses_real_commit(adapter_services, monkeypatch):
    steps, source, _, _ = await old_committed_world(adapter_services, monkeypatch)
    canonical = steps._canonical()
    old_hash = steps._candidate_hash()
    assert await steps.validate_resume() is True
    assert steps._record()['audits'] == {}
    checkpoint = steps.export_checkpoint()
    steps.restore_checkpoint(checkpoint)
    assert await steps.validate_resume() is True
    result = await steps.run_step('commit', {'dag_run_id': 'resume-normalized-commit', 'setup_unit': 'world_review'})
    assert result == {'commit_id': 'legacy-commit', 'already_committed': True, 'saved_data_verified': True}
    assert steps._canonical() == canonical
    assert steps.repos['attempt'].get(source.id).content == source.content
    assert steps.repos['session'].get(steps.session.id).status == InvocationSessionStatus.COMPLETED
    assert steps._candidate_hash() != old_hash
    steps._assert_audited()
    calls = deepcopy(steps._record()['model_calls'])
    await steps._verify_unit()
    await steps._verify_unit()
    assert steps._record()['model_calls'] == calls
    assert [row['purpose'] for row in calls] == [
        'deviation_review', 'anti_ai_semantic_review', 'deviation_review', 'anti_ai_semantic_review']


@pytest.mark.asyncio
@pytest.mark.parametrize('tamper', ['external_edit', 'decision_payload', 'decision_attempt', 'commit_status', 'commit_id'])
async def test_saved_aggregate_normalization_cannot_hide_edits_or_wrong_commit(adapter_services, monkeypatch, tamper):
    steps, _, decision, world = await old_committed_world(adapter_services, monkeypatch)
    if tamper == 'external_edit':
        geography = deepcopy(world.get_worldbuilding('novel').normalized_dimensions()['geography'])
        geography['terrain'] = '另一位作者改写了渡口的位置与通行规则，这不是旧候选的格式转换。'
        world.update_worldbuilding('novel', geography=geography)
    elif tamper.startswith('decision'):
        if tamper == 'decision_payload':
            decision.accepted_content = '{}'
        else:
            # Repository decisions freeze their attempt identity; use another
            # existing decision to model a mixed checkpoint without changing it.
            steps._record()['attempt_id'] = 'another-attempt'
            steps.attempt.id = 'another-attempt'
        steps.repos['adoption'].save_decision(decision)
    else:
        commit = steps.repos['adoption'].get_commit_for_decision(decision.id)
        if tamper == 'commit_status':
            commit.status = AdoptionCommitStatus.FAILED
            steps.repos['adoption'].save_commit(commit)
        else:
            steps._record()['commit_id'] = 'unrelated-commit'
    before = steps._canonical()
    with pytest.raises(ValueError):
        await steps.validate_resume()
    assert steps._canonical() == before


@pytest.mark.asyncio
async def test_normalization_marker_cannot_claim_different_text_was_the_old_commit(adapter_services, monkeypatch):
    steps, _, _, world = await old_committed_world(adapter_services, monkeypatch)
    assert await steps.validate_resume() is True
    changed = deepcopy(steps.payload['worldbuilding']['geography'])
    changed['terrain'] = '另一位作者将渡口移到山脊，新内容不可能由旧采纳候选的格式转换产生。'
    world.update_worldbuilding('novel', geography=changed)
    steps.payload['worldbuilding']['geography'] = changed
    steps._record()['saved_normalization']['candidate_hash'] = steps._candidate_hash()
    steps._record()['committed_hash'] = steps._candidate_hash()
    steps._capture()
    with pytest.raises(ValueError, match='规范化'):
        await steps.validate_resume()


@pytest.mark.asyncio
async def test_new_review_repair_after_saved_normalization_creates_a_fresh_commit(adapter_services, setup_db, monkeypatch):
    from application.engine.services.execution_lease import claim_execution_lease, release_execution_lease

    def rejected(request):
        return {**approved_review(request), 'passed': False,
                'issues': [{'description': 'Keep the harbor crossing tied to the existing fuel depot.'}]}

    repaired = whole_world()
    repaired['worldbuilding']['geography']['terrain'] = '渡口上游的燃料仓与石桥相连，守卫必须先记账领取燃料，涨水后才能启用备用绞盘。'
    steps, _, _, world = await old_committed_world(adapter_services, monkeypatch,
        post_replies=[rejected, repaired, approved_review])
    assert await steps.validate_resume() is True
    assert claim_execution_lease('novel', 'setup', 'new-saved-review', 120, setup_db)
    try:
        result = await steps.run_step('commit', {'dag_run_id': 'new-saved-review', 'setup_unit': 'world_review'})
    finally:
        release_execution_lease('novel', 'setup', 'new-saved-review', setup_db)
    assert result['saved_data_verified'] is True
    assert result['commit_id'] != 'legacy-commit'
    assert world.get_worldbuilding('novel').normalized_dimensions()['geography']['terrain'] == repaired['worldbuilding']['geography']['terrain']
    steps._assert_audited()
