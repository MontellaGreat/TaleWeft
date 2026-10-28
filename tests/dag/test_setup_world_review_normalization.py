"""Aggregate repairs must audit exactly the prose that canonical saves keep."""
from copy import deepcopy
import json

import pytest

from application.ai_invocation.dtos import InvocationAttemptStatus
from application.audit.services import anti_ai_audit
from application.world.worldbuilding_schema import WORLD_BUILDING_DIMENSION_KEYS, schema_field_order
from tests.dag.test_setup_dag_adversarial import adapter_services, setup_db
from tests.dag.test_setup_generation_gates import approved_review
from tests.dag.test_setup_world_schema_recovery import geography_candidate


PROSE = '渡口守卫每天清点燃料与绳索，潮水上涨后关闭低处通道，居民改走上游的石桥。'


def whole_world(shape='dict'):
    value = {'约束': PROSE}
    if shape == 'list':
        value = [value]
    elif shape == 'json':
        value = json.dumps(value, ensure_ascii=False)
    return {'style': '用具体动作与物件呈现代价，避免解释性旁白替读者概括感受。', 'worldbuilding': {
        unit: {field: deepcopy(value) for field in schema_field_order(unit)}
        for unit in WORLD_BUILDING_DIMENSION_KEYS}}


def aggregate_candidate(replies=(), *, shape='dict'):
    steps = geography_candidate(replies)
    steps.unit = 'world_review'
    steps.units = {}
    steps._install_candidate(whole_world(shape), purpose='fixture_aggregate_repair')
    return steps


@pytest.mark.parametrize('shape', ['dict', 'list', 'json'])
def test_aggregate_projection_matches_actual_sqlite_save(shape, adapter_services):
    _, world, _, _ = adapter_services
    steps = aggregate_candidate(shape=shape)
    projected = steps._narrow(steps.payload)
    saved = world.update_worldbuilding('novel', **projected['worldbuilding']).normalized_dimensions()
    assert projected['worldbuilding'] == saved
    assert saved['geography']['terrain'] == ('约束：' if shape == 'list' else '【约束】') + PROSE


def test_legacy_aggregate_validation_installs_provenance_and_clears_old_audits():
    steps = aggregate_candidate()
    old_id = steps.attempt.id
    steps._record()['audits'] = {'deviation': {'passed': True}, 'anti_ai': {'passed': True}}
    steps.validate('validate')
    assert steps.payload['worldbuilding']['geography']['terrain'] == '【约束】' + PROSE
    assert steps.attempt.id != old_id
    assert steps.attempt.accounting['source_attempt_id'] == old_id
    assert steps._record()['audits'] == {}
    with pytest.raises(ValueError, match='审核'):
        steps._assert_audited()


@pytest.mark.parametrize('tamper', ['content', 'session', 'status'])
def test_legacy_aggregate_conversion_rejects_wrong_source(tamper):
    steps = aggregate_candidate()
    if tamper == 'content':
        steps.attempt.content = '{}'
    elif tamper == 'session':
        steps.attempt.session_id = 'another-session'
    else:
        steps.attempt.status = InvocationAttemptStatus.FAILED
    with pytest.raises(ValueError, match='原始调用'):
        steps.validate('validate')


@pytest.mark.asyncio
async def test_damaged_aggregate_repair_resumes_with_all_dimensions_and_fresh_model_reviews(monkeypatch):
    monkeypatch.setattr(anti_ai_audit, 'get_anti_ai_auditor', lambda **_: anti_ai_audit.AntiAIAuditor())
    complete = whole_world()
    partial = deepcopy(complete)
    partial['worldbuilding'].pop('culture')

    def rejected(request):
        return {**approved_review(request), 'passed': False,
                'issues': [{'description': 'Keep all five world dimensions and their established costs.'}]}

    steps = aggregate_candidate([rejected, partial, complete, approved_review])
    steps.run_id = 'before-interruption'
    with pytest.raises(ValueError, match='culture'):
        await steps.deviation()
    checkpoint = steps.export_checkpoint()
    assert 'culture' not in checkpoint['units']['world_review']['payload']['worldbuilding']
    assert checkpoint['units']['world_review']['audits'] == {}
    steps.restore_checkpoint(checkpoint)
    await steps.run_step('anti_ai', {'dag_run_id': 'after-restart', 'setup_unit': 'world_review'})
    assert set(steps.payload['worldbuilding']) == set(WORLD_BUILDING_DIMENSION_KEYS)
    assert all(isinstance(text, str) for fields in steps.payload['worldbuilding'].values() for text in fields.values())
    steps._assert_audited()
    calls = steps._record()['model_calls']
    assert [row['purpose'] for row in calls] == [
        'deviation_review', 'deviation_repair', 'schema_repair', 'deviation_review', 'anti_ai_semantic_review']
