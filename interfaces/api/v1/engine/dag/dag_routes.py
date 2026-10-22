"""Production chapter DAG control, immutable run evidence and real node events."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from application.engine.dag.models import (
    DAGDefinition,
    NodeConfig,
    NodeDefinition,
    NodeMeta,
    NodeRunState,
    NodeStatus,
    get_default_dag,
)
from application.engine.dag.registry import NodeRegistry
from application.engine.dag.runtime import DAGExecutionManager, DAGRuntimeConflict
from application.engine.narrative_projection.dag_runtime_projection import (
    node_states_to_sse_events,
    project_node_states,
    snapshot_from_shared,
)
from application.engine.narrative_projection.linkage_kernel import linkage_bundle
from interfaces.api.v1.engine.dag.dag_runtime_settings import get_dag_runtime_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dag", tags=["DAG 工作流"])

# ─── 全局单例 ───

# SSE 事件订阅者管理
_sse_subscribers: Dict[str, List[asyncio.Queue]] = {}  # novel_id -> [Queue]

# ★ DAG 定义内存缓存（暂时不走数据库）
_dag_cache: "OrderedDict[str, DAGDefinition]" = OrderedDict()


def _evict_dag_cache_overflow() -> None:
    max_size = get_dag_runtime_settings().dag_cache_max_size
    while len(_dag_cache) > max_size:
        _dag_cache.popitem(last=False)


def _get_dag_for_novel(novel_id: str) -> DAGDefinition:
    """获取或初始化小说的 DAG 定义（内存缓存，不走数据库）"""
    dag = _dag_cache.get(novel_id)
    if dag is not None:
        _dag_cache.move_to_end(novel_id)
        return dag

    dag = get_default_dag()
    _dag_cache[novel_id] = dag
    _evict_dag_cache_overflow()
    return dag


def publish_sse_event(novel_id: str, event_data: dict):
    """向指定小说的 SSE 订阅者推送事件"""
    subscribers = _sse_subscribers.get(novel_id, [])
    dead_queues = []
    for queue in subscribers:
        try:
            queue.put_nowait(event_data)
        except asyncio.QueueFull:
            dead_queues.append(queue)
    # 清理满队列
    for q in dead_queues:
        subscribers.remove(q)


# ─── Request/Response Models ───


class DAGStatusResponse(BaseModel):
    """DAG 运行状态响应"""
    novel_id: str
    dag_enabled: bool
    current_version: int
    node_states: Dict[str, Dict[str, Any]]
    execution_mode: str = "autopilot_projection"
    status: str = "idle"
    dag_run_id: Optional[str] = None
    chapter_number: Optional[int] = None
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    capabilities: Dict[str, Any] = Field(default_factory=dict)


_execution_manager: Optional[DAGExecutionManager] = None


def get_execution_manager() -> DAGExecutionManager:
    global _execution_manager
    if _execution_manager is None:
        from infrastructure.persistence.database.connection import get_database
        from interfaces.api.dependencies import get_llm_service
        _execution_manager = DAGExecutionManager(get_database(), get_llm_service(), publish=publish_sse_event)
    return _execution_manager


async def shutdown_dag_jobs():
    if _execution_manager is not None:
        await _execution_manager.shutdown()


def _execution_capabilities() -> Dict[str, Any]:
    return {"can_run": True, "can_stop": True, "can_configure": True,
            "can_checkpoint": False, "can_rewrite": True, "can_resume": True,
            "reason": "真实单章 DAG：生成→去 AI 味审计→有界重写→重新审计→提交确认→章后分析。"}


def _runtime_http_error(exc):
    if isinstance(exc, LookupError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, DAGRuntimeConflict):
        return HTTPException(status_code=409, detail={"code": "DAG_RUNTIME_CONFLICT", "reason": str(exc)})
    if isinstance(exc, ValueError):
        return HTTPException(status_code=422, detail=str(exc))
    logger.exception("DAG 操作未成功提交")
    return HTTPException(status_code=503, detail={"code": "DAG_COMMIT_UNCONFIRMED", "reason": str(exc)})


async def _load_runtime_dag(novel_id):
    try:
        dag = await asyncio.to_thread(get_execution_manager().load_dag, novel_id)
        _dag_cache[novel_id] = dag
        _dag_cache.move_to_end(novel_id)
        _evict_dag_cache_overflow()
        return dag
    except Exception as exc:
        raise _runtime_http_error(exc) from exc


# ═══════════════════════════════════════════════════════════════
# 静态路由 — 必须在 /{novel_id} 参数化路由之前定义
# ═══════════════════════════════════════════════════════════════


# ─── 健康检查 ───


@router.get("/health/dag")
async def dag_health_check():
    """DAG 引擎健康检查"""
    checks = {}
    checks["execution"] = _execution_capabilities()

    # 节点注册表
    checks["node_registry"] = {
        "registered_types": len(NodeRegistry.all_types()),
        "types": sorted(NodeRegistry.all_types()),
    }

    # SSE 订阅者统计
    total_subscribers = sum(len(qs) for qs in _sse_subscribers.values())
    checks["sse"] = {
        "active_novels": len(_sse_subscribers),
        "total_subscribers": total_subscribers,
    }

    overall = "ok" if all(
        c.get("status") != "error" for c in checks.values()
    ) else "degraded"

    return {"status": overall, "checks": checks}


# ─── 节点类型注册表 ───


@router.get("/registry/types")
async def list_node_types():
    """获取所有已注册的节点类型"""
    metas = NodeRegistry.all_meta()
    return {
        "types": {
            node_type: meta.model_dump(mode="json")
            for node_type, meta in metas.items()
        }
    }


@router.get("/registry/types/{node_type}")
async def get_node_type_meta(node_type: str):
    """获取单个节点类型的元数据"""
    try:
        meta = NodeRegistry.get_meta(node_type)
        return meta.model_dump(mode="json")
    except KeyError:
        raise HTTPException(status_code=404, detail=f"节点类型 '{node_type}' 未注册")


@router.get("/registry/linkage")
async def get_dag_registry_linkage():
    """DAG 默认画布与 CPMS 一一对应表 + 全类型 CPMS 索引（单一联动内核导出）。"""
    return linkage_bundle()


# ─── SSE 事件流 ───


@router.get("/events")
async def dag_event_stream(request: Request, novel_id: str = Query(..., description="小说 ID")):
    """SSE 事件流 — 前端实时接收节点状态变更"""
    runtime_settings = get_dag_runtime_settings()
    queue: asyncio.Queue = asyncio.Queue(maxsize=runtime_settings.sse_queue_size)

    # 注册订阅者
    if novel_id not in _sse_subscribers:
        _sse_subscribers[novel_id] = []
    _sse_subscribers[novel_id].append(queue)

    async def event_generator():
        try:
            from interfaces.runtime_state import get_shared_novel_state

            # 发送初始连接确认
            yield f"event: connected\ndata: {json.dumps({'novel_id': novel_id, 'timestamp': time.time()})}\n\n"

            manager = get_execution_manager()
            actual = await manager.status(novel_id)
            if actual:
                yield f"event: dag_run_state\ndata: {json.dumps(actual, ensure_ascii=False)}\n\n"
                last = request.headers.get("Last-Event-ID", "")
                after = 0
                if last.startswith(actual["dag_run_id"] + ":"):
                    try:
                        after = int(last.rsplit(":", 1)[-1])
                    except ValueError:
                        pass
                for ev in await asyncio.to_thread(manager.store.events, actual["dag_run_id"], after):
                    event_id = f"{actual['dag_run_id']}:{ev['sequence']}"
                    yield f"id: {event_id}\nevent: {ev.get('type','node_status_change')}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n"

            prev_proj: Dict[str, Dict[str, Any]] = {}
            projection_bootstrapped = False
            idle_ticks = 0

            while True:
                try:
                    event_data = await asyncio.wait_for(
                        queue.get(),
                        timeout=runtime_settings.sse_idle_poll_seconds,
                    )
                    idle_ticks = 0
                    event_type = event_data.get("type", "message")
                    event_id = f"{event_data.get('dag_run_id', '')}:{event_data.get('sequence', 0)}"
                    yield f"id: {event_id}\nevent: {event_type}\ndata: {json.dumps(event_data, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    idle_ticks += 1
                    event_data = None

                if await manager.status(novel_id):
                    if idle_ticks >= runtime_settings.sse_heartbeat_every_idle_ticks:
                        yield f"event: heartbeat\ndata: {json.dumps({'timestamp': time.time()})}\n\n"
                        idle_ticks = 0
                    continue
                dag = _get_dag_for_novel(novel_id)
                shared = get_shared_novel_state(novel_id)
                snap = snapshot_from_shared(novel_id, shared)
                node_ids = [(n.id, n.type, n.enabled) for n in dag.nodes]
                new_proj = project_node_states(node_ids, snap)
                if not projection_bootstrapped:
                    prev_proj = new_proj
                    projection_bootstrapped = True
                else:
                    for ev in node_states_to_sse_events(novel_id, prev_proj, new_proj):
                        et = ev.get("type", "node_status_change")
                        yield f"event: {et}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n"
                    prev_proj = new_proj
                if idle_ticks >= runtime_settings.sse_heartbeat_every_idle_ticks:
                    yield f"event: heartbeat\ndata: {json.dumps({'timestamp': time.time()})}\n\n"
                    idle_ticks = 0
        except asyncio.CancelledError:
            pass
        finally:
            if novel_id in _sse_subscribers:
                try:
                    _sse_subscribers[novel_id].remove(queue)
                    if not _sse_subscribers[novel_id]:
                        del _sse_subscribers[novel_id]
                except ValueError:
                    pass

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ═══════════════════════════════════════════════════════════════
# 参数化路由 — /{novel_id}
# ═══════════════════════════════════════════════════════════════


# ─── DAG 定义（只读） ───


@router.get("/{novel_id}")
async def get_dag(novel_id: str):
    """获取当前 DAG 定义（只读展示）"""
    dag = await _load_runtime_dag(novel_id)
    return dag.model_dump(mode="json")


# ─── 节点详情（只读） ───


@router.get("/{novel_id}/nodes/{node_id}")
async def get_node(novel_id: str, node_id: str):
    """获取节点详情（只读展示）"""
    dag = await _load_runtime_dag(novel_id)

    node = dag.get_node(node_id)
    if not node:
        raise HTTPException(status_code=404, detail=f"节点 '{node_id}' 不存在")

    # 附加节点元数据
    result = node.model_dump(mode="json")
    try:
        meta = NodeRegistry.get_meta(node.type)
        result["meta"] = meta.model_dump(mode="json")
    except KeyError:
        result["meta"] = None

    return result


# ─── 节点启禁用（唯一写操作） ───


@router.post("/{novel_id}/nodes/{node_id}/toggle")
async def toggle_node(novel_id: str, node_id: str):
    try:
        dag = await get_execution_manager().configure(novel_id, node_id, toggle=True)
        _dag_cache[novel_id] = dag
        return dag.model_dump(mode="json")
    except Exception as exc:
        raise _runtime_http_error(exc) from exc


# ─── 运行状态（只读） ───


@router.get("/{novel_id}/status")
async def get_dag_status(novel_id: str):
    """获取运行状态（含所有节点状态）— 由全托管共享状态投影，与 DAG 定义节点 id 对齐。"""
    from interfaces.runtime_state import get_shared_novel_state

    dag = await _load_runtime_dag(novel_id)
    actual = await get_execution_manager().status(novel_id)
    if actual:
        return DAGStatusResponse(novel_id=novel_id, dag_enabled=True, current_version=dag.version,
            node_states=actual["node_states"], execution_mode="dag_runner", capabilities=_execution_capabilities(),
            status=actual["status"], dag_run_id=actual["dag_run_id"], chapter_number=actual["chapter_number"],
            result=actual.get("result"), error=actual.get("error"))
    shared = get_shared_novel_state(novel_id)
    snap = snapshot_from_shared(novel_id, shared)
    node_ids = [(n.id, n.type, n.enabled) for n in dag.nodes]
    states = project_node_states(node_ids, snap)

    return DAGStatusResponse(
        novel_id=novel_id,
        dag_enabled=False,
        current_version=dag.version,
        node_states=states,
        capabilities=_execution_capabilities(),
    )


# ─── 提示词来源（只读） ───


@router.get("/{novel_id}/nodes/{node_id}/prompt-live")
async def get_node_prompt_live(novel_id: str, node_id: str):
    """获取节点当前的实时提示词"""
    dag = await _load_runtime_dag(novel_id)

    node_def = next((n for n in dag.nodes if n.id == node_id), None)
    if not node_def:
        raise HTTPException(status_code=404, detail=f"节点 {node_id} 不存在")

    try:
        base_node = NodeRegistry.create_instance(node_def.type, config=node_def.config)
        prompt_dict = base_node.get_effective_prompt()

        cpms_node_key = ""
        if base_node.meta and base_node.meta.cpms_node_key:
            cpms_node_key = base_node.meta.cpms_node_key

        # 收集 CPMS 子注入点信息
        cpms_sub_keys = []
        if base_node.meta and base_node.meta.cpms_sub_keys:
            cpms_sub_keys = [
                {
                    "cpms_node_key": inj.cpms_node_key,
                    "target_variable": inj.target_variable,
                    "description": inj.description,
                    "required": inj.required,
                }
                for inj in base_node.meta.cpms_sub_keys
            ]

        prompt_mode = ""
        if base_node.meta and base_node.meta.prompt_mode:
            prompt_mode = base_node.meta.prompt_mode.value

        return {
            "node_id": node_id,
            "system": prompt_dict["system"],
            "user_template": prompt_dict["user_template"],
            "source": prompt_dict["source"],
            "cpms_node_key": cpms_node_key,
            "cpms_sub_keys": cpms_sub_keys,
            "prompt_mode": prompt_mode,
        }
    except KeyError:
        raise HTTPException(status_code=404, detail=f"节点类型 {node_def.type} 未注册")


# ─── 渲染后 Prompt（只读预览） ───


@router.get("/{novel_id}/nodes/{node_id}/prompt")
async def get_rendered_prompt(novel_id: str, node_id: str):
    """获取渲染后的 Prompt（预览）"""
    dag = await _load_runtime_dag(novel_id)

    node = dag.get_node(node_id)
    if not node:
        raise HTTPException(status_code=404, detail=f"节点 '{node_id}' 不存在")

    template = node.config.prompt_template or ""
    variables = node.config.prompt_variables or {}

    # 渲染模板
    rendered = template
    for key, value in variables.items():
        rendered = rendered.replace(f"{{{{{key}}}}}", str(value)).replace(f"{{{key}}}", str(value))

    return {
        "node_id": node_id,
        "template": template,
        "variables": variables,
        "rendered": rendered,
    }


# ─── 节点配置更新（nodeEditorStore 使用） ───


class UpdateNodeConfigRequest(BaseModel):
    """更新节点配置请求"""
    prompt_template: Optional[str] = None
    prompt_variables: Optional[Dict[str, str]] = None
    thresholds: Optional[Dict[str, float]] = None
    model_override: Optional[str] = None
    max_retries: Optional[int] = Field(default=None, ge=0, le=5)
    timeout_seconds: Optional[int] = Field(default=None, ge=10, le=600)
    temperature: Optional[float] = Field(default=None, ge=0.0, le=2.0)
    max_tokens: Optional[int] = Field(default=None, ge=100)


@router.put("/{novel_id}/nodes/{node_id}")
async def update_node_config(novel_id: str, node_id: str, request: UpdateNodeConfigRequest):
    try:
        dag = await get_execution_manager().configure(novel_id, node_id, patch=request.model_dump(exclude_unset=True))
        _dag_cache[novel_id] = dag
        return dag.model_dump(mode="json")
    except Exception as exc:
        raise _runtime_http_error(exc) from exc


class RunDAGRequest(BaseModel):
    chapter_number: Optional[int] = Field(default=None, ge=1)
    resume_run_id: Optional[str] = None


class StopDAGRequest(BaseModel):
    dag_run_id: Optional[str] = None


@router.post("/{novel_id}/run")
async def run_dag(novel_id: str, request: Optional[RunDAGRequest] = None):
    try:
        request = request or RunDAGRequest()
        return await get_execution_manager().start(novel_id, request.chapter_number, resume_run_id=request.resume_run_id)
    except Exception as exc:
        raise _runtime_http_error(exc) from exc


@router.post("/{novel_id}/stop")
async def stop_dag(novel_id: str, request: Optional[StopDAGRequest] = None):
    try:
        return await get_execution_manager().stop(novel_id, request.dag_run_id if request else None)
    except Exception as exc:
        raise _runtime_http_error(exc) from exc


@router.get("/{novel_id}/runs")
async def list_dag_runs(novel_id: str):
    return {"runs": await asyncio.to_thread(get_execution_manager().store.history, novel_id)}


@router.get("/{novel_id}/runs/{run_id}")
async def get_dag_run(novel_id: str, run_id: str):
    manager = get_execution_manager()
    job = await asyncio.to_thread(manager.store.get, novel_id, run_id)
    if job is None:
        raise HTTPException(status_code=404, detail="运行记录不存在")
    return {"dag_run_id": job["id"], "novel_id": novel_id, "status": job["status"],
            "chapter_number": job["chapter_number"], "graph": json.loads(job["graph_json"]),
            "result": json.loads(job["result_json"]) if job["result_json"] else None,
            "events": await asyncio.to_thread(manager.store.events, run_id)}
