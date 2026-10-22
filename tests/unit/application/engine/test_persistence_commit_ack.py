"""Exercise real writer/SQLite ACKs without user databases or model providers."""
import multiprocessing as mp
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from application.engine.services import persistence_queue as queue_module
from application.engine.services.persistence_queue import (
    PersistenceCommandType, PersistenceCommitError, PersistenceCommitTimeout,
    PersistenceQueue, register_persistence_handlers,
)
from infrastructure.persistence.database.connection import DatabaseConnection


def _spawned_submit(shared_queue, output, db_path):
    """Top-level spawn target: no inherited Python objects/handlers required."""
    queue = PersistenceQueue()
    queue.inject_queue(shared_queue)
    try:
        receipt = queue.push_and_wait(
            PersistenceCommandType.EXECUTE_SQL.value,
            {"sql": "INSERT INTO ack_probe(value) VALUES (?)", "params": ["daemon"],
             "db_path": db_path}, timeout=10,
        )
        output.send((receipt.committed, receipt.result["rowcount"]))
    except Exception as exc:
        output.send((False, str(exc)))
    finally:
        output.close()


@pytest.fixture
def writer(tmp_path, monkeypatch):
    monkeypatch.delenv("PLOTPILOT_ALLOW_DIRECT_SQLITE_WRITES", raising=False)
    monkeypatch.delenv("AITEXT_ALLOW_DIRECT_SQLITE_WRITES", raising=False)
    from infrastructure.persistence.database import connection as connection_module
    db = DatabaseConnection(str(tmp_path / "ack.sqlite"))
    monkeypatch.setattr(connection_module, "_db_instance", db)
    monkeypatch.setattr(connection_module, "_db_instances_by_path",
                        {str(Path(db.db_path).resolve()): db})
    queue = PersistenceQueue()
    queue.initialize()
    monkeypatch.setattr(queue_module, "_persistence_queue", queue)
    register_persistence_handlers()
    queue.start_consumer()
    db.execute("CREATE TABLE ack_probe(id INTEGER PRIMARY KEY, value TEXT UNIQUE)")
    yield queue, db
    queue.stop_consumer()
    db.close_all(skip_checkpoint=True)
    queue.get_queue().close()
    queue.get_queue().join_thread()


def test_commit_wait_and_idle_barrier_include_inflight_work(writer):
    queue, _db = writer
    entered, release = threading.Event(), threading.Event()

    def delayed(payload):
        entered.set()
        assert release.wait(3)
        return payload["value"]

    queue.register_handler("delayed", delayed)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(queue.push_and_wait, "delayed", {"value": 42}, 3)
        try:
            assert entered.wait(1)
            assert queue.get_queue().qsize() == 0
            assert not future.done()
            assert queue.wait_until_idle(timeout=0.03) is False
        finally:
            release.set()
        receipt = future.result(timeout=3)
    assert receipt.committed and receipt.result == 42
    assert queue.wait_until_idle(timeout=1)


def test_missing_handler_and_handler_failure_reject_ack(writer):
    queue, _db = writer
    with pytest.raises(PersistenceCommitError, match="未注册"):
        queue.push_and_wait("unknown", {}, timeout=1)

    def rejected(_payload):
        raise sqlite3.IntegrityError("foreign key rejected")

    queue.register_handler("rejected", rejected)
    with pytest.raises(PersistenceCommitError, match="IntegrityError.*foreign key"):
        queue.push_and_wait("rejected", {}, timeout=1)
    assert queue.get_stats()["failed"] == 2


@pytest.mark.parametrize('change', ['epoch','job','expired','aba'])
def test_generation_commit_checks_fence_and_revision_on_writer(writer, change):
    queue, db = writer
    from application.engine.services import autopilot_control as control
    db.execute("INSERT INTO novels(id,title,slug) VALUES ('fenced','test','fenced')")
    db.execute("INSERT INTO chapters(id,novel_id,number,content) VALUES ('fc','fenced',1,'base')")
    command = control.submit_command('fenced','start',{'next_stage':'writing', 'max_auto_chapters':10,
        'target_chapters':10, 'target_words_per_chapter':2000}, db=db)
    control.process_pending_commands(db=db)
    before = db.fetch_one("SELECT revision FROM chapters WHERE id='fc'")
    request = {'db_path':db.db_path, 'novel_id':'fenced', 'chapter_number':1,
        'content':'generated', 'status':'completed', 'expected_absent':False,
        'expected_revision':before['revision'], 'execution_identity':control.executor_identity('fenced',db)}
    if change == 'epoch':
        db.execute("UPDATE novels SET autopilot_run_epoch=autopilot_run_epoch+1 WHERE id='fenced'")
    elif change == 'job':
        db.execute("UPDATE novel_execution_leases SET job_id='successor' WHERE novel_id='fenced'")
    elif change == 'expired':
        db.execute("UPDATE novel_execution_leases SET expires_at=0 WHERE novel_id='fenced'")
    else:
        db.execute("UPDATE chapters SET content='intermediate' WHERE id='fc'")
        db.execute("UPDATE chapters SET content='base' WHERE id='fc'")
    db.commit()
    with pytest.raises(PersistenceCommitError, match='拒绝提交'):
        queue.push_and_wait('upsert_chapter', request, timeout=2)
    assert db.fetch_one("SELECT content FROM chapters WHERE id='fc'")['content'] == 'base'


def test_timeout_is_pending_and_late_ack_does_not_kill_writer(writer):
    queue, db = writer
    release = threading.Event()

    def delayed(_payload):
        assert release.wait(2)
        db.execute("INSERT INTO ack_probe(value) VALUES ('late')")
        db.commit()

    queue.register_handler("delayed", delayed)
    try:
        with pytest.raises(PersistenceCommitTimeout):
            queue.push_and_wait("delayed", {}, timeout=0.03)
    finally:
        release.set()
    assert queue.wait_until_idle(timeout=2)
    assert db.fetch_one("SELECT value FROM ack_probe")["value"] == "late"
    assert queue.is_consumer_running()


def test_execute_returns_real_affected_rows_and_returning_data(writer):
    _queue, db = writer
    cursor = db.execute("UPDATE ack_probe SET value = 'nothing' WHERE id = 404")
    assert cursor.rowcount == 0
    cursor = db.execute("INSERT INTO ack_probe(value) VALUES ('written') RETURNING id, value")
    assert cursor.rowcount == 1
    row = cursor.fetchone()
    assert row[1] == row["value"] == "written"
    assert dict(row)["id"] == cursor.lastrowid
    assert cursor.fetchone() is None
    # Independent connection sees the return only after the real commit.
    with sqlite3.connect(db.db_path) as independent:
        assert independent.execute("SELECT value FROM ack_probe").fetchone()[0] == "written"


def test_transaction_ack_is_atomic_and_updates_staged_cursor(writer):
    _queue, db = writer
    with db.transaction() as connection:
        cursor = connection.execute("INSERT INTO ack_probe(value) VALUES ('batch')")
        assert cursor.rowcount == -1
    assert cursor.rowcount == 1
    with pytest.raises(PersistenceCommitError, match="UNIQUE"):
        with db.transaction() as connection:
            connection.execute("INSERT INTO ack_probe(value) VALUES ('rolled back')")
            connection.execute("INSERT INTO ack_probe(value) VALUES ('batch')")
    assert db.fetch_one("SELECT 1 FROM ack_probe WHERE value = 'rolled back'") is None
    assert db.execute("UPDATE ack_probe SET value='still works'").rowcount == 1


def test_upsert_foreign_key_failure_is_not_success(writer):
    queue, _db = writer
    with pytest.raises(PersistenceCommitError, match="FOREIGN KEY"):
        queue.push_and_wait(PersistenceCommandType.UPSERT_CHAPTER.value,
                            {"novel_id": "missing", "chapter_number": 1,
                             "content": "must survive"}, timeout=1)


def test_real_spawn_daemon_receives_post_commit_ack(writer):
    queue, db = writer
    context = mp.get_context("spawn")
    receive, send = context.Pipe(duplex=False)
    process = context.Process(target=_spawned_submit,
                              args=(queue.get_queue(), send, db.db_path))
    process.start()
    send.close()
    try:
        assert receive.poll(15), "spawned producer did not receive commit ACK"
        assert receive.recv() == (True, 1)
        process.join(timeout=5)
        assert process.exitcode == 0
        assert db.fetch_one("SELECT value FROM ack_probe")["value"] == "daemon"
    finally:
        receive.close()
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
