"""AOF startup and legacy completion survive delayed/rejected writer ACKs."""
import asyncio
import hashlib
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from application.engine.services import draft_aof
from application.engine.services.persistence_queue import PersistenceCommitTimeout
from domain.novel.value_objects.novel_id import NovelId
from engine.runtime.daemon_host import DaemonHostMixin
from infrastructure.persistence.database.sqlite_chapter_repository import SqliteChapterRepository
from interfaces.runtime import BackendLifecycle
from interfaces.api.v1 import system as system_routes
from tests.unit.application.engine.test_persistence_commit_ack import writer


@pytest.fixture
def aof_writer(writer, tmp_path, monkeypatch):
    queue, db = writer
    directory = tmp_path / "drafts"
    directory.mkdir()
    monkeypatch.setattr(draft_aof, "_draft_dir", lambda: directory)
    monkeypatch.setattr("application.paths.get_db_path", lambda: db.db_path)
    db.execute("INSERT INTO novels(id,title,slug,current_beat_index,beats_completed) "
               "VALUES ('novel','Novel','novel',0,1)")
    db.execute("INSERT INTO chapters(id,novel_id,number,title,content,status) "
               "VALUES ('chapter','novel',1,'First','seed','draft')")
    path = directory / "novel_ch1.draft"
    path.write_text("seed final", encoding="utf-8")
    return queue, db, path


def lifecycle_recover():
    """Use the actual production startup recovery wrapper, before daemon start."""
    lifecycle = BackendLifecycle(start_daemon=lambda: None, stop_daemon=lambda: None)
    lifecycle.recover_drafts()


def job(db):
    return db.fetch_one("SELECT * FROM aof_recovery_jobs")


def test_default_startup_waits_for_delayed_writer_before_unlink(aof_writer):
    queue, db, path = aof_writer
    original = queue._handlers["execute_sql"]
    entered, release = threading.Event(), threading.Event()

    def delayed(payload):
        if "UPDATE chapters SET content" in payload.get("sql", ""):
            entered.set()
            assert release.wait(5)
        return original(payload)

    queue.register_handler("execute_sql", delayed)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(lifecycle_recover)
        try:
            assert entered.wait(3)
            assert path.read_text(encoding="utf-8") == "seed final"
            assert job(db)["status"] == "running"
            assert db.fetch_one("SELECT content FROM chapters")["content"] == "seed"
        finally:
            release.set()
        future.result(timeout=5)
    saved = job(db)
    assert saved["status"] == "completed" and saved["attempts"] == 1
    assert saved["committed_revision"] == 1
    assert saved["committed_content_hash"] == hashlib.sha256(b"seed final").hexdigest()
    assert not path.exists()


def test_rejected_startup_retains_file_and_explicit_job_retry_recovers(aof_writer):
    queue, db, path = aof_writer
    original = queue._handlers["execute_sql"]

    def rejected(payload):
        if "UPDATE chapters SET content" in payload.get("sql", ""):
            raise sqlite3.IntegrityError("injected rejection")
        return original(payload)

    queue.register_handler("execute_sql", rejected)
    lifecycle_recover()
    saved = job(db)
    assert saved["status"] == "failed" and path.exists()
    assert db.fetch_one("SELECT content FROM chapters")["content"] == "seed"
    queue.register_handler("execute_sql", original)
    result = draft_aof.run_recovery_job(saved["job_id"])
    assert result["status"] == "completed" and result["attempts"] == 2
    assert not path.exists()
    assert db.fetch_one("SELECT content FROM chapters")["content"] == "seed final"


def test_restart_repairs_state_after_body_committed_before_state_write(aof_writer, monkeypatch):
    _queue, db, path = aof_writer
    original = draft_aof._mark_recovered_chapter_state
    monkeypatch.setattr(draft_aof, "_mark_recovered_chapter_state",
                        lambda *_args: (_ for _ in ()).throw(RuntimeError("interrupted state write")))
    assert draft_aof.recover_all_drafts() == 0
    assert path.exists() and job(db)["status"] == "failed"
    assert db.fetch_one("SELECT content FROM chapters")["content"] == "seed final"
    assert db.fetch_one("SELECT beats_completed FROM novels")["beats_completed"] == 1
    # Simulate a process exiting with an outstanding claim.  The default
    # startup wrapper is the only entry allowed to release a running job.
    db.execute("UPDATE aof_recovery_jobs SET status='running'")
    monkeypatch.setattr(draft_aof, "_mark_recovered_chapter_state", original)
    lifecycle_recover()
    assert not path.exists() and job(db)["status"] == "completed"
    assert db.fetch_one("SELECT current_beat_index,beats_completed FROM novels") == {
        "current_beat_index": 1, "beats_completed": 0,
    }
    assert db.fetch_one("SELECT revision FROM chapters")["revision"] == 1


def test_restart_finishes_verified_receipt_after_file_unlink(aof_writer, monkeypatch):
    _queue, db, path = aof_writer
    original = draft_aof._set_recovery_job_status

    def interrupted(database, job_id, status, *args, **kwargs):
        if status in {"completed", "failed"}:
            raise RuntimeError("process exited after unlink")
        return original(database, job_id, status, *args, **kwargs)

    monkeypatch.setattr(draft_aof, "_set_recovery_job_status", interrupted)
    assert draft_aof.recover_all_drafts() == 0
    assert not path.exists() and job(db)["status"] == "verified"
    monkeypatch.setattr(draft_aof, "_set_recovery_job_status", original)
    lifecycle_recover()
    assert job(db)["status"] == "completed"
    assert db.fetch_one("SELECT revision FROM chapters")["revision"] == 1


def legacy_host(queue, db):
    host = DaemonHostMixin()
    host.chapter_repository = SqliteChapterRepository(db)
    host._commit_persistence_command = lambda command, payload: queue.push_and_wait(command, payload, timeout=3)
    novel = SimpleNamespace(novel_id=NovelId("novel"), generation_prefs=None)
    chapter = SimpleNamespace(id="chapter", number=1, title="First", outline="")
    return host, novel, chapter


@pytest.mark.asyncio
async def test_legacy_completion_keeps_aof_until_writer_ack_then_records_receipt(aof_writer):
    queue, db, path = aof_writer
    original = queue._handlers["upsert_chapter"]
    entered, release = threading.Event(), threading.Event()

    def delayed(payload):
        entered.set()
        assert release.wait(5)
        return original(payload)

    queue.register_handler("upsert_chapter", delayed)
    host, novel, chapter = legacy_host(queue, db)
    task = asyncio.create_task(host._upsert_chapter_content(novel, chapter, "seed final", "completed"))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        assert path.exists() and job(db)["status"] == "pending"
        assert db.fetch_one("SELECT status FROM chapters")["status"] == "draft"
    finally:
        release.set()
    await task
    # This is the same cleanup operation invoked by run_legacy_writing after
    # the completion method returns; it records the matching committed body.
    draft_aof.delete_draft("novel", 1)
    assert not path.exists() and job(db)["status"] == "completed"
    assert db.fetch_one("SELECT status FROM chapters")["status"] == "completed"
    assert job(db)["committed_revision"] == 1


@pytest.mark.asyncio
async def test_rejected_legacy_completion_restores_draft_on_restart(aof_writer):
    queue, db, path = aof_writer
    queue.register_handler("upsert_chapter", lambda _payload: (_ for _ in ()).throw(sqlite3.IntegrityError("rejected")))
    host, novel, chapter = legacy_host(queue, db)
    with pytest.raises(RuntimeError, match="rejected"):
        await host._upsert_chapter_content(novel, chapter, "seed final", "completed")
    assert path.exists() and job(db)["status"] == "pending"
    lifecycle_recover()
    assert not path.exists() and job(db)["status"] == "completed"
    assert db.fetch_one("SELECT status,content FROM chapters") == {"status": "draft", "content": "seed final"}
    assert db.fetch_one("SELECT beats_completed FROM novels")["beats_completed"] == 0


@pytest.mark.asyncio
async def test_legacy_timeout_then_late_commit_is_reconciled_after_restart(aof_writer):
    queue, db, path = aof_writer
    original = queue._handlers["upsert_chapter"]
    entered, release = threading.Event(), threading.Event()

    def delayed(payload):
        entered.set()
        assert release.wait(5)
        return original(payload)

    queue.register_handler("upsert_chapter", delayed)
    host, novel, chapter = legacy_host(queue, db)
    host._commit_persistence_command = lambda command, payload: queue.push_and_wait(command, payload, timeout=0.05)
    task = asyncio.create_task(host._upsert_chapter_content(novel, chapter, "seed final", "completed"))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        with pytest.raises(PersistenceCommitTimeout):
            await task
        assert path.exists() and job(db)["status"] == "pending"
    finally:
        release.set()
    assert await asyncio.to_thread(queue.wait_until_idle, 3)
    assert path.exists()  # An unobserved late ACK never cleans the AOF.
    lifecycle_recover()
    assert not path.exists() and job(db)["status"] == "completed"
    assert db.fetch_one("SELECT status,revision FROM chapters") == {"status": "completed", "revision": 1}
    assert db.fetch_one("SELECT current_beat_index,beats_completed FROM novels") == {
        "current_beat_index": 0, "beats_completed": 1,
    }


def test_normal_retry_never_steals_an_active_claim(aof_writer):
    _queue, db, path = aof_writer
    job_id = draft_aof.prepare_draft_cleanup("novel", 1)
    db.execute("UPDATE aof_recovery_jobs SET status='running' WHERE job_id=?", (job_id,))
    assert draft_aof.recover_all_drafts() == 0
    assert draft_aof.run_recovery_job(job_id)["status"] == "running"
    assert path.exists() and job(db)["attempts"] == 0
    lifecycle_recover()
    assert job(db)["status"] == "completed" and not path.exists()


@pytest.mark.asyncio
async def test_local_status_and_retry_api_reconciles_conflict_without_leaking_body(aof_writer):
    _queue, db, path = aof_writer
    db.execute("UPDATE chapters SET content='author edit' WHERE id='chapter'")
    lifecycle_recover()
    assert job(db)["status"] == "conflict" and path.exists()
    app = FastAPI()
    app.include_router(system_routes.router, prefix="/api/v1")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1234)), base_url="http://localhost") as client:
        response = await client.get("/api/v1/system/aof-recovery-jobs", params={"novel_id": "novel"})
        assert response.status_code == 200
        listed = response.json()["jobs"]
        assert len(listed) == 1 and listed[0]["status"] == "conflict"
        assert "content" not in listed[0]
        db.execute("UPDATE novels SET autopilot_status='running'")
        blocked = await client.post(f"/api/v1/system/aof-recovery-jobs/{listed[0]['job_id']}/retry")
        assert blocked.status_code == 409 and path.exists()
        assert job(db)["status"] == "conflict"
        db.execute("UPDATE novels SET autopilot_status='stopped'")
        db.execute("UPDATE chapters SET content='seed' WHERE id='chapter'")
        response = await client.post(f"/api/v1/system/aof-recovery-jobs/{listed[0]['job_id']}/retry")
        assert response.status_code == 200 and response.json()["status"] == "completed"
        assert "content" not in response.json()
    assert not path.exists()


def test_writer_rejects_recovery_if_a_new_run_started_after_the_read(aof_writer, monkeypatch):
    _queue, db, path = aof_writer
    original = db.execute

    def new_run(sql, params=()):
        if "UPDATE chapters SET content" in sql:
            original("UPDATE novels SET autopilot_status='running'")
        return original(sql, params)

    monkeypatch.setattr(db, "execute", new_run)
    assert draft_aof.recover_all_drafts() == 0
    assert path.exists() and job(db)["status"] == "failed"
    assert db.fetch_one("SELECT content FROM chapters")["content"] == "seed"
