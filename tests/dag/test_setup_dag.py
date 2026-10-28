import asyncio

import pytest

from application.onboarding.setup_dag import SetupDAGManager, build_setup_graph
from application.engine.dag.engine import DAGEngine
from application.engine.dag.runtime import DAGRuntimeConflict
from application.engine.services.execution_lease import claim_execution_lease, get_execution_lease
from infrastructure.persistence.database.connection import DatabaseConnection


@pytest.fixture
def setup_db(tmp_path):
    db = DatabaseConnection(str(tmp_path / 'setup.sqlite'))
    db.execute("INSERT INTO novels(id,title,slug,target_chapters) VALUES ('novel','测试','setup-test',6)")
    db.commit()
    yield db
    db.close_all(skip_checkpoint=True)


class Steps:
    def __init__(self, *, fail=None, block=None):
        self.calls = []
        self.fail = fail
        self.block = block

    def handles(self, _node_type):
        return False

    async def run_step(self, action, state):
        self.calls.append(action)
        if action == self.fail:
            raise ValueError('模拟校验失败')
        if action == 'generate' and self.block:
            self.block.set()
            await asyncio.Future()
        return {f'{action}_ok': True}


@pytest.mark.parametrize('stage', ['worldbuilding', 'characters', 'locations', 'plot_outline'])
def test_setup_graph_is_executable_and_orders_verify_after_commit(stage):
    graph = build_setup_graph(stage)
    assert DAGEngine().validate(graph) == []
    assert graph.nodes[0].id.endswith('_prepare')
    assert graph.nodes[-1].id.endswith('_verify')
    assert any(e.source.endswith('_commit') and e.target.endswith('_verify') for e in graph.edges)


@pytest.mark.asyncio
async def test_setup_completion_has_actual_node_receipts_and_persisted_readback(setup_db):
    steps = Steps()
    manager = SetupDAGManager(setup_db, lambda _novel, _stage: steps)
    ack = await manager.start('novel', 'worldbuilding')
    await manager.tasks['novel']
    result = await manager.status('novel', ack['run_id'])
    assert result['status'] == 'completed'
    assert result['progress']['percent'] == 100
    assert steps.calls[:7] == ['prepare', 'generate', 'validate', 'deviation', 'anti_ai', 'commit', 'verify']
    assert steps.calls.count('generate') == 6  # Style and each of the five dimensions.
    assert steps.calls.count('deviation') == steps.calls.count('anti_ai') == 7  # Includes aggregate review.
    assert len(steps.calls) == 48
    assert all(node['execution_receipt']['executor_completed'] for node in result['node_states'].values())
    reopened = SetupDAGManager(setup_db, lambda *_: Steps())
    assert (await reopened.status('novel', ack['run_id']))['status'] == 'completed'
    assert get_execution_lease('novel', setup_db) is None


@pytest.mark.asyncio
async def test_invalid_generated_material_blocks_commit_and_completion(setup_db):
    steps = Steps(fail='validate')
    manager = SetupDAGManager(setup_db, lambda *_: steps)
    ack = await manager.start('novel', 'worldbuilding')
    await manager.tasks['novel']
    result = await manager.status('novel', ack['run_id'])
    assert result['status'] == 'error'
    assert 'commit' not in steps.calls
    assert result['node_states']['worldbuilding_style_commit']['status'] == 'bypassed'
    assert result['progress']['percent'] < 100
    assert '模拟校验失败' in result['error']
    assert get_execution_lease('novel', setup_db) is None


@pytest.mark.asyncio
async def test_setup_stop_cancels_generation_before_releasing_ownership(setup_db):
    entered = asyncio.Event()
    steps = Steps(block=entered)
    manager = SetupDAGManager(setup_db, lambda *_: steps)
    ack = await manager.start('novel', 'characters')
    await entered.wait()
    with pytest.raises(DAGRuntimeConflict):
        await manager.start('novel', 'locations')
    assert not claim_execution_lease('novel', 'dag', 'other', db=setup_db)
    stopped = await manager.stop('novel', ack['run_id'])
    assert stopped['status'] == 'cancelled'
    assert 'commit' not in steps.calls
    assert get_execution_lease('novel', setup_db) is None


@pytest.mark.asyncio
async def test_expired_setup_run_is_interrupted_instead_of_completed(setup_db):
    manager = SetupDAGManager(setup_db, lambda *_: Steps())
    graph = build_setup_graph('locations')
    manager.store.create('abandoned', 'novel', 'locations', graph)
    result = await manager.status('novel', 'abandoned')
    assert result['status'] == 'interrupted'
    assert result['progress']['percent'] < 100


@pytest.mark.asyncio
async def test_active_autopilot_lease_blocks_setup_without_changing_it(setup_db):
    assert claim_execution_lease('novel', 'autopilot', 'active', db=setup_db)
    manager = SetupDAGManager(setup_db, lambda *_: Steps())
    with pytest.raises(DAGRuntimeConflict):
        await manager.start('novel', 'worldbuilding')
    assert get_execution_lease('novel', setup_db)['job_id'] == 'active'
