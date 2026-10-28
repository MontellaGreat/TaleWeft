"""Checkpoint recovery must preserve actual work, audits and ownership."""
import asyncio
import copy

import pytest

from application.onboarding.setup_dag import SetupDAGManager, build_setup_graph
from application.engine.services.execution_lease import get_execution_lease
from infrastructure.persistence.database.connection import DatabaseConnection


@pytest.fixture
def db(tmp_path):
    database = DatabaseConnection(str(tmp_path / 'resume.sqlite'))
    database.execute("INSERT INTO novels(id,title,slug,target_chapters) VALUES ('novel','test','resume-test',6)")
    database.commit()
    yield database
    database.close_all(skip_checkpoint=True)


class DurableSteps:
    def __init__(self, calls, fail=None, *, stale=False):
        self.calls, self.fail, self.stale = calls, fail, stale
        self.checkpoint = {'version': 2, 'completed': []}

    def handles(self, _type):
        return False

    def export_checkpoint(self):
        return copy.deepcopy(self.checkpoint)

    def restore_checkpoint(self, checkpoint):
        self.checkpoint = copy.deepcopy(checkpoint)

    def validate_resume(self, checkpoint):
        return not self.stale

    async def run_step(self, action, state):
        pair = (state['setup_unit'], action)
        self.calls.append(pair)
        if pair == self.fail:
            raise ValueError('injected setup failure')
        self.checkpoint['completed'].append(list(pair))
        return {'last_unit': pair[0], 'last_action': action}


def test_worldbuilding_dimensions_are_sequential_and_each_has_real_quality_gates():
    graph = build_setup_graph('worldbuilding')
    operations = [(n.config.prompt_variables['unit'], n.config.prompt_variables['action']) for n in graph.nodes]
    actions = ['prepare', 'generate', 'validate', 'deviation', 'anti_ai', 'commit', 'verify']
    for unit in ['style', 'core_rules', 'geography', 'society', 'culture', 'daily_life']:
        assert [action for target, action in operations if target == unit] == actions
    assert [action for target, action in operations if target == 'world_review'] == [
        'prepare', 'validate', 'deviation', 'anti_ai', 'commit', 'verify']
    assert operations.index(('core_rules', 'verify')) < operations.index(('geography', 'prepare'))
    assert operations.index(('daily_life', 'verify')) < operations.index(('world_review', 'deviation'))


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['generate', 'deviation', 'anti_ai', 'commit', 'verify'])
async def test_restart_manager_continues_first_unsuccessful_node_without_regenerating_prefix(db, failure):
    first_calls, second_calls = [], []
    first = SetupDAGManager(db, lambda *_: DurableSteps(first_calls, ('geography', failure)))
    initial = await first.start('novel', 'worldbuilding')
    await first.tasks['novel']
    failed = await first.status('novel', initial['run_id'])
    assert failed['status'] == 'error'
    assert ('core_rules', 'verify') in first_calls
    assert get_execution_lease('novel', db) is None
    reopened = SetupDAGManager(db, lambda *_: DurableSteps(second_calls))
    continued = await reopened.start('novel', 'worldbuilding')
    await reopened.tasks['novel']
    result = await reopened.status('novel', continued['run_id'])
    assert result['status'] == 'completed'
    assert result['resume_from_run_id'] == initial['run_id']
    assert result['resumed_node_count'] > 0
    assert second_calls[0] == ('geography', failure)
    assert ('core_rules', 'generate') not in second_calls
    if failure != 'generate':
        assert ('geography', 'generate') not in second_calls
    reused = result['node_states']['worldbuilding_core_rules_verify']
    assert reused['reused_from_run_id'] == initial['run_id']
    assert reused['execution_receipt']['run_id'] == initial['run_id']
    assert result['progress']['percent'] == 100


@pytest.mark.asyncio
async def test_explicit_restart_repeats_generation_and_stale_inputs_cannot_reuse_approvals(db):
    first = SetupDAGManager(db, lambda *_: DurableSteps([], ('geography', 'generate')))
    await first.start('novel', 'worldbuilding')
    await first.tasks['novel']
    calls = []
    changed = SetupDAGManager(db, lambda *_: DurableSteps(calls, stale=True))
    ack = await changed.start('novel', 'worldbuilding')
    await changed.tasks['novel']
    rejected = await changed.status('novel', ack['run_id'])
    assert rejected['status'] == 'error'
    assert rejected['stale_resume'] is True
    assert calls == []
    restarted = await changed.start('novel', 'worldbuilding', restart=True)
    await changed.tasks['novel']
    result = await changed.status('novel', restarted['run_id'])
    assert result['status'] == 'completed'
    assert calls[0] == ('style', 'prepare')
    assert result['resumed_node_count'] == 0


@pytest.mark.asyncio
async def test_edited_receipt_cannot_be_reused_as_a_successful_audit(db):
    first = SetupDAGManager(db, lambda *_: DurableSteps([], ('characters', 'commit')))
    ack = await first.start('novel', 'characters')
    await first.tasks['novel']
    snapshot = first.store.get('novel', ack['run_id'])
    snapshot['node_states']['characters_anti_ai']['execution_receipt']['executor_completed'] = False
    first.store.save(snapshot)
    calls = []
    second = SetupDAGManager(db, lambda *_: DurableSteps(calls))
    resumed = await second.start('novel', 'characters')
    await second.tasks['novel']
    result = await second.status('novel', resumed['run_id'])
    assert result['status'] == 'completed'
    assert calls[0] == ('characters', 'anti_ai')


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value', [('node_id', 'characters_generate'),
                                       ('node_type', 'text_generation'),
                                       ('run_id', 'an-unrelated-run'),
                                       ('executed', 'false'), ('executor_started', 'false'),
                                       ('executor_completed', 'false')])
async def test_matching_output_hash_cannot_reuse_another_executor_receipt(db, field, value):
    first = SetupDAGManager(db, lambda *_: DurableSteps([], ('characters', 'commit')))
    ack = await first.start('novel', 'characters')
    await first.tasks['novel']
    snapshot = first.store.get('novel', ack['run_id'])
    snapshot['node_states']['characters_anti_ai']['execution_receipt'][field] = value
    first.store.save(snapshot)
    calls = []
    second = SetupDAGManager(db, lambda *_: DurableSteps(calls))
    resumed = await second.start('novel', 'characters')
    await second.tasks['novel']
    result = await second.status('novel', resumed['run_id'])
    assert result['status'] == 'completed'
    assert calls[0] == ('characters', 'anti_ai')


@pytest.mark.asyncio
async def test_legacy_rule_only_workflow_cannot_reuse_its_successful_audit_nodes(db):
    first = SetupDAGManager(db, lambda *_: DurableSteps([], ('characters', 'commit')))
    ack = await first.start('novel', 'characters')
    await first.tasks['novel']
    snapshot = first.store.get('novel', ack['run_id'])
    snapshot['workflow_version'] = 2
    first.store.save(snapshot)
    calls = []
    second = SetupDAGManager(db, lambda *_: DurableSteps(calls))
    resumed = await second.start('novel', 'characters')
    await second.tasks['novel']
    result = await second.status('novel', resumed['run_id'])
    assert result['workflow_version'] >= 3
    assert result['resumed_node_count'] == 0
    assert calls[0] == ('characters', 'prepare')
    assert first.store.get('novel', ack['run_id'])['workflow_version'] == 2
