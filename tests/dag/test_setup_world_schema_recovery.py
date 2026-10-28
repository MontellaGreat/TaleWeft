"""Malformed model JSON must remain recoverable at the failed schema node."""
import json
from copy import deepcopy

import pytest

from application.ai_invocation.dtos import InvocationAttempt, InvocationAttemptStatus, stable_hash
from tests.dag.test_setup_generation_gates import candidate, approved_review


GEOGRAPHY = {
    'terrain': '渡口城依河岸坡地分为上下两片，低处旧仓库每逢涨水需要关闭通道。',
    'climate': '春季连雨会使石阶打滑，夏季风向改变，渡船每日依水位调整班次。',
    'resources': '城外林地出产修船木料，上游石场供应护岸石，运费随水位而涨。',
    'ecology': '岸边芦苇供水鸟筑巢，鱼群沿潮沟洄游，居民按汛期限制捕捞。',
}
# Same extra-closing-brace pattern as the failed desktop generation; no user prose.
MALFORMED = ('{"worldbuilding":{"geography":{"terrain":' + json.dumps(GEOGRAPHY['terrain'])
             + '},"climate":' + json.dumps(GEOGRAPHY['climate'])
             + '},"resources":' + json.dumps(GEOGRAPHY['resources'])
             + '},"ecology":' + json.dumps(GEOGRAPHY['ecology']) + '}}}')


def geography_candidate(replies):
    steps = candidate(replies)
    session = steps.session
    steps.repos['session'].get = lambda key: session if key == session.id else None
    steps._save = lambda: steps.repos['attempt'].save(steps.attempt) if steps.attempt is not None else None
    steps.unit = 'geography'
    steps.units = {}
    steps.bound = {}
    original = InvocationAttempt(id='raw-generation', session_id=steps.session.id,
        status=InvocationAttemptStatus.SUCCEEDED, prompt_snapshot=None, content=MALFORMED)
    steps.repos['attempt'].save(original)
    steps.attempt = original
    steps._install_candidate({'worldbuilding': {'geography': {'terrain': GEOGRAPHY['terrain']}}},
                             purpose='generated_current_unit')
    steps._ensure_services = lambda: None
    return steps


async def validate_node(steps):
    return await steps.run_step('validate', {'dag_run_id': 'schema-recovery', 'setup_unit': 'geography'})


@pytest.mark.asyncio
async def test_resume_repairs_missing_world_fields_using_original_raw_model_output():
    steps = geography_candidate([{'worldbuilding': {'geography': GEOGRAPHY}}])
    previous = deepcopy(steps.payload)
    steps.approved = {'style': '以动作推进故事。', 'core_rules': {'power_system': '普通人依靠工具渡河。'}}
    approved = deepcopy(steps.approved)
    result = await validate_node(steps)
    assert result['schema_validated'] is True
    assert steps.payload == {'worldbuilding': {'geography': GEOGRAPHY}}
    assert previous != steps.payload
    request = json.loads(steps.generator.llm_service.prompts[0].user)
    assert request['review']['original_model_output'] == MALFORMED
    assert all(key in request['review']['output_contract'] for key in GEOGRAPHY)
    assert steps.approved == approved
    assert steps._record()['audits'] == {}
    with pytest.raises(ValueError, match='审核'):
        steps._assert_audited()


@pytest.mark.asyncio
async def test_partial_schema_repairs_are_bounded_and_cannot_save_or_inherit_old_audits():
    partial = {'worldbuilding': {'geography': {
        'terrain': GEOGRAPHY['terrain'], 'climate': GEOGRAPHY['climate']}}}
    steps = geography_candidate([partial, {'worldbuilding': {'geography': {
        **partial['worldbuilding']['geography'], 'resources': GEOGRAPHY['resources']}}}])
    steps._record()['audits'] = {'deviation': {'passed': True}, 'anti_ai': {'passed': True}}
    with pytest.raises(ValueError, match='ecology'):
        await validate_node(steps)
    assert steps._record()['total_repairs']['schema'] == 2
    assert steps._record()['audits'] == {}
    assert 'validated_hash' not in steps._record()
    with pytest.raises(ValueError, match='审核'):
        steps._assert_audited()


@pytest.mark.asyncio
async def test_schema_repair_outage_retains_candidate_and_next_continue_can_retry():
    steps = geography_candidate([RuntimeError('schema gateway unavailable'),
        {'worldbuilding': {'geography': GEOGRAPHY}}])
    before = deepcopy(steps.payload)
    with pytest.raises(RuntimeError, match='gateway unavailable'):
        await validate_node(steps)
    assert steps.payload == before
    assert (await validate_node(steps))['schema_validated'] is True
    assert steps.payload['worldbuilding']['geography'] == GEOGRAPHY


@pytest.mark.asyncio
async def test_complete_world_dimension_does_not_trigger_repair_or_clear_current_audits():
    steps = geography_candidate([])
    steps._install_candidate({'worldbuilding': {'geography': GEOGRAPHY}}, purpose='complete')
    steps._record()['audits'] = {'deviation': {'passed': True, 'input_hash': steps._candidate_hash()}}
    before = deepcopy(steps._record()['audits'])
    assert (await validate_node(steps))['schema_validated'] is True
    assert steps.generator.llm_service.prompts == []
    assert steps._record()['audits'] == before


def test_world_validation_identifies_the_missing_fields():
    steps = geography_candidate([])
    with pytest.raises(ValueError, match='climate.*resources.*ecology'):
        steps.validate('validate')


@pytest.mark.asyncio
async def test_repaired_world_candidate_still_requires_fresh_real_review_calls(monkeypatch):
    from application.audit.services import anti_ai_audit
    monkeypatch.setattr(anti_ai_audit, 'get_anti_ai_auditor', lambda **_: anti_ai_audit.AntiAIAuditor())
    steps = geography_candidate([{'worldbuilding': {'geography': GEOGRAPHY}}, approved_review])
    await validate_node(steps)
    await steps.deviation()
    await steps.anti_ai()
    steps._assert_audited()
    audits = steps._record()['audits']
    assert audits['deviation']['input_hash'] == stable_hash(steps.payload)
    assert audits['anti_ai']['input_hash'] == stable_hash(steps.payload)
    assert {call['purpose'] for call in steps._record()['model_calls']} == {
        'schema_repair', 'deviation_review', 'anti_ai_semantic_review'}


@pytest.mark.asyncio
@pytest.mark.parametrize('resume_at', ['deviation', 'anti_ai'])
async def test_audit_node_resume_can_recover_an_incomplete_rewrite(monkeypatch, resume_at):
    from application.audit.services import anti_ai_audit
    monkeypatch.setattr(anti_ai_audit, 'get_anti_ai_auditor', lambda **_: anti_ai_audit.AntiAIAuditor())
    steps = geography_candidate([{'worldbuilding': {'geography': GEOGRAPHY}}, approved_review])
    await steps.run_step(resume_at, {'dag_run_id': 'schema-recovery', 'setup_unit': 'geography'})
    assert steps.payload['worldbuilding']['geography'] == GEOGRAPHY
    assert steps._record()['audits']['deviation']['input_hash'] == steps._candidate_hash()
    if resume_at == 'anti_ai':
        steps._assert_audited()


@pytest.mark.asyncio
async def test_unchanged_schema_repair_never_bypasses_validation():
    partial = {'worldbuilding': {'geography': {'terrain': GEOGRAPHY['terrain']}}}
    steps = geography_candidate([partial])
    with pytest.raises(ValueError, match='未修改'):
        await validate_node(steps)
    assert steps.payload == partial
    assert 'validated_hash' not in steps._record()


@pytest.mark.asyncio
@pytest.mark.parametrize('unit,field', [('core_rules', 'power_system'), ('society', 'politics'),
                                      ('culture', 'history'), ('daily_life', 'food_clothing')])
async def test_other_world_dimensions_recover_through_the_same_contract(unit, field):
    from application.world.worldbuilding_schema import schema_field_order
    complete = {key: GEOGRAPHY['terrain'] for key in schema_field_order(unit)}
    steps = geography_candidate([{'worldbuilding': {unit: complete}}])
    steps.unit = unit
    steps.units = {}
    steps._install_candidate({'worldbuilding': {unit: {field: GEOGRAPHY['terrain']}}}, purpose='generated')
    result = await steps.run_step('validate', {'dag_run_id': 'schema-recovery', 'setup_unit': unit})
    assert result['schema_validated'] is True
    assert steps.payload == {'worldbuilding': {unit: complete}}
