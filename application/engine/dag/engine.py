"""Validated acyclic execution with explicit per-node outcomes.

The optional LangGraph path previously used a different scheduling/data contract.
Until checkpoint and bounded rewrite support share this contract, installing an
optional package must not change execution correctness.
"""
from __future__ import annotations

import asyncio
import inspect
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Set

from application.engine.dag.models import (
    DAGDefinition, DAGRunResult, EdgeCondition, NodeDefinition, NodeResult, NodeStatus,
)
from application.engine.dag.registry import NodeRegistry
from application.engine.dag.validator import DAGValidator
from application.engine.dag.execution_evidence import data_hash, did_execute, now, receipt_for, unexecuted_result


class DAGExecutionError(Exception):
    """Invalid or unsupported execution contract."""


class DAGEngine:
    def __init__(self, checkpointer=None):
        self._checkpointer = checkpointer
        self._use_langgraph = False

    async def run(self, dag: DAGDefinition, initial_state: Dict[str, Any], thread_id: str = "") -> DAGRunResult:
        start = time.monotonic()
        started_at = datetime.now(timezone.utc).isoformat()
        initial_state = {**initial_state, "dag_run_id": str(initial_state.get("dag_run_id") or f"run_{time.time_ns()}")}
        results: Dict[str, NodeResult] = {
            n.id: unexecuted_result(NodeResult(status=NodeStatus.PENDING if n.enabled else NodeStatus.DISABLED,
                error=None if n.enabled else "DAG 配置已禁用节点"), n, initial_state,
                "等待调度" if n.enabled else "DAG 配置已禁用节点")
            for n in dag.nodes
        }
        errors = self.validate(dag)
        if self._checkpointer is not None:
            errors.append("DAG checkpoint/resume execution is unavailable in this runner")
        if errors:
            for node in dag.nodes:
                if node.enabled:
                    results[node.id] = unexecuted_result(NodeResult(status=NodeStatus.ERROR, error="; ".join(errors)), node, initial_state, "; ".join(errors))
            status = "error"
        else:
            try:
                final_state = await self._run_with_topological_sort(dag, initial_state, results)
                status = "error" if any(r.status == NodeStatus.ERROR for r in results.values()) else "completed"
                if status == "completed" and any(
                    r.outputs.get("breaker_status") == "open" or r.outputs.get("approved") is False
                    for r in results.values()
                ):
                    status = "interrupted"
            except asyncio.CancelledError:
                # Cancellation remains observable by the task owner; gather also
                # cancels and awaits in-flight child tasks.
                raise
            except Exception as exc:
                for node_id, result in results.items():
                    if result.status == NodeStatus.PENDING:
                        results[node_id] = unexecuted_result(NodeResult(status=NodeStatus.ERROR, error=str(exc)), dag.get_node(node_id), initial_state, str(exc))
                status = "error"
        return DAGRunResult(
            dag_run_id=str(initial_state.get("dag_run_id") or f"run_{time.time_ns()}"),
            novel_id=str(initial_state.get("novel_id", "")), status=status,
            node_results=results, error_count=sum(r.status == NodeStatus.ERROR for r in results.values()),
            total_duration_ms=int((time.monotonic() - start) * 1000),
            started_at=started_at, completed_at=datetime.now(timezone.utc).isoformat(),
            final_state={k: v for k, v in locals().get("final_state", {}).items() if k not in {"llm_service", "services", "on_node_event"}},
        )

    async def run_from_node(self, dag: DAGDefinition, node_id: str, state: Dict[str, Any], thread_id: str = "") -> DAGRunResult:
        if dag.get_node(node_id) is None:
            raise DAGExecutionError(f"节点 '{node_id}' 不存在")
        ids = self._find_downstream_nodes(dag, node_id) | {node_id}
        pruned = dag.model_copy(update={
            "nodes": [n for n in dag.nodes if n.id in ids],
            "edges": [e for e in dag.edges if e.source in ids and e.target in ids],
        })
        return await self.run(pruned, state, thread_id)

    async def _run_with_langgraph(self, dag, initial_state, thread_id):
        return await self._run_with_topological_sort(dag, initial_state)

    async def _run_with_topological_sort(self, dag, initial_state, node_results=None):
        state = dict(initial_state)
        previous_errors = int(initial_state.get("error_count", 0))
        results = node_results if node_results is not None else {}
        for node in dag.nodes:
            if not node.enabled:
                await self._emit(state, node, results.get(node.id) or unexecuted_result(
                    NodeResult(status=NodeStatus.DISABLED, error="DAG 配置已禁用节点"), node, state, "DAG 配置已禁用节点"))
        for layer in self._topological_layers(dag):
            executable = []
            incoming_by_node = {}
            for node in layer:
                incoming = [e for e in dag.edges if e.target == node.id]
                active = [e for e in incoming if self._edge_active(e, results, state)]
                plain = [e for e in incoming if e.condition == EdgeCondition.ALWAYS]
                conditional = [e for e in incoming if e.condition != EdgeCondition.ALWAYS]
                # Unconditional inputs are AND dependencies; alternative control
                # branches are OR. Unsatisfied branches get a recorded bypass.
                if any(e not in active for e in plain) or (conditional and not any(e in active for e in conditional)):
                    results[node.id] = unexecuted_result(NodeResult(status=NodeStatus.BYPASSED, error="上游失败、禁用或分支条件未满足"), node, state, "上游失败、禁用或分支条件未满足")
                    await self._emit(state, node, results[node.id])
                    continue
                inputs = dict(state)
                for edge in active:
                    if edge.target_port:
                        inputs[edge.target_port] = results[edge.source].outputs.get(edge.source_port)
                incoming_by_node[node.id] = inputs
                executable.append(node)
            children = [asyncio.create_task(self._execute_node(node, incoming_by_node[node.id]))
                        for node in executable]
            try:
                batch = await asyncio.gather(*children)
            except BaseException:
                # gather propagates a sibling exception without cancelling the
                # remaining tasks. Keep ownership until all work, including a
                # shielded writer ACK, has actually stopped.
                for child in children:
                    if not child.done():
                        child.cancel()
                cleanup = asyncio.gather(*children, return_exceptions=True)
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        continue
                cleanup.result()
                raise
            for node, result in zip(executable, batch):
                results[node.id] = result
                if result.status in (NodeStatus.SUCCESS, NodeStatus.WARNING, NodeStatus.COMPLETED):
                    state.update(result.outputs)
                    injections = dict(state.get("_dag_injection_receipts", {}))
                    for key in ("behavior_protocol", "character_state_lock", "allowlist_block", "refresh_block", "finale_block"):
                        value = result.outputs.get(key)
                        if isinstance(value, str) and value.strip():
                            import hashlib
                            injections[key] = {"node_id": node.id, "run_id": state.get("dag_run_id", ""),
                                "output_hash": result.execution_receipt.output_hash if result.execution_receipt else "",
                                "fragment_hash": hashlib.sha256(value.encode("utf-8")).hexdigest()}
                    state["_dag_injection_receipts"] = injections
            state["error_count"] = previous_errors + sum(r.status == NodeStatus.ERROR for r in results.values())
        return state

    @staticmethod
    def _edge_active(edge, results, state):
        result = results.get(edge.source)
        if result is None or result.status in (NodeStatus.BYPASSED, NodeStatus.DISABLED, NodeStatus.PENDING):
            return False
        if edge.condition == EdgeCondition.ON_ERROR:
            return result.status == NodeStatus.ERROR
        if result.status == NodeStatus.ERROR:
            return False
        condition_keys = {
            EdgeCondition.ON_DRIFT_ALERT: "drift_alert", EdgeCondition.ON_NO_DRIFT: "drift_alert",
            EdgeCondition.ON_BREAKER_OPEN: "breaker_status", EdgeCondition.ON_BREAKER_CLOSED: "breaker_status",
            EdgeCondition.ON_REVIEW_APPROVED: "approved", EdgeCondition.ON_REVIEW_REJECTED: "approved",
        }
        key = condition_keys.get(edge.condition)
        if key and key not in result.outputs and not (key == "approved" and "review_approved" in result.outputs):
            raise DAGExecutionError(f"条件边 '{edge.id}' 的源节点未提供 {key}")
        return _make_condition_function(edge.condition, edge.target)({**result.outputs, "status": result.status.value})

    async def _execute_node(self, node_def: NodeDefinition, state: Dict[str, Any]) -> NodeResult:
        started_at = now()
        await self._emit(state, node_def, NodeResult(status=NodeStatus.RUNNING,
            execution_receipt=receipt_for(node_def.id, node_def.type, state, inputs=state, reason="正在校验执行前提")))
        executor = NodeRegistry.create_executor(node_def.type, node_def.id, node_def.config)
        result = await executor(state)
        if not isinstance(result, NodeResult):
            raise DAGExecutionError(f"节点 {node_def.id} 必须返回 NodeResult")
        if result.status not in {NodeStatus.SUCCESS, NodeStatus.WARNING, NodeStatus.COMPLETED,
                NodeStatus.ERROR, NodeStatus.BYPASSED, NodeStatus.DISABLED}:
            result.status, result.error = NodeStatus.ERROR, f"节点 {node_def.id} 返回非终态"
        if result.error and result.status in (NodeStatus.SUCCESS, NodeStatus.COMPLETED):
            result.status = NodeStatus.ERROR
        if result.execution_receipt is None:
            skipped = result.status in (NodeStatus.BYPASSED, NodeStatus.DISABLED)
            result.execution_receipt = receipt_for(node_def.id, node_def.type, state,
                started_at=started_at, executed=not skipped, completed=True, inputs=state,
                outputs=result.outputs, reason=result.error or ("节点条件不适用" if skipped else None))
        receipt = result.execution_receipt
        receipt.run_id, receipt.node_id, receipt.node_type = str(state.get("dag_run_id") or ""), node_def.id, node_def.type
        receipt.output_hash = data_hash(result.outputs)
        if result.status in (NodeStatus.SUCCESS, NodeStatus.WARNING, NodeStatus.COMPLETED) and not (
                receipt.executed and receipt.executor_started and receipt.executor_completed):
            result.status, result.error = NodeStatus.ERROR, "节点声称成功但没有完整执行回执"
        await self._emit(state, node_def, result)
        return result

    @staticmethod
    async def _emit(state, node, result):
        callback = state.get("on_node_event")
        if callback is None:
            return
        value = callback({"type": "node_status_change", "node_id": node.id,
                          "node_type": node.type, "novel_id": state.get("novel_id", ""),
                          "dag_run_id": state.get("dag_run_id", ""), "attempt": state.get("attempt", 0),
                          "timestamp": datetime.now(timezone.utc).isoformat(), "source": "dag_runner", "executed": did_execute(result),
                          **result.model_dump(mode="json")})
        if inspect.isawaitable(value):
            await value

    def _topological_layers(self, dag: DAGDefinition) -> List[List[NodeDefinition]]:
        enabled = {n.id: n for n in dag.nodes if n.enabled}
        degree = {nid: 0 for nid in enabled}
        for edge in dag.edges:
            if edge.source in enabled and edge.target in enabled:
                degree[edge.target] += 1
        queue = [nid for nid, d in degree.items() if d == 0]
        layers = []
        seen = set()
        while queue:
            layers.append([enabled[nid] for nid in queue])
            seen.update(queue)
            following = []
            for nid in queue:
                for edge in dag.edges:
                    if edge.source == nid and edge.target in degree:
                        degree[edge.target] -= 1
                        if degree[edge.target] == 0:
                            following.append(edge.target)
            queue = following
        if seen != set(enabled):
            raise DAGExecutionError(f"DAG 节点未排入执行序列: {sorted(set(enabled) - seen)}；反馈循环暂不支持")
        return layers

    def _find_downstream_nodes(self, dag, start_node_id) -> Set[str]:
        seen, queue = set(), [start_node_id]
        while queue:
            for successor in dag.get_successors(queue.pop(0)):
                if successor not in seen:
                    seen.add(successor)
                    queue.append(successor)
        return seen

    def validate(self, dag: DAGDefinition) -> List[str]:
        return DAGValidator().validate(dag).errors

    def _has_cycle(self, dag: DAGDefinition) -> bool:
        return bool(DAGValidator()._check_acyclic(dag))


def _make_condition_function(condition: EdgeCondition, target: str):
    def condition_fn(state: dict) -> bool:
        if condition == EdgeCondition.ON_SUCCESS:
            return state.get("status") in ("success", "warning", "completed")
        if condition == EdgeCondition.ON_ERROR:
            return state.get("status") == "error"
        if condition == EdgeCondition.ON_DRIFT_ALERT:
            return state.get("drift_alert") is True
        if condition == EdgeCondition.ON_NO_DRIFT:
            return state.get("drift_alert") is False
        if condition == EdgeCondition.ON_BREAKER_OPEN:
            return state.get("breaker_status") == "open"
        if condition == EdgeCondition.ON_BREAKER_CLOSED:
            return state.get("breaker_status") == "closed"
        if condition == EdgeCondition.ON_REVIEW_APPROVED:
            return state.get("review_approved", state.get("approved")) is True
        if condition == EdgeCondition.ON_REVIEW_REJECTED:
            return state.get("review_approved", state.get("approved")) is False
        return True
    return condition_fn
