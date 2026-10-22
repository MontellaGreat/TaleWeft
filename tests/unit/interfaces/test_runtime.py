# Modified for TaleWeft on 2026-10-22: independent branding; upstream attribution retained.
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from application.core.config.config_loader import reload_config
from interfaces import runtime_state
from interfaces.runtime import AppRuntime, BackendLifecycle, get_backend_lifecycle_settings


def test_app_runtime_shared_novel_state_roundtrip():
    runtime = AppRuntime()
    runtime._shared_state = {}

    runtime.update_shared_novel_state("novel-1", stage="writing", progress=3)

    state = runtime.get_shared_novel_state("novel-1")
    assert state["novel_id"] == "novel-1"
    assert state["stage"] == "writing"
    assert state["progress"] == 3
    assert "_updated_at" in state


def test_app_runtime_missing_state_returns_empty_dict():
    runtime = AppRuntime()
    runtime._shared_state = {}

    assert runtime.get_shared_novel_state("missing") == {}


def test_app_runtime_shutdown_waits_for_real_owned_manager_process():
    runtime = AppRuntime()
    runtime.update_shared_novel_state("novel", stage="writing")
    manager_process = runtime._mp_manager._process
    assert manager_process.is_alive()
    try:
        runtime.shutdown()
        assert not manager_process.is_alive()
        assert runtime._mp_manager is None and runtime._shared_state is None
        runtime.shutdown()  # repeated shutdown cannot spawn another child
        with pytest.raises(RuntimeError, match="已关闭"):
            runtime.get_shared_state()
    finally:
        if manager_process.is_alive():
            manager_process.terminate()
            manager_process.join(timeout=5)


def test_app_runtime_shutdown_does_not_initialize_unused_manager(monkeypatch):
    monkeypatch.setattr("interfaces.runtime.multiprocessing.Manager", lambda: pytest.fail("must not spawn during shutdown"))
    runtime = AppRuntime()
    runtime.shutdown()
    assert runtime._mp_manager is None


def test_app_runtime_shutdown_retains_unstopped_manager_for_retry():
    alive = [True]
    manager = SimpleNamespace(shutdown=lambda: None,
        _process=SimpleNamespace(is_alive=lambda: alive[0], pid=42))
    runtime = AppRuntime()
    runtime._mp_manager, runtime._shared_state = manager, {}
    with pytest.raises(RuntimeError, match="停止未确认"):
        runtime.shutdown()
    assert runtime._mp_manager is manager
    with pytest.raises(RuntimeError, match="已关闭"):
        runtime.get_shared_state()
    alive[0] = False
    runtime.shutdown()
    assert runtime._mp_manager is None


def test_runtime_state_module_is_canonical_shared_state_accessor(monkeypatch):
    runtime = AppRuntime()
    runtime._shared_state = {}
    monkeypatch.setattr(runtime_state, "_runtime", runtime)

    runtime_state.update_shared_novel_state("novel-2", stage="auditing")

    assert runtime_state.get_shared_novel_state("novel-2")["stage"] == "auditing"


def test_main_reexports_runtime_state_accessors():
    import interfaces.main as main

    assert main.get_shared_novel_state is runtime_state.get_shared_novel_state
    assert main.update_shared_novel_state is runtime_state.update_shared_novel_state
    assert main._get_shared_state is runtime_state._get_shared_state


def test_backend_lifecycle_startup_orchestrates_runtime_steps(monkeypatch):
    calls = []
    lifecycle = BackendLifecycle(
        start_daemon=lambda: calls.append("start_daemon"),
        stop_daemon=lambda: calls.append("stop_daemon"),
        cleanup_orphans=lambda: calls.append("cleanup_orphans"),
    )
    monkeypatch.setattr("interfaces.runtime.os.name", "nt")
    monkeypatch.setattr(
        "infrastructure.persistence.database.write_dispatch.startup_sqlite_writes_bypass_queue",
        lambda: nullcontext(),
    )
    monkeypatch.setattr(lifecycle, "stop_all_running_novels", lambda: calls.append("stop_running"))
    monkeypatch.setattr(lifecycle, "bootstrap_persistence_consumer", lambda: calls.append("persistence"))
    monkeypatch.setattr(lifecycle, "recover_drafts", lambda: calls.append("recover_drafts"))
    monkeypatch.setattr(lifecycle, "init_dag_node_registry", lambda: calls.append("dag_registry"))

    lifecycle.startup(registered_route_count=3)

    assert calls == [
        "cleanup_orphans",
        "stop_running",
        "persistence",
        "recover_drafts",
        "start_daemon",
        "dag_registry",
    ]


def test_backend_lifecycle_shutdown_orchestrates_cleanup(monkeypatch):
    calls = []
    lifecycle = BackendLifecycle(
        start_daemon=lambda: calls.append("start_daemon"),
        stop_daemon=lambda: calls.append("stop_daemon"),
        stop_async_bridge=lambda: calls.append("async_bridge"),
        stop_background_tasks=lambda: calls.append("background_tasks"),
        stop_persistence_consumer=lambda: calls.append("persistence_consumer"),
        stop_managed_resources=lambda: calls.append("managed_resources"),
        stop_shared_state=lambda: calls.append("shared_state"),
        start_force_exit_watchdog=lambda: calls.append("watchdog"),
    )
    monkeypatch.setattr(lifecycle, "close_database", lambda skip_checkpoint: calls.append(f"db:{skip_checkpoint}"))
    monkeypatch.setattr(lifecycle, "checkpoint_sqlite_wal_safe", lambda: calls.append("wal"))
    monkeypatch.setattr(lifecycle, "close_llm_service", lambda: calls.append("llm"))
    monkeypatch.setattr(lifecycle, "log_stopped", lambda title: calls.append(title))

    lifecycle.shutdown()

    assert calls == [
        "watchdog",
        "stop_daemon",
        "background_tasks",
        "async_bridge",
        "managed_resources",
        "persistence_consumer",
        "shared_state",
        "db:True",
        "wal",
        "llm",
        "TaleWeft service stopped",
    ]


def test_windows_shutdown_cannot_exit_zero_before_manager_stop_confirmation(monkeypatch):
    calls = []
    def fail_manager():
        calls.append("shared_state")
        raise RuntimeError("共享状态后台进程停止未确认")
    lifecycle = BackendLifecycle(start_daemon=lambda: None, stop_daemon=lambda: calls.append("daemon"),
        stop_shared_state=fail_manager)
    monkeypatch.setattr(lifecycle, "close_database", lambda **_: calls.append("database"))
    monkeypatch.setattr(lifecycle, "log_stopped", lambda *_: calls.append("stopped"))
    monkeypatch.setattr("interfaces.runtime.os._exit", lambda code: calls.append(("exit", code)))
    with pytest.raises(RuntimeError, match="停止未确认"):
        lifecycle.windows_forced_shutdown()
    assert calls == ["daemon", "shared_state"]


def test_shutdown_background_task_service_only_stops_cached_instance(monkeypatch):
    from interfaces.api import dependencies

    calls = []

    class FakeProvider:
        def __init__(self, currsize):
            self.currsize = currsize
            self.service = SimpleNamespace(shutdown=lambda: calls.append("shutdown"))

        def __call__(self):
            calls.append("create_or_fetch")
            return self.service

        def cache_info(self):
            return SimpleNamespace(currsize=self.currsize)

        def cache_clear(self):
            calls.append("cache_clear")

    monkeypatch.setattr(dependencies, "get_background_task_service", FakeProvider(currsize=0))
    dependencies.shutdown_background_task_service_if_initialized()
    assert calls == []

    monkeypatch.setattr(dependencies, "get_background_task_service", FakeProvider(currsize=1))
    dependencies.shutdown_background_task_service_if_initialized()
    assert calls == ["create_or_fetch", "shutdown", "cache_clear"]


def test_shutdown_persistence_queue_only_stops_existing_queue(monkeypatch):
    from application.engine.services import persistence_queue

    calls = []
    monkeypatch.setattr(persistence_queue, "_persistence_queue", None)
    persistence_queue.shutdown_persistence_queue_if_initialized()
    assert calls == []

    fake_queue = SimpleNamespace(stop_consumer=lambda: calls.append("stop_consumer"))
    monkeypatch.setattr(persistence_queue, "_persistence_queue", fake_queue)
    persistence_queue.shutdown_persistence_queue_if_initialized()
    assert calls == ["stop_consumer"]


def test_backend_lifecycle_settings_follow_performance_config(tmp_path):
    config_path = tmp_path / "performance.yaml"
    config_path.write_text(
        """
backend:
  lifecycle:
    startup_reset_max_retries: 5
    startup_reset_retry_backoff_seconds: 0.25
    shutdown_response_delay_seconds: 0.1
    force_exit_timeout_seconds: 3.5
    force_exit_watchdog_poll_seconds: 0.2
""",
        encoding="utf-8",
    )

    try:
        reload_config(str(config_path))
        settings = get_backend_lifecycle_settings()

        assert settings.startup_reset_max_retries == 5
        assert settings.startup_reset_retry_backoff_seconds == pytest.approx(0.25)
        assert settings.shutdown_response_delay_seconds == pytest.approx(0.1)
        assert settings.force_exit_timeout_seconds == pytest.approx(3.5)
        assert settings.force_exit_watchdog_poll_seconds == pytest.approx(0.2)
    finally:
        reload_config()
