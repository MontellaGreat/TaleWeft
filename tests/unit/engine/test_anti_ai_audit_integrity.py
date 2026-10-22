import hashlib
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from application.audit.services import anti_ai_audit, anti_ai_learning
from application.audit.services.anti_ai_audit import AntiAIAuditReport
from engine.runtime.daemon_host import DaemonHostMixin
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.sqlite_anti_ai_audit_repository import SqliteAntiAiAuditRepository


@pytest.fixture
def audit_db(tmp_path, monkeypatch):
    from infrastructure.persistence.database import connection
    db = DatabaseConnection(str(tmp_path / "audit.sqlite"))
    monkeypatch.setattr(connection, "get_database", lambda *a, **k: db)
    monkeypatch.setattr(anti_ai_learning, "get_anti_ai_learning_service", lambda: SimpleNamespace(
        analyze_chapter_audit=lambda **kw: None))
    db.execute("INSERT INTO novels(id,title,author,premise,slug) VALUES ('novel','Test','Test','Test','novel')")
    db.commit()
    yield db
    db.close_all(skip_checkpoint=True)


def test_scan_persists_exact_body_and_rule_version_with_real_hits(audit_db):
    body = "他的嘴角上扬，眼中闪过一丝笑意。"
    report = DaemonHostMixin()._sync_anti_ai_audit("novel", 1, body)
    assert report.scan_status == "completed"
    assert report.persistence_status == "committed"
    assert report.metrics.total_hits >= 2
    assert report.content_hash == hashlib.sha256(body.encode()).hexdigest()
    assert len(report.rule_version) == 64
    runs = SqliteAntiAiAuditRepository(audit_db).list_runs("novel")
    assert len(runs) == 1
    assert runs[0]["content_hash"] == report.content_hash
    # Read from a new connection: this proves commit, not an uncommitted same-connection view.
    with sqlite3.connect(audit_db.db_path) as other:
        assert other.execute("SELECT count(*) FROM anti_ai_audit_runs").fetchone()[0] == 1


@pytest.mark.parametrize("content", ["", "   "])
def test_empty_scan_is_failed_record_without_overwriting_previous_summary(audit_db, content):
    host = DaemonHostMixin()
    successful = host._sync_anti_ai_audit("novel", 1, "他的眼中闪过一丝笑意。")
    failed = host._sync_anti_ai_audit("novel", 1, content)
    assert failed.scan_status == "failed"
    assert failed.metrics.overall_assessment == "未检测"
    assert failed.persistence_status == "committed"
    repo = SqliteAntiAiAuditRepository(audit_db)
    assert repo.get_by_chapter("novel", 1)["total_hits"] == successful.metrics.total_hits
    assert repo.list_runs("novel")[0]["scan_status"] == "failed"


def test_scanner_initialization_failure_is_visible_and_archived(audit_db, monkeypatch):
    def unavailable():
        raise RuntimeError("scanner missing")
    monkeypatch.setattr(anti_ai_audit, "get_anti_ai_auditor", unavailable)
    report = DaemonHostMixin()._sync_anti_ai_audit("novel", 1, "body")
    assert report.scan_status == "failed"
    assert report.persistence_status == "committed"
    assert "scanner missing" in report.error


@pytest.mark.parametrize("invalid", ["default", "hash", "metrics"])
def test_invalid_completed_report_is_failed_and_keeps_previous_summary(audit_db, monkeypatch, invalid):
    host = DaemonHostMixin()
    original = host._sync_anti_ai_audit("novel", 1, "他的嘴角上扬。")
    report = anti_ai_audit.get_anti_ai_auditor().scan_chapter("ch-1", "新正文")
    if invalid == "default":
        report = AntiAIAuditReport()
    elif invalid == "hash":
        report.content_hash = "stale"
    else:
        report.metrics.severity_score = float("nan")
    monkeypatch.setattr(anti_ai_audit, "get_anti_ai_auditor", lambda: SimpleNamespace(scan_chapter=lambda *_: report))
    failed = host._sync_anti_ai_audit("novel", 1, "新正文")
    assert failed.scan_status == "failed" and failed.persistence_status == "committed"
    assert SqliteAntiAiAuditRepository(audit_db).get_by_chapter("novel", 1)["total_hits"] == original.metrics.total_hits


def test_archive_failure_rolls_back_summary_in_same_transaction(audit_db):
    host = DaemonHostMixin()
    old = host._sync_anti_ai_audit("novel", 1, "他的眼中闪过一丝笑意。")
    audit_db.execute("DROP TABLE anti_ai_audit_runs")
    audit_db.commit()
    failed = host._sync_anti_ai_audit("novel", 1, "没有命中的新正文")
    assert failed.scan_status == "completed"
    assert failed.persistence_status == "failed"
    assert SqliteAntiAiAuditRepository(audit_db).get_by_chapter("novel", 1)["total_hits"] == old.metrics.total_hits
    stage = audit_db.fetch_one("SELECT stage,persistence_status FROM anti_ai_scan_progress WHERE run_id=?", (failed.run_id,))
    assert stage["stage"] == "failed" and stage["persistence_status"] == "failed"


def test_durable_scanning_stage_exists_before_scanner_and_terminal_survives_event_failure(audit_db, monkeypatch):
    real = anti_ai_audit.get_anti_ai_auditor()
    def scan(chapter, body):
        row = audit_db.fetch_one("SELECT * FROM anti_ai_scan_progress ORDER BY rowid DESC LIMIT 1")
        assert row["stage"] == "scanning" and row["scan_status"] == "running"
        return real.scan_chapter(chapter, body)
    monkeypatch.setattr(anti_ai_audit,"get_anti_ai_auditor",lambda: SimpleNamespace(scan_chapter=scan))
    host = DaemonHostMixin()
    monkeypatch.setattr(host,"_execution_identity",lambda _: {"job_id":"job","run_epoch":7})
    audit_db.execute("INSERT INTO chapters(id,novel_id,number,title,content,revision) VALUES ('chapter','novel',1,'title','正文',3)")
    audit_db.commit()
    report = host._sync_anti_ai_audit("novel",1,"正文",on_progress=lambda _: (_ for _ in ()).throw(RuntimeError("SSE offline")))
    row=audit_db.fetch_one("SELECT * FROM anti_ai_scan_progress WHERE run_id=?",(report.run_id,))
    assert row["stage"]=="completed" and row["job_id"]=="job" and row["run_epoch"]==7
    assert row["chapter_revision"]==3 and row["content_hash"]==report.content_hash


def test_failed_summary_archive_has_queryable_failed_receipt(audit_db, monkeypatch):
    monkeypatch.setattr(SqliteAntiAiAuditRepository, "upsert", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("summary write failed")))
    events = []
    report = DaemonHostMixin()._sync_anti_ai_audit("novel", 1, "正常正文", on_progress=events.append)
    assert report.scan_status == "completed" and report.persistence_status == "failed"
    stored = SqliteAntiAiAuditRepository(audit_db).get_run("novel", report.run_id)
    assert stored["scan_status"] == "completed" and stored["persistence_status"] == "failed"
    assert [event["status"] for event in events] == ["scanning", "persisting", "failed"]
    assert len({event["run_id"] for event in events}) == 1
    assert all(event["rule_version"] == report.rule_version for event in events[1:])
    assert not SqliteAntiAiAuditRepository(audit_db).get_by_chapter("novel", 1)


@pytest.mark.asyncio
async def test_rule_scan_runs_before_manual_review_can_pause(monkeypatch):
    from engine.runtime import audit_delegate
    from domain.novel.value_objects.novel_id import NovelId
    from domain.novel.entities.novel import NovelStage, AutopilotStatus
    calls = []
    chapter = SimpleNamespace(content="chapter", id="chapter-1", number=1)
    novel = SimpleNamespace(novel_id=NovelId("novel"), current_stage=NovelStage.AUDITING,
                            autopilot_status=AutopilotStatus.RUNNING)

    class Host:
        chapter_repository = SimpleNamespace(get_by_novel_and_number=lambda *_: chapter)
        _is_still_running = lambda *_: True
        _latest_completed_chapter_number = lambda *_: 1
        _sync_novel_current_act_from_chapter_number = lambda *_: None
        _cache_stats_to_shared_memory = lambda *_: None
        _update_shared_state = lambda *a, **k: None
        _publish_audit_event = lambda *a, **k: None
        _flush_novel = lambda *_: None

        async def _run_anti_ai_audit(self, *args):
            calls.append("scan")
            return AntiAIAuditReport(persistence_status="committed")

        async def _score_voice_only(self, *args):
            calls.append("voice")
            return {}

        async def _call_with_timeout(self, awaitable, **kwargs):
            return await awaitable

        async def _apply_voice_rewrite_loop(self, novel, chapter, content, result):
            return content, result

    monkeypatch.setattr(audit_delegate, "_write_autopilot_invocation_input", lambda **kw: None)
    monkeypatch.setattr(audit_delegate, "_consume_pending_payload", lambda *a, **kw: None)
    monkeypatch.setattr(audit_delegate, "_read_shared_state", lambda *_: {
        "requires_ai_review": True, "active_invocation_session_id": "manual-review"})
    await audit_delegate.run_chapter_audit(Host(), novel)
    assert calls == ["scan", "voice"]
    assert novel.current_stage == NovelStage.PAUSED_FOR_REVIEW


@pytest.mark.asyncio
async def test_rejected_voice_save_keeps_audited_candidate_and_original_body(audit_db, tmp_path, monkeypatch):
    from application import paths
    from application.engine.services.chapter_generation_workspace import ChapterGenerationWorkspace
    from application.engine.services.chapter_rewrite_candidates import ChapterRewriteCandidates
    from application.audit.services import anti_ai_rewrite_workflow as workflow_module
    from engine.runtime.policy_validator import PolicyReport
    from domain.novel.value_objects.novel_id import NovelId
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path)
    audit_db.execute("INSERT INTO chapters(id,novel_id,number,title,content,status) VALUES ('chapter','novel',1,'Test','old body','completed')")
    audit_db.commit()
    ChapterRewriteCandidates(audit_db).set_policy("novel", "auto", max_attempts=1)
    monkeypatch.setattr(workflow_module, "PolicyValidator", lambda: SimpleNamespace(check=lambda **kw: PolicyReport(passed=True)))
    novel = SimpleNamespace(novel_id=NovelId("novel"))
    chapter = SimpleNamespace(number=1, revision=0)
    scored = []
    host = DaemonHostMixin()
    host.chapter_repository = SimpleNamespace(db=audit_db)
    monkeypatch.setattr(host, "_should_attempt_voice_rewrite", lambda *_: True)
    monkeypatch.setattr(host, "_is_still_running", lambda *_: True)
    monkeypatch.setattr(host, "_execution_identity", lambda *_: {"job_id": "run", "run_epoch": 1, "executor_pid": 1})
    monkeypatch.setattr(host, "_publish_audit_event", lambda *a, **k: None)
    monkeypatch.setattr(host, "_update_shared_state", lambda *a, **k: None)

    async def rewrite(*_):
        return "rewritten candidate"

    async def score(*_, **kwargs):
        scored.append(True)
        return {"status": "completed", "similarity_score": 0.9, "drift_alert": False}

    def reject_commit(*_, **kwargs):
        raise RuntimeError("未确认提交")

    monkeypatch.setattr(host, "_rewrite_chapter_for_voice", rewrite)
    monkeypatch.setattr(host, "_score_voice_only", score)
    monkeypatch.setattr(ChapterRewriteCandidates, "commit_candidate", reject_commit)
    with pytest.raises(RuntimeError, match="未确认提交"):
        await host._apply_voice_rewrite_loop(novel, chapter, "old body", {"similarity_score": 0.3})
    assert scored == [True]
    assert audit_db.fetch_one("SELECT content FROM chapters WHERE id='chapter'")["content"] == "old body"
    candidate = ChapterRewriteCandidates(audit_db).list_for_chapter("novel")[0]
    assert candidate["content"] == "rewritten candidate" and candidate["status"] == "audited"
    assert ChapterGenerationWorkspace().read_latest_preview("novel", 1) == "rewritten candidate"


@pytest.mark.asyncio
async def test_pending_candidate_review_stops_audit_before_invocation_and_completion(monkeypatch):
    from engine.runtime import audit_delegate
    from domain.novel.value_objects.novel_id import NovelId
    from domain.novel.entities.novel import NovelStage, AutopilotStatus
    events, states, flushes = [], [], []
    chapter = SimpleNamespace(content="original body", id="chapter-1", number=1)
    novel = SimpleNamespace(novel_id=NovelId("novel"), current_stage=NovelStage.AUDITING,
                            autopilot_status=AutopilotStatus.RUNNING)

    class Host:
        chapter_repository = SimpleNamespace(get_by_novel_and_number=lambda *_: chapter)
        _is_still_running = lambda *_: True
        _latest_completed_chapter_number = lambda *_: 1
        _sync_novel_current_act_from_chapter_number = lambda *_: None
        _cache_stats_to_shared_memory = lambda *_: None
        _update_shared_state = lambda *a, **kw: states.append(kw)
        _publish_audit_event = lambda self, nid, name, payload: events.append((name, payload))
        _flush_novel = lambda *_: flushes.append(True)

        async def _run_anti_ai_audit(self, *args):
            return AntiAIAuditReport(persistence_status="committed")

        async def _score_voice_only(self, *args):
            return {"similarity_score": 0.3, "drift_alert": True}

        async def _call_with_timeout(self, awaitable, **kwargs):
            return await awaitable

        async def _apply_voice_rewrite_loop(self, novel, chapter, content, result):
            return content, {**result, "rewrite_status": "awaiting_confirmation", "candidate_id": "candidate-1", "rewrite_run_id": "rewrite-1"}

    monkeypatch.setattr(audit_delegate, "_write_autopilot_invocation_input", lambda **kw: pytest.fail("review candidate must block downstream calls"))
    await audit_delegate.run_chapter_audit(Host(), novel)
    assert novel.current_stage == NovelStage.PAUSED_FOR_REVIEW and chapter.content == "original body"
    assert flushes == [True]
    assert states[-1]["autopilot_pause_reason"] == "awaiting_rewrite_confirmation"
    assert events[-1][0] == "audit_rewrite_review_required"
    assert not any(name == "audit_complete" for name, _ in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["policy", "anti_ai", "voice_provider", "voice_drift", "missing_voice_baseline"])
async def test_required_audit_failure_blocks_auto_completion_but_missing_baseline_stays_advisory(monkeypatch, failure):
    from engine.runtime import audit_delegate
    from application.audit.services.anti_ai_audit import AntiAIMetrics
    from domain.novel.value_objects.novel_id import NovelId
    from domain.novel.entities.novel import NovelStage, AutopilotStatus
    events, calls = [], []
    chapter = SimpleNamespace(content="original body", id="chapter-1", number=1, revision=4)
    novel = SimpleNamespace(novel_id=NovelId("novel"), current_stage=NovelStage.AUDITING,
                            autopilot_status=AutopilotStatus.RUNNING, auto_approve_mode=True)
    voice = ({"status": "unavailable", "baseline_available": False, "similarity_score": None}
        if failure == "missing_voice_baseline" else
        {"status": "failed", "similarity_score": None} if failure == "voice_provider" else
        {"status": "completed", "similarity_score": 0.3, "drift_alert": True, "baseline_available": True}
        if failure == "voice_drift" else {"status": "completed", "similarity_score": 0.9, "baseline_available": True})

    class Host:
        chapter_repository = SimpleNamespace(get_by_novel_and_number=lambda *_: chapter)
        _is_still_running = lambda *_: True
        _latest_completed_chapter_number = lambda *_: 1
        _sync_novel_current_act_from_chapter_number = lambda *_: None
        _cache_stats_to_shared_memory = lambda *_: None
        _update_shared_state = lambda *a, **kw: None
        _publish_audit_event = lambda self, nid, name, payload: events.append((name, payload))
        _flush_novel = lambda *_: None
        _similarity_below_warning_threshold = lambda self, value: value is not None and value < 0.75
        _validate_chapter_audit_content = lambda *a: {"status": "failed" if failure == "policy" else "passed", "passed": failure != "policy"}

        async def _run_anti_ai_audit(self, *args):
            return AntiAIAuditReport(persistence_status="committed",
                metrics=AntiAIMetrics(severity_score=5.0 if failure == "anti_ai" else 0.0))

        async def _score_voice_only(self, *args):
            return voice

        async def _call_with_timeout(self, awaitable, **kwargs):
            return await awaitable

        async def _apply_voice_rewrite_loop(self, novel, chapter, content, result):
            return content, {**result, "rewrite_status": "scan_only"}

    monkeypatch.setattr(audit_delegate, "_write_autopilot_invocation_input", lambda **kw: calls.append(True))
    monkeypatch.setattr(audit_delegate, "_consume_pending_payload", lambda *a, **kw: None)
    monkeypatch.setattr(audit_delegate, "_read_shared_state", lambda *_: {
        "requires_ai_review": True, "active_invocation_session_id": "manual"})
    await audit_delegate.run_chapter_audit(Host(), novel)
    assert novel.current_stage == NovelStage.PAUSED_FOR_REVIEW
    failed = [payload for name, payload in events if name == "audit_required_check_failed"]
    if failure == "missing_voice_baseline":
        assert calls == [True] and failed == []
    else:
        assert calls == [] and len(failed) == 1
        assert failed[0]["success"] is False and failed[0]["revision"] == 4
        assert failed[0]["content_hash"] == hashlib.sha256(chapter.content.encode()).hexdigest()
    assert not any(name == "audit_complete" for name, _ in events)
