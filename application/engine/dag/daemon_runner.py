"""Compatibility facade over the shared production DAG runtime."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict, Optional

from application.engine.dag.models import DAGDefinition
from application.engine.dag.runtime import DAGExecutionManager, DAGRuntimeConflict
from application.engine.dag.version_manager import DAGVersionManager
from application.engine.services.execution_lease import get_execution_lease
from infrastructure.engine.dag_environment import DAGEnvironmentSettings


class DAGDaemonRunner:
    """All entry points use the same durable jobs, ownership and chapter gates."""

    def __init__(self, streaming_bus=None, state_publisher=None,
                 persistence_queue=None, circuit_breaker=None,
                 data_root: Optional[str] = None, *, db=None, llm_service=None,
                 services_factory=None):
        if db is None:
            from infrastructure.persistence.database.connection import get_database
            db = get_database(str(Path(data_root) / 'plotpilot.db') if data_root else None)
        if llm_service is None:
            from interfaces.api.dependencies import get_llm_service
            llm_service = get_llm_service()
        self._bus, self._publisher, self._breaker = streaming_bus, state_publisher, circuit_breaker
        self._runtime = DAGExecutionManager(db, llm_service, publish=self._publish,
                                            services_factory=services_factory)
        self._version_mgr = DAGVersionManager(repository=self._runtime.versions)
        self._running_tasks = self._runtime.tasks

    def _publish(self, novel_id, event):
        if self._bus:
            self._bus.publish_node_event(novel_id, event)
        if self._publisher and event.get('type') == 'dag_run_complete':
            self._publisher.update_novel_state(novel_id, dag_run_id=event['dag_run_id'],
                                              dag_status=event['status'])

    async def run_novel(self, novel_id: str, dag: Optional[DAGDefinition] = None,
                        *, chapter_number=None):
        if dag is not None:
            async with self._runtime._lock:
                if await asyncio.to_thread(get_execution_lease, novel_id, self._runtime.db):
                    raise DAGRuntimeConflict('停止并确认当前任务退出后再替换 DAG')
                errors = self._runtime.validate_production(dag)
                if errors:
                    raise ValueError('; '.join(errors))
                await asyncio.to_thread(self._runtime.versions.save, novel_id, dag)
        await self._runtime.start(novel_id, chapter_number)
        await self._running_tasks[novel_id]
        status = await self._runtime.status(novel_id)
        if self._breaker:
            if status['status'] == 'error':
                self._breaker.record_failure()
            elif status['status'] in {'completed', 'awaiting_review'}:
                self._breaker.record_success()
        return status

    async def resume_novel(self, novel_id: str, *, run_id=None):
        if run_id is None:
            previous = await asyncio.to_thread(self._runtime.store.latest, novel_id)
            if not previous:
                raise ValueError('没有可恢复的 DAG 任务')
            run_id = previous['id']
        await self._runtime.start(novel_id, resume_run_id=run_id)
        await self._running_tasks[novel_id]
        return await self._runtime.status(novel_id)

    async def stop_novel(self, novel_id: str, *, run_id=None):
        return await self._runtime.stop(novel_id, run_id)

    async def _build_initial_state(self, novel_id: str):
        _chapter, initial = await asyncio.to_thread(self._runtime._chapter, novel_id)
        return initial

    async def toggle_node(self, novel_id: str, node_id: str):
        return await self._runtime.configure(novel_id, node_id, toggle=True)

    async def update_node_config(self, novel_id: str, node_id: str, config: Dict[str, Any]):
        return await self._runtime.configure(novel_id, node_id, patch=config)

    async def shutdown(self):
        await self._runtime.shutdown()


class EngineSelector:
    """引擎选择器 -- 根据 Feature Flag 决定使用旧守护进程或 DAG 引擎"""

    def __init__(self, dag_enabled: Optional[bool] = None):
        if dag_enabled is None:
            dag_enabled = DAGEnvironmentSettings.from_env().enabled
        self.dag_enabled = dag_enabled
        self._novel_flags: Dict[str, bool] = {}

    def should_use_dag(self, novel_id: str) -> bool:
        if novel_id in self._novel_flags:
            return self._novel_flags[novel_id]
        return self.dag_enabled

    def set_novel_flag(self, novel_id: str, use_dag: bool):
        self._novel_flags[novel_id] = use_dag
