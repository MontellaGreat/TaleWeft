"""Durable setup DAGs; author review separates the four generation stages."""
from __future__ import annotations

import asyncio
import copy
import inspect
import json
import uuid
from datetime import datetime, timezone

from application.engine.dag.engine import DAGEngine
from application.engine.dag.models import DAGDefinition, EdgeDefinition, NodeConfig, NodeDefinition
from application.engine.dag.registry import NodeRegistry
from application.engine.dag.runtime import DAGRuntimeConflict
from application.engine.dag.execution_evidence import data_hash
from application.engine.services.execution_lease import claim_execution_lease, get_execution_lease, refresh_execution_lease, release_execution_lease

STAGES = (
    ('worldbuilding', '文风 / 世界观', []),
    ('characters', '人物', ['worldbuilding']),
    ('locations', '地图', ['worldbuilding', 'characters']),
    ('plot_outline', '剧情总纲', ['worldbuilding', 'characters', 'locations']),
)
WORKFLOW_VERSION = 3
WORLD_UNITS = (
    ('style', '文风公约'), ('core_rules', '核心法则'), ('geography', '地理生态'),
    ('society', '社会结构'), ('culture', '历史文化'), ('daily_life', '沉浸感细节'),
    ('world_review', '完整世界观复审'),
)
UNIT_ACTIONS = (
    ('prepare', '准备上下文'), ('generate', '生成'), ('validate', '结构校验'),
    ('deviation', '偏离检测与修复'), ('anti_ai', 'Anti-AI 审计与修复'),
    ('commit', '保存资料与关联'), ('verify', '回读确认'),
)


def build_setup_graph(stage):
    from application.engine.dag.nodes.setup import SetupStepNode
    if stage not in {row[0] for row in STAGES}:
        raise ValueError('不支持的新书设置阶段')
    if not NodeRegistry.has('setup_step'):
        NodeRegistry.register('setup_step')(SetupStepNode)
    label = next(row[1] for row in STAGES if row[0] == stage)
    units = WORLD_UNITS if stage == 'worldbuilding' else ((stage, label),)
    nodes = []
    for row, (unit, unit_label) in enumerate(units):
        actions = [item for item in UNIT_ACTIONS if unit != 'world_review' or item[0] != 'generate']
        for col, (action, name) in enumerate(actions):
            prefix = f'{stage}_{unit}' if stage == 'worldbuilding' else stage
            nodes.append(NodeDefinition(id=f'{prefix}_{action}', type='setup_step',
                label=f'{unit_label} · {name}', position={'x': col * 280, 'y': row * 180 + 80},
                config=NodeConfig(max_retries=0, timeout_seconds=600,
                                  prompt_variables={'action': action, 'unit': unit})))
    edges = [EdgeDefinition(id=f'edge_{stage}_{index}', source=a.id, target=b.id)
             for index, (a, b) in enumerate(zip(nodes, nodes[1:]))]
    return DAGDefinition(id=f'setup_{stage}', name=f'新书设置 · {label}', nodes=nodes, edges=edges)


async def owned_db_call(function, *args):
    """Cancellation waits for the SQLite owner before releasing its lease."""
    worker = asyncio.create_task(asyncio.to_thread(function, *args))
    cancelled = False
    while not worker.done():
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError:
            cancelled = True
    result = worker.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


def _now():
    return datetime.now(timezone.utc).isoformat()


class SetupDAGStore:
    def __init__(self, db):
        self.db = db

    def ensure(self):
        self.db.execute('''CREATE TABLE IF NOT EXISTS setup_dag_runs (
            id TEXT PRIMARY KEY, novel_id TEXT NOT NULL, stage TEXT NOT NULL,
            status TEXT NOT NULL, snapshot_json TEXT NOT NULL, started_at TEXT NOT NULL)''')
        self.db.execute('CREATE INDEX IF NOT EXISTS setup_runs_novel ON setup_dag_runs(novel_id, started_at DESC)')
        self.db.commit()

    def create(self, run_id, novel_id, stage, graph):
        self.ensure()
        snapshot = dict(run_id=run_id, novel_id=novel_id, stage=stage, status='running',
                        workflow_version=WORKFLOW_VERSION, checkpoint={},
                        resume_from_run_id=None, resumed_node_count=0,
                        graph=graph.model_dump(mode='json'), started_at=_now(), completed_at=None,
                        node_states={n.id: {'status': 'pending'} for n in graph.nodes}, error=None)
        self.db.execute('INSERT INTO setup_dag_runs VALUES (?,?,?,?,?,?)',
                        (run_id, novel_id, stage, 'running', json.dumps(snapshot, ensure_ascii=False), snapshot['started_at']))
        self.db.commit()
        return snapshot

    def save(self, snapshot):
        self.db.execute('UPDATE setup_dag_runs SET status=?,snapshot_json=? WHERE id=? AND novel_id=?',
                        (snapshot['status'], json.dumps(snapshot, ensure_ascii=False), snapshot['run_id'], snapshot['novel_id']))
        self.db.commit()

    def get(self, novel_id, run_id):
        self.ensure()
        row = self.db.fetch_one('SELECT snapshot_json FROM setup_dag_runs WHERE novel_id=? AND id=?', (novel_id, run_id))
        if row is None:
            raise LookupError('新书工作流记录不存在')
        return json.loads(row['snapshot_json'])

    def latest(self, novel_id, stage=None):
        self.ensure()
        clause = ' AND stage=?' if stage else ''
        row = self.db.fetch_one(f'SELECT snapshot_json FROM setup_dag_runs WHERE novel_id=?{clause} ORDER BY started_at DESC LIMIT 1',
                                (novel_id, stage) if stage else (novel_id,))
        return json.loads(row['snapshot_json']) if row else None


def with_progress(snapshot):
    snapshot = dict(snapshot)
    nodes = snapshot['graph']['nodes']
    states = snapshot['node_states']
    completed = sum(states.get(node['id'], {}).get('status') in {'success', 'completed'} for node in nodes)
    current = next((node for node in nodes if states.get(node['id'], {}).get('status') == 'running'), None)
    if current is None:
        current = next((node for node in nodes if states.get(node['id'], {}).get('status') == 'error'), None)
    label = current['label'] if current else ''
    snapshot['progress'] = dict(completed=completed, total=len(nodes), percent=int(completed * 100 / len(nodes)),
                                current_node_id=current['id'] if current else None, current_node_label=label,
                                message=label or {'completed': '生成与保存已完成，请核对后确认', 'cancelled': '工作流已停止',
                                                  'interrupted': '工作流中断，已保存进度，可继续生成',
                                                  'error': '当前步骤失败，已保存进度，可继续生成'}.get(snapshot['status'], '等待调度'))
    return snapshot


class SetupDAGManager:
    def __init__(self, db, services_factory, *, lease_seconds=120):
        self.db, self.services_factory = db, services_factory
        self.store = SetupDAGStore(db)
        self.tasks = {}
        self.run_ids = {}
        self.live = {}
        self.lease_seconds = lease_seconds
        self._lock = asyncio.Lock()

    async def start(self, novel_id, stage, *, restart=False):
        graph = build_setup_graph(stage)
        async with self._lock:
            if novel_id in self.tasks and not self.tasks[novel_id].done():
                raise DAGRuntimeConflict('新书工作流正在执行，请查看进度或停止后重试')
            if not await owned_db_call(self.db.fetch_one, 'SELECT id FROM novels WHERE id=?', (novel_id,)):
                raise LookupError('小说不存在')
            run_id = f'setup_{uuid.uuid4().hex}'
            try:
                claimed = await owned_db_call(claim_execution_lease, novel_id, 'setup', run_id, self.lease_seconds, self.db)
                if not claimed:
                    lease = await owned_db_call(get_execution_lease, novel_id, self.db)
                    raise DAGRuntimeConflict(f"本书正由 {str((lease or {}).get('owner', '其他任务'))} 执行，请先查看运行状态并停止，不能并发生成设定")
                previous = None if restart else await owned_db_call(self.store.latest, novel_id, stage)
                snapshot = await owned_db_call(self.store.create, run_id, novel_id, stage, graph)
                if (previous and previous.get('status') != 'completed'
                        and previous.get('workflow_version') == WORKFLOW_VERSION
                        and previous.get('checkpoint')
                        and all(previous['graph'].get(key) == snapshot['graph'].get(key)
                                for key in ('id', 'nodes', 'edges'))):
                    snapshot['checkpoint'] = copy.deepcopy(previous['checkpoint'])
                    snapshot['resume_from_run_id'] = previous['run_id']
                    # Only a contiguous prefix with real matching execution
                    # evidence may be reused. Its original receipt stays intact.
                    for node in graph.nodes:
                        old = previous['node_states'].get(node.id, {})
                        receipt = old.get('execution_receipt') or {}
                        if not (old.get('status') in {'success', 'completed'}
                                and receipt.get('executed') is True and receipt.get('executor_started') is True
                                and receipt.get('executor_completed') is True
                                and receipt.get('node_id') == node.id
                                and receipt.get('node_type') == node.type
                                and receipt.get('run_id') == (old.get('reused_from_run_id') or previous['run_id'])
                                and 'outputs' in old
                                and receipt.get('output_hash') == data_hash(old['outputs'])):
                            break
                        snapshot['node_states'][node.id] = {
                            **copy.deepcopy(old),
                            'reused_from_run_id': old.get('reused_from_run_id') or previous['run_id'],
                        }
                        snapshot['resumed_node_count'] += 1
                    await owned_db_call(self.store.save, snapshot)
            except BaseException:
                try:
                    abandoned = await owned_db_call(self.store.get, novel_id, run_id)
                    abandoned['status'], abandoned['completed_at'] = 'cancelled', _now()
                    await owned_db_call(self.store.save, abandoned)
                except LookupError:
                    pass
                finally:
                    await owned_db_call(release_execution_lease, novel_id, 'setup', run_id, self.db)
                raise
            self.live[run_id] = snapshot
            self.run_ids[novel_id] = run_id
            self.tasks[novel_id] = asyncio.create_task(self._run(novel_id, stage, run_id, graph))
            return with_progress(snapshot)

    async def _run(self, novel_id, stage, run_id, graph):
        snapshot = self.live[run_id]
        heartbeat = asyncio.create_task(self._heartbeat(novel_id, run_id, asyncio.current_task()))
        services = None

        async def checkpoint(value=None):
            if value is None and services is not None and hasattr(services, 'export_checkpoint'):
                value = services.export_checkpoint()
                if inspect.isawaitable(value):
                    value = await value
            if value is not None:
                # Copy now, before giving another thread a snapshot to encode.
                snapshot['checkpoint'] = json.loads(json.dumps(value, ensure_ascii=False))
            await owned_db_call(self.store.save, copy.deepcopy(snapshot))

        async def event(data):
            node_id = data['node_id']
            snapshot['node_states'][node_id] = {key: data[key] for key in
                ('status', 'error', 'duration_ms', 'execution_receipt', 'outputs') if key in data}
            await checkpoint()

        async def guard():
            # A heartbeat alone leaves a gap before a fast synchronous commit.
            # Revalidate this exact run before every real step, especially writes.
            if not await owned_db_call(refresh_execution_lease, novel_id, 'setup', run_id,
                                       self.lease_seconds, self.db):
                snapshot['error'] = '执行租约丢失，已中断以避免并发修改'
                raise asyncio.CancelledError

        try:
            services = self.services_factory(novel_id, stage)
            initial_state = {'novel_id': novel_id, 'dag_run_id': run_id,
                'setup_stage': stage, 'services': services, 'on_node_event': event,
                'setup_ownership_guard': guard, 'setup_checkpoint': copy.deepcopy(snapshot['checkpoint']),
                'setup_emit_checkpoint': checkpoint}
            count = snapshot['resumed_node_count']
            if snapshot.get('resume_from_run_id'):
                if not hasattr(services, 'restore_checkpoint'):
                    raise ValueError('此工作流不支持恢复生成数据，请选择重新生成')
                restored = services.restore_checkpoint(copy.deepcopy(snapshot['checkpoint']))
                if inspect.isawaitable(restored):
                    await restored
                if hasattr(services, 'validate_resume'):
                    valid = services.validate_resume(copy.deepcopy(snapshot['checkpoint']))
                    if inspect.isawaitable(valid):
                        valid = await valid
                    if not valid:
                        snapshot['stale_resume'] = True
                        raise ValueError('基础设定或已确认资料发生变化，请重新生成当前阶段，不能沿用过期审计')
                for node in graph.nodes[:count]:
                    initial_state.update(snapshot['node_states'][node.id].get('outputs') or {})
            remaining = graph.model_copy(update={
                'nodes': graph.nodes[count:],
                'edges': [edge for edge in graph.edges
                          if edge.source in {node.id for node in graph.nodes[count:]}
                          and edge.target in {node.id for node in graph.nodes[count:]}],
            })
            if not remaining.nodes:
                # A crash after the last writer ACK still needs readback, not
                # a completed badge inferred solely from cached success states.
                remaining = graph.model_copy(update={'nodes': graph.nodes[-1:], 'edges': []})
            result = await DAGEngine().run(remaining, initial_state)
            snapshot['status'] = result.status
            errors = [r.error for r in result.node_results.values() if r.status.value == 'error' and r.error]
            snapshot['error'] = '\n'.join(dict.fromkeys(errors)) or None
            if result.status == 'completed' and not all(
                    row.get('status') in {'success', 'completed'} for row in snapshot['node_states'].values()):
                snapshot['status'], snapshot['error'] = 'error', '工作流仍有未完成的校验，不能确认保存'
        except asyncio.CancelledError:
            snapshot['status'] = 'interrupted' if snapshot.get('error') else 'cancelled'
        except Exception as exc:
            snapshot['status'], snapshot['error'] = 'error', str(exc)
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            for state in snapshot['node_states'].values():
                if state['status'] == 'running':
                    state['status'] = 'interrupted'
            snapshot['completed_at'] = _now()
            try:
                await owned_db_call(self.store.save, snapshot)
            finally:
                await owned_db_call(release_execution_lease, novel_id, 'setup', run_id, self.db)

    async def _heartbeat(self, novel_id, run_id, owner):
        while True:
            await asyncio.sleep(max(0.2, self.lease_seconds / 3))
            try:
                refreshed = await owned_db_call(refresh_execution_lease, novel_id, 'setup', run_id, self.lease_seconds, self.db)
            except Exception:
                refreshed = False
            if not refreshed:
                self.live[run_id]['error'] = '执行租约丢失，已中断以避免并发修改'
                owner.cancel()
                return

    async def status(self, novel_id, run_id):
        snapshot = await owned_db_call(self.store.get, novel_id, run_id)
        if snapshot['status'] == 'running':
            lease = await owned_db_call(get_execution_lease, novel_id, self.db)
            if not lease or lease['owner'] != 'setup' or lease['job_id'] != run_id:
                snapshot['status'], snapshot['error'] = 'interrupted', '应用已重启或工作流执行已中断，已保存进度，可继续生成'
                snapshot['completed_at'] = _now()
                for node in snapshot['node_states'].values():
                    if node['status'] == 'running':
                        node['status'] = 'interrupted'
                await owned_db_call(self.store.save, snapshot)
        return with_progress(snapshot)

    async def overview(self, novel_id):
        if not await owned_db_call(self.db.fetch_one, 'SELECT id FROM novels WHERE id=?', (novel_id,)):
            raise LookupError('小说不存在')
        stages = []
        for stage, label, dependencies in STAGES:
            latest = await owned_db_call(self.store.latest, novel_id, stage)
            stages.append(dict(id=stage, label=label, dependencies=dependencies,
                               graph=build_setup_graph(stage).model_dump(mode='json'),
                               latest_run=await self.status(novel_id, latest['run_id']) if latest else None))
        active = next((s['latest_run'] for s in stages if s['latest_run'] and s['latest_run']['status'] == 'running'), None)
        return {'stages': stages, 'active_run': active}

    async def stop(self, novel_id, run_id):
        async with self._lock:
            if self.run_ids.get(novel_id) == run_id and (task := self.tasks.get(novel_id)):
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                # A task cancelled before its first turn cannot run its finally.
                snapshot = self.live.get(run_id)
                if snapshot and snapshot['status'] == 'running':
                    snapshot['status'], snapshot['completed_at'] = 'cancelled', _now()
                    await owned_db_call(self.store.save, snapshot)
                    await owned_db_call(release_execution_lease, novel_id, 'setup', run_id, self.db)
            result = await self.status(novel_id, run_id)
            if result['status'] == 'running':
                raise DAGRuntimeConflict('工作流由另一个执行器持有，请在启动任务的应用中停止，或等待执行租约失效后重试')
            return result

    async def shutdown(self):
        for novel_id, run_id in list(self.run_ids.items()):
            await self.stop(novel_id, run_id)
