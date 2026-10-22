import asyncio
import json
from types import SimpleNamespace

import pytest

from application.ai_invocation.dtos import InvocationPolicy, InvocationSession, InvocationAttemptStatus, InvocationSessionStatus
from application.ai_invocation.services import AttemptService
from application.engine.rules.anti_ai_stream_guard import AntiAIStreamGuard, AntiAIStreamInterrupted
from domain.ai.value_objects.prompt import Prompt
from infrastructure.persistence.database.connection import DatabaseConnection


@pytest.fixture
def stream_db(tmp_path, monkeypatch):
    from infrastructure.persistence.database import connection
    db = DatabaseConnection(str(tmp_path / "stream.sqlite"))
    db.execute("INSERT INTO novels(id,title,author,premise,slug) VALUES ('novel','Test','Test','Test','novel')")
    db.commit()
    monkeypatch.setattr(connection, "get_database", lambda *args, **kwargs: db)
    yield db
    db.close_all(skip_checkpoint=True)


async def chunks(parts, closed=None):
    try:
        for part in parts:
            yield part
    finally:
        if closed is not None:
            closed.append(True)


@pytest.mark.asyncio
async def test_default_scan_only_warns_and_preserves_cross_window_text(stream_db):
    events, closed, output = [], [], []
    guard = AntiAIStreamGuard(db=stream_db, novel_id="novel", chapter_number=1, chunk_size=4,
        on_event=lambda name, data: events.append((name, data)))
    async for part in guard.wrap(chunks(["xx嘴", "角", "上扬", "zz"], closed)):
        output.append(part)
    assert "".join(output) == "xx嘴角上扬zz" and closed == [True]
    assert guard.action == "warn" and guard.summary["status"] == "completed"
    assert guard.summary["final_scan_status"] == "completed"
    warnings = [data for name, data in events if name == "anti_ai_stream_warning"]
    matches = [hit for data in warnings for hit in data["hits"] if hit["pattern_name"] == "嘴角微表情"]
    assert len(matches) == 1 and matches[0]["position"] == 2
    assert [name for name, _data in events][-2:] == ["anti_ai_stream_final_scan", "anti_ai_stream_terminal"]
    row = stream_db.fetch_one("SELECT * FROM anti_ai_stream_runs WHERE run_id=?", (guard.run_id,))
    assert row["status"] == "completed" and row["scan_status"] == "completed"
    assert row["content_hash"] == guard.summary["content_hash"]


@pytest.mark.asyncio
async def test_explicit_threshold_interrupts_closes_source_and_keeps_already_yielded_fragment(stream_db):
    from application.engine.services.chapter_rewrite_candidates import ChapterRewriteCandidates
    ChapterRewriteCandidates(stream_db).set_policy("novel", "scan_only", stream_action="cancel", stream_critical_limit=1)
    closed, output = [], []
    guard = AntiAIStreamGuard(db=stream_db, novel_id="novel", chunk_size=1, on_event=lambda *_: None)
    with pytest.raises(AntiAIStreamInterrupted, match="阈值"):
        async for part in guard.wrap(chunks(["死", "死", "不会消费"], closed)):
            output.append(part)
    assert output == ["死", "死"] and closed == [True]
    assert guard.summary["status"] == "interrupted" and guard.summary["final_scan_status"] == "not_run"
    assert stream_db.fetch_one("SELECT status FROM anti_ai_stream_runs WHERE run_id=?", (guard.run_id,))["status"] == "interrupted"


@pytest.mark.asyncio
async def test_cancellation_closes_pending_provider_and_has_no_completed_receipt(stream_db):
    started, closed = asyncio.Event(), []

    async def provider():
        try:
            yield "已生成片段"
            started.set()
            await asyncio.Event().wait()
        finally:
            closed.append(True)

    guard = AntiAIStreamGuard(db=stream_db, on_event=lambda *_: None)

    async def consume():
        async for _part in guard.wrap(provider()):
            pass

    task = asyncio.create_task(consume())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed == [True]
    assert guard.summary["status"] == "cancelled"
    assert guard.summary["final_scan_status"] == "not_run"


@pytest.mark.asyncio
async def test_consumer_stop_closes_source_without_running_final_scan(stream_db):
    closed = []
    guard = AntiAIStreamGuard(db=stream_db, on_event=lambda *_: None)
    stream = guard.wrap(chunks(["first", "second"], closed))
    assert await anext(stream) == "first"
    await stream.aclose()
    assert closed == [True] and guard.summary["status"] == "cancelled"
    assert guard.summary["final_scan_status"] == "not_run"


@pytest.mark.asyncio
async def test_final_scan_failure_never_completes_even_when_windows_pass(stream_db, monkeypatch):
    guard = AntiAIStreamGuard(db=stream_db, chunk_size=2, on_event=lambda *_: None)
    monkeypatch.setattr(guard.auditor, "scan_chapter", lambda *_: (_ for _ in ()).throw(RuntimeError("final scanner unavailable")))
    with pytest.raises(RuntimeError, match="final scanner unavailable"):
        async for _part in guard.wrap(chunks(["正常正文"])):
            pass
    assert guard.summary["status"] == "failed" and guard.summary["final_scan_status"] == "failed"


@pytest.mark.asyncio
async def test_provider_close_failure_cannot_archive_completed(stream_db):
    class Source:
        def __init__(self): self.done=False
        def __aiter__(self): return self
        async def __anext__(self):
            if self.done: raise StopAsyncIteration
            self.done=True
            return '正文'
        async def aclose(self): raise RuntimeError('provider close failed')
    guard=AntiAIStreamGuard(db=stream_db,on_event=lambda *_:None)
    body=[]
    with pytest.raises(RuntimeError,match='provider close failed'):
        async for piece in guard.wrap(Source()): body.append(piece)
    assert body==['正文'] and guard.summary['status']=='failed'
    row=stream_db.fetch_one('SELECT status,error FROM anti_ai_stream_runs WHERE run_id=?',(guard.run_id,))
    assert row['status']=='failed' and 'provider close failed' in row['error']


@pytest.mark.asyncio
async def test_provider_close_cancellation_cannot_archive_completed(stream_db):
    class Source:
        def __init__(self): self.done = False
        def __aiter__(self): return self
        async def __anext__(self):
            if self.done: raise StopAsyncIteration
            self.done = True
            return "保留正文"
        async def aclose(self): raise asyncio.CancelledError("provider close cancelled")

    guard = AntiAIStreamGuard(db=stream_db, on_event=lambda *_: None)
    body = []
    with pytest.raises(asyncio.CancelledError, match="provider close cancelled"):
        async for piece in guard.wrap(Source()): body.append(piece)
    assert body == ["保留正文"] and guard.summary["status"] == "cancelled"
    row = stream_db.fetch_one("SELECT status,error FROM anti_ai_stream_runs WHERE run_id=?", (guard.run_id,))
    assert row["status"] == "cancelled" and "provider close cancelled" in row["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["hash", "metrics", "rule_version"])
async def test_final_scan_rejects_stale_body_or_invalid_metrics(stream_db, monkeypatch, invalid):
    guard = AntiAIStreamGuard(db=stream_db, on_event=lambda *_: None)
    report = guard.auditor.scan_chapter("", "正常正文")
    if invalid == "hash":
        report.content_hash = "0" * 64
    elif invalid == "metrics":
        report.metrics.severity_score = float("nan")
    else:
        report.rule_version = "stale-version"
    monkeypatch.setattr(guard.auditor, "scan_chapter", lambda *_: report)
    with pytest.raises(RuntimeError):
        async for _part in guard.wrap(chunks(["正常正文"])):
            pass
    row = stream_db.fetch_one("SELECT status,scan_status FROM anti_ai_stream_runs WHERE run_id=?", (guard.run_id,))
    assert row["status"] == "failed" and row["scan_status"] == "failed"


@pytest.mark.asyncio
async def test_event_delivery_failure_preserves_body_and_queryable_receipt(stream_db):
    def unavailable(*_args):
        raise RuntimeError("event bus offline")
    guard = AntiAIStreamGuard(db=stream_db, on_event=unavailable)
    output = [part async for part in guard.wrap(chunks(["正常正文"]))]
    assert output == ["正常正文"] and guard.summary["status"] == "completed"
    assert guard.summary["event_delivery_errors"] == 3
    row = stream_db.fetch_one("SELECT status,summary_json FROM anti_ai_stream_runs WHERE run_id=?", (guard.run_id,))
    assert row["status"] == "completed" and "event bus offline" in row["summary_json"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_table", ["anti_ai_stream_runs", "anti_ai_prompt_runs"])
async def test_receipt_persistence_failure_prevents_normal_stream_completion(stream_db, failed_table):
    with stream_db.transaction() as writer:
        writer.execute(f"DROP TABLE {failed_table}")
    prompt_evidence = {"prompt_hash": "prompt-hash", "rule_version": "rules-v1"} \
        if failed_table == "anti_ai_prompt_runs" else None
    guard = AntiAIStreamGuard(db=stream_db, prompt_evidence=prompt_evidence,
        on_event=lambda *_: None)
    output = []

    with pytest.raises(RuntimeError, match="回执未持久化"):
        async for part in guard.wrap(chunks(["最终扫描通过，但不能交付成功状态"])):
            output.append(part)

    assert "".join(output) == "最终扫描通过，但不能交付成功状态"
    assert guard.summary["final_scan_status"] == "completed"
    assert guard.summary["persistence_status"] == "failed"
    if failed_table == "anti_ai_prompt_runs":
        assert guard.summary["prompt_receipt"]["persistence_status"] == "failed"
        row = stream_db.fetch_one("SELECT status,persistence_status,summary_json FROM anti_ai_stream_runs WHERE run_id=?", (guard.run_id,))
        assert row["status"] == "failed" and row["persistence_status"] == "failed"
        stored = json.loads(row["summary_json"])
        assert stored["persistence_status"] == "failed"
        assert stored["prompt_receipt"]["persistence_status"] == "failed"


@pytest.mark.asyncio
async def test_growing_regex_match_counts_once_across_overlap_windows(stream_db):
    from application.engine.rules.stream_ac_scanner import StreamACScanner
    from application.engine.rules.chunked_buffer_scanner import ChunkedBufferScanner
    scanner = StreamACScanner()
    scanner.add_pattern("growing", "a.+", severity="critical")
    guard = AntiAIStreamGuard(db=stream_db, chunk_size=2, on_event=lambda *_: None)
    guard.scanner = ChunkedBufferScanner(chunk_size=2, scanner=scanner)
    assert [part async for part in guard.wrap(chunks(["ab", "cd", "ef"]))] == ["ab", "cd", "ef"]
    assert guard.summary["incremental_hits"] == 1 and guard.summary["critical_hits"] == 1


@pytest.mark.asyncio
async def test_scene_density_exemption_is_checked_at_eof_before_explicit_cancel(stream_db, monkeypatch):
    from infrastructure.ai.quality_rule_store import QualityRuleStore
    effective={'version':'rules','patterns': [('呼吸一窒','呼吸一窒','critical','生理性','hint')]}
    monkeypatch.setattr(QualityRuleStore,'effective',lambda _:effective)
    guard=AntiAIStreamGuard(db=stream_db,scene_type='battle',action='cancel',critical_limit=1,chunk_size=2,on_event=lambda *_:None)
    body='呼吸一窒。'+'填'*1000
    assert ''.join([part async for part in guard.wrap(chunks([body]))])==body
    assert guard.summary['status']=='completed' and guard.summary['final_critical_hits']==0
    assert guard.summary['exempted_hits']==1 and guard.summary['incremental_cancel_deferred_for_scene']


@pytest.mark.asyncio
async def test_completed_stream_scan_does_not_replace_committed_chapter_audit(stream_db):
    from engine.runtime.daemon_host import DaemonHostMixin
    guard = AntiAIStreamGuard(db=stream_db, novel_id="novel", chapter_number=1, on_event=lambda *_: None)
    body = "".join([part async for part in guard.wrap(chunks(["他的嘴角上扬。"]))])
    assert stream_db.fetch_one("SELECT count(*) AS n FROM anti_ai_audit_runs")["n"] == 0
    final_audit = DaemonHostMixin()._sync_anti_ai_audit("novel", 1, body)
    assert final_audit.scan_status == "completed" and final_audit.persistence_status == "committed"
    assert final_audit.run_id != guard.run_id and final_audit.content_hash == guard.summary["content_hash"]
    assert stream_db.fetch_one("SELECT count(*) AS n FROM anti_ai_audit_runs")["n"] == 1


@pytest.mark.asyncio
async def test_prose_invocation_installs_real_guard_and_records_attempt_identity(stream_db):
    class Provider:
        stream_generate = lambda *_: chunks(["正文", "结束"])
    session = InvocationSession("session", "autopilot.chapter.prose.generate", "node", InvocationPolicy.DIRECT,
        context={"novel_id": "novel", "chapter_number": 2})
    attempt = await AttemptService(Provider()).generate_streaming(session=session,
        prompt_snapshot=SimpleNamespace(prompt=Prompt(system="system", user="user")))
    assert attempt.status == InvocationAttemptStatus.SUCCEEDED and attempt.content == "正文结束"
    guard = attempt.accounting["anti_ai_stream"]
    assert guard["job_id"] == attempt.id and guard["final_scan_status"] == "completed"
    assert stream_db.fetch_one("SELECT job_id FROM anti_ai_stream_runs WHERE run_id=?", (guard["run_id"],))["job_id"] == attempt.id


@pytest.mark.asyncio
async def test_non_prose_script_operation_does_not_install_prose_guard(stream_db, monkeypatch):
    class Provider:
        stream_generate = lambda *_: chunks(['{"script":"死死"}'])
    monkeypatch.setattr(AttemptService, "prose_stream_guard_factory", lambda **_kwargs: pytest.fail("script must not enter prose scanner"))
    session = InvocationSession("session", "autopilot.script.generate", "node", InvocationPolicy.DIRECT)
    attempt = await AttemptService(Provider()).generate_streaming(session=session,
        prompt_snapshot=SimpleNamespace(prompt=Prompt(system="system", user="user")))
    assert attempt.status == InvocationAttemptStatus.SUCCEEDED
    assert stream_db.fetch_one("SELECT count(*) AS n FROM anti_ai_stream_runs")["n"] == 0


@pytest.mark.asyncio
async def test_prose_invocation_interruption_cannot_be_adopted_as_success(stream_db):
    from application.engine.services.chapter_rewrite_candidates import ChapterRewriteCandidates
    ChapterRewriteCandidates(stream_db).set_policy("novel", "review", stream_action="cancel", stream_critical_limit=1)
    class Provider:
        stream_generate = lambda *_: chunks(["死死"])
    service = AttemptService(Provider())
    session = InvocationSession("session", "autopilot.prose.generate", "node", InvocationPolicy.DIRECT, context={"novel_id": "novel"})
    with pytest.raises(AntiAIStreamInterrupted):
        await service.generate_streaming(session=session, prompt_snapshot=SimpleNamespace(prompt=Prompt(system="system", user="user")))
    attempt = service.get(session.attempts[0])
    assert attempt.status == InvocationAttemptStatus.FAILED and attempt.content == "死死"
    assert session.status == InvocationSessionStatus.FAILED
    assert attempt.accounting["anti_ai_stream"]["status"] == "interrupted"


def test_stream_migration_supports_fresh_and_reopened_database(tmp_path):
    path = str(tmp_path / "migrations.sqlite")
    for _ in range(2):
        db = DatabaseConnection(path)
        assert db.fetch_one("SELECT count(*) AS n FROM anti_ai_stream_runs")["n"] == 0
        columns = {row["name"] for row in db.fetch_all("PRAGMA table_info(anti_ai_rewrite_policies)")}
        assert {"stream_action", "stream_critical_limit"} <= columns
        db.close_all(skip_checkpoint=True)
