"""Exercise the conventional daemon against the real single SQLite writer."""
import asyncio
import os
import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from application.audit.services import anti_ai_rewrite_workflow as workflow_module
from application.audit.services.anti_ai_rewrite_workflow import AntiAIRewriteWorkflow
from application.engine.services import persistence_queue as queue_module
from application.engine.services.chapter_rewrite_candidates import ChapterRewriteCandidates, CandidateConflictError, content_hash
from application.engine.services.persistence_queue import PersistenceQueue, PersistenceCommitTimeout, register_persistence_handlers
from application.engine.services.autopilot_control import ensure_control_schema
from application.engine.services.execution_lease import claim_execution_lease
from domain.novel.value_objects.novel_id import NovelId
from engine.runtime.daemon_host import DaemonHostMixin
from engine.runtime.policy_validator import PolicyReport
from infrastructure.persistence.database.connection import DatabaseConnection


RAW = "他的嘴角上扬，眼中闪过一丝笑意。"
CLEAN = "他把钥匙压在账本下，抽走夹着日期的纸条。门外的人敲了两下，他收紧手指，没有应声。"


@pytest.fixture
def daemon_writer(tmp_path, monkeypatch):
    monkeypatch.delenv("PLOTPILOT_ALLOW_DIRECT_SQLITE_WRITES", raising=False)
    monkeypatch.delenv("AITEXT_ALLOW_DIRECT_SQLITE_WRITES", raising=False)
    from application import paths
    from infrastructure.persistence.database import connection as connection_module
    from application.engine.services.novel_stop_signal import clear_local_novel_stop
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path)
    db = DatabaseConnection(str(tmp_path / "plotpilot.db"))
    monkeypatch.setattr(connection_module, "_db_instance", db)
    monkeypatch.setattr(connection_module, "_db_instances_by_path", {str(Path(db.db_path).resolve()): db})
    pq = PersistenceQueue()
    pq.initialize()
    monkeypatch.setattr(queue_module, "_persistence_queue", pq)
    register_persistence_handlers()
    pq.start_consumer()
    db.execute("INSERT INTO novels(id,title,slug,autopilot_status,autopilot_run_epoch) VALUES ('novel','Test','test','running',1)")
    db.execute("INSERT INTO chapters(id,novel_id,number,title,content,status) VALUES ('chapter','novel',1,'First',?,'completed')", (RAW,))
    ensure_control_schema(db)
    guard = {"job_id": "run-1", "run_epoch": 1, "executor_pid": os.getpid()}
    db.execute("""INSERT INTO autopilot_commands(command_id,novel_id,command_type,run_epoch,payload_json,state,requested_at,executor_pid)
                  VALUES ('run-1','novel','start',1,'{}','completed',?,?)""", (time.time(), os.getpid()))
    assert claim_execution_lease("novel", "autopilot", "run-1", db=db)
    db.commit()
    clear_local_novel_stop("novel")
    clear_local_novel_stop("__all__")
    ChapterRewriteCandidates(db).set_policy("novel", "auto", max_attempts=1)
    monkeypatch.setattr(workflow_module, "PolicyValidator", lambda: SimpleNamespace(check=lambda **kw: PolicyReport(passed=True)))
    host = DaemonHostMixin()
    host.chapter_repository = SimpleNamespace(db=db)
    host._execution_identities = {"novel": guard}
    host._update_shared_state = lambda *a, **kw: None
    host._publish_audit_event = lambda *a, **kw: None
    scores = []

    async def rewrite(*args):
        return CLEAN

    async def score(novel_id, chapter_number, body, **kwargs):
        scores.append((body, kwargs))
        return {"status": "completed", "similarity_score": 0.9, "drift_alert": False}

    host._rewrite_chapter_for_voice = rewrite
    host._score_voice_only = score
    novel = SimpleNamespace(novel_id=NovelId("novel"))
    chapter = SimpleNamespace(id="chapter", number=1, content=RAW, revision=0)
    yield db, pq, host, novel, chapter, scores
    clear_local_novel_stop("novel")
    clear_local_novel_stop("__all__")
    pq.stop_consumer()
    db.close_all(skip_checkpoint=True)
    pq.get_queue().close()
    pq.get_queue().join_thread()


async def apply(fixture):
    db, pq, host, novel, chapter, scores = fixture
    return await host._apply_voice_rewrite_loop(novel, chapter, RAW,
        {"status": "completed", "similarity_score": 0.3, "drift_alert": True})


@pytest.mark.asyncio
async def test_daemon_candidate_revalidation_precedes_ack_and_final_body_matches(daemon_writer):
    db, _pq, _host, _novel, _chapter, scores = daemon_writer
    body, result = await apply(daemon_writer)
    assert body == CLEAN and result["rewrite_status"] == "committed"
    assert scores == [(CLEAN, {"persist": False})]
    candidate = ChapterRewriteCandidates(db).get(result["candidate_id"])
    assert candidate["status"] == "committed"
    assert candidate["base_content"] == RAW and candidate["base_revision"] == 0
    assert candidate["run_id"] and candidate["diff"] and candidate["audit"]["accepted"]
    assert candidate["audit"]["scan"]["metrics"]["total_hits"] == 0
    assert candidate["audit"]["committed_revision"] == 1
    with sqlite3.connect(db.db_path) as independent:
        persisted = independent.execute("SELECT content,revision FROM chapters WHERE id='chapter'").fetchone()
    assert persisted == (CLEAN, 1)
    assert result["committed_content_hash"] == content_hash(persisted[0])


@pytest.mark.asyncio
@pytest.mark.parametrize("voice", [
    {"status": "unavailable", "similarity_score": None},
    {"status": "completed", "similarity_score": 0.2, "drift_alert": False},
    {"status": "completed", "similarity_score": float("nan"), "drift_alert": False},
    {"status": "completed", "similarity_score": 0.9, "drift_alert": True},
])
async def test_voice_revalidation_failure_preserves_original_and_candidate(daemon_writer, voice):
    db, _pq, host, *_ = daemon_writer

    async def score(*a, **kw):
        return voice

    host._score_voice_only = score
    with pytest.raises(RuntimeError, match="未通过复验"):
        await apply(daemon_writer)
    assert db.fetch_one("SELECT content,revision FROM chapters WHERE id='chapter'") == {"content": RAW, "revision": 0}
    candidate = ChapterRewriteCandidates(db).list_for_chapter("novel")[0]
    assert candidate["status"] == "rejected" and candidate["content"] == CLEAN
    assert any("声线" in reason for reason in candidate["audit"]["reasons"])


@pytest.mark.asyncio
async def test_scanner_failure_blocks_rewrite_and_commit(daemon_writer, monkeypatch):
    db, _pq, host, *_ = daemon_writer
    calls = []
    host._rewrite_chapter_for_voice = lambda *_: calls.append(True)
    monkeypatch.setattr(workflow_module.AntiAIRewriteWorkflow, "scan", lambda *_: (_ for _ in ()).throw(RuntimeError("scanner broken")))
    with pytest.raises(RuntimeError, match="scanner broken"):
        await apply(daemon_writer)
    assert calls == []
    assert db.fetch_one("SELECT content FROM chapters WHERE id='chapter'")["content"] == RAW
    assert ChapterRewriteCandidates(db).list_for_chapter("novel") == []


@pytest.mark.asyncio
async def test_candidate_scanner_failure_preserves_both_bodies(daemon_writer, monkeypatch):
    db, _pq, _host, *_ = daemon_writer
    original_scan = AntiAIRewriteWorkflow.scan

    def scan(self, chapter_id, content):
        if content == CLEAN:
            raise RuntimeError("candidate scan failed")
        return original_scan(self, chapter_id, content)

    monkeypatch.setattr(AntiAIRewriteWorkflow, "scan", scan)
    with pytest.raises(RuntimeError, match="candidate scan failed"):
        await apply(daemon_writer)
    candidate = ChapterRewriteCandidates(db).list_for_chapter("novel")[0]
    assert candidate["status"] == "failed" and candidate["content"] == CLEAN
    assert db.fetch_one("SELECT content FROM chapters WHERE id='chapter'")["content"] == RAW


@pytest.mark.asyncio
async def test_noop_candidate_is_rejected_and_never_saved_as_a_rewrite(daemon_writer):
    db, _pq, host, *_ = daemon_writer

    async def no_op(*args):
        return RAW

    host._rewrite_chapter_for_voice = no_op
    with pytest.raises(RuntimeError, match="未通过复验"):
        await apply(daemon_writer)
    candidate = ChapterRewriteCandidates(db).list_for_chapter("novel")[0]
    assert candidate["status"] == "rejected" and "没有产生正文变化" in candidate["audit"]["reasons"]
    assert db.fetch_one("SELECT revision FROM chapters WHERE id='chapter'")["revision"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["scan_only", "review"])
async def test_explicit_scan_and_review_policies_do_not_auto_commit(daemon_writer, mode):
    db, _pq, _host, *_ = daemon_writer
    ChapterRewriteCandidates(db).set_policy("novel", mode, max_attempts=1)
    body, result = await apply(daemon_writer)
    assert body == RAW and result["rewrite_status"] == ("scan_only" if mode == "scan_only" else "awaiting_confirmation")
    assert db.fetch_one("SELECT revision FROM chapters WHERE id='chapter'")["revision"] == 0
    candidates = ChapterRewriteCandidates(db).list_for_chapter("novel")
    assert candidates == [] if mode == "scan_only" else candidates[0]["status"] == "pending_review"


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["author_edit", "aba", "stop_epoch", "lease_owner", "lease_expired", "lease_job"])
async def test_writer_cas_rechecks_author_version_and_execution_identity(daemon_writer, monkeypatch, mutation):
    db, _pq, host, *_ = daemon_writer
    actual = ChapterRewriteCandidates.commit_candidate

    def race(self, *args, **kwargs):
        if mutation == "author_edit":
            db.execute("UPDATE chapters SET content='作者新稿' WHERE id='chapter'")
        elif mutation == "aba":
            db.execute("UPDATE chapters SET content='作者新稿' WHERE id='chapter'")
            db.execute("UPDATE chapters SET content=? WHERE id='chapter'", (RAW,))
        elif mutation == "stop_epoch":
            db.execute("UPDATE novels SET autopilot_run_epoch=2 WHERE id='novel'")
        elif mutation == "lease_owner":
            db.execute("UPDATE novel_execution_leases SET owner='dag' WHERE novel_id='novel'")
        elif mutation == "lease_expired":
            db.execute("UPDATE novel_execution_leases SET expires_at=0 WHERE novel_id='novel'")
        else:
            db.execute("UPDATE novel_execution_leases SET job_id='successor' WHERE novel_id='novel'")
        db.commit()
        return actual(self, *args, **kwargs)

    # Simulate a race after all caller-side checks; only SQL guards can catch it.
    monkeypatch.setattr(ChapterRewriteCandidates, "commit_candidate", race)
    with pytest.raises(CandidateConflictError, match="冲突"):
        await apply(daemon_writer)
    candidate = ChapterRewriteCandidates(db).list_for_chapter("novel")[0]
    assert candidate["status"] == "conflict" and candidate["content"] == CLEAN
    expected = "作者新稿" if mutation == "author_edit" else RAW
    assert db.fetch_one("SELECT content FROM chapters WHERE id='chapter'")["content"] == expected


@pytest.mark.asyncio
async def test_stop_after_provider_preserves_candidate_and_never_commits(daemon_writer):
    db, _pq, host, *_ = daemon_writer

    async def stopped(*args):
        db.execute("UPDATE novels SET autopilot_status='stopped' WHERE id='novel'")
        db.commit()
        return CLEAN

    host._rewrite_chapter_for_voice = stopped
    body, result = await apply(daemon_writer)
    assert body == RAW and result["rewrite_status"] == "cancelled"
    assert ChapterRewriteCandidates(db).list_for_chapter("novel")[0]["status"] == "cancelled"
    assert db.fetch_one("SELECT content FROM chapters WHERE id='chapter'")["content"] == RAW


@pytest.mark.asyncio
async def test_commit_timeout_never_returns_success_and_keeps_candidate(daemon_writer, monkeypatch):
    db, _pq, _host, *_ = daemon_writer

    def timeout(*args, **kwargs):
        raise PersistenceCommitTimeout("commit outcome unknown", "command-unknown")

    monkeypatch.setattr(ChapterRewriteCandidates, "commit_candidate", timeout)
    with pytest.raises(PersistenceCommitTimeout, match="unknown"):
        await apply(daemon_writer)
    candidate = ChapterRewriteCandidates(db).list_for_chapter("novel")[0]
    assert candidate["status"] == "audited" and candidate["content"] == CLEAN
    assert db.fetch_one("SELECT content FROM chapters WHERE id='chapter'")["content"] == RAW


@pytest.mark.asyncio
async def test_lease_expiry_while_writer_waits_is_checked_at_sql_execution(daemon_writer):
    db, pq, _host, *_ = daemon_writer
    db.execute("UPDATE novel_execution_leases SET expires_at=? WHERE novel_id='novel'", (time.time() + 1.0,))
    db.commit()
    entered, release = threading.Event(), threading.Event()
    actual = pq._handlers["execute_sql_txn_batch"]

    def delayed(payload):
        if any("UPDATE chapters SET content" in op.get("sql", "") for op in payload.get("operations", [])):
            entered.set()
            assert release.wait(5)
        return actual(payload)

    pq._handlers["execute_sql_txn_batch"] = delayed
    task = asyncio.create_task(apply(daemon_writer))
    assert await asyncio.to_thread(entered.wait, 5)
    # A caller-side time parameter would incorrectly accept this expired lease.
    await asyncio.sleep(1.05)
    release.set()
    with pytest.raises(CandidateConflictError, match="租约冲突"):
        await task
    assert db.fetch_one("SELECT content FROM chapters WHERE id='chapter'")["content"] == RAW
    assert ChapterRewriteCandidates(db).list_for_chapter("novel")[0]["status"] == "conflict"


@pytest.mark.asyncio
async def test_anti_ai_auto_rewrite_runs_without_voice_drift(daemon_writer):
    db, _pq, host, novel, chapter, _scores = daemon_writer
    calls = []

    class Provider:
        async def generate(self, prompt, config):
            calls.append(prompt)
            return SimpleNamespace(content=CLEAN)

    async def wrong_route(*args):
        raise AssertionError("Anti-AI rules must trigger rewrite even when voice is healthy")

    host.llm_service = Provider()
    host._rewrite_chapter_for_voice = wrong_route
    body, result = await host._apply_voice_rewrite_loop(novel, chapter, RAW,
        {"status": "completed", "similarity_score": 0.9, "drift_alert": False, "baseline_available": True})
    assert body == CLEAN and result["rewrite_status"] == "committed" and len(calls) == 1
    assert ChapterRewriteCandidates(db).get(result["candidate_id"])["kind"] == "rewrite"


@pytest.mark.asyncio
async def test_cancellation_waits_for_inflight_ack_before_finishing(daemon_writer, monkeypatch):
    db, _pq, _host, *_ = daemon_writer
    entered, release = threading.Event(), threading.Event()
    actual = ChapterRewriteCandidates.commit_candidate

    def delayed(self, *args, **kwargs):
        entered.set()
        assert release.wait(5)
        return actual(self, *args, **kwargs)

    monkeypatch.setattr(ChapterRewriteCandidates, "commit_candidate", delayed)
    task = asyncio.create_task(apply(daemon_writer))
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    await asyncio.sleep(0.01)
    assert not task.done()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    candidate = ChapterRewriteCandidates(db).list_for_chapter("novel")[0]
    assert candidate["status"] == "committed"
    assert db.fetch_one("SELECT content FROM chapters WHERE id='chapter'")["content"] == CLEAN


@pytest.mark.asyncio
async def test_final_audit_cannot_reuse_revision_after_author_edit(daemon_writer, monkeypatch):
    db, _pq, _host, *_ = daemon_writer
    actual = AntiAIRewriteWorkflow.scan
    candidate_scans = []

    def scan(self, chapter_id, body):
        result = actual(self, chapter_id, body)
        if body == CLEAN:
            candidate_scans.append(True)
            if len(candidate_scans) == 2:
                db.execute("UPDATE chapters SET content='作者后来编辑的稿件' WHERE id='chapter'")
                db.commit()
        return result

    monkeypatch.setattr(AntiAIRewriteWorkflow, "scan", scan)
    with pytest.raises(CandidateConflictError, match="旧审计"):
        await apply(daemon_writer)
    assert db.fetch_one("SELECT content FROM chapters WHERE id='chapter'")["content"] == "作者后来编辑的稿件"
    assert ChapterRewriteCandidates(db).list_for_chapter("novel")[0]["status"] == "committed"
