"""Exercise the desktop child-process entry point with an isolated SQLite DB."""
import asyncio
import logging
import sys
import time
from types import SimpleNamespace

import pytest

from application.engine.services import autopilot_control as control
from application.engine.services import execution_lease as leases
from application.engine.services.novel_stop_signal import clear_all_stop_signals
from engine.runtime.daemon_host import DaemonHostMixin
from engine.runtime.daemon_loop import _process_novel_with_lease_heartbeat
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.write_dispatch import sqlite_writes_bypass_queue
from interfaces.daemon_manager import run_autopilot_daemon_process


class OnePoll:
    def __init__(self):
        self.stopped = False

    def is_set(self):
        return self.stopped

    def wait(self, timeout=None):
        self.stopped = True


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    with sqlite_writes_bypass_queue():
        db = DatabaseConnection(str(tmp_path / "daemon.sqlite"))
        db.execute("INSERT INTO novels(id,title,slug,current_stage) VALUES ('book','book','book','macro_planning')")
        db.commit()
        control._processing.clear()
        control._last_lease_refresh = 0
        clear_all_stop_signals()
        monkeypatch.setattr(control, "_db", lambda database=None: database or db)
        shared = {}
        monkeypatch.setitem(sys.modules, "__shared_state", shared)
        monkeypatch.setattr("interfaces.api.middleware.logging_config.setup_logging", lambda **kwargs: None)
        host = DaemonHostMixin()
        host.poll_interval = 0.01
        host.circuit_breaker = None
        host._cleanup_stale_stop_signals = lambda novels: None
        host._get_active_novels = lambda: [SimpleNamespace(novel_id=SimpleNamespace(value="book"))] if db.fetch_one(
            "SELECT autopilot_status FROM novels WHERE id='book'"
        )["autopilot_status"] == "running" else []
        monkeypatch.setitem(sys.modules, "scripts.start_daemon", SimpleNamespace(build_daemon=lambda: host))
        yield db, host, shared
        clear_all_stop_signals()
        control._processing.clear()
        db.close_all(skip_checkpoint=True)


def start(db):
    return control.submit_command("book", "start", {
        "next_stage": "macro_planning", "max_auto_chapters": 10,
        "target_chapters": 10, "target_words_per_chapter": 2000,
    }, db=db)


def test_desktop_daemon_dispatches_pending_start_and_confirms_stop_after_work(runtime):
    db, host, shared = runtime
    command = start(db)
    processed = []
    stop_commands = []

    async def process(novel):
        assert control.get_command(command["command_id"], db=db)["state"] == "completed"
        assert "book" in control._processing
        stop = control.submit_command("book", "stop", {}, db=db)
        stop_commands.append(stop["command_id"])
        control.process_pending_commands(db=db)
        assert control.get_command(stop["command_id"], db=db)["state"] == "applied"
        processed.append(novel.novel_id.value)

    host._process_novel = process
    run_autopilot_daemon_process(OnePoll(), logging.INFO, "unused.log")

    assert processed == ["book"]
    assert shared["_daemon_heartbeat"] > 0
    assert control.get_command(stop_commands[0], db=db)["state"] == "completed"
    assert "book" not in control._processing
    assert leases.get_execution_lease("book", db=db) is None


def test_desktop_daemon_heartbeat_renews_owned_execution_lease(runtime):
    db, host, shared = runtime
    command = start(db)
    control.process_pending_commands(db=db)
    expires = time.time() + 10
    db.execute("UPDATE novel_execution_leases SET expires_at=?,updated_at=0", (expires,))
    db.commit()
    host._get_active_novels = lambda: []
    run_autopilot_daemon_process(OnePoll(), logging.INFO, "unused.log")

    lease = leases.get_execution_lease("book", db=db)
    assert lease["job_id"] == command["command_id"]
    assert lease["expires_at"] > expires
    assert shared["_daemon_heartbeat"] > 0


def test_desktop_daemon_closes_its_event_loop(runtime, monkeypatch):
    loops = []
    original = asyncio.new_event_loop

    def create_loop():
        loop = original()
        loops.append(loop)
        return loop

    monkeypatch.setattr(asyncio, "new_event_loop", create_loop)
    run_autopilot_daemon_process(OnePoll(), logging.INFO, "unused.log")
    assert len(loops) == 1
    assert loops[0].is_closed()


def test_long_provider_call_refreshes_execution_heartbeat():
    calls = []

    class Host:
        async def _process_novel(self, novel):
            await asyncio.sleep(0.06)

        def _write_daemon_heartbeat(self):
            calls.append(time.monotonic())

    asyncio.run(_process_novel_with_lease_heartbeat(Host(), object(), 0.01))
    assert calls, "a long provider call must renew the owned execution lease"


@pytest.mark.asyncio
async def test_durable_stop_cancels_a_pending_planning_provider_before_ack(runtime):
    db, host, _shared = runtime
    command = start(db)
    control.process_pending_commands(db=db)
    control.mark_processing("book")
    stopped = []
    cancelled = False

    async def process(_novel):
        nonlocal cancelled
        stopped.append(control.submit_command("book", "stop", {}, db=db)["command_id"])
        try:
            await asyncio.Event().wait()  # a model that has not returned yet
        finally:
            cancelled = True

    host._process_novel = process
    novel = SimpleNamespace(novel_id=SimpleNamespace(value="book"))
    try:
        await asyncio.wait_for(_process_novel_with_lease_heartbeat(host, novel, 0.01), timeout=0.3)
        assert cancelled
        assert control.get_command(stopped[0], db=db)["state"] == "applied"
    finally:
        control.confirm_quiescent("book", db=db)

    assert control.get_command(stopped[0], db=db)["state"] == "completed"
    assert control.get_command(command["command_id"], db=db)["state"] == "completed"
    assert leases.get_execution_lease("book", db=db) is None
