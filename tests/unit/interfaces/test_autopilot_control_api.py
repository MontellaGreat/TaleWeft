"""HTTP receipts backed by a real temporary SQLite database, without LLM calls."""
import asyncio
import threading
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from application.engine.services import autopilot_control as control
from interfaces.api.v1.engine import autopilot_routes as routes
from infrastructure.persistence.database import connection
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.sqlite_novel_repository import SqliteNovelRepository
from infrastructure.persistence.database.write_dispatch import sqlite_writes_bypass_queue


@pytest.fixture
def db(tmp_path, monkeypatch):
    with sqlite_writes_bypass_queue():
        database = DatabaseConnection(str(tmp_path / "api-control.db"))
        database.execute("INSERT INTO novels (id,title,slug,current_stage) VALUES ('book','测试','test','macro_planning')")
        database.commit()
    # Thread-local bootstrap mode is explicitly enabled only for this test database.
    monkeypatch.setattr("infrastructure.persistence.database.write_dispatch.allow_direct_sqlite_writes", lambda: True)
    monkeypatch.setattr(connection, "get_database", lambda *args, **kwargs: database)
    monkeypatch.setattr(routes, "get_novel_repository", lambda: SqliteNovelRepository(database))
    from application.engine.services import query_service, novel_stop_signal
    monkeypatch.setattr(query_service, "get_query_service", lambda: SimpleNamespace(get_novel_status_dict=lambda nid: None))
    monkeypatch.setattr(novel_stop_signal, "publish_start_signal", lambda *args, **kwargs: False)
    monkeypatch.setattr(novel_stop_signal, "publish_stop_signal", lambda *args, **kwargs: False)
    monkeypatch.setattr(routes, "_get_shared_state_for_novel", lambda nid: None)
    monkeypatch.setattr(routes, "_autopilot_executor_running", lambda: True, raising=False)
    control._processing.clear()
    yield database
    control._processing.clear()
    database.close_all(skip_checkpoint=True)


def app():
    api = FastAPI()
    api.include_router(routes.router)
    return api


@pytest.mark.asyncio
async def test_http_start_stop_receipts_reflect_executor_ack(db):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app()), base_url="http://test") as client:
        response = await client.post("/autopilot/book/start", json={"target_chapters": 10, "target_words_per_chapter": 2000})
        assert response.status_code == 200
        started = response.json()
        assert started["accepted"] is True
        assert started["execution_confirmed"] is False
        assert started["ipc_delivered"] is False
        assert db.fetch_one("SELECT autopilot_status FROM novels WHERE id='book'")["autopilot_status"] == "stopped"
        assert (await client.get(f"/autopilot/book/commands/{started['command_id']}" )).json()["command_status"] == "pending"
        control.process_pending_commands(db=db)
        confirmed = (await client.get(f"/autopilot/book/commands/{started['command_id']}" )).json()
        assert confirmed["execution_confirmed"] is True
        control.mark_processing("book")
        stopped = (await client.post("/autopilot/book/stop")).json()
        assert stopped["execution_confirmed"] is False
        control.process_pending_commands(db=db)
        assert (await client.get(f"/autopilot/book/commands/{stopped['command_id']}" )).json()["command_status"] == "applied"
        control.confirm_quiescent("book", db=db)
        assert (await client.get(f"/autopilot/book/commands/{stopped['command_id']}" )).json()["execution_confirmed"] is True
        assert db.fetch_one("SELECT autopilot_status FROM novels WHERE id='book'")["autopilot_status"] == "stopped"


@pytest.mark.asyncio
async def test_start_is_rejected_before_persisting_when_executor_is_unavailable(db, monkeypatch):
    monkeypatch.setattr(routes, "_autopilot_executor_running", lambda: False, raising=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app()), base_url="http://test") as client:
        response = await client.post("/autopilot/book/start", json={"target_chapters": 10})

    assert response.status_code == 503
    assert "执行器" in response.json()["detail"]
    assert db.fetch_one("SELECT autopilot_status,autopilot_run_epoch FROM novels WHERE id='book'") == {
        "autopilot_status": "stopped", "autopilot_run_epoch": 0,
    }
    assert db.fetch_one("SELECT COUNT(*) AS n FROM sqlite_master WHERE type='table' AND name='autopilot_commands'")["n"] == 0


@pytest.mark.asyncio
async def test_completed_book_noop_and_target_increase_409(db):
    db.execute("UPDATE novels SET current_stage='completed',autopilot_status='completed',target_chapters=10 WHERE id='book'")
    db.commit()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app()), base_url="http://test") as client:
        same = await client.post("/autopilot/book/start", json={"target_chapters": 10})
        assert same.status_code == 200 and same.json()["no_op"] is True
        larger = await client.post("/autopilot/book/start", json={"target_chapters": 11})
        assert larger.status_code == 409
        assert db.fetch_one("SELECT autopilot_run_epoch FROM novels WHERE id='book'")["autopilot_run_epoch"] == 0


@pytest.mark.asyncio
async def test_completed_book_continuation_requires_proposal_then_enters_macro_planning(db):
    db.execute("UPDATE novels SET current_stage='completed',autopilot_status='completed',target_chapters=10 WHERE id='book'")
    db.execute("INSERT INTO chapters (id,novel_id,number,title,content,status) VALUES ('c10','book',10,'第十章','正文','completed')")
    db.commit()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app()), base_url="http://test") as client:
        blocked = await client.post("/autopilot/book/start", json={"target_chapters": 12})
        assert blocked.status_code == 409

        created = await client.post(
            "/autopilot/book/continuation-proposals",
            json={"target_chapters": 12, "mode": "continue", "rationale": "补完尾声"},
        )
        assert created.status_code == 200
        proposal = created.json()
        assert proposal["status"] == "proposed"
        assert proposal["plan"]["next_stage"] == "macro_planning"
        assert proposal["plan"]["preserves_completed_chapters"] is True
        assert db.fetch_one("SELECT current_stage,target_chapters FROM novels WHERE id='book'") == {
            "current_stage": "completed", "target_chapters": 10,
        }

        approved = await client.post(
            f"/autopilot/book/continuation-proposals/{proposal['proposal_id']}/approve",
            json={},
        )
        assert approved.status_code == 200
        receipt = approved.json()
        assert receipt["accepted"] is True
        assert receipt["execution_confirmed"] is False
        command = db.fetch_one("SELECT payload_json,state FROM autopilot_commands WHERE command_id=?", (receipt["command_id"],))
        assert command["state"] == "pending"
        assert proposal["proposal_id"] in command["payload_json"]

        control.process_pending_commands(db=db)
        row = db.fetch_one("SELECT current_stage,autopilot_status,target_chapters FROM novels WHERE id='book'")
        assert dict(row) == {"current_stage": "macro_planning", "autopilot_status": "running", "target_chapters": 12}
        assert db.fetch_one("SELECT status FROM autopilot_continuation_proposals WHERE proposal_id=?", (proposal["proposal_id"],))["status"] == "macro_planning"


@pytest.mark.asyncio
async def test_continuation_proposal_rejects_stale_completed_baseline(db):
    db.execute("UPDATE novels SET current_stage='completed',autopilot_status='completed',target_chapters=10 WHERE id='book'")
    db.commit()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app()), base_url="http://test") as client:
        created = await client.post("/autopilot/book/continuation-proposals", json={"target_chapters": 12})
        assert created.status_code == 200
        proposal = created.json()
        db.execute("UPDATE novels SET target_chapters=11 WHERE id='book'")
        db.commit()
        approved = await client.post(f"/autopilot/book/continuation-proposals/{proposal['proposal_id']}/approve", json={})
        assert approved.status_code == 409
        assert db.fetch_one("SELECT status FROM autopilot_continuation_proposals WHERE proposal_id=?", (proposal["proposal_id"],))["status"] == "proposed"


@pytest.mark.asyncio
async def test_continuation_approval_commit_failure_returns_proposal_to_proposed(db):
    db.execute("UPDATE novels SET current_stage='completed',autopilot_status='completed',target_chapters=10 WHERE id='book'")
    db.commit()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app()), base_url="http://test") as client:
        created = await client.post("/autopilot/book/continuation-proposals", json={"target_chapters": 12})
        proposal = created.json()
        control.ensure_control_schema(db)
        db.execute("CREATE TRIGGER reject_continuation_command BEFORE INSERT ON autopilot_commands BEGIN SELECT RAISE(ABORT,'injected continuation failure'); END")
        db.commit()
        response = await client.post(f"/autopilot/book/continuation-proposals/{proposal['proposal_id']}/approve", json={})
        assert response.status_code == 503
        assert db.fetch_one("SELECT status FROM autopilot_continuation_proposals WHERE proposal_id=?", (proposal["proposal_id"],))["status"] == "proposed"


@pytest.mark.asyncio
async def test_commit_failure_returns_503_without_fake_running(db):
    control.ensure_control_schema(db)
    db.execute("CREATE TRIGGER reject_command BEFORE INSERT ON autopilot_commands BEGIN SELECT RAISE(ABORT,'injected failure'); END")
    db.commit()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app()), base_url="http://test") as client:
        response = await client.post("/autopilot/book/start", json={"target_chapters": 10})
        assert response.status_code == 503
        assert db.fetch_one("SELECT autopilot_status FROM novels WHERE id='book'")["autopilot_status"] == "stopped"


@pytest.mark.asyncio
async def test_timeout_unknown_can_be_followed_by_late_commit(db, monkeypatch):
    original = routes._persist_autopilot_running_sync
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        try:
            return original(*args, **kwargs)
        finally:
            finished.set()
    monkeypatch.setattr(routes, "_persist_autopilot_running_sync", delayed)
    monkeypatch.setattr(routes, "get_autopilot_runtime_settings", lambda: SimpleNamespace(db_persist_timeout_seconds=0.01))
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app()), base_url="http://test") as client:
            response = await client.post("/autopilot/book/start", json={"target_chapters": 10})
            assert response.status_code == 202
            body = response.json()
            assert body["accepted"] is None and body["execution_confirmed"] is False
            release.set()
            assert await asyncio.to_thread(finished.wait, 5)
            committed = (await client.get(f"/autopilot/book/commands/{body['command_id']}" )).json()
            assert committed["accepted"] is True and committed["command_status"] == "pending"
    finally:
        release.set()


def test_heartbeat_rejects_stale_and_future_shared_projection(monkeypatch):
    from interfaces import runtime_state
    import time
    for timestamp in [time.time() - 61, time.time() + 61, None]:
        monkeypatch.setattr(runtime_state, "_get_shared_state", lambda: {"_daemon_heartbeat": timestamp})
        assert routes._daemon_lease_status()["daemon_alive"] is False
    monkeypatch.setattr(runtime_state, "_get_shared_state", lambda: {"_daemon_heartbeat": time.time()})
    assert routes._daemon_lease_status()["daemon_alive"] is True


@pytest.mark.parametrize("running", [True, False])
def test_executor_availability_uses_owned_process_even_during_long_model_call(monkeypatch, running):
    import sys
    monkeypatch.setitem(sys.modules, "interfaces.main", SimpleNamespace(
        _daemon_manager=SimpleNamespace(status=lambda: SimpleNamespace(running=running))))
    monkeypatch.setattr(routes, "_daemon_lease_status", lambda: {"daemon_alive": False})
    assert routes._autopilot_executor_running() is running


@pytest.mark.asyncio
async def test_pending_status_explains_missing_executor_and_reservation(db, monkeypatch):
    from interfaces import runtime_state
    monkeypatch.setattr(runtime_state, "_get_shared_state", lambda: {})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app()), base_url="http://test") as client:
        started = (await client.post("/autopilot/book/start", json={"target_chapters": 10})).json()
        status = (await client.get("/autopilot/book/status")).json()
        assert status["command_id"] == started["command_id"]
        assert status["command_status"] == "pending"
        assert status["execution_confirmed"] is False
        assert status["execution_owner"] == "autopilot"
        assert status["command_wait_seconds"] >= 0
        assert "执行器" in status["control_message"]
        assert status["daemon_alive"] is False
        assert status["execution_reservation_expired"] is False


@pytest.mark.asyncio
async def test_expired_pending_status_is_explicit_and_read_only(db):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app()), base_url="http://test") as client:
        started = (await client.post("/autopilot/book/start", json={"target_chapters": 10})).json()
        db.execute("UPDATE novel_execution_leases SET expires_at=0")
        db.commit()
        status = (await client.get("/autopilot/book/status")).json()
        assert status["execution_reservation_expired"] is True
        assert status["execution_owner"] is None
        assert "过期" in status["control_message"]
        assert control.get_command(started["command_id"], db=db)["state"] == "pending"


@pytest.mark.asyncio
async def test_status_and_command_gets_never_mutate_database(db, monkeypatch):
    from infrastructure.persistence.database.write_dispatch import sql_is_mutating
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app()), base_url="http://test") as client:
        started = (await client.post("/autopilot/book/start", json={"target_chapters": 10})).json()
        original = db.execute
        reads = []

        def readonly(sql, params=()):
            assert not sql_is_mutating(sql), sql
            reads.append(sql)
            return original(sql, params)

        monkeypatch.setattr(db, "execute", readonly)
        assert (await client.get("/autopilot/book/status")).status_code == 200
        assert (await client.get(f"/autopilot/book/commands/{started['command_id']}")).status_code == 200
        assert reads
