"""Strict update shutdown barrier, separate from best-effort window closing."""
from __future__ import annotations

import asyncio
import sqlite3


async def prepare_update(main, request, gate):
    from interfaces.api.v1.engine.dag.dag_routes import shutdown_dag_jobs
    from interfaces.api.v1.engine.generation import shutdown_generation_stream_requests
    from application.engine.services.persistence_queue import get_persistence_queue
    from infrastructure.persistence.database.connection import get_database
    from application.paths import get_db_path

    # Cancel producers first. Concurrent HTTP writes still cause exclusive()
    # to reject; no ACK is returned until all request owners have left.
    await shutdown_dag_jobs()
    await shutdown_generation_stream_requests()
    recovery = getattr(request.app.state, "chapter_derivation_recovery_task", None)
    if recovery is not None and not recovery.done():
        recovery.cancel()
        await asyncio.gather(recovery, return_exceptions=True)
    with gate.exclusive(request.scope):
        from interfaces.api.v1.engine.dag import dag_routes
        from interfaces.api.v1.engine import generation
        dag_manager = dag_routes._execution_manager
        if dag_manager is not None and any(not task.done() for task in dag_manager.tasks.values()):
            raise RuntimeError("DAG 任务停止未确认，未允许安装更新")
        if any(not task.done() for stream_manager in generation._generation_stream_managers.values()
               for task in stream_manager.tasks.values()):
            raise RuntimeError("生成任务停止未确认，未允许安装更新")
        manager = main._get_daemon_manager()
        lifecycle = main._get_lifecycle()

        def drain():
            manager.stop(stop_writer=False)
            main._stop_background_task_service()
            main._stop_async_bridge()
            main._stop_managed_resources()
            if not get_persistence_queue().wait_until_idle(timeout=30):
                raise RuntimeError("持久化提交屏障未确认，未允许安装更新")
            main._stop_persistence_consumer()
            main._stop_shared_state()
            if manager.status().running or not lifecycle.database_runtime_is_quiescent():
                raise RuntimeError("后台任务仍在运行，未允许安装更新")
            get_database().close_all(skip_checkpoint=True)
            with sqlite3.connect(get_db_path(), timeout=5) as connection:
                result = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if result is None or result[0] != 0 or result[1] != result[2]:
                    raise RuntimeError("数据库 WAL 未完成落盘，未允许安装更新")

        await asyncio.to_thread(drain)
        # This survives the context's exit and blocks new writes until exit.
        gate.seal(request.scope)
    return {"ok": True, "update_prepared": True, "writers_drained": True,
            "background_stopped": True, "wal_checkpointed": True}
