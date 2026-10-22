"""Production scheduler/scanner/SQLite writer exercised with an isolated provider."""
import asyncio
import json
import sqlite3
import threading
from pathlib import Path

import pytest

from application.engine.dag.chapter_services import DAGChapterServices, content_hash
from application.engine.dag.runtime import DAGExecutionManager, DAGRuntimeConflict
from application.engine.services import persistence_queue as queue_module
from application.engine.services.persistence_queue import PersistenceQueue, register_persistence_handlers
from application.engine.services.execution_lease import claim_execution_lease, get_execution_lease, release_execution_lease
from infrastructure.persistence.database.connection import DatabaseConnection


RAW = "他的嘴角上扬，眼中闪过一丝笑意。"
CLEAN = "他把钥匙压在账本下，抽走夹着日期的纸条。门外的人敲了两下，他收紧手指，没有应声。"


class Provider:
    def __init__(self, *, dirty=True, rewrite=CLEAN, delay=None):
        self.dirty, self.rewrite, self.delay = dirty, rewrite, delay
        self.calls = []

    async def generate(self, prompt, config):
        self.calls.append((prompt, config))
        if "plot_tension" in prompt.system or "plot_tension" in prompt.user:
            return json.dumps({"plot_tension": 61, "emotional_tension": 54, "pacing_tension": 48})
        if "次文风定向修正" in prompt.user:
            return self.rewrite
        if self.delay:
            self.delay.set()
            await asyncio.Future()
        return RAW if self.dirty else CLEAN


class Aftermath:
    async def run_after_chapter_saved(self, *args, **kwargs):
        return {"narrative_sync_ok": True, "summary": "钥匙线索"}


@pytest.fixture
def writer(tmp_path, monkeypatch):
    monkeypatch.delenv("PLOTPILOT_ALLOW_DIRECT_SQLITE_WRITES", raising=False)
    monkeypatch.delenv("AITEXT_ALLOW_DIRECT_SQLITE_WRITES", raising=False)
    from infrastructure.persistence.database import connection as connection_module
    db = DatabaseConnection(str(tmp_path / "dag.sqlite"))
    monkeypatch.setattr(connection_module, "_db_instance", db)
    monkeypatch.setattr(connection_module, "_db_instances_by_path", {str(Path(db.db_path).resolve()): db})
    queue = PersistenceQueue()
    queue.initialize()
    monkeypatch.setattr(queue_module, "_persistence_queue", queue)
    register_persistence_handlers()
    queue.start_consumer()
    db.execute("INSERT INTO novels(id,title,slug,target_chapters) VALUES ('novel','临时测试','temporary',2)")
    db.execute("INSERT INTO chapters(id,novel_id,number,title,outline,status,content) VALUES ('chapter','novel',1,'第一章','1. 寻找账本。\n2. 发现钥匙。','draft','旧正文')")
    db.commit()
    yield db, queue
    queue.stop_consumer()
    db.close_all(skip_checkpoint=True)
    queue.get_queue().close()
    queue.get_queue().join_thread()


def manager(db, provider, *, service_cls=DAGChapterServices, events=None, lease_seconds=120):
    return DAGExecutionManager(db, provider, publish=lambda _novel, event: (events if events is not None else []).append(event),
        services_factory=lambda database, llm, **kwargs: service_cls(database, llm, aftermath=Aftermath(), **kwargs), lease_seconds=lease_seconds)


@pytest.mark.asyncio
async def test_real_scanner_rewrite_reaudit_commit_and_history(writer):
    db, _queue = writer
    events, provider = [], Provider()
    runtime = manager(db, provider, events=events)
    ack = await runtime.start("novel")
    assert ack["status"] == "started" and runtime.store.get("novel", ack["dag_run_id"])
    await runtime.tasks["novel"]
    status = await runtime.status("novel")
    assert status["status"] == "awaiting_review", status
    with sqlite3.connect(db.db_path) as independent:
        text, chapter_status = independent.execute("SELECT content,status FROM chapters WHERE id='chapter'").fetchone()
    assert text == CLEAN and chapter_status == "reviewing"
    assert status["result"]["final_state"]["saved_content_hash"] == content_hash(CLEAN)
    audits = [event for event in events if event.get("node_id") == "val_anti_ai" and event["type"] == "node_status_change" and event.get("outputs")]
    assert len(audits) == 2 and audits[0]["outputs"]["total_hits"] > 0 and audits[1]["outputs"]["total_hits"] == 0
    assert audits[1]["attempt"] == 1
    assert status["result"]["node_results"]["val_anti_ai"]["outputs"]["total_hits"] == 0
    assert any("Anti-AI" in call[0].user for call in provider.calls)
    assert get_execution_lease("novel", db) is None
    stored = runtime.store.events(ack["dag_run_id"])
    assert any(event.get("outputs", {}).get("candidate_id") for event in stored)
    candidates = db.fetch_all("SELECT kind,status,content FROM chapter_rewrite_candidates ORDER BY rowid")
    assert [(c["kind"], c["status"]) for c in candidates] == [("generated", "rejected"), ("rewrite", "committed")]


@pytest.mark.asyncio
async def test_scanner_failure_blocks_commit_and_preserves_candidate(writer, monkeypatch):
    db, _queue = writer
    from application.audit.services import anti_ai_audit
    monkeypatch.setattr(anti_ai_audit, "get_anti_ai_auditor", lambda: (_ for _ in ()).throw(RuntimeError("scanner broken")))
    runtime = manager(db, Provider())
    await runtime.start("novel")
    await runtime.tasks["novel"]
    status = await runtime.status("novel")
    assert status["status"] == "error"
    assert status["node_states"]["val_anti_ai"]["status"] == "error"
    assert db.fetch_one("SELECT content FROM chapters WHERE id='chapter'")["content"] == "旧正文"
    assert db.fetch_one("SELECT content FROM chapter_rewrite_candidates")["content"] == RAW


@pytest.mark.asyncio
async def test_rewrite_budget_exhaustion_does_not_save(writer):
    db, _queue = writer
    runtime = manager(db, Provider(rewrite=RAW + '他没有动。'))
    await runtime.configure("novel", "gw_retry", patch={"max_retries": 1})
    await runtime.start("novel")
    await runtime.tasks["novel"]
    status = await runtime.status("novel")
    assert status["status"] == "error"
    assert "上限 (1)" in status["node_states"]["gw_retry"]["error"]
    assert db.fetch_one("SELECT content FROM chapters WHERE id='chapter'")["content"] == "旧正文"
    assert len(db.fetch_all("SELECT * FROM chapter_rewrite_candidates")) == 2


@pytest.mark.asyncio
async def test_rewrite_noop_fails_without_committing(writer):
    db, _queue = writer
    runtime = manager(db, Provider(rewrite=RAW))
    await runtime.start('novel')
    await runtime.tasks['novel']
    status = await runtime.status('novel')
    assert status['status'] == 'error'
    assert '原正文' in status['node_states']['gw_retry']['error']
    assert db.fetch_one("SELECT content FROM chapters WHERE id='chapter'")['content'] == '旧正文'


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['lease','expiry','aba'])
async def test_commit_fence_blocks_lost_lease_and_aba(writer, change):
    db, _queue = writer

    class ChangingServices(DAGChapterServices):
        def commit_chapter(self, state):
            if change == 'lease':
                self.db.execute("UPDATE novel_execution_leases SET owner='autopilot',job_id='successor'")
            elif change == 'expiry':
                self.db.execute('UPDATE novel_execution_leases SET expires_at=0')
            else:
                self.db.execute("UPDATE chapters SET content='作者中间版' WHERE id='chapter'")
                self.db.execute("UPDATE chapters SET content='旧正文' WHERE id='chapter'")
            self.db.commit()
            return super().commit_chapter(state)

    runtime = manager(db, Provider(dirty=False), service_cls=ChangingServices)
    await runtime.start('novel')
    await runtime.tasks['novel']
    status = await runtime.status('novel')
    assert status['status'] == 'error'
    assert not status['result']['final_state'].get('chapter_committed')
    assert db.fetch_one("SELECT content FROM chapters WHERE id='chapter'")['content'] == '旧正文'
    assert db.fetch_one('SELECT status FROM chapter_rewrite_candidates')['status'] != 'committed'


@pytest.mark.asyncio
async def test_legacy_facade_uses_real_chapter_and_production_runtime(writer):
    from application.engine.dag.daemon_runner import DAGDaemonRunner
    db, _queue = writer
    runner = DAGDaemonRunner(db=db, llm_service=Provider(dirty=False),
        services_factory=lambda database, llm, **kwargs: DAGChapterServices(database, llm, aftermath=Aftermath(), **kwargs))
    initial = await runner._build_initial_state('novel')
    assert initial['chapter_number'] == 1 and initial['chapter_id'] == 'chapter'
    status = await runner.run_novel('novel')
    assert status['status'] == 'awaiting_review'
    assert status['result']['final_state']['chapter_committed']
    assert db.fetch_one("SELECT content FROM chapters WHERE id='chapter'")['content'] == CLEAN


@pytest.mark.asyncio
async def test_shared_lease_rejects_other_engine_and_stop_cancels_provider(writer):
    db, _queue = writer
    assert claim_execution_lease("novel", "autopilot", "autopilot-run", db=db)
    runtime = manager(db, Provider())
    with pytest.raises(DAGRuntimeConflict):
        await runtime.start("novel")
    assert release_execution_lease("novel", "autopilot", "autopilot-run", db)
    entered = asyncio.Event()
    runtime = manager(db, Provider(delay=entered))
    ack = await runtime.start("novel")
    await asyncio.wait_for(entered.wait(), 5)
    with pytest.raises(DAGRuntimeConflict):
        await runtime.start("novel")
    with pytest.raises(DAGRuntimeConflict):
        await runtime.configure("novel", "exec_writer", patch={"temperature": 0.1})
    stop = await runtime.stop("novel", ack["dag_run_id"])
    assert stop["quiescent"] and runtime.tasks["novel"].done()
    assert (await runtime.status("novel"))["status"] == "cancelled"
    assert get_execution_lease("novel", db) is None
    assert db.fetch_one("SELECT content FROM chapters WHERE id='chapter'")["content"] == "旧正文"


@pytest.mark.asyncio
async def test_concurrent_author_edit_rejects_cas_and_keeps_both_texts(writer):
    db, _queue = writer

    class EditingServices(DAGChapterServices):
        def commit_chapter(self, state):
            self.db.execute("UPDATE chapters SET content='作者新稿' WHERE id='chapter'")
            self.db.commit()
            return super().commit_chapter(state)

    runtime = manager(db, Provider(dirty=False), service_cls=EditingServices)
    await runtime.start("novel")
    await runtime.tasks["novel"]
    assert (await runtime.status("novel"))["status"] == "error"
    assert db.fetch_one("SELECT content FROM chapters WHERE id='chapter'")["content"] == "作者新稿"
    assert db.fetch_one("SELECT content FROM chapter_rewrite_candidates")["content"] == CLEAN


@pytest.mark.asyncio
async def test_config_is_committed_frozen_and_applied_to_provider(writer):
    db, _queue = writer
    provider = Provider(dirty=False)
    runtime = manager(db, provider)
    before = runtime.load_dag("novel")
    saved = await runtime.configure("novel", "exec_writer", patch={"temperature": 0.17, "model_override": "chosen-model", "max_tokens": 777})
    assert saved.version > before.version
    reopened = manager(db, provider).load_dag("novel")
    assert reopened.get_node("exec_writer").config.temperature == 0.17
    await runtime.start("novel")
    await runtime.tasks["novel"]
    writing = next(config for prompt, config in provider.calls if "plot_tension" not in prompt.system and "plot_tension" not in prompt.user)
    assert (writing.temperature, writing.model, writing.max_tokens) == (0.17, "chosen-model", 777)
    assert json.loads(runtime.store.latest("novel")["graph_json"])["version"] == saved.version


@pytest.mark.asyncio
async def test_stop_waits_for_inflight_commit_ack_before_releasing_lease(writer):
    db, _queue = writer
    entered, release = threading.Event(), threading.Event()

    class DelayedServices(DAGChapterServices):
        def commit_chapter(self, state):
            entered.set()
            assert release.wait(5)
            return super().commit_chapter(state)

    runtime = manager(db, Provider(dirty=False), service_cls=DelayedServices)
    await runtime.start("novel")
    assert await asyncio.to_thread(entered.wait, 5)
    stopping = asyncio.create_task(runtime.stop("novel"))
    await asyncio.sleep(0.05)
    assert not stopping.done() and get_execution_lease("novel", db)
    another_stop = asyncio.create_task(runtime.stop("novel"))
    release.set()
    await asyncio.gather(stopping, another_stop)
    assert get_execution_lease("novel", db) is None
    status = await runtime.status("novel")
    assert status["status"] == "cancelled" and status["result"]["final_state"]["chapter_committed"] is True
    assert db.fetch_one("SELECT content FROM chapters WHERE id='chapter'")["content"] == CLEAN
