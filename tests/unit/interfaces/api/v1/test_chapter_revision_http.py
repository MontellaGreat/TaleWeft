"""Real HTTP serialization, single writer ACKs, CAS races and durable recovery."""
import asyncio
import json
import sqlite3
import threading

import httpx
import pytest
from fastapi import FastAPI

from application.core.services.chapter_service import ChapterService
from application.core.services.chapter_version_service import ChapterVersionService, content_hash
from application.core.services.chapter_derivation_jobs import recover_chapter_derivation_jobs, run_chapter_derivation_job
from infrastructure.persistence.database.sqlite_chapter_repository import SqliteChapterRepository
from infrastructure.persistence.database.sqlite_novel_repository import SqliteNovelRepository
from infrastructure.persistence.database.chapter_draft_repository import ChapterDraftRepository
from interfaces.api.dependencies import get_chapter_service, get_chapter_aftermath_pipeline, get_knowledge_service
from interfaces.api.v1.core import chapters as routes
from tests.unit.application.engine.test_persistence_commit_ack import writer


class Pipeline:
    def __init__(self):
        self.calls = []
        self.indexes = []
        self.fail_index = False
        self._indexing = self

    async def index_chapter_summary(self, *args):
        if self.fail_index:
            raise RuntimeError('fake vector unavailable')
        self.indexes.append(args)

    async def run_after_chapter_saved(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return {"narrative_sync_status": "ok"}


@pytest.mark.asyncio
async def test_index_rebuild_and_aftermath_retry_are_visible_durable_tasks(chapter_http):
    app, db, pipeline, _knowledge = chapter_http
    response = await request(app, 'POST', '/derivation-jobs', json={'action': 'rebuild_indexes'})
    assert response.status_code == 202, response.text
    assert response.json()['status'] == 'pending'
    job = (await request(app, 'GET', '/derivation-jobs')).json()[0]
    assert job['status'] == 'completed'
    assert pipeline.calls == []  # explicit index rebuild does not regenerate a summary with an LLM
    assert pipeline.indexes == [('book', 1, '原文')]
    pipeline.fail_index = True
    failed = await request(app, 'POST', '/derivation-jobs', json={'action': 'rebuild_indexes'})
    assert failed.status_code == 202
    job = (await request(app, 'GET', '/derivation-jobs')).json()[0]
    assert job['status'] == 'unavailable'
    assert job['attempts'] == 2
    retry = await request(app, 'POST', '/derivation-jobs', json={'action': 'retry_aftermath'})
    assert retry.status_code == 202
    assert (await request(app, 'GET', '/derivation-jobs')).json()[0]['status'] == 'completed'
    assert len(pipeline.calls) == 1
    assert db.fetch_one("SELECT content,revision FROM chapters WHERE id='chapter'") == {'content': '原文', 'revision': 0}


@pytest.mark.asyncio
async def test_duplicate_pending_rebuild_does_not_replace_payload_or_start_extra_model(chapter_http):
    app, db, pipeline, _knowledge = chapter_http
    db.execute("INSERT INTO chapter_derivation_jobs(job_id,chapter_id,novel_id,chapter_number,revision,payload_json,status) VALUES ('active','chapter','book',1,0,?,'running')",
               (json.dumps({'content': '原文', 'operation': 'retry_aftermath'}),))
    response = await request(app, 'POST', '/derivation-jobs', json={'action': 'rebuild_indexes'})
    assert response.status_code == 202
    assert response.json()['job_id'] == 'active'
    saved = db.fetch_one("SELECT payload_json,status,attempts FROM chapter_derivation_jobs WHERE job_id='active'")
    assert json.loads(saved['payload_json'])['operation'] == 'retry_aftermath'
    assert saved['status'] == 'running' and saved['attempts'] == 0
    assert pipeline.indexes == pipeline.calls == []


@pytest.mark.asyncio
async def test_index_only_cannot_overwrite_failed_aftermath_and_bypass_reconciliation(chapter_http):
    app, db, pipeline, _knowledge = chapter_http
    failed_payload = json.dumps({'content': '原文', 'operation': 'retry_aftermath', 'reconcile_following': True})
    db.execute("INSERT INTO chapter_derivation_jobs(job_id,chapter_id,novel_id,chapter_number,revision,payload_json,status,cascade_status) VALUES('failed-analysis','chapter','book',1,0,?,'unavailable','unavailable')", (failed_payload,))
    response = await request(app, 'POST', '/derivation-jobs', json={'action': 'rebuild_indexes'})
    assert response.status_code == 409 and '重跑章后关联分析' in response.text
    assert db.fetch_one("SELECT status,payload_json FROM chapter_derivation_jobs WHERE job_id='failed-analysis'") == {'status': 'unavailable', 'payload_json': failed_payload}
    assert pipeline.indexes == pipeline.calls == []


@pytest.mark.asyncio
async def test_parallel_retry_does_not_run_completed_cascade_twice(chapter_http):
    _app, db, pipeline, knowledge = chapter_http
    db.execute("INSERT INTO chapter_derivation_jobs(job_id,chapter_id,novel_id,chapter_number,revision,payload_json) VALUES('same-job','chapter','book',1,0,?)",
               (json.dumps({'content': '原文', 'reconcile_following': True}),))
    original = pipeline.run_after_chapter_saved
    entered, release = asyncio.Event(), asyncio.Event()
    async def blocked(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)
    pipeline.run_after_chapter_saved = blocked
    first = asyncio.create_task(run_chapter_derivation_job(db, 'same-job', pipeline, knowledge))
    await entered.wait()
    second = asyncio.create_task(run_chapter_derivation_job(db, 'same-job', pipeline, knowledge))
    await asyncio.sleep(0)
    release.set()
    assert await first == 'completed'
    assert await second is None
    assert len(pipeline.calls) == 1
    assert db.fetch_one("SELECT attempts,cascade_status FROM chapter_derivation_jobs WHERE job_id='same-job'") == {'attempts': 1, 'cascade_status': 'completed'}


@pytest.mark.asyncio
async def test_auxiliary_failure_is_not_acknowledged_as_completed(chapter_http):
    """Deferred auxiliary errors must produce a durable retryable receipt."""
    _app, db, pipeline, knowledge = chapter_http
    db.execute(
        "INSERT INTO chapter_derivation_jobs(job_id,chapter_id,novel_id,chapter_number,revision,payload_json) "
        "VALUES('aux-failed','chapter','book',1,0,?)",
        (json.dumps({'content': '原文', 'operation': 'retry_aftermath'}),),
    )
    result = {"narrative_sync_ok": True, "narrative_sync_status": "synced", "stage_errors": [], "auxiliary_deferred": True}

    async def run_after(*args, **kwargs):
        pipeline.calls.append((args, kwargs))
        return result

    async def drain_auxiliary():
        # Simulate a provider/storage failure after the narrative summary has
        # already been committed by the critical stage.
        result["stage_errors"].append("evolution_snapshot: provider rejected key=DO_NOT_PERSIST")

    pipeline.run_after_chapter_saved = run_after
    pipeline.drain_auxiliary_stages = drain_auxiliary

    assert await run_chapter_derivation_job(db, 'aux-failed', pipeline, knowledge) == 'unavailable'
    job = db.fetch_one("SELECT status,last_error FROM chapter_derivation_jobs WHERE job_id='aux-failed'")
    assert job['status'] == 'unavailable'
    assert job['last_error'] == 'evolution_snapshot: failed'
    assert 'DO_NOT_PERSIST' not in job['last_error']
    assert db.fetch_one("SELECT content,revision FROM chapters WHERE id='chapter'") == {'content': '原文', 'revision': 0}


@pytest.mark.asyncio
@pytest.mark.parametrize('payload_json', ['{not-json', '[]', 'null'])
async def test_invalid_derivation_payload_gets_durable_unavailable_status(chapter_http, payload_json):
    _app, db, pipeline, knowledge = chapter_http
    db.execute(
        "INSERT INTO chapter_derivation_jobs(job_id,chapter_id,novel_id,chapter_number,revision,payload_json) "
        "VALUES('corrupt-payload','chapter','book',1,0,?)",
        (payload_json,),
    )

    assert await run_chapter_derivation_job(db, 'corrupt-payload', pipeline, knowledge) == 'unavailable'
    job = db.fetch_one("SELECT status,last_error FROM chapter_derivation_jobs WHERE job_id='corrupt-payload'")
    assert job['status'] == 'unavailable'
    assert '派生任务载荷无效' in job['last_error']
    assert pipeline.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize('result', [None, {}, {'derivation_status': 'blocked'}])
async def test_pipeline_must_return_explicit_success_evidence(chapter_http, result):
    _app, db, pipeline, knowledge = chapter_http
    db.execute(
        "INSERT INTO chapter_derivation_jobs(job_id,chapter_id,novel_id,chapter_number,revision,payload_json) "
        "VALUES('bad-result','chapter','book',1,0,?)",
        (json.dumps({'content': '原文', 'operation': 'retry_aftermath'}),),
    )

    async def run_after(*args, **kwargs):
        return result

    pipeline.run_after_chapter_saved = run_after

    assert await run_chapter_derivation_job(db, 'bad-result', pipeline, knowledge) == 'unavailable'
    job = db.fetch_one("SELECT status,last_error FROM chapter_derivation_jobs WHERE job_id='bad-result'")
    assert job['status'] == 'unavailable'
    assert job['last_error']


class Knowledge:
    def __init__(self):
        self.calls = []
        self.fail = False

    def patch_chapter_micro_beats(self, *args, **kwargs):
        if self.fail:
            raise RuntimeError("sidecar temporarily offline")
        self.calls.append(args)


@pytest.fixture
def chapter_http(writer, monkeypatch):
    _queue, db = writer
    db.execute("INSERT INTO novels(id,title,slug) VALUES('book','Book','book')")
    db.execute("INSERT INTO chapters(id,novel_id,number,title,content,status) VALUES('chapter','book',1,'第一章','原文','draft')")
    service = ChapterService(SqliteChapterRepository(db), SqliteNovelRepository(db))
    pipeline, knowledge = Pipeline(), Knowledge()
    monkeypatch.setattr("application.manuscript.reindex_job.reindex_chapter_entity_mentions", lambda *_args, **_kwargs: None)
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/v1/novels")
    app.dependency_overrides[get_chapter_service] = lambda: service
    app.dependency_overrides[get_chapter_aftermath_pipeline] = lambda: pipeline
    app.dependency_overrides[get_knowledge_service] = lambda: knowledge
    app.dependency_overrides[routes._get_draft_repo] = lambda: ChapterDraftRepository(db)
    return app, db, pipeline, knowledge


async def request(app, method, suffix="", **kwargs):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
        return await client.request(method, "/api/v1/novels/book/chapters/1" + suffix, **kwargs)


@pytest.mark.asyncio
async def test_title_only_edit_is_versioned_receipted_and_restoreable(chapter_http):
    app, db, _pipeline, _knowledge = chapter_http
    db.execute("INSERT INTO story_nodes(id,novel_id,node_type,number,title,order_index) VALUES('node','book','chapter',1,'第一章',1)")
    payload = {'content': '原文', 'title': '新的章节名', 'expected_revision': 0, 'save_request_id': 'rename-chapter-0001'}
    result = await request(app, 'PUT', json=payload)
    assert result.status_code == 200, result.text
    assert result.json()['title'] == '新的章节名'
    assert db.fetch_one("SELECT title FROM story_nodes WHERE id='node'")['title'] == '新的章节名'
    mismatch = await request(app, 'PUT', json={**payload, 'title': '不同章节名'})
    assert mismatch.status_code == 409
    assert db.fetch_one("SELECT title FROM content_versions WHERE chapter_id='chapter' AND revision=1")['title'] == '新的章节名'
    restored = await request(app, 'POST', '/versions/0/restore', json={'expected_revision': 1})
    assert restored.status_code == 200, restored.text
    assert restored.json()['title'] == '第一章'
    assert restored.json()['content'] == '原文'


@pytest.mark.asyncio
async def test_edit_first_chapter_replays_second_and_reports_suffix_failure(chapter_http):
    app, db, pipeline, _knowledge = chapter_http
    db.execute("INSERT INTO chapters(id,novel_id,number,title,content,status) VALUES('chapter-2','book',2,'第二章','第二章原文','draft')")
    original = pipeline.run_after_chapter_saved
    async def fail_second(*args, **kwargs):
        result = await original(*args, **kwargs)
        return {'stage_errors': ['prop_extract: RuntimeError']} if args[1] == 2 else result
    pipeline.run_after_chapter_saved = fail_second
    saved = await request(app, 'PUT', json={'content': '第一章改写', 'title': '改写标题', 'expected_revision': 0})
    assert saved.status_code == 200, saved.text
    assert [call[0][1] for call in pipeline.calls] == [1, 2]
    job = (await request(app, 'GET', '/derivation-jobs')).json()[0]
    assert job['status'] == 'completed' and job['cascade_status'] == 'unavailable'
    assert job['cascade_error']
    assert db.fetch_one("SELECT content,revision FROM chapters WHERE id='chapter-2'") == {'content': '第二章原文', 'revision': 0}
    pipeline.run_after_chapter_saved = original
    retry = await request(app, 'POST', '/derivation-jobs', json={'action': 'retry_aftermath'})
    assert retry.status_code == 202, retry.text
    job = (await request(app, 'GET', '/derivation-jobs')).json()[0]
    assert job['status'] == 'completed' and job['cascade_status'] == 'completed'
    assert [call[0][1] for call in pipeline.calls] == [1, 2, 1, 2]


@pytest.mark.asyncio
async def test_earlier_edit_reconciles_a_suffix_with_an_old_failed_cascade(chapter_http):
    from application.core.services.chapter_derivation_readiness import assert_previous_derivations_ready

    app, db, pipeline, _knowledge = chapter_http
    db.execute("INSERT INTO chapters(id,novel_id,number,title,content,status) VALUES('chapter-2','book',2,'第二章','第二章原文','draft')")
    db.execute("INSERT INTO chapters(id,novel_id,number,title,content,status) VALUES('chapter-3','book',3,'第三章','第三章原文','draft')")
    db.execute("INSERT INTO chapter_derivation_jobs(job_id,chapter_id,novel_id,chapter_number,revision,payload_json,status,cascade_status,cascade_error) "
               "VALUES('old-suffix','chapter-2','book',2,0,?,'completed','unavailable','old failure')",
               (json.dumps({'content': '第二章原文', 'reconcile_following': True}),))

    saved = await request(app, 'PUT', json={'content': '第一章改写', 'expected_revision': 0})

    assert saved.status_code == 200, saved.text
    assert [call[0][1] for call in pipeline.calls] == [1, 2, 3]
    assert db.fetch_one("SELECT status,cascade_status,cascade_error FROM chapter_derivation_jobs WHERE job_id='old-suffix'") == {
        'status': 'completed', 'cascade_status': 'not_requested', 'cascade_error': '',
    }
    assert_previous_derivations_ready(db, 'book', 4)


@pytest.mark.asyncio
async def test_save_receipt_survives_reopen_and_replay_does_not_add_versions(chapter_http):
    from application.core.services.chapter_save_receipts import ChapterSaveReceipts
    from infrastructure.persistence.database.connection import DatabaseConnection
    app, db, pipeline, _knowledge = chapter_http
    payload = {'content': 'immutable saved prose', 'expected_revision': 0, 'save_request_id': 'receipt-reopen-0001'}
    saved = await request(app, 'PUT', json=payload)
    assert saved.status_code == 200, saved.text
    replay = await request(app, 'PUT', json=payload)
    assert replay.status_code == 200 and replay.json()['revision'] == 1
    assert len(pipeline.calls) == 1
    await request(app, 'PUT', json={'content': 'newer prose', 'expected_revision': 1})
    reopened = DatabaseConnection(db.db_path)
    try:
        receipt = ChapterSaveReceipts(reopened).get(payload['save_request_id'], 'book', 1)
        assert receipt['status'] == 'committed'
        assert receipt['chapter']['content'] == payload['content']
        assert receipt['chapter']['revision'] == 1
        assert receipt['content_hash'] == content_hash(payload['content'])
        assert ChapterSaveReceipts(reopened).get(payload['save_request_id'], 'other-book', 1) is None
    finally:
        reopened.close_all(skip_checkpoint=True)
    assert len(ChapterVersionService(db).list_versions('chapter')) == 3
    mismatch = await request(app, 'PUT', json={**payload, 'content': 'reused key with different body'})
    assert mismatch.status_code == 409
    assert mismatch.json()['detail']['code'] == 'save_request_payload_conflict'


@pytest.mark.asyncio
async def test_writer_rejection_has_durable_failed_receipt(chapter_http):
    app, db, _pipeline, _knowledge = chapter_http
    db.execute("CREATE TRIGGER receipt_reject BEFORE UPDATE OF content ON chapters BEGIN SELECT RAISE(ABORT,'writer rejection'); END")
    failed = await request(app, 'PUT', json={'content': 'reject', 'expected_revision': 0, 'save_request_id': 'receipt-failure-0001'})
    assert failed.status_code == 503
    queried = await request(app, 'GET', '/save-requests/receipt-failure-0001')
    assert queried.json()['status'] == 'failed'
    assert queried.json()['error_code'] == 'writer_failed'
    assert 'chapter' not in queried.json()
    assert db.fetch_one("SELECT content,revision FROM chapters WHERE id='chapter'") == {'content': '原文', 'revision': 0}


@pytest.mark.asyncio
async def test_save_timeout_is_unconfirmed_until_late_writer_commits_matching_receipt(chapter_http, writer, monkeypatch):
    app, db, _pipeline, _knowledge = chapter_http
    queue, _ = writer
    entered, release = threading.Event(), threading.Event()
    original_handler = queue._handlers['execute_sql_txn_batch']
    original_wait = queue.push_and_wait

    def delayed(payload):
        entered.set()
        assert release.wait(5)
        return original_handler(payload)

    def short_wait(kind, payload, timeout=None):
        return original_wait(kind, payload, 0.05 if kind == 'execute_sql_txn_batch' else timeout)

    queue.register_handler('execute_sql_txn_batch', delayed)
    monkeypatch.setattr(queue, 'push_and_wait', short_wait)
    try:
        response = await request(app, 'PUT', json={'content': 'late commit', 'expected_revision': 0,
                                                  'save_request_id': 'receipt-delayed-0001'})
        assert entered.is_set() and response.status_code == 503
        assert response.json()['detail']['save_request_id'] == 'receipt-delayed-0001'
        pending = await request(app, 'GET', '/save-requests/receipt-delayed-0001')
        assert pending.json()['status'] == 'unconfirmed' and 'chapter' not in pending.json()
        duplicate = await request(app, 'PUT', json={'content': 'late commit', 'expected_revision': 0,
                                                    'save_request_id': 'receipt-delayed-0001'})
        assert duplicate.status_code == 202 and 'content' not in duplicate.json()
        assert db.fetch_one("SELECT content FROM chapters WHERE id='chapter'")['content'] == '原文'
    finally:
        release.set()
        monkeypatch.setattr(queue, 'push_and_wait', original_wait)
    assert await asyncio.to_thread(queue.wait_until_idle, 5)
    result = (await request(app, 'GET', '/save-requests/receipt-delayed-0001')).json()
    assert result['status'] == 'committed'
    assert result['chapter']['content'] == 'late commit'
    assert result['content_hash'] == content_hash('late commit')


@pytest.mark.asyncio
async def test_registered_request_without_execution_is_never_reported_saved(chapter_http):
    from application.core.services.chapter_save_receipts import ChapterSaveReceipts
    app, db, _pipeline, _knowledge = chapter_http
    created, _ = ChapterSaveReceipts(db).begin('receipt-not-run-0001', 'book', 1, {'content': 'unexecuted'})
    assert created
    result = (await request(app, 'GET', '/save-requests/receipt-not-run-0001')).json()
    assert result['status'] == 'unconfirmed' and 'chapter' not in result
    missing = await request(app, 'GET', '/save-requests/missing')
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_receipt_with_mismatched_version_hash_is_unavailable(chapter_http):
    app, db, _pipeline, _knowledge = chapter_http
    await request(app, 'PUT', json={'content': 'saved', 'expected_revision': 0, 'save_request_id': 'receipt-hash-bad-0001'})
    db.execute("UPDATE chapter_save_receipts SET content_hash='wrong' WHERE request_id='receipt-hash-bad-0001'")
    result = (await request(app, 'GET', '/save-requests/receipt-hash-bad-0001')).json()
    assert result['status'] == 'unavailable' and result['error_code'] == 'receipt_version_mismatch'
    assert 'chapter' not in result


@pytest.mark.asyncio
async def test_http_etag_preconditions_commit_receipt_and_conflict(chapter_http):
    app, db, _pipeline, _knowledge = chapter_http
    loaded = await request(app, "GET")
    assert loaded.status_code == 200
    baseline = loaded.json()
    assert loaded.headers["etag"] == baseline["etag"]
    assert (await request(app, "PUT", json={"content": "missing"})).status_code == 428
    saved = await request(app, "PUT", headers={"If-Match": baseline["etag"]}, json={"content": "已提交正文", "expected_revision": 0, "expected_hash": baseline["content_hash"]})
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["revision"] == 1 and saved.headers["etag"] == body["etag"]
    assert body["derived_status"] == "deferred" and body["derivation_job_id"]
    # An independent SQLite connection must observe the body before success.
    with sqlite3.connect(db.db_path) as independent:
        assert independent.execute("SELECT content,revision FROM chapters WHERE id='chapter'").fetchone() == ("已提交正文", 1)
    stale = await request(app, "PUT", headers={"If-Match": baseline["etag"]}, json={"content": "过期编辑", "expected_revision": 0})
    assert stale.status_code == 409 and stale.json()["detail"]["current"]["revision"] == 1
    assert "+++ local" in stale.json()["detail"]["diff"]
    # The header must not override a contradictory stale body precondition.
    contradictory = await request(app, "PUT", headers={"If-Match": body["etag"]}, json={"content": "unsafe", "expected_revision": 0})
    assert contradictory.status_code == 409


@pytest.mark.asyncio
async def test_two_prepared_writers_have_one_winner_and_one_conflict_without_phantom_outbox(chapter_http, monkeypatch):
    app, db, _pipeline, _knowledge = chapter_http
    barrier = threading.Barrier(2)
    prepare = ChapterVersionService.prepare_save

    def synchronized_prepare(self, *args, **kwargs):
        statement = prepare(self, *args, **kwargs)
        barrier.wait(timeout=5)
        return statement

    monkeypatch.setattr(ChapterVersionService, "prepare_save", synchronized_prepare)
    results = await asyncio.gather(*[
        request(app, "PUT", json={"content": content, "expected_revision": 0})
        for content in ("并发 A", "并发 B")
    ])
    assert sorted(response.status_code for response in results) == [200, 409]
    winner = next(response.json() for response in results if response.status_code == 200)
    assert db.fetch_one("SELECT content,revision FROM chapters WHERE id='chapter'") == {"content": winner["content"], "revision": 1}
    jobs = db.fetch_all("SELECT payload_json FROM chapter_derivation_jobs")
    assert len(jobs) == 1 and json.loads(jobs[0]["payload_json"])["content"] == winner["content"]
    assert len(ChapterVersionService(db).list_versions("chapter")) == 2


@pytest.mark.asyncio
async def test_failed_writer_rolls_back_body_history_and_outbox(chapter_http):
    app, db, _pipeline, _knowledge = chapter_http
    db.execute("CREATE TRIGGER reject_probe BEFORE UPDATE OF content ON chapters WHEN NEW.content='reject' BEGIN SELECT RAISE(ABORT,'reject probe'); END")
    failed = await request(app, "PUT", json={"content": "reject", "expected_revision": 0})
    assert failed.status_code == 503
    assert db.fetch_one("SELECT revision,content FROM chapters WHERE id='chapter'") == {"revision": 0, "content": "原文"}
    assert db.fetch_all("SELECT * FROM chapter_derivation_jobs") == []
    assert len(ChapterVersionService(db).list_versions("chapter")) == 1


@pytest.mark.asyncio
async def test_startup_recovery_avoids_loading_models_without_pending_jobs(chapter_http):
    _app, db, _pipeline, _knowledge = chapter_http
    def unexpected_factory():
        pytest.fail('an empty recovery queue must not initialize model services')
    assert await recover_chapter_derivation_jobs(db, startup=True,
        pipeline_factory=unexpected_factory, knowledge_service_factory=unexpected_factory) == []


@pytest.mark.asyncio
async def test_recovery_factories_run_off_event_loop_and_replay_pending_job(chapter_http):
    _app, db, pipeline, knowledge = chapter_http
    loop_thread = threading.get_ident()
    factory_threads = []
    def create_pipeline():
        factory_threads.append(threading.get_ident())
        return pipeline
    def create_knowledge():
        factory_threads.append(threading.get_ident())
        return knowledge
    ChapterVersionService(db).save_if_revision('chapter', 0, '恢复正文',
        pending_job={'job_id': 'lazy-recovery', 'payload': {'content': '恢复正文'}})
    assert await recover_chapter_derivation_jobs(db, startup=True,
        pipeline_factory=create_pipeline, knowledge_service_factory=create_knowledge) == ['completed']
    assert len(factory_threads) == 2 and all(item != loop_thread for item in factory_threads)
    assert db.fetch_one("SELECT status FROM chapter_derivation_jobs WHERE job_id='lazy-recovery'")['status'] == 'completed'


@pytest.mark.asyncio
async def test_sidecar_failure_is_explicit_and_restart_recovery_replays_pending_job(chapter_http):
    app, db, pipeline, knowledge = chapter_http
    knowledge.fail = True
    response = await request(app, "PUT", json={"content": "已提交正文", "expected_revision": 0, "micro_beats": [{"description": "动作"}]})
    assert response.status_code == 200 and response.json()["derived_status"] == "deferred"
    job_id = response.json()["derivation_job_id"]
    assert db.fetch_one("SELECT status,last_error FROM chapter_derivation_jobs WHERE job_id=?", (job_id,))["status"] == "unavailable"
    jobs = await request(app, "GET", "/derivation-jobs")
    assert jobs.json()[0]["status"] == "unavailable"
    knowledge.fail = False
    # Mimic a process stopped while a claimed job was running.
    db.execute("UPDATE chapter_derivation_jobs SET status='running' WHERE job_id=?", (job_id,))
    assert await recover_chapter_derivation_jobs(db, pipeline, knowledge, startup=True) == ["completed"]
    assert len(knowledge.calls) == 1
    assert db.fetch_one("SELECT attempts,status FROM chapter_derivation_jobs WHERE job_id=?", (job_id,)) == {"attempts": 2, "status": "completed"}
    assert await run_chapter_derivation_job(db, job_id, pipeline, knowledge) is None


@pytest.mark.asyncio
async def test_recovery_discards_superseded_version_and_hash_mismatch(chapter_http):
    _app, db, pipeline, knowledge = chapter_http
    versions = ChapterVersionService(db)
    saved = versions.save_if_revision("chapter", 0, "old", pending_job={"job_id": "stale", "payload": {"content": "old", "micro_beats": [{"description": "old beat"}]}})
    versions.save_if_revision("chapter", saved["revision"], "current")
    assert await run_chapter_derivation_job(db, "stale", pipeline, knowledge) == "superseded"
    # Even an otherwise matching revision cannot replay a mismatched payload.
    db.execute("INSERT INTO chapter_derivation_jobs(job_id,chapter_id,novel_id,chapter_number,revision,payload_json) VALUES(?,?,?,?,?,?)", ("hash-mismatch", "chapter", "book", 1, 2, json.dumps({"content": "wrong", "micro_beats": []})))
    assert await run_chapter_derivation_job(db, "hash-mismatch", pipeline, knowledge) == "superseded"
    assert not pipeline.calls and not knowledge.calls


@pytest.mark.asyncio
async def test_history_diff_and_restore_create_new_version_with_cas(chapter_http):
    app, db, _pipeline, _knowledge = chapter_http
    saved = await request(app, "PUT", json={"content": "最新稿", "expected_revision": 0})
    current = saved.json()
    versions = await request(app, "GET", "/versions")
    assert [version["revision"] for version in versions.json()] == [1, 0]
    diff = await request(app, "GET", "/versions/0/diff")
    assert "--- revision-0" in diff.json()["diff"] and "+++ revision-1" in diff.json()["diff"]
    assert (await request(app, "POST", "/versions/0/restore", json={})).status_code == 428
    assert (await request(app, "POST", "/versions/0/restore", json={"expected_revision": 0})).status_code == 409
    restored = await request(app, "POST", "/versions/0/restore", headers={"If-Match": current["etag"]}, json={"expected_revision": 1, "expected_hash": content_hash("最新稿")})
    assert restored.status_code == 200, restored.text
    assert restored.json()["revision"] == 2 and restored.json()["content"] == "原文"
    history = ChapterVersionService(db).list_versions("chapter")
    assert [version["revision"] for version in history] == [2, 1, 0]
    assert history[0]["source"] == "restore:0" and history[1]["content"] == "最新稿"
    assert (await request(app, "GET", "/versions/999/diff")).status_code == 404
    assert (await request(app, "POST", "/versions/999/restore", json={"expected_revision": 2})).status_code == 404


@pytest.mark.asyncio
async def test_legacy_draft_diff_restore_is_cas_guarded_and_audited(chapter_http):
    app, db, _pipeline, _knowledge = chapter_http
    draft_response = await request(app, "POST", "/drafts", json={"source": "pre_regen"})
    assert draft_response.status_code == 201, draft_response.text
    draft = draft_response.json()
    changed = await request(app, "PUT", json={"content": "重新生成稿", "expected_revision": 0})
    current = changed.json()

    diff = await request(app, "GET", f"/drafts/{draft['id']}/diff")
    assert diff.status_code == 200
    assert diff.json()["against_revision"] == 1
    assert f"--- draft-{draft['id']}" in diff.json()["diff"]
    assert "+++ revision-1" in diff.json()["diff"]

    restored = await request(
        app,
        "POST",
        f"/drafts/{draft['id']}/restore",
        headers={"If-Match": current["etag"]},
        json={"expected_revision": 1, "expected_hash": content_hash("重新生成稿")},
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["revision"] == 2
    assert restored.json()["content"] == "原文"
    assert db.fetch_one(
        "SELECT draft_id,from_revision,to_revision,draft_content_hash,source "
        "FROM chapter_draft_restore_audits WHERE draft_id=?",
        (draft["id"],),
    ) == {
        "draft_id": draft["id"],
        "from_revision": 1,
        "to_revision": 2,
        "draft_content_hash": content_hash("原文"),
        "source": f"restore:draft:{draft['id']}",
    }
    stale = await request(
        app,
        "POST",
        f"/drafts/{draft['id']}/restore",
        json={"expected_revision": 1},
    )
    assert stale.status_code == 409
    # Legacy retention trims old snapshots; an audit must remain readable and
    # must never turn the ten-entry retention policy into a write failure.
    for _ in range(11):
        assert (await request(app, "POST", "/drafts", json={"source": "manual_save"})).status_code == 201
    assert db.fetch_one(
        "SELECT draft_id FROM chapter_draft_restore_audits WHERE draft_id=?",
        (draft["id"],),
    )
    assert (await request(app, "GET", "/drafts/not-owned/diff")).status_code == 404
