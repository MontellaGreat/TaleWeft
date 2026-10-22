"""Durable command/lease invariants against a real isolated SQLite database."""
import concurrent.futures
import os
import threading
import time

import pytest

from application.engine.services import autopilot_control as control
from application.engine.services import execution_lease as leases
from application.engine.services.novel_stop_signal import clear_all_stop_signals, is_novel_stopped
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.write_dispatch import sqlite_writes_bypass_queue


@pytest.fixture
def db(tmp_path):
    with sqlite_writes_bypass_queue():
        database = DatabaseConnection(str(tmp_path / "control.db"))
        database.execute("INSERT INTO novels (id,title,slug,current_stage) VALUES ('book','测试','test','macro_planning')")
        database.commit()
        control._processing.clear()
        control._last_lease_refresh = 0
        clear_all_stop_signals()
        yield database
        clear_all_stop_signals()
        control._processing.clear()
        database.close_all(skip_checkpoint=True)


def payload():
    return {"next_stage": "macro_planning", "max_auto_chapters": 20, "target_chapters": 10,
            "target_words_per_chapter": 2000, "current_act": 0}


def test_api_submission_does_not_claim_executor_running(db):
    command = control.submit_command("book", "start", payload(), db=db)
    assert command["state"] == "pending"
    assert control.command_response(command)["execution_confirmed"] is False
    assert db.fetch_one("SELECT autopilot_status FROM novels WHERE id='book'")["autopilot_status"] == "stopped"
    assert leases.get_execution_lease("book", db=db)["job_id"] == command["command_id"]
    assert control.process_pending_commands(db=db) == [command["command_id"]]
    acknowledged = control.get_command(command["command_id"], db=db)
    assert acknowledged["state"] == "completed"
    assert acknowledged["executor_pid"] == os.getpid()
    assert db.fetch_one("SELECT autopilot_status FROM novels WHERE id='book'")["autopilot_status"] == "running"


def test_stop_only_acknowledged_after_processing_exits(db):
    start = control.submit_command("book", "start", payload(), db=db)
    control.process_pending_commands(db=db)
    control.mark_processing("book")
    stop = control.submit_command("book", "stop", {}, db=db)
    control.process_pending_commands(db=db)
    assert is_novel_stopped("book")
    assert control.stop_requested("book", db=db)
    assert control.get_command(stop["command_id"], db=db)["state"] == "applied"
    assert db.fetch_one("SELECT autopilot_status FROM novels WHERE id='book'")["autopilot_status"] == "running"
    control.refresh_owned_leases(db=db)
    assert leases.get_execution_lease("book", db=db)["job_id"] == start["command_id"]
    assert control.confirm_quiescent("book", db=db) == [stop["command_id"]]
    assert control.get_command(stop["command_id"], db=db)["state"] == "completed"
    assert db.fetch_one("SELECT autopilot_status FROM novels WHERE id='book'")["autopilot_status"] == "stopped"
    assert leases.get_execution_lease("book", db=db) is None


def test_stale_stop_superseded_by_new_start(db):
    stop = control.submit_command("book", "stop", {}, db=db)
    start = control.submit_command("book", "start", payload(), db=db)
    control.process_pending_commands(db=db)
    assert control.get_command(stop["command_id"], db=db)["state"] == "superseded"
    assert control.get_command(start["command_id"], db=db)["state"] == "completed"
    assert not is_novel_stopped("book")
    assert not control.stop_requested("book", db=db)


def test_identical_command_id_is_idempotent(db):
    command = control.submit_command("book", "start", payload(), db=db, command_id="one")
    assert control.submit_command("book", "start", payload(), db=db, command_id="one")["run_epoch"] == command["run_epoch"]
    assert len(db.fetch_all("SELECT * FROM autopilot_commands")) == 1


def test_dag_owner_blocks_control_without_epoch_or_command(db):
    assert leases.claim_execution_lease("book", "dag", "run", db=db)
    for kind in ["start", "resume", "stop"]:
        with pytest.raises(ValueError):
            control.submit_command("book", kind, payload(), db=db)
    assert db.fetch_one("SELECT autopilot_run_epoch FROM novels WHERE id='book'")["autopilot_run_epoch"] == 0
    assert not db.fetch_all("SELECT * FROM autopilot_commands")
    assert leases.get_execution_lease("book", db=db)["owner"] == "dag"


def test_missing_book_does_not_leave_lease(db):
    with pytest.raises(LookupError):
        control.submit_command("missing", "start", payload(), db=db)
    assert leases.get_execution_lease("missing", db=db) is None


def test_failed_command_transaction_rolls_back_lease_and_epoch(db):
    control.ensure_control_schema(db)
    db.execute("CREATE TRIGGER reject_command BEFORE INSERT ON autopilot_commands BEGIN SELECT RAISE(ABORT,'injected write failure'); END")
    db.commit()
    with pytest.raises(Exception, match="injected write failure"):
        control.submit_command("book", "start", payload(), db=db)
    assert leases.get_execution_lease("book", db=db) is None
    assert db.fetch_one("SELECT autopilot_run_epoch FROM novels WHERE id='book'")["autopilot_run_epoch"] == 0
    assert not db.fetch_all("SELECT * FROM autopilot_commands")


def test_old_job_cannot_release_new_owner(db):
    assert leases.claim_execution_lease("book", "autopilot", "old", db=db)
    assert leases.claim_execution_lease("book", "autopilot", "new", db=db, replace_existing_owner=True)
    assert not leases.release_execution_lease("book", "autopilot", "old", db=db)
    assert leases.get_execution_lease("book", db=db)["job_id"] == "new"


def test_running_restart_is_rejected_even_with_stale_api_read(db):
    control.submit_command("book", "start", payload(), db=db)
    control.process_pending_commands(db=db)
    with pytest.raises(ValueError):
        control.submit_command("book", "start", payload(), db=db)
    assert len(db.fetch_all("SELECT * FROM autopilot_commands")) == 1


def test_expired_execution_owner_is_not_renewed(db):
    command = control.submit_command("book", "start", payload(), db=db)
    control.process_pending_commands(db=db)
    db.execute("UPDATE novel_execution_leases SET expires_at=0")
    db.commit()
    assert control.refresh_owned_leases(db=db) == {"book"}
    assert is_novel_stopped("book")
    assert leases.claim_execution_lease("book", "dag", "replacement", db=db)
    assert not leases.release_execution_lease("book", "autopilot", command["command_id"], db=db)


def test_read_error_is_not_treated_as_no_command_or_owner():
    class BrokenDb:
        def fetch_one(self, *args):
            raise RuntimeError("disk unavailable")
        def fetch_all(self, *args):
            raise RuntimeError("disk unavailable")
    for operation in [lambda: control.latest_command("book", BrokenDb()),
                      lambda: control.process_pending_commands(db=BrokenDb()),
                      lambda: leases.get_execution_lease("book", BrokenDb())]:
        with pytest.raises(RuntimeError, match="disk unavailable"):
            operation()


def test_concurrent_dag_and_autopilot_have_one_atomic_owner(db):
    control.ensure_control_schema(db)
    leases.ensure_execution_schema(db)
    barrier = threading.Barrier(2)
    def autopilot():
        with sqlite_writes_bypass_queue():
            barrier.wait()
            try:
                control.submit_command("book", "start", payload(), db=db)
                return True
            except ValueError:
                return False
    def dag():
        with sqlite_writes_bypass_queue():
            barrier.wait()
            return leases.claim_execution_lease("book", "dag", "dag-job", db=db)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda fn: fn(), [autopilot, dag]))
    assert sum(results) == 1
    owner = leases.get_execution_lease("book", db=db)["owner"]
    commands = db.fetch_all("SELECT * FROM autopilot_commands")
    assert bool(commands) == (owner == "autopilot")
