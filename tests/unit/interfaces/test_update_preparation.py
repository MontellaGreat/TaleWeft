from types import SimpleNamespace
import asyncio
import sqlite3

import pytest

from interfaces.api.middleware.database_maintenance import DatabaseMaintenanceGate, DatabaseMaintenanceConflict
from interfaces.update_preparation import prepare_update
from scripts.generate_updater_config import make_config, validate_endpoint

PUBLIC_KEY = "untrusted comment: minisign public key: A49EF4746FA6E6D7\nRWTX5qZvdPSepDGWg2bz8n6ArQnBMwgQK1geVosorcbLzcunD+KJ+cYe\n"


@pytest.mark.parametrize("url", ["http://releases.github.com/app", "https://localhost/update", "https://127.0.0.1/a",
    "https://example.com/update", "https://updates.invalid/a", "https://user:password@github.com/update", "https://github.com/a#fragment"])
def test_release_configuration_rejects_unsafe_or_placeholder_urls(url):
    with pytest.raises(ValueError):
        validate_endpoint(url)


def test_release_configuration_is_explicit_and_requires_real_minisign_public_key():
    config = make_config(["https://github.com/tauri-apps/plugins-workspace"], PUBLIC_KEY)
    assert config["plugins"]["updater"]["requireSignedVersion"] is True
    assert config["bundle"]["createUpdaterArtifacts"] is True
    assert "windows" not in config["bundle"]  # no invented code signing identity
    for invalid in ("YOUR_PUBLIC_KEY", "untrusted comment: secret key\nAAAA", PUBLIC_KEY.replace("RWTX", "AAAA")):
        with pytest.raises(ValueError):
            make_config(["https://github.com/tauri-apps/plugins-workspace"], invalid)


def test_windows_certificate_configuration_is_validated():
    with pytest.raises(ValueError):
        make_config(["https://github.com/tauri-apps/plugins-workspace"], PUBLIC_KEY, "placeholder", "https://github.com")
    config = make_config(["https://github.com/tauri-apps/plugins-workspace"], PUBLIC_KEY, "A" * 40, "https://timestamp.digicert.com")
    assert config["bundle"]["windows"]["digestAlgorithm"] == "sha256"


@pytest.fixture
def preparation(monkeypatch, tmp_path):
    from interfaces.api.v1.engine.dag import dag_routes
    from interfaces.api.v1.engine import generation
    from application.engine.services import persistence_queue
    from infrastructure.persistence.database import connection
    from application import paths

    calls = []
    async def stop_dag(): calls.append("dag")
    async def stop_streams(): calls.append("streams")
    monkeypatch.setattr(dag_routes, "shutdown_dag_jobs", stop_dag)
    monkeypatch.setattr(generation, "shutdown_generation_stream_requests", stop_streams)
    monkeypatch.setattr(dag_routes, "_execution_manager", None)
    monkeypatch.setattr(generation, "_generation_stream_managers", {})
    queue = SimpleNamespace(wait_until_idle=lambda timeout: calls.append("barrier") or True)
    monkeypatch.setattr(persistence_queue, "get_persistence_queue", lambda: queue)
    monkeypatch.setattr(connection, "get_database", lambda: SimpleNamespace(close_all=lambda **_: calls.append("close")))
    db_path = tmp_path / "update.db"
    with sqlite3.connect(db_path) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE body (content TEXT)")
    monkeypatch.setattr(paths, "get_db_path", lambda: db_path)
    manager = SimpleNamespace(stop=lambda **kwargs: calls.append(("daemon", kwargs)), status=lambda: SimpleNamespace(running=False))
    lifecycle = SimpleNamespace(database_runtime_is_quiescent=lambda: True)
    main = SimpleNamespace(_get_daemon_manager=lambda: manager, _get_lifecycle=lambda: lifecycle,
        _stop_background_task_service=lambda: calls.append("background"), _stop_async_bridge=lambda: calls.append("async"),
        _stop_managed_resources=lambda: calls.append("resources"), _stop_persistence_consumer=lambda: calls.append("writer"),
        _stop_shared_state=lambda: calls.append("shared_state"))
    request = SimpleNamespace(scope={}, app=SimpleNamespace(state=SimpleNamespace()))
    gate = DatabaseMaintenanceGate()
    assert gate.enter(request.scope)
    return main, request, gate, calls, queue, manager, lifecycle


@pytest.mark.asyncio
async def test_update_ack_requires_committed_writes_and_keeps_admission_closed(preparation):
    main, request, gate, calls, *_ = preparation
    receipt = await prepare_update(main, request, gate)
    assert receipt["update_prepared"] and receipt["writers_drained"] and receipt["wal_checkpointed"]
    assert calls.index("barrier") < calls.index("writer") < calls.index("shared_state") < calls.index("close")
    assert ("daemon", {"stop_writer": False}) in calls
    gate.leave(request.scope)
    assert not gate.enter({})


@pytest.mark.asyncio
async def test_writer_timeout_never_acknowledges_or_seals(preparation):
    main, request, gate, calls, queue, *_ = preparation
    queue.wait_until_idle = lambda **_: False
    with pytest.raises(RuntimeError, match="提交屏障"):
        await prepare_update(main, request, gate)
    assert "close" not in calls
    assert gate.enter({})


@pytest.mark.asyncio
async def test_live_background_owner_never_acknowledges(preparation):
    main, request, gate, calls, _, _, lifecycle = preparation
    lifecycle.database_runtime_is_quiescent = lambda: False
    with pytest.raises(RuntimeError, match="仍在运行"):
        await prepare_update(main, request, gate)
    assert "close" not in calls
    assert gate.enter({})


@pytest.mark.asyncio
async def test_concurrent_save_blocks_update_preparation(preparation):
    main, request, gate, calls, *_ = preparation
    other = {}
    assert gate.enter(other)
    with pytest.raises(DatabaseMaintenanceConflict):
        await prepare_update(main, request, gate)
    assert "barrier" not in calls


@pytest.mark.asyncio
async def test_unstopped_daemon_blocks_update(preparation):
    main, request, gate, calls, _, manager, _ = preparation
    manager.status = lambda: SimpleNamespace(running=True)
    with pytest.raises(RuntimeError, match="仍在运行"):
        await prepare_update(main, request, gate)
    assert "close" not in calls


@pytest.mark.asyncio
async def test_shutdown_failure_propagates_without_success_receipt(preparation):
    main, request, gate, calls, *_ = preparation
    def failure(): raise RuntimeError("producer failed to stop")
    main._stop_background_task_service = failure
    with pytest.raises(RuntimeError, match="producer failed"):
        await prepare_update(main, request, gate)
    assert "barrier" not in calls
    assert gate.enter({})


@pytest.mark.asyncio
async def test_manager_stop_failure_cannot_ack_update_or_checkpoint(preparation):
    main, request, gate, calls, *_ = preparation
    def failure(): raise RuntimeError("共享状态后台进程停止未确认")
    main._stop_shared_state = failure
    with pytest.raises(RuntimeError, match="停止未确认"):
        await prepare_update(main, request, gate)
    assert "close" not in calls
    assert gate.enter({})


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["dag", "stream"])
async def test_task_started_during_cancellation_cannot_escape_update_barrier(preparation, monkeypatch, kind):
    main, request, gate, calls, *_ = preparation
    task = SimpleNamespace(done=lambda: False)
    if kind == "dag":
        from interfaces.api.v1.engine.dag import dag_routes
        monkeypatch.setattr(dag_routes, "_execution_manager", SimpleNamespace(tasks={"run": task}))
    else:
        from interfaces.api.v1.engine import generation
        monkeypatch.setattr(generation, "_generation_stream_managers", {"new": SimpleNamespace(tasks={"run": task})})
    with pytest.raises(RuntimeError, match="停止未确认"):
        await prepare_update(main, request, gate)
    assert "barrier" not in calls


@pytest.mark.asyncio
async def test_update_preparation_http_requires_local_session_and_propagates_refusal(monkeypatch):
    import httpx
    from interfaces import main, update_preparation
    from interfaces.api.v1 import system
    monkeypatch.setattr(system, "database_maintenance_gate", DatabaseMaintenanceGate())
    started = []
    monkeypatch.setattr(main, "_internal_shutdown_after_response", lambda: started.append("exit"))
    calls = []
    async def prepare(*args):
        calls.append("prepare")
        raise RuntimeError("writer ACK not received")
    monkeypatch.setattr(update_preparation, "prepare_update", prepare)
    app = main.create_app()
    headers = {"Authorization": "Bearer " + app.state.session_authority.token}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 3333)), base_url="http://127.0.0.1") as client:
        assert (await client.post("/internal/prepare-update")).status_code == 401
        refused = await client.post("/internal/prepare-update", headers=headers)
        assert refused.status_code == 409
        assert "writer ACK" in refused.text
    assert calls == ["prepare"] and started == []
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("203.0.113.10", 3333)), base_url="http://127.0.0.1") as client:
        assert (await client.post("/internal/prepare-update", headers=headers)).status_code == 403
    assert calls == ["prepare"]
