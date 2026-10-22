"""Fault/ownership probes against production DAG paths and the real SQLite writer."""
import asyncio
import hashlib
from types import SimpleNamespace

import pytest

from application.audit.services.anti_ai_audit import AntiAIAuditReport, AntiAIAuditor
from application.engine.dag.anti_ai_scan import scan_chapter
from application.engine.dag.engine import DAGEngine
from application.engine.dag.models import DAGDefinition, NodeDefinition, NodeResult, NodeConfig
from application.engine.dag.registry import NodeRegistry
from application.engine.services.execution_lease import get_execution_lease
from tests.dag.test_production_runtime import writer, manager, Provider, CLEAN


@pytest.mark.asyncio
async def test_stop_before_run_coroutine_starts_releases_lease_and_archives_cancel(writer):
    db, _queue = writer
    runtime = manager(db, Provider())
    ack = await runtime.start('novel')
    await runtime.stop('novel', ack['dag_run_id'])
    assert runtime.tasks['novel'].done()
    assert get_execution_lease('novel', db) is None
    assert runtime.store.get('novel', ack['dag_run_id'])['status'] == 'cancelled'
    assert (await runtime.status('novel'))['status'] == 'cancelled'


@pytest.mark.asyncio
async def test_orphaned_running_job_can_resume_its_preserved_candidate(writer):
    db, _queue = writer
    runtime = manager(db, Provider(dirty=False))
    dag = runtime.load_dag('novel')
    chapter, initial = runtime._chapter('novel')
    initial['dag_run_id'] = 'crashed-run'
    runtime.store.create('crashed-run', 'novel', chapter, dag, initial)
    runtime.store.event('crashed-run', 'novel', {'node_id': 'exec_writer', 'status': 'success', 'outputs': {'content': CLEAN}})
    assert (await runtime.status('novel'))['status'] == 'interrupted'
    ack = await runtime.start('novel', resume_run_id='crashed-run')
    await runtime.tasks['novel']
    assert (await runtime.status('novel'))['status'] == 'awaiting_review'
    assert runtime.store.get('novel', ack['dag_run_id'])['status'] == 'awaiting_review'
    assert db.fetch_one("SELECT content FROM chapters WHERE id='chapter'")['content'] == CLEAN


@pytest.mark.asyncio
async def test_event_archive_failure_cancels_and_joins_parallel_node(monkeypatch):
    dag = DAGDefinition(id='fault', name='fault', nodes=[
        NodeDefinition(id='exec_writer', type='exec_writer'),
        NodeDefinition(id='val_tension', type='val_tension'),
    ])
    running, cancelled = asyncio.Event(), asyncio.Event()
    def executor(kind, node_id, config):
        async def run(state):
            if node_id == 'exec_writer':
                await running.wait()
                return NodeResult(outputs={'content': CLEAN})
            running.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()
        return run
    async def archive(event):
        if event['node_id'] == 'exec_writer' and event['status'] == 'success':
            raise RuntimeError('durable event writer failed')
    monkeypatch.setattr(NodeRegistry, 'create_executor', executor)
    result = await DAGEngine().run(dag, {'on_node_event': archive})
    assert result.status == 'error'
    assert cancelled.is_set(), 'No sibling may outlive the terminal job result/lease release'


@pytest.mark.parametrize('failure', ['failed_status', 'wrong_hash', 'nan_score'])
def test_invalid_scanner_evidence_never_reports_success(monkeypatch, failure):
    report = AntiAIAuditReport(content_hash=hashlib.sha256(CLEAN.encode()).hexdigest())
    if failure == 'failed_status':
        report.scan_status, report.error = 'failed', 'scanner interrupted'
    elif failure == 'wrong_hash':
        report.content_hash = 'f' * 64
    else:
        report.metrics.severity_score = float('nan')
    monkeypatch.setattr(AntiAIAuditor, 'scan_chapter', lambda *args: report)
    from application.audit.services import anti_ai_audit
    monkeypatch.setattr(anti_ai_audit, 'get_anti_ai_auditor', lambda db=None: AntiAIAuditor())
    result = scan_chapter(CLEAN, {})
    assert result.status.value == 'error'
    assert result.error


def test_project_scanner_uses_project_rules_not_global_database(writer):
    from application.audit.services.quality_rule_models import QualityRuleConfig, LiteralQualityRule
    from infrastructure.ai.quality_rule_store import QualityRuleStore
    db, _queue = writer
    store = QualityRuleStore(db)
    store.publish(QualityRuleConfig(rules=[LiteralQualityRule(name='project-only', text='账本', severity='critical')]),
                  expected_version=store.current()['id'], note='isolated project rules')
    # The active application DB accessor must not participate in this run.
    from infrastructure.ai import quality_rule_store
    original = quality_rule_store.get_database
    quality_rule_store.get_database = lambda: (_ for _ in ()).throw(AssertionError('global DB accessed'))
    try:
        result = scan_chapter(CLEAN, {'shared_state': {'chapter_id': 'chapter'}, 'services': SimpleNamespace(db=db)})
    finally:
        quality_rule_store.get_database = original
    assert result.status.value == 'warning', result
    assert any(hit['pattern'] == 'project-only' for hit in result.outputs['hits'])


@pytest.mark.asyncio
async def test_dag_aftermath_receives_ack_version_and_candidate_records_it(writer):
    from application.engine.dag.chapter_services import DAGChapterServices
    db, _queue = writer
    observed = []
    class FrozenAftermath:
        async def run_after_chapter_saved(self, novel_id, chapter_number, content, **kwargs):
            observed.append(kwargs)
            assert kwargs['expected_revision'] == db.fetch_one("SELECT revision FROM chapters WHERE id='chapter'")['revision']
            assert kwargs['expected_content_hash'] == hashlib.sha256(content.encode()).hexdigest()
            return {'narrative_sync_ok': True, 'derivation_status': 'completed'}
    runtime = manager(db, Provider(dirty=False))
    runtime.services_factory = lambda database, llm, **kwargs: DAGChapterServices(database, llm, aftermath=FrozenAftermath(), **kwargs)
    await runtime.start('novel')
    await runtime.tasks['novel']
    assert (await runtime.status('novel'))['status'] == 'awaiting_review'
    assert len(observed) == 1
    from application.engine.services.chapter_rewrite_candidates import ChapterRewriteCandidates
    candidate = ChapterRewriteCandidates(db).list_for_chapter('novel')[0]
    assert candidate['audit']['committed_revision'] == observed[0]['expected_revision']


@pytest.mark.asyncio
async def test_missing_required_audit_dependency_rejected_before_provider_or_commit(writer):
    db, _queue = writer
    provider = Provider(dirty=False)
    runtime = manager(db, provider)
    dag = runtime.load_dag('novel')
    dag.edges = [edge for edge in dag.edges if not (edge.source == 'exec_writer' and edge.target == 'val_tension')]
    runtime.versions.save('novel', dag)
    try:
        with pytest.raises(ValueError, match='val_tension'):
            await runtime.start('novel')
    finally:
        await runtime.shutdown()
    assert not provider.calls
    assert db.fetch_one("SELECT content FROM chapters WHERE id='chapter'")['content'] == '旧正文'
    assert get_execution_lease('novel', db) is None


@pytest.mark.asyncio
async def test_critical_rules_cannot_be_bypassed_by_high_aggregate_threshold(writer):
    db, _queue = writer
    runtime = manager(db, Provider(rewrite=CLEAN))
    await runtime.configure('novel', 'gw_retry', patch={'thresholds': {'anti_ai_max_score': 100}, 'max_retries': 0})
    await runtime.start('novel')
    await runtime.tasks['novel']
    assert (await runtime.status('novel'))['status'] == 'error'
    assert db.fetch_one("SELECT content FROM chapters WHERE id='chapter'")['content'] == '旧正文'


@pytest.mark.parametrize('value', [float('nan'), float('inf'), -1.0, 101.0])
def test_nonfinite_or_out_of_range_quality_threshold_cannot_disable_gate(value):
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        NodeConfig(thresholds={'anti_ai_max_score': value})


@pytest.mark.asyncio
async def test_resume_uses_failed_jobs_chapter_instead_of_first_draft(writer):
    db, _queue = writer
    db.execute("INSERT INTO chapters(id,novel_id,number,title,outline,status,content) VALUES ('chapter2','novel',2,'第二章','寻找账本','draft','第二章旧稿')")
    runtime = manager(db, Provider(dirty=False))
    dag = runtime.load_dag('novel')
    chapter, initial = runtime._chapter('novel', 2)
    runtime.store.create('failed-second', 'novel', chapter, dag, initial)
    db.execute("UPDATE dag_jobs SET status='error' WHERE id='failed-second'")
    runtime.store.event('failed-second', 'novel', {'node_id': 'exec_writer', 'status': 'success', 'outputs': {'content': CLEAN}})
    ack = await runtime.start('novel', resume_run_id='failed-second')
    await runtime.tasks['novel']
    assert ack['chapter_number'] == 2
    assert (await runtime.status('novel'))['status'] == 'awaiting_review'
    assert db.fetch_one("SELECT content FROM chapters WHERE id='chapter'")['content'] == '旧正文'
    assert db.fetch_one("SELECT content FROM chapters WHERE id='chapter2'")['content'] == CLEAN


@pytest.mark.asyncio
async def test_late_content_change_after_quality_gate_does_not_reach_writer(writer):
    from application.engine.dag.chapter_services import DAGChapterServices
    db, _queue = writer
    class Tampered(DAGChapterServices):
        def commit_chapter(self, state):
            return super().commit_chapter({**state, 'content': '另一节点晚到的未审计正文'})
    runtime = manager(db, Provider(dirty=False), service_cls=Tampered)
    await runtime.start('novel')
    await runtime.tasks['novel']
    assert (await runtime.status('novel'))['status'] == 'error'
    assert db.fetch_one("SELECT content FROM chapters WHERE id='chapter'")['content'] == '旧正文'
    assert db.fetch_one('SELECT content,status FROM chapter_rewrite_candidates') == {'content': CLEAN, 'status': 'audited'}


@pytest.mark.asyncio
async def test_candidate_rejected_after_validation_before_writer_cannot_commit(writer, monkeypatch):
    from contextlib import contextmanager
    from application.engine.dag.chapter_services import DAGChapterServices
    db, _queue = writer
    class Rejected(DAGChapterServices):
        def commit_chapter(self, state):
            transaction = self.db.transaction
            @contextmanager
            def intervene(*args, **kwargs):
                self.candidates.mark_failed(self.candidate_id, '作者并发拒绝', 'rejected')
                with transaction(*args, **kwargs) as actual:
                    yield actual
            with monkeypatch.context() as patch:
                patch.setattr(self.db, 'transaction', intervene)
                return super().commit_chapter(state)
    runtime = manager(db, Provider(dirty=False), service_cls=Rejected)
    await runtime.start('novel')
    await runtime.tasks['novel']
    assert (await runtime.status('novel'))['status'] == 'error'
    assert db.fetch_one("SELECT content FROM chapters WHERE id='chapter'")['content'] == '旧正文'
    assert db.fetch_one('SELECT status FROM chapter_rewrite_candidates')['status'] == 'rejected'


@pytest.mark.asyncio
async def test_unavailable_quality_validator_blocks_clean_candidate(writer):
    from application.engine.dag.chapter_services import DAGChapterServices
    from engine.runtime.policy_validator import PolicyReport
    db, _queue = writer
    validator = SimpleNamespace(check=lambda **kwargs: PolicyReport(status='unavailable', error='quality checker offline'))
    runtime = manager(db, Provider(dirty=False))
    runtime.services_factory = lambda database, llm, **kwargs: DAGChapterServices(database, llm, aftermath=SimpleNamespace(), validator=validator, **kwargs)
    await runtime.start('novel')
    await runtime.tasks['novel']
    assert (await runtime.status('novel'))['status'] == 'error'
    assert db.fetch_one("SELECT content FROM chapters WHERE id='chapter'")['content'] == '旧正文'


def test_aof_recovery_rejects_author_aba_and_keeps_draft(writer, tmp_path, monkeypatch):
    from application.engine.services import draft_aof
    db, _queue = writer
    directory = tmp_path / 'drafts'
    directory.mkdir()
    monkeypatch.setattr(draft_aof, '_draft_dir', lambda: directory)
    monkeypatch.setattr('application.paths.get_db_path', lambda: db.db_path)
    path = directory / 'novel_ch1.draft'
    path.write_text('旧正文加上崩溃前增量', encoding='utf-8')
    execute = db.execute
    def author_aba(sql, params=()):
        if "COALESCE(content, '') = ?" in sql:
            execute("UPDATE chapters SET content='作者中间版' WHERE id='chapter'")
            execute("UPDATE chapters SET content='旧正文' WHERE id='chapter'")
        return execute(sql, params)
    monkeypatch.setattr(db, 'execute', author_aba)
    assert draft_aof.recover_all_drafts() == 0
    assert path.read_text(encoding='utf-8') == '旧正文加上崩溃前增量'
    assert db.fetch_one("SELECT content FROM chapters WHERE id='chapter'")['content'] == '旧正文'

